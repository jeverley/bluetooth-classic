"""RFCOMM links to Bluetooth Classic devices, through one aio-rfcomm owner.

:class:`RfcommClient` connects to a device's RFCOMM service, such as its
Serial Port (``SERIAL_PORT``), shaped like bleak's ``BleakClient``: data and
disconnections arrive as callbacks. :func:`establish_rfcomm_connection`
connects one, retrying while BlueZ says the device is busy, as
bleak-retry-connector's ``establish_connection()`` does.
:func:`create_rfcomm_connection` is the lower-level form underneath: it
attaches an asyncio protocol, as ``loop.create_connection()`` does.

aio-rfcomm makes the connection: on Linux, BlueZ connects the service
through its profile API, finds the channel itself and hands the connected
socket over. aio-rfcomm uses BlueZ's first adapter. Bytes given as
``write_on_connect`` are sent the moment the channel opens, before anything
else runs: some devices change state soon after a connection opens and only
answer a request sent before then.

aio-rfcomm allows one Bluetooth object per process. This module owns it,
opened on first use, on an event loop of its own in a background thread, so
the opening bytes go out however busy the caller's loop is; every caller in
the process shares it, so open links through here rather than opening
aio-rfcomm directly. Data passes between that loop and the caller's.

An open link reports its address and service through ``get_extra_info()``
(``rfcomm_address``, ``rfcomm_service``) and is listed by
:func:`open_rfcomm_links` while open.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import threading
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar
from uuid import UUID

import aio_rfcomm
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    BluetoothUnavailableError,
    ChannelClosedError,
    DeviceNotFoundError,
    RfcommError,
)

from .client import normalize_address
from .exceptions import RfcommConnectError

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 10.0
# Seconds a close waits for queued bytes to be sent before dropping them.
DRAIN_TIMEOUT = 2.0

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
class _Target:
    """A device's service to connect to."""

    address: str
    service: UUID


@dataclass(frozen=True, slots=True)
class RfcommLink:
    """An open RFCOMM link."""

    address: str
    service: UUID
    #: When it opened, in ``time.monotonic()`` seconds.
    since: float = field(default_factory=time.monotonic)


# The link registry.

_links: dict[int, RfcommLink] = {}
# Device services with a link open or opening: one at a time each.
_busy: set[tuple[str, UUID]] = set()
_links_lock = threading.Lock()
_listeners: list[Callable[[], None]] = []


def open_rfcomm_links() -> list[RfcommLink]:
    """Return the RFCOMM links this process has open."""
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
            # A device that stops reading must not hold the close forever.
            _, pending = await asyncio.wait([self._sender], timeout=DRAIN_TIMEOUT)
            if pending:
                self._sender.cancel()
                await asyncio.wait([self._sender])
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.wait([self._reader])
        try:
            await self._stack.aclose()
        except Exception:
            _LOGGER.debug("Closing the RFCOMM channel failed", exc_info=True)


