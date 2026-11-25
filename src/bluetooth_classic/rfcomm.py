"""A serialx handler for Bluetooth Classic RFCOMM services: ``rfcomm://`` URLs.

``rfcomm://00:11:22:33:44:55?service=1101`` opens that device's Serial Port
service (``1101``) as a serial connection. ``service`` is a UUID, or its
16- or 32-bit short form.

aio-rfcomm makes the connection: on Linux, BlueZ connects the service
through its profile API, finds the channel itself and hands the connected
socket over. aio-rfcomm uses BlueZ's first adapter. Bytes given as
``write_on_connect`` are sent the moment the channel opens, before anything
else runs: some devices change state soon after a connection opens and only
answer a request sent before then.

aio-rfcomm allows one Bluetooth object per process. The handler opens it on
first use and keeps it, on an event loop of its own in a background thread,
so the opening bytes go out however busy the caller's loop is. Data passes
between that loop and the caller's.

An open link reports its service through ``get_extra_info("rfcomm_service")``
and is listed by :func:`open_rfcomm_links` while open.

Importing this module (or ``bluetooth_classic``) registers the handler with
serialx's asyncio API (``serialx.create_serial_connection()``); its blocking
``serial_for_url()`` refuses ``rfcomm://`` URLs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import io
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import aio_rfcomm
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    BluetoothUnavailableError,
    ChannelClosedError,
    DeviceNotFoundError,
    RfcommError,
)
from serialx import (
    BaseSerial,
    BaseSerialTransport,
    ModemPins,
    Parity,
    StopBits,
    create_serial_connection,
    register_uri_handler,
)
from serialx.platforms.serial_socket import SocketSerial

from .client import normalize_address
from .exceptions import RfcommConnectError

_LOGGER = logging.getLogger(__name__)

SCHEME = "rfcomm://"
DEFAULT_CONNECT_TIMEOUT = 10.0

# The Bluetooth Base UUID, which 16- and 32-bit UUIDs are short forms of.
_BASE_UUID = UUID("00000000-0000-1000-8000-00805f9b34fb")
#: The Serial Port Profile.
SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")


def service_uuid(service: UUID | str | int) -> UUID:
    """Return a service UUID from a UUID, its string, or a 16/32-bit short form.

    ``"1101"``, ``0x1101`` and ``"00001101-0000-1000-8000-00805f9b34fb"`` all
    give the Serial Port Profile.

    Raises:
        ValueError: Not a UUID.
    """
    if isinstance(service, UUID):
        return service
    if isinstance(service, int):
        return _from_short(service)
    text = service.strip().lower().removeprefix("0x")
    if len(text) in (4, 8):
        return _from_short(int(text, 16))
    return UUID(text)


def _from_short(value: int) -> UUID:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"Not a 16- or 32-bit UUID: {value:#x}")
    return UUID(int=_BASE_UUID.int | value << 96)


@dataclass(frozen=True, slots=True)
class RfcommTarget:
    """What an ``rfcomm://`` URL connects to."""

    address: str
    service: UUID


@dataclass(frozen=True, slots=True)
class RfcommLink:
    """An open RFCOMM link."""

    address: str
    service: UUID
    #: When it opened, in ``time.monotonic()`` seconds.
    since: float = field(default_factory=time.monotonic)


def rfcomm_url(address: str, *, service: UUID | str | int) -> str:
    """Return the ``rfcomm://`` URL of a device's service.

    Raises:
        ValueError: A service that is not valid.
    """
    target = RfcommTarget(normalize_address(address), service_uuid(service))
    query = urllib.parse.urlencode({"service": str(target.service)})
    return f"{SCHEME}{target.address}?{query}"


