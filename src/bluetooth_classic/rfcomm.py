"""RFCOMM connections to Bluetooth Classic devices, through one aio-rfcomm owner.

:class:`RfcommConnection` is a connection to a device's RFCOMM service, such
as its Serial Port (``SERIAL_PORT``): open it, write to it and close it;
data and its closing arrive as callbacks. :func:`establish_rfcomm_connection`
opens one, trying again while BlueZ says the device is busy, as
bleak-retry-connector's ``establish_connection()`` does.

aio-rfcomm makes the connection: on Linux, BlueZ connects the service
through its profile API, finds the channel itself and hands the connected
socket over. aio-rfcomm uses BlueZ's first adapter.

aio-rfcomm allows one Bluetooth object per process. This module owns it,
opened on first use, on an event loop of its own in a background thread, so
bytes given as ``write_on_open`` go out the moment the link opens, however
busy the caller's loop is; every caller in the process shares it, so open
connections through here rather than opening aio-rfcomm directly.
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
from dataclasses import dataclass
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


# Device services with a link open or opening: one at a time each.
_busy: set[tuple[str, UUID]] = set()
_links_lock = threading.Lock()


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
        self,
        stack: AsyncExitStack,
        channel: aio_rfcomm.RfcommChannel,
        opened_at: float,
    ) -> None:
        self._stack = stack
        self._channel = channel
        #: When it opened, in time.monotonic() seconds, on this thread.
        self.opened_at = opened_at
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
    target: _Target, timeout: float | None, write_on_open: bytes
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
        if write_on_open:
            await channel.send(write_on_open)
    except BaseException as err:
        _log_failure(target, time.monotonic() - asked, err)
        await asyncio.shield(stack.aclose())
        raise
    _LOGGER.debug(
        "Opened %s's service %s %.3f s after asking; %d bytes sent %.1f ms later",
        target.address,
        target.service,
        opened - asked,
        len(write_on_open),
        (time.monotonic() - opened) * 1000,
    )
    return _Channel(stack, channel, opened)


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


# Connections.

# BlueZ's errors for a device or service still busy, as just after a link
# closed. Which one BlueZ gives is unconfirmed.
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


class RfcommConnection:
    """A connection to one of a device's RFCOMM services (a data link connection).

    It is one service connection on the device's link, which BlueZ brings up
    when it opens and keeps while any service on the device needs it:
    closing this connection ends only this connection.

    Open it with :meth:`open`, or with :func:`establish_rfcomm_connection`
    to retry while the device is busy. Data the device sends is passed to
    ``data_callback`` as it arrives, from the first byte: it is a stream, so
    the chunks have no message boundaries and framing is the caller's job.
    ``closed_callback`` is called with the connection when it closes, and
    ``close_reason`` then says why (``None`` when closed here).
    """

    def __init__(
        self,
        address: str,
        *,
        service: UUID | str | int,
        data_callback: Callable[[bytes], None] | None = None,
        closed_callback: Callable[[RfcommConnection], None] | None = None,
        timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Describe the connection; :meth:`open` opens it.

        Raises:
            ValueError: An address or service that is not valid.
        """
        self.address = normalize_address(address)
        self.service = service_uuid(service)
        self._data_callback = data_callback
        self._closed_callback = closed_callback
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channel: _Channel | None = None
        # Whether this connection holds the device service's one link.
        self._holding = False
        # Set once closing has begun, here or by the device.
        self._closing = False
        # Set once the caller has been told it closed.
        self._reported = False
        # Set once the channel has closed and the service is free.
        self._closed: asyncio.Event | None = None
        # Set while open() is opening the link, and once it has finished.
        self._opening: asyncio.Event | None = None
        # Set when close() is called while the link is still opening.
        self._abandoned = False
        self._unsent = 0
        self._sent = asyncio.Event()
        self._sent.set()
        #: When the link last opened, in ``time.monotonic()`` seconds, taken
        #: on the owner's thread the moment it opened.
        self.opened_at: float | None = None
        #: Why the link last closed: ``None`` when closed here.
        self.close_reason: Exception | None = None

    @property
    def _key(self) -> tuple[str, UUID]:
        return (self.address, self.service)

    @property
    def is_open(self) -> bool:
        """Return whether the link is open."""
        return self._channel is not None and not self._closing

    async def open(
        self, *, timeout: float | None = None, write_on_open: bytes = b""
    ) -> None:
        """Open the link, sending ``write_on_open`` the moment it opens.

        The bytes go out from the owner's thread, however busy the caller's
        event loop is: some devices change state soon after a connection
        opens and only answer a request sent before then.

        Raises:
            RfcommConnectError: The service could not be opened; with
                ``errno`` ``EBUSY`` when it is busy for now (BlueZ says so,
                or an old link to it is still closing), ``ECONNABORTED``
                when closed while opening.
            TimeoutError: No connection within the timeout.
        """
        if self.is_open:
            return
        key = self._key
        with _links_lock:
            if key in _busy:
                # Busy, as BlueZ would say: an old link may still be closing.
                raise RfcommConnectError(
                    errno.EBUSY,
                    f"{self.address} already has a link to {self.service}",
                )
            _busy.add(key)
        self._holding = True
        self._loop = asyncio.get_running_loop()
        self._channel = None
        self._closing = self._reported = self._abandoned = False
        self._closed = asyncio.Event()
        self.close_reason = None
        opening = self._opening = asyncio.Event()
        try:
            channel = await self._open_channel(
                timeout=self._timeout if timeout is None else timeout,
                write_on_open=bytes(write_on_open),
            )
        finally:
            self._opening = None
            opening.set()
        self._channel = channel
        self.opened_at = channel.opened_at
        loop = self._loop
        _owner.loop().call_soon_threadsafe(
            channel.start,
            _call_on(loop, self._deliver),
            _call_on(loop, self._on_sent),
            _call_on(loop, self._end),
        )
        if self._abandoned:
            # close() was called while it opened: close it again.
            await self.close()
            raise RfcommConnectError(
                errno.ECONNABORTED, f"Closed {self.address} while opening"
            )

    async def _open_channel(
        self, *, timeout: float | None, write_on_open: bytes
    ) -> _Channel:
        key = self._key
        opening = asyncio.run_coroutine_threadsafe(
            _open_channel(_Target(*key), timeout, write_on_open), _owner.loop()
        )
        waiting = asyncio.wrap_future(opening)
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # Left to finish on the background loop, then closed; the device
            # service stays busy until then. Its outcome is read here, so
            # nothing is logged as never retrieved.
            self._holding = False
            waiting.add_done_callback(_ignore_outcome)
            opening.add_done_callback(lambda done: _abandon(done, key))
            raise
        except RfcommError as err:
            self._free()
            hint = ""
            if isinstance(err, DeviceNotFoundError):
                hint = (
                    " (aio-rfcomm uses BlueZ's first adapter: is the device "
                    "paired on it?)"
                )
            message = (
                f"{self.address}'s service {self.service} could not be "
                f"opened: {err}{hint}"
            )
            if _is_busy(err):
                # As a socket reports it, so callers can retry as they would.
                raise RfcommConnectError(errno.EBUSY, message) from err
            raise RfcommConnectError(message) from err
        except BaseException:
            self._free()
            raise

    def write_nowait(self, data: bytes) -> None:
        """Queue bytes to send, in order, without waiting.

        Raises:
            RfcommConnectError: Not open (``errno`` ``ENOTCONN``).
        """
        channel = self._channel
        if channel is None or self._closing:
            raise RfcommConnectError(errno.ENOTCONN, f"Not open to {self.address}")
        if not data:
            return
        chunk = bytes(data)
        self._unsent += len(chunk)
        self._sent.clear()
        _owner.loop().call_soon_threadsafe(channel.send, chunk)

    async def write(self, data: bytes) -> None:
        """Send bytes, waiting until they are on their way.

        It does not wait for the device to receive them: a stream has no
        acknowledgement.

        Raises:
            RfcommConnectError: Not open (``errno`` ``ENOTCONN``).
        """
        self.write_nowait(data)
        await self._sent.wait()

    async def close(self) -> None:
        """Close the link once what is queued has been sent, and wait.

        A device that stops reading holds it at most ``DRAIN_TIMEOUT``
        seconds. Called while the link is still opening, it waits for that,
        and the link is closed as soon as it opens.
        """
        opening = self._opening
        if opening is not None:
            self._abandoned = True
            await opening.wait()
        if self._channel is None or self._closed is None:
            return
        if not self._closing:
            self._closing = True
            self._close_channel(drain=True, then="finish")
        await self._closed.wait()

    def abort(self) -> None:
        """Close at once, dropping what is queued."""
        if self._channel is None:
            return
        first = not self._closing
        self._closing = True
        self._close_channel(drain=False, then="finish" if first else None)

    async def __aenter__(self) -> RfcommConnection:
        """Open."""
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close."""
        await self.close()

    def _deliver(self, data: bytes) -> None:
        if not self._reported and self._data_callback is not None:
            self._data_callback(data)

    def _on_sent(self, count: int) -> None:
        self._unsent = max(0, self._unsent - count)
        if not self._unsent:
            self._sent.set()

    def _end(self, exc: Exception) -> None:
        """The device or Bluetooth ended the link."""
        if self._closing:
            return
        self._closing = True
        self.close_reason = exc
        # Reported at once; the device service is freed once the channel
        # has closed behind it, so a new link cannot race its teardown.
        self._report()
        self._close_channel(drain=False, then="free")

    def _close_channel(self, *, drain: bool, then: str | None) -> None:
        channel = self._channel
        assert channel is not None and self._loop is not None
        closing = asyncio.run_coroutine_threadsafe(
            channel.close(drain=drain), _owner.loop()
        )
        if then is not None:
            after = _call_on(
                self._loop, self._finish if then == "finish" else self._free
            )
            closing.add_done_callback(lambda _: after())

    def _finish(self) -> None:
        self._free()
        self._report()

    def _free(self) -> None:
        """Let the device service be connected again."""
        if self._holding:
            _release(self._key)
            self._holding = False
        if self._closed is not None:
            self._closed.set()

    def _report(self) -> None:
        if self._reported:
            return
        self._reported = True
        self._unsent = 0
        self._sent.set()
        if self._closed_callback is not None:
            try:
                self._closed_callback(self)
            except Exception:
                _LOGGER.exception("RFCOMM closed callback failed")


class _Openable(Protocol):
    async def open(
        self, *, timeout: float | None = None, write_on_open: bytes = b""
    ) -> None: ...


_ConnectionT = TypeVar("_ConnectionT", bound=_Openable)

#: How many times establish_rfcomm_connection() tries, and how long it waits
#: between tries, while the device is busy.
MAX_ATTEMPTS = 4
RETRY_DELAY = 1.0


async def establish_rfcomm_connection(
    connection: _ConnectionT,
    *,
    timeout: float | None = None,
    write_on_open: bytes = b"",
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> _ConnectionT:
    """Open a connection, trying again while the device is busy.

    The equivalent of bleak-retry-connector's ``establish_connection()``:
    just after a link closes, BlueZ can refuse a new one for a moment. Any
    other failure is raised at once.

    Raises:
        ValueError: ``max_attempts`` below 1.
        RfcommConnectError: The service could not be opened.
        TimeoutError: No connection within the timeout.
    """
    attempts = MAX_ATTEMPTS if max_attempts is None else max_attempts
    if attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, not {attempts}")
    delay = RETRY_DELAY if retry_delay is None else retry_delay
    for attempt in range(1, attempts + 1):
        try:
            await connection.open(timeout=timeout, write_on_open=write_on_open)
        except OSError as err:
            if err.errno != errno.EBUSY or attempt == attempts:
                raise
            _LOGGER.debug(
                "Busy (attempt %d of %d), trying again in %.1f s: %s",
                attempt,
                attempts,
                delay,
                err,
            )
            await asyncio.sleep(delay)
        else:
            return connection
    raise AssertionError("unreachable")  # pragma: no cover