async def _open_channel(
    target: _Target, timeout: float | None, write_on_connect: bytes
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


def _log_failure(target: _Target, after: float, err: BaseException) -> None:
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


# Links.


class RfcommTransport(asyncio.Transport):
    """An open RFCOMM link, passing data between aio-rfcomm and a protocol."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        protocol: asyncio.Protocol,
        channel: _Channel,
        target: _Target,
    ) -> None:
        """Not made directly: :func:`create_rfcomm_connection` makes it."""
        super().__init__(
            {"rfcomm_address": target.address, "rfcomm_service": target.service}
        )
        self._loop = loop
        self._protocol = protocol
        self._channel = channel
        self._busy: tuple[str, UUID] | None = (target.address, target.service)
        self._closing = False
        self._lost = False
        # Why the link ended, if the device or Bluetooth ended it.
        self._ended: Exception | None = None
        self._unsent = 0
        self._sent = asyncio.Event()
        self._sent.set()

    def _start(self) -> None:
        """Pass data on from now: the protocol has been told the link opened."""
        _owner.loop().call_soon_threadsafe(
            self._channel.start,
            _call_on(self._loop, self._deliver),
            _call_on(self._loop, self._on_sent),
            _call_on(self._loop, self._end),
        )

    def get_protocol(self) -> asyncio.BaseProtocol:
        """Return the protocol."""
        return self._protocol

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        """Pass data to another protocol."""
        assert isinstance(protocol, asyncio.Protocol)
        self._protocol = protocol

    def is_closing(self) -> bool:
        """Return whether the link is closing or closed."""
        return self._closing

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Send data, in order."""
        if self._closing or not data:
            return
        chunk = bytes(data)
        self._unsent += len(chunk)
        self._sent.clear()
        _owner.loop().call_soon_threadsafe(self._channel.send, chunk)

    def can_write_eof(self) -> bool:
        """RFCOMM has no half close."""
        return False

    def get_write_buffer_size(self) -> int:
        """Return the number of bytes queued but not yet sent."""
        return self._unsent

    def close(self) -> None:
        """Close once what is queued has been sent (at most DRAIN_TIMEOUT)."""
        if self._closing:
            return
        self._closing = True
        self._close_channel(drain=True, then_finish=True)

    def abort(self) -> None:
        """Close at once, dropping what is queued."""
        first = not self._closing
        self._closing = True
        self._close_channel(drain=False, then_finish=first)

    def _deliver(self, data: bytes) -> None:
        if not self._lost:
            self._protocol.data_received(data)

    async def drain(self) -> None:
        """Wait until everything written has been sent, or the link has ended."""
        await self._sent.wait()

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
        # Reported at once; the device service is freed once the channel
        # has closed behind it, so a new link cannot race its teardown.
        self._report_lost()
        self._close_channel(drain=False, then_finish=False, then_free=True)

    def _close_channel(
        self, *, drain: bool, then_finish: bool, then_free: bool = False
    ) -> None:
        closing = asyncio.run_coroutine_threadsafe(
            self._channel.close(drain=drain), _owner.loop()
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
        if self._lost:
            return
        self._lost = True
        _remove_link(id(self))
        self._unsent = 0
        self._sent.set()
        try:
            self._protocol.connection_lost(self._ended)
        except Exception as err:
            self._loop.call_exception_handler(
                {
                    "message": "protocol.connection_lost() failed",
                    "exception": err,
                    "transport": self,
                    "protocol": self._protocol,
                }
            )


# BlueZ's errors for a device or service still busy, as just after a link
# closed. Which one BlueZ gives the soundbar is unconfirmed.
_BUSY_ERRORS = frozenset({"org.bluez.Error.InProgress", "org.bluez.Error.Busy"})


def _is_busy(err: BaseException) -> bool:
    """Return whether aio-rfcomm's error says the device is busy for now."""
    cause = err.__cause__
    if getattr(cause, "type", None) in _BUSY_ERRORS:
        return True
    said = f"{err} {cause or ''}".lower()
    return "busy" in said or "in progress" in said


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


async def create_rfcomm_connection(
    address: str,
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    service: UUID | str | int,
    timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    write_on_connect: bytes = b"",
) -> tuple[RfcommTransport, asyncio.Protocol]:
    """Open a device's RFCOMM service and attach a protocol.

    Args:
        address: The device's address. BlueZ must know it (paired).
        protocol_factory: Returns the protocol to attach.
        service: The service to connect to, such as ``SERIAL_PORT``: a UUID,
            or its 16- or 32-bit short form.
        timeout: Seconds to wait for the link to open.
        write_on_connect: Bytes sent the moment the channel opens, before
            the protocol is attached. Replies wait until it is.

    Raises:
        ValueError: An address or service that is not valid.
        RfcommConnectError: The service could not be opened, the device
            already has a link to it, or aio-rfcomm is open elsewhere in
            this process.
        TimeoutError: No connection within ``timeout`` seconds.
    """
    loop = asyncio.get_running_loop()
    target = _Target(normalize_address(address), service_uuid(service))
    key = (target.address, target.service)
    with _links_lock:
        if key in _busy:
            raise RfcommConnectError(
                f"{target.address} already has a link to {target.service}"
            )
        _busy.add(key)

    opening = asyncio.run_coroutine_threadsafe(
        _open_channel(target, timeout, bytes(write_on_connect)), _owner.loop()
    )
    waiting = asyncio.wrap_future(opening)
    try:
        channel = await asyncio.shield(waiting)
    except asyncio.CancelledError:
        # Left to finish on the background loop, then closed; the device
        # service stays busy until then. Its outcome is read here, so
        # nothing is logged as never retrieved.
        waiting.add_done_callback(_ignore_outcome)
        opening.add_done_callback(lambda done: _abandon(done, key))
        raise
    except RfcommError as err:
        _release(key)
        hint = ""
        if isinstance(err, DeviceNotFoundError):
            hint = (
                " (aio-rfcomm uses BlueZ's first adapter: is the device paired on it?)"
            )
        message = (
            f"{target.address}'s service {target.service} could not be "
            f"opened: {err}{hint}"
        )
        if _is_busy(err):
            # As a socket reports it, so callers can retry as they would.
            raise RfcommConnectError(errno.EBUSY, message) from err
        raise RfcommConnectError(message) from err
    except BaseException:
        _release(key)
        raise

    try:
        protocol = protocol_factory()
    except BaseException:
        _abandon(opening, key)
        raise
    transport = RfcommTransport(loop, protocol, channel, target)
    _add_link(id(transport), RfcommLink(target.address, target.service))
    # Data is passed on from this loop, so none can reach the protocol
    # before this.
    protocol.connection_made(transport)
    transport._start()
    return transport, protocol


# A client, shaped like bleak's.


class RfcommClient:
    """A connection to a device's RFCOMM service, shaped like bleak's BleakClient.

    Data the device sends is passed to ``data_callback`` as it arrives, from
    the first byte; ``disconnected_callback`` is called with the client when
    the link ends, and ``disconnect_reason`` then says why (``None`` when it
    was closed here).
    """

    def __init__(
        self,
        address: str,
        *,
        service: UUID | str | int,
        data_callback: Callable[[bytes], None] | None = None,
        disconnected_callback: Callable[[RfcommClient], None] | None = None,
        timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Describe the connection; ``connect()`` opens it.

        Raises:
            ValueError: An address or service that is not valid.
        """
        self.address = normalize_address(address)
        self.service = service_uuid(service)
        self._data_callback = data_callback
        self._disconnected_callback = disconnected_callback
        self._timeout = timeout
        self._transport: RfcommTransport | None = None
        self._closed: asyncio.Event | None = None
        #: When the link last opened, in ``time.monotonic()`` seconds.
        self.connected_at: float | None = None
        #: Why the link last ended: ``None`` when closed here.
        self.disconnect_reason: Exception | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether the link is open."""
        return self._transport is not None and not self._transport.is_closing()

    async def connect(
        self, *, timeout: float | None = None, write_on_connect: bytes = b""
    ) -> None:
        """Open the link, sending ``write_on_connect`` the moment it opens.

        Raises:
            RfcommConnectError: The service could not be opened; with
                ``errno`` ``EBUSY`` when BlueZ says it is busy for now.
            TimeoutError: No connection within the timeout.
        """
        if self.is_connected:
            return
        self._closed = asyncio.Event()
        await create_rfcomm_connection(
            self.address,
            lambda: _ClientProtocol(self),
            service=self.service,
            timeout=self._timeout if timeout is None else timeout,
            write_on_connect=write_on_connect,
        )

    def write_nowait(self, data: bytes) -> None:
        """Queue bytes to send, in order, without waiting.

        Raises:
            RfcommConnectError: Not connected.
        """
        if self._transport is None or self._transport.is_closing():
            raise RfcommConnectError(errno.ENOTCONN, f"Not connected to {self.address}")
        self._transport.write(data)

    async def write(self, data: bytes) -> None:
        """Send bytes, waiting until they are on their way.

        Raises:
            RfcommConnectError: Not connected.
        """
        self.write_nowait(data)
        assert self._transport is not None
        await self._transport.drain()

    async def disconnect(self) -> None:
        """Close the link, once what is queued has been sent, and wait."""
        transport, closed = self._transport, self._closed
        if transport is None or closed is None:
            return
        transport.close()
        await closed.wait()

    async def __aenter__(self) -> RfcommClient:
        """Connect."""
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Disconnect."""
        await self.disconnect()


class _ClientProtocol(asyncio.Protocol):
    """Passes a link's events to its client."""

    def __init__(self, client: RfcommClient) -> None:
        self._client = client

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, RfcommTransport)
        self._client._transport = transport
        self._client.connected_at = time.monotonic()
        self._client.disconnect_reason = None

    def data_received(self, data: bytes) -> None:
        if self._client._data_callback is not None:
            self._client._data_callback(data)

    def connection_lost(self, exc: Exception | None) -> None:
        client = self._client
        client._transport = None
        client.disconnect_reason = exc
        if client._closed is not None:
            client._closed.set()
        if client._disconnected_callback is not None:
            client._disconnected_callback(client)


class _Connectable(Protocol):
    async def connect(
        self, *, timeout: float | None = None, write_on_connect: bytes = b""
    ) -> None: ...


_ClientT = TypeVar("_ClientT", bound=_Connectable)

#: How many times establish_rfcomm_connection() tries, and how long it waits
#: between tries, while the device is busy.
MAX_ATTEMPTS = 4
RETRY_DELAY = 1.0


async def establish_rfcomm_connection(
    client: _ClientT,
    *,
    timeout: float | None = None,
    write_on_connect: bytes = b"",
    max_attempts: int = MAX_ATTEMPTS,
    retry_delay: float = RETRY_DELAY,
) -> _ClientT:
    """Connect a client, retrying while BlueZ says the device is busy.

    The equivalent of bleak-retry-connector's ``establish_connection()``:
    just after a link closes, BlueZ can refuse a new one for a moment.
    Any other failure is raised at once.

    Raises:
        RfcommConnectError: The service could not be opened.
        TimeoutError: No connection within the timeout.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            await client.connect(timeout=timeout, write_on_connect=write_on_connect)
        except OSError as err:
            if err.errno != errno.EBUSY or attempt == max_attempts:
                raise
            _LOGGER.debug(
                "Busy (attempt %d of %d), trying again in %.1f s: %s",
                attempt,
                max_attempts,
                retry_delay,
                err,
            )
            await asyncio.sleep(retry_delay)
        else:
            return client
    raise AssertionError("unreachable")