def parse_rfcomm_url(url: str) -> RfcommTarget:
    """Return what an ``rfcomm://`` URL connects to.

    A Bluetooth address has colons, so the URL cannot be parsed as host and
    port; the service is a query parameter instead.

    Raises:
        ValueError: Not an ``rfcomm://`` URL with an address and a service,
            given once, and nothing else.
    """
    if not url.startswith(SCHEME):
        raise ValueError(f"Not an rfcomm:// URL: {url}")
    address, _, query = url.removeprefix(SCHEME).partition("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    for name, values in params.items():
        if name == "adapter":
            raise ValueError(
                f"rfcomm:// cannot choose an adapter; aio-rfcomm uses BlueZ's "
                f"first: {url}"
            )
        if name != "service":
            raise ValueError(f"Unknown rfcomm:// parameter {name}: {url}")
        if len(values) != 1:
            raise ValueError(f"rfcomm:// {name} given more than once: {url}")
        if not values[0]:
            raise ValueError(f"rfcomm:// {name} is empty: {url}")
    if "service" not in params:
        raise ValueError(f"rfcomm:// needs a service: {url}")
    try:
        service = service_uuid(params["service"][0])
    except ValueError as err:
        raise ValueError(f"rfcomm:// service is not a UUID: {url}") from err
    return RfcommTarget(normalize_address(address), service)


# The link registry.

_links: dict[int, RfcommLink] = {}
# Device services with a link open or opening: one at a time each.
_busy: set[tuple[str, UUID]] = set()
_links_lock = threading.Lock()
_listeners: list[Callable[[], None]] = []


def open_rfcomm_links() -> list[RfcommLink]:
    """Return the RFCOMM links this process has open through the handler."""
    with _links_lock:
        return list(_links.values())


def on_rfcomm_links_changed(callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` whenever a link opens or closes; returns an unsubscribe.

    It is called on the event loop.
    """
    _listeners.append(callback)

    def unsubscribe() -> None:
        if callback in _listeners:
            _listeners.remove(callback)

    return unsubscribe


def _add_link(key: int, link: RfcommLink) -> None:
    with _links_lock:
        _links[key] = link
    _notify()


def _remove_link(key: int) -> None:
    with _links_lock:
        if _links.pop(key, None) is None:
            return
    _notify()


def _notify() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:
            _LOGGER.exception("RFCOMM link listener failed")


def _release(key: tuple[str, UUID]) -> None:
    with _links_lock:
        _busy.discard(key)


# aio-rfcomm, on a loop of its own.


class _Owner:
    """The process's aio-rfcomm Bluetooth object, on its own loop and thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._bluetooth: aio_rfcomm.Bluetooth | None = None
        self._stack = AsyncExitStack()
        self._opening: asyncio.Lock | None = None

    def loop(self) -> asyncio.AbstractEventLoop:
        """Return the background loop, starting its thread on first use."""
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                thread = threading.Thread(
                    target=run, name="bluetooth-classic RFCOMM", daemon=True
                )
                thread.start()
                ready.wait()
                self._loop, self._thread = loop, thread
            return self._loop

    async def bluetooth(self) -> aio_rfcomm.Bluetooth:
        """Return the Bluetooth object, opening it once; on the background loop."""
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self._bluetooth is None:
                try:
                    self._bluetooth = await self._stack.enter_async_context(
                        aio_rfcomm.open_bluetooth()
                    )
                except BluetoothAlreadyOpenError as err:
                    raise RfcommConnectError(
                        "aio-rfcomm is already open in this process, outside "
                        "bluetooth-classic, and allows one owner"
                    ) from err
            return self._bluetooth

    def shutdown(self) -> None:
        """Close aio-rfcomm and stop the thread (tests; the process ends it too)."""
        with self._lock:
            loop, thread, stack = self._loop, self._thread, self._stack
            self._loop = self._thread = self._bluetooth = self._opening = None
            self._stack = AsyncExitStack()
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(stack.aclose(), loop).result(2)
        except Exception:
            _LOGGER.debug("Closing aio-rfcomm failed", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


_owner = _Owner()


class _Channel:
    """An open aio-rfcomm channel, its reader and its sender; background loop only."""

    def __init__(
        self, stack: AsyncExitStack, channel: aio_rfcomm.RfcommChannel
    ) -> None:
        self._stack = stack
        self._channel = channel
        # Bytes to send, in order; None ends the sender once all before it went.
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._closing: asyncio.Future[None] | None = None

    def start(
        self,
        on_data: Callable[[bytes], None],
        on_sent: Callable[[int], None],
        on_end: Callable[[Exception], None],
    ) -> None:
        """Start passing data on; the callbacks are called on this loop."""
        if self._closing is not None:
            return
        self._reader = asyncio.ensure_future(self._read(on_data, on_end))
        self._sender = asyncio.ensure_future(self._send(on_sent, on_end))

    def send(self, data: bytes) -> None:
        """Queue bytes to send."""
        self._queue.put_nowait(data)

    async def _read(
        self, on_data: Callable[[bytes], None], on_end: Callable[[Exception], None]
    ) -> None:
        try:
            while True:
                on_data(await self._channel.receive())
        except Exception as err:
            on_end(_closed(err))

    async def _send(
        self, on_sent: Callable[[int], None], on_end: Callable[[Exception], None]
    ) -> None:
        # One sender, as a channel takes one send at a time.
        try:
            while (data := await self._queue.get()) is not None:
                await self._channel.send(data)
                on_sent(len(data))
        except Exception as err:
            on_end(_closed(err))

    async def close(self, *, drain: bool) -> None:
        """Close the channel; with ``drain``, once what is queued has been sent."""
        if self._sender is not None:
            if drain:
                self._queue.put_nowait(None)
            else:
                self._sender.cancel()
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._close())
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        if self._sender is not None:
            await asyncio.wait([self._sender])
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.wait([self._reader])
        try:
            await self._stack.aclose()
        except Exception:
            _LOGGER.debug("Closing the RFCOMM channel failed", exc_info=True)


async def _open_channel(
    target: RfcommTarget, timeout: float | None, write_on_connect: bytes
) -> _Channel:
    """Open the channel and send the opening bytes at once; background loop."""
    asked = time.monotonic()
    stack = AsyncExitStack()
    try:
        async with asyncio.timeout(timeout):
            bluetooth = await _owner.bluetooth()
            channel = await stack.enter_async_context(
                bluetooth.open_service(
                    aio_rfcomm.RfcommDeviceInfo(target.address), target.service
                )
            )
        opened = time.monotonic()
        if write_on_connect:
            await channel.send(write_on_connect)
    except BaseException as err:
        _log_failure(target, time.monotonic() - asked, err)
        await asyncio.shield(stack.aclose())
        raise
    _LOGGER.debug(
        "Opened %s's service %s %.3f s after asking; %d bytes sent %.1f ms later",
        target.address,
        target.service,
        opened - asked,
        len(write_on_connect),
        (time.monotonic() - opened) * 1000,
    )
    return _Channel(stack, channel)


def _log_failure(target: RfcommTarget, after: float, err: BaseException) -> None:
    """Log why a channel did not open, with what BlueZ said if known."""
    if isinstance(err, asyncio.CancelledError):
        _LOGGER.debug(
            "Gave up opening %s's service %s %.3f s after asking",
            target.address,
            target.service,
            after,
        )
        return
    cause = err.__cause__
    said = ""
    if isinstance(err, TimeoutError):
        # Its cause is only the cancellation the timeout made.
        cause = None
    if cause is not None:
        # aio-rfcomm raises its own error from BlueZ's D-Bus error.
        name = getattr(cause, "type", None) or type(cause).__name__
        said = f"; from {name}: {cause}"
    _LOGGER.debug(
        "Could not open %s's service %s %.3f s after asking: %s: %s%s",
        target.address,
        target.service,
        after,
        type(err).__name__,
        err,
        said,
    )


def _closed(err: BaseException) -> OSError:
    """Report a channel ending as the connection error it is.

    aio-rfcomm does not keep why a link ended: RFCOMM reports even an
    orderly hang-up as a reset. So a link that ended is a reset, as a socket
    reports it, and Bluetooth itself going away is an abort.
    """
    error: OSError
    if isinstance(err, BluetoothUnavailableError):
        error = ConnectionAbortedError(
            errno.ECONNABORTED, f"Bluetooth is not available: {err}"
        )
    elif isinstance(err, ChannelClosedError):
        error = ConnectionResetError(errno.ECONNRESET, "The link has ended")
    else:
        error = ConnectionResetError(
            errno.ECONNRESET, f"The link failed: {type(err).__name__}: {err}"
        )
    error.__cause__ = err
    return error


def _call_on(
    loop: asyncio.AbstractEventLoop, callback: Callable[..., None]
) -> Callable[..., None]:
    """Return a function that calls ``callback`` on ``loop``, from any thread."""

    def call(*args: Any) -> None:
        # A closed loop leaves nobody to tell.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback, *args)

    return call


# The serialx handler.


class RfcommSerial(SocketSerial):
    """Serial settings for an ``rfcomm://`` URL; opening it blocking is refused.

    aio-rfcomm needs an event loop: use serialx's asyncio API.
    """

    def __init__(
        self,
        *args: Any,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        write_on_connect: bytes = b"",
        **kwargs: Any,
    ) -> None:
        """Initialise from an ``rfcomm://`` URL; the baud rate is ignored."""
        # SocketSerial's own initialiser parses a host and port.
        BaseSerial.__init__(self, *args, **kwargs)
        self._target = parse_rfcomm_url(str(self._path))
        self._connect_timeout = connect_timeout
        self._write_on_connect = bytes(write_on_connect)
        self._socket = None

    def _open(self) -> None:
        """Refuse: only serialx's asyncio API can open ``rfcomm://`` URLs."""
        raise io.UnsupportedOperation(
            "rfcomm:// supports only serialx's asyncio API (create_serial_connection)"
        )


class RfcommSerialTransport(BaseSerialTransport):
    """Serial transport over an RFCOMM service that aio-rfcomm opened."""

    transport_name = "rfcomm"

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialise the transport."""
        super().__init__(loop, protocol)
        self._channel: _Channel | None = None
        self._busy: tuple[str, UUID] | None = None
        # Why the link ended, if the device or Bluetooth ended it.
        self._ended: Exception | None = None
        self._unsent = 0
        self._sent = asyncio.Event()
        self._sent.set()

    async def _connect(  # type: ignore[override]
        self,
        *,
        path: str,
        baudrate: int,
        parity: Parity = Parity.NONE,
        stopbits: StopBits | int | float = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        byte_size: int = 8,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        write_on_connect: bytes = b"",
        **kwargs: Any,
    ) -> None:
        serial = RfcommSerial(
            path=path,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            byte_size=byte_size,
            connect_timeout=connect_timeout,
            write_on_connect=write_on_connect,
        )
        self._serial = serial
        self._extra["serial"] = serial
        target = serial._target

        key = (target.address, target.service)
        with _links_lock:
            if key in _busy:
                raise RfcommConnectError(
                    f"{target.address} already has a link to {target.service}"
                )
            _busy.add(key)
        self._busy = key

        bg = _owner.loop()
        opening = asyncio.run_coroutine_threadsafe(
            _open_channel(target, connect_timeout, serial._write_on_connect), bg
        )
        waiting = asyncio.wrap_future(opening)
        try:
            channel = await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # Left to finish on the background loop, then closed; the device
            # service stays busy until then. Its outcome is read here, so
            # nothing is logged as never retrieved.
            self._busy = None
            waiting.add_done_callback(_ignore_outcome)
            opening.add_done_callback(lambda done: _abandon(done, key))
            raise
        except RfcommError as err:
            hint = ""
            if isinstance(err, DeviceNotFoundError):
                hint = (
                    " (aio-rfcomm uses BlueZ's first adapter: is the device "
                    "paired on it?)"
                )
            raise RfcommConnectError(
                f"{target.address}'s service {target.service} could not be "
                f"opened: {err}{hint}"
            ) from err
        self._channel = channel
        bg.call_soon_threadsafe(
            channel.start,
            _call_on(self._loop, self._deliver),
            _call_on(self._loop, self._on_sent),
            _call_on(self._loop, self._end),
        )
        self._extra["rfcomm_service"] = target.service
        _add_link(id(self), RfcommLink(target.address, target.service))
        # Data is passed on from the caller's loop, so none can reach the
        # protocol before this.
        self._call_protocol_connection_made()

    def _deliver(self, data: bytes) -> None:
        if not self._connection_lost_called:
            self._protocol.data_received(data)

    def _on_sent(self, count: int) -> None:
        self._unsent = max(0, self._unsent - count)
        if not self._unsent:
            self._sent.set()

    def _end(self, exc: Exception) -> None:
        """The device or Bluetooth ended the link."""
        if self._closing:
            return
        self._closing = True
        self._ended = exc
        self._mark_broken(exc)
        # Reported at once; the device service is freed once the channel
        # has closed behind it, so a new link cannot race its teardown.
        self._report_lost()
        self._close_channel(drain=False, then_finish=False, then_free=True)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Send data, in order."""
        self._check_broken()
        if self._closing or self._channel is None or not data:
            return
        chunk = bytes(data)
        self._unsent += len(chunk)
        self._sent.clear()
        _owner.loop().call_soon_threadsafe(self._channel.send, chunk)

    def close(self) -> None:
        """Close once what is queued has been sent."""
        if self._closing:
            return
        self._closing = True
        self._mark_user_closed()
        self._arm_close_timeout()
        self._close_channel(drain=True, then_finish=True)

    def abort(self) -> None:
        """Close at once, dropping what is queued."""
        first = not self._closing
        self._closing = True
        self._mark_user_closed()
        self._close_channel(drain=False, then_finish=first)

    def _close_channel(
        self, *, drain: bool, then_finish: bool, then_free: bool = False
    ) -> None:
        channel = self._channel
        if channel is None:
            if then_finish or then_free:
                self._finish()
            return
        closing = asyncio.run_coroutine_threadsafe(
            channel.close(drain=drain), _owner.loop()
        )
        if then_finish:
            finish = _call_on(self._loop, self._finish)
            closing.add_done_callback(lambda _: finish())
        elif then_free:
            free = _call_on(self._loop, self._free)
            closing.add_done_callback(lambda _: free())

    def _finish(self) -> None:
        self._free()
        self._report_lost()

    def _free(self) -> None:
        """Let the device service be connected again."""
        if self._busy is not None:
            _release(self._busy)
            self._busy = None

    def _report_lost(self) -> None:
        _remove_link(id(self))
        self._unsent = 0
        self._sent.set()
        self._call_protocol_connection_lost(self._ended)

    def get_write_buffer_size(self) -> int:
        """Return the number of bytes queued but not yet sent."""
        return self._unsent

    async def _flush(self) -> None:
        await self._sent.wait()

    async def _get_modem_pins(self) -> ModemPins:
        return ModemPins()

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        """RFCOMM has no modem pins to set."""

    async def _reconfigure_port(self, update: Any) -> None:
        """RFCOMM has no port settings."""


def _ignore_outcome(waiting: asyncio.Future[_Channel]) -> None:
    """Read a given-up open's outcome, which _abandon deals with."""
    if not waiting.cancelled():
        waiting.exception()


def _abandon(
    opening: concurrent.futures.Future[_Channel], key: tuple[str, UUID]
) -> None:
    """Close a channel that opened after its caller gave up; free its service."""
    if opening.cancelled() or opening.exception() is not None:
        _release(key)
        return
    closing = asyncio.run_coroutine_threadsafe(
        opening.result().close(drain=False), _owner.loop()
    )
    closing.add_done_callback(lambda _: _release(key))


def _register() -> None:
    """Register the handler, unless another copy of it already has."""
    try:
        register_uri_handler(
            scheme=SCHEME,
            unique_scheme=SCHEME,
            sync_cls=RfcommSerial,
            async_transport_cls=RfcommSerialTransport,
            connect_kwargs=frozenset({"connect_timeout", "write_on_connect"}),
        )
    except ValueError:
        # serialx refuses a second handler for a scheme; one registered by
        # another copy of this module (imported under another name, or
        # vendored) serves just as well.
        _LOGGER.debug("An rfcomm:// handler is already registered")


_register()


async def open_rfcomm(
    address: str,
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    service: UUID | str | int,
    timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    write_on_connect: bytes = b"",
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    """Open a device's RFCOMM service through serialx; attach a protocol.

    A shortcut for ``serialx.create_serial_connection()`` with an
    ``rfcomm://`` URL.

    Args:
        address: The device's address. BlueZ must know it (paired).
        protocol_factory: Returns the protocol to attach.
        service: The service to connect to, such as ``SERIAL_PORT``.
        timeout: Seconds to wait for the link to open.
        write_on_connect: Bytes sent the moment the channel opens, before
            the protocol is attached. Replies wait until it is.

    Raises:
        ValueError: A service that is not valid.
        RfcommConnectError: The service could not be opened, or the device
            already has a link to it.
        TimeoutError: No connection within ``timeout`` seconds.
    """
    return await create_serial_connection(
        asyncio.get_running_loop(),
        protocol_factory,
        rfcomm_url(address, service=service),
        baudrate=9600,  # Required by serialx; RFCOMM has no baud rate.
        connect_timeout=timeout,
        write_on_connect=write_on_connect,
    )
