"""Tests for RFCOMM connections, with a fake aio-rfcomm."""

from __future__ import annotations

import asyncio
import errno
import gc
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from aio_rfcomm import Availability
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    BluetoothUnavailableError,
    ChannelClosedError,
    ConnectionFailedError,
    DeviceNotFoundError,
)

from bluetooth_classic import (
    SERIAL_PORT,
    RfcommConnectError,
    RfcommConnection,
    establish_rfcomm_connection,
    rfcomm,
    service_uuid,
)

from .conftest import SPEAKER_ADDRESS

OTHER = "66:77:88:99:AA:BB"
REQUEST = b"hello"


class FakeChannel:
    """An aio-rfcomm channel; used on the owner's background loop."""

    def __init__(self, address: str, service: UUID) -> None:
        self.address = address
        self.service = service
        self.sent: list[bytes] = []
        self.closed = threading.Event()
        # Bytes from the device; None ends the link, an exception fails it.
        self.incoming: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        # Cleared to hold sends, as a device that stops reading does.
        self.sending = asyncio.Event()
        self.sending.set()

    async def send(self, data: bytes) -> None:
        await self.sending.wait()
        if self.closed.is_set():
            raise ChannelClosedError
        self.sent.append(data)

    async def receive(self) -> bytes:
        data = await self.incoming.get()
        if data is None:
            raise ChannelClosedError
        if isinstance(data, Exception):
            raise data
        return data


class FakeBluetooth:
    """aio-rfcomm's Bluetooth object."""

    def __init__(self) -> None:
        self.opened = 0
        self.channels: list[FakeChannel] = []
        self.delay = 0.0
        self.hang = False
        self.fail: Exception | None = None
        self.already_open = False
        # Seconds a channel takes to close.
        self.close_delay = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None

    @asynccontextmanager
    async def open_bluetooth(self) -> AsyncIterator[FakeBluetooth]:
        if self.already_open:
            raise BluetoothAlreadyOpenError("open elsewhere")
        self.opened += 1
        self.loop = asyncio.get_running_loop()
        yield self

    @asynccontextmanager
    async def open_service(
        self, device: Any, service: UUID
    ) -> AsyncIterator[FakeChannel]:
        await asyncio.sleep(self.delay)
        if self.hang:
            await asyncio.sleep(10)
        if self.fail is not None:
            raise self.fail
        channel = FakeChannel(device.address, service)
        self.channels.append(channel)
        try:
            yield channel
        finally:
            await asyncio.sleep(self.close_delay)
            channel.closed.set()

    def on_loop(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback`` on the owner's background loop."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(callback, *args)

    def from_device(self, channel: FakeChannel, data: bytes | Exception | None) -> None:
        """The device sends bytes, ends the link (None), or it fails."""
        self.on_loop(channel.incoming.put_nowait, data)


@pytest.fixture
def bluetooth() -> Iterator[FakeBluetooth]:
    """aio-rfcomm, faked; the owner's thread is stopped afterwards."""
    fake = FakeBluetooth()
    with patch.object(rfcomm.aio_rfcomm, "open_bluetooth", fake.open_bluetooth):
        yield fake
    rfcomm._owner.shutdown()
    rfcomm._busy.clear()


class _Recorder:
    """A connection's callbacks, recorded."""

    def __init__(self) -> None:
        self.data: list[bytes] = []
        self.closed: list[RfcommConnection] = []
        self.got_data = asyncio.Event()
        self.got_closed = asyncio.Event()

    def on_data(self, data: bytes) -> None:
        self.data.append(data)
        self.got_data.set()

    def on_closed(self, connection: RfcommConnection) -> None:
        self.closed.append(connection)
        self.got_closed.set()


def _connection(
    address: str = SPEAKER_ADDRESS, recorder: _Recorder | None = None
) -> RfcommConnection:
    recorder = recorder or _Recorder()
    return RfcommConnection(
        address,
        service=SERIAL_PORT,
        data_callback=recorder.on_data,
        closed_callback=recorder.on_closed,
    )


async def _open(
    address: str = SPEAKER_ADDRESS, **kwargs: Any
) -> tuple[RfcommConnection, _Recorder]:
    recorder = _Recorder()
    connection = _connection(address, recorder)
    await connection.open(**kwargs)
    return connection, recorder


def _open_now(connection: RfcommConnection) -> bool:
    # Read through a function, so a type checker does not narrow it.
    return connection.is_open


async def _until(condition: Callable[[], Any], timeout: float = 1.0) -> None:
    """Wait until ``condition()`` holds, here or on the owner's thread."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


def test_service_uuids() -> None:
    for form in ("1101", "0x1101", 0x1101, str(SERIAL_PORT), SERIAL_PORT):
        assert service_uuid(form) == SERIAL_PORT
    assert service_uuid("00001101") == SERIAL_PORT
    with pytest.raises(ValueError):
        service_uuid("serial")


def test_a_bad_service_is_refused() -> None:
    with pytest.raises(ValueError):
        RfcommConnection(SPEAKER_ADDRESS, service="serial")


async def test_the_request_goes_first_and_data_passes_both_ways(
    bluetooth: FakeBluetooth,
) -> None:
    recorder = _Recorder()
    connection = RfcommConnection(
        "00-11-22-33-44-55",
        service=SERIAL_PORT,
        data_callback=recorder.on_data,
        closed_callback=recorder.on_closed,
    )
    assert not _open_now(connection)
    await connection.open(write_on_open=REQUEST)
    assert _open_now(connection)
    assert connection.opened_at is not None
    [channel] = bluetooth.channels
    assert (channel.address, channel.service) == (SPEAKER_ADDRESS, SERIAL_PORT)
    assert channel.sent == [REQUEST]
    await connection.write(b"one")
    assert channel.sent == [REQUEST, b"one"]
    connection.write_nowait(b"two")
    await _until(lambda: channel.sent[-1] == b"two")
    bluetooth.from_device(channel, b"reply")
    await asyncio.wait_for(recorder.got_data.wait(), 1)
    await connection.close()
    assert not _open_now(connection)
    assert recorder.data == [b"reply"]
    assert recorder.closed == [connection]
    assert connection.close_reason is None
    assert channel.closed.is_set()


async def test_opened_at_is_when_the_link_really_opened(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.delay = 0.05
    connection = _connection()
    task = asyncio.ensure_future(connection.open())
    await asyncio.sleep(0.01)
    # The caller's loop is blocked while the link opens.
    time.sleep(0.3)
    resumed = time.monotonic()
    await task
    assert connection.opened_at is not None
    assert connection.opened_at < resumed - 0.2
    await connection.close()


async def test_aio_rfcomm_is_opened_once(bluetooth: FakeBluetooth) -> None:
    first, _ = await _open()
    second, _ = await _open(OTHER)
    assert bluetooth.opened == 1
    await first.close()
    await second.close()


async def test_the_device_ending_the_link_is_reported(
    bluetooth: FakeBluetooth,
) -> None:
    connection, recorder = await _open()
    [channel] = bluetooth.channels
    bluetooth.from_device(channel, None)
    await asyncio.wait_for(recorder.got_closed.wait(), 1)
    reason = connection.close_reason
    # As a socket reports it: RFCOMM reports even a hang-up as a reset.
    assert isinstance(reason, ConnectionResetError)
    assert reason.errno == errno.ECONNRESET
    assert not _open_now(connection)
    await _until(channel.closed.is_set)
    with pytest.raises(RfcommConnectError) as raised:
        connection.write_nowait(b"late")
    assert raised.value.errno == errno.ENOTCONN


async def test_bluetooth_going_away_is_an_abort(bluetooth: FakeBluetooth) -> None:
    connection, recorder = await _open()
    bluetooth.from_device(
        bluetooth.channels[0], BluetoothUnavailableError(Availability.SWITCHED_OFF)
    )
    await asyncio.wait_for(recorder.got_closed.wait(), 1)
    reason = connection.close_reason
    assert isinstance(reason, ConnectionAbortedError)
    assert reason.errno == errno.ECONNABORTED


async def test_a_second_link_to_a_device_is_refused_as_busy(
    bluetooth: FakeBluetooth,
) -> None:
    connection, recorder = await _open()
    with pytest.raises(RfcommConnectError, match="already has a link") as raised:
        await _open()
    # Busy, so establish_rfcomm_connection() tries again.
    assert raised.value.errno == errno.EBUSY
    assert len(bluetooth.channels) == 1
    assert not recorder.closed
    # Another device can be connected meanwhile.
    other, _ = await _open(OTHER)
    await other.close()
    await connection.close()
    # Once closed, the device can be connected again.
    again, _ = await _open()
    await again.close()


async def test_after_a_drop_the_device_is_freed_once_the_link_has_closed(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.close_delay = 0.2
    _, recorder = await _open()
    [channel] = bluetooth.channels
    bluetooth.from_device(channel, None)
    # The drop is reported at once...
    await asyncio.wait_for(recorder.got_closed.wait(), 0.1)
    # ...but a new link waits for the old one to close.
    with pytest.raises(RfcommConnectError, match="already has a link"):
        await _open()
    await _until(channel.closed.is_set)
    await _until(lambda: not rfcomm._busy)
    bluetooth.close_delay = 0
    again, _ = await _open()
    await again.close()


async def test_failing_to_open(bluetooth: FakeBluetooth) -> None:
    bluetooth.fail = ConnectionFailedError("refused")
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    # An OSError too, as the link could not be opened.
    assert isinstance(raised.value, OSError)
    assert isinstance(raised.value.__cause__, ConnectionFailedError)
    # The device can be tried again.
    bluetooth.fail = None
    again, _ = await _open()
    await again.close()


class _BlueZError(Exception):
    def __init__(self, error_type: str, text: str) -> None:
        super().__init__(text)
        self.type = error_type


def _refusal(error_type: str, text: str) -> ConnectionFailedError:
    failure = ConnectionFailedError("refused")
    failure.__cause__ = _BlueZError(error_type, text)
    return failure


async def test_a_busy_device_is_marked_ebusy(bluetooth: FakeBluetooth) -> None:
    for error_type, text, expected in (
        ("org.bluez.Error.InProgress", "Operation in progress", errno.EBUSY),
        ("org.bluez.Error.Failed", "br-connection-busy", errno.EBUSY),
        ("org.bluez.Error.Failed", "Host is down", None),
    ):
        bluetooth.fail = _refusal(error_type, text)
        with pytest.raises(RfcommConnectError) as raised:
            await _open()
        assert raised.value.errno == expected


async def test_a_failure_is_logged_with_what_bluez_said(
    bluetooth: FakeBluetooth, caplog: pytest.LogCaptureFixture
) -> None:
    bluetooth.fail = _refusal("org.bluez.Error.InProgress", "br-connection-busy")
    with (
        caplog.at_level(logging.DEBUG, logger=rfcomm.__name__),
        pytest.raises(RfcommConnectError),
    ):
        await _open()
    [record] = [r for r in caplog.records if "Could not open" in r.getMessage()]
    message = record.getMessage()
    assert "ConnectionFailedError: refused" in message
    assert "from org.bluez.Error.InProgress: br-connection-busy" in message


async def test_a_timeout_is_logged_as_one(
    bluetooth: FakeBluetooth, caplog: pytest.LogCaptureFixture
) -> None:
    bluetooth.hang = True
    with (
        caplog.at_level(logging.DEBUG, logger=rfcomm.__name__),
        pytest.raises(TimeoutError),
    ):
        await _open(timeout=0.05)
    [record] = [r for r in caplog.records if "Could not open" in r.getMessage()]
    assert "TimeoutError" in record.getMessage()
    assert "Cancelled" not in record.getMessage()
    # The device can be tried again.
    bluetooth.hang = False
    again, _ = await _open()
    await again.close()


async def test_an_unknown_device_says_which_adapter(bluetooth: FakeBluetooth) -> None:
    bluetooth.fail = DeviceNotFoundError(SPEAKER_ADDRESS)
    with pytest.raises(RfcommConnectError, match="first adapter"):
        await _open()


async def test_aio_rfcomm_open_elsewhere(bluetooth: FakeBluetooth) -> None:
    bluetooth.already_open = True
    with pytest.raises(RfcommConnectError, match="one owner"):
        await _open()


async def test_the_request_goes_out_while_the_caller_is_busy(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.delay = 0.05
    connection = _connection()
    task = asyncio.ensure_future(connection.open(write_on_open=REQUEST))
    await asyncio.sleep(0.01)
    # The caller's event loop is blocked (Home Assistant starting up) while
    # the link opens.
    time.sleep(0.3)
    # Already sent, from the owner's loop, before the caller's ran again.
    assert bluetooth.channels[0].sent == [REQUEST]
    await task
    await connection.close()


async def test_an_open_given_up_midway_is_closed(bluetooth: FakeBluetooth) -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))
    try:
        bluetooth.delay = 0.1
        task = asyncio.ensure_future(_open())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The link it opened anyway is closed, and the device freed after.
        await _until(
            lambda: bluetooth.channels and bluetooth.channels[0].closed.is_set()
        )
        await _until(lambda: not rfcomm._busy)
        bluetooth.delay = 0
        again, _ = await _open()
        await again.close()
    finally:
        loop.set_exception_handler(None)


async def test_a_failing_open_given_up_leaves_nothing_unretrieved(
    bluetooth: FakeBluetooth,
) -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))
    try:
        bluetooth.delay = 0.05
        bluetooth.fail = ConnectionFailedError("refused")
        task = asyncio.ensure_future(_open())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: not rfcomm._busy)
        await asyncio.sleep(0.01)
        gc.collect()
        await asyncio.sleep(0.01)
        assert unhandled == []
    finally:
        loop.set_exception_handler(None)


async def test_closing_while_opening_closes_the_link(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.delay = 0.1
    connection = _connection()
    opening = asyncio.ensure_future(connection.open())
    await asyncio.sleep(0.02)
    await connection.close()
    with pytest.raises(RfcommConnectError) as raised:
        await opening
    assert raised.value.errno == errno.ECONNABORTED
    assert not _open_now(connection)
    await _until(bluetooth.channels[0].closed.is_set)


async def test_close_sends_what_is_queued_first(bluetooth: FakeBluetooth) -> None:
    connection, recorder = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    connection.write_nowait(b"last")
    closing = asyncio.ensure_future(connection.close())
    await asyncio.sleep(0.05)
    assert not recorder.closed
    bluetooth.on_loop(channel.sending.set)
    await asyncio.wait_for(closing, 1)
    assert channel.sent == [b"last"]
    assert channel.closed.is_set()


async def test_abort_drops_what_is_queued(bluetooth: FakeBluetooth) -> None:
    connection, recorder = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    connection.write_nowait(b"dropped")
    connection.abort()
    await asyncio.wait_for(recorder.got_closed.wait(), 1)
    assert channel.sent == []
    await _until(channel.closed.is_set)


async def test_a_close_drops_what_a_stalled_device_does_not_take(
    bluetooth: FakeBluetooth,
) -> None:
    connection, _ = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    connection.write_nowait(b"stuck")
    with patch.object(rfcomm, "DRAIN_TIMEOUT", 0.05):
        await asyncio.wait_for(connection.close(), 1)
    assert channel.sent == []
    assert channel.closed.is_set()


async def test_as_a_context_manager(bluetooth: FakeBluetooth) -> None:
    async with _connection() as connection:
        assert _open_now(connection)
    assert not _open_now(connection)
    await _until(bluetooth.channels[0].closed.is_set)


class _Flaky:
    """A connection that is busy a few times, then opens."""

    def __init__(self, busy: int, error: OSError | None = None) -> None:
        self.busy = busy
        self.error = error or OSError(errno.EBUSY, "busy")
        self.attempts = 0

    async def open(
        self, *, timeout: float | None = None, write_on_open: bytes = b""
    ) -> None:
        self.attempts += 1
        if self.attempts <= self.busy:
            raise self.error


async def test_establish_retries_while_busy() -> None:
    connection = _Flaky(busy=2)
    assert await establish_rfcomm_connection(connection, retry_delay=0) is connection
    assert connection.attempts == 3


async def test_establish_gives_up_after_its_attempts() -> None:
    connection = _Flaky(busy=10)
    with pytest.raises(OSError) as raised:
        await establish_rfcomm_connection(connection, max_attempts=3, retry_delay=0)
    assert raised.value.errno == errno.EBUSY
    assert connection.attempts == 3


async def test_establish_does_not_retry_other_failures() -> None:
    connection = _Flaky(busy=1, error=OSError(errno.EHOSTDOWN, "Host is down"))
    with pytest.raises(OSError):
        await establish_rfcomm_connection(connection, retry_delay=0)
    assert connection.attempts == 1


async def test_establish_needs_an_attempt() -> None:
    with pytest.raises(ValueError):
        await establish_rfcomm_connection(_Flaky(busy=0), max_attempts=0)


async def test_establish_opens_a_real_connection(bluetooth: FakeBluetooth) -> None:
    connection = _connection()
    await establish_rfcomm_connection(connection, write_on_open=REQUEST)
    assert bluetooth.channels[0].sent == [REQUEST]
    await connection.close()


async def test_establish_waits_out_an_old_link_still_closing(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.close_delay = 0.2
    _, recorder = await _open()
    bluetooth.from_device(bluetooth.channels[0], None)
    await asyncio.wait_for(recorder.got_closed.wait(), 0.1)
    connection = _connection()
    with patch.object(rfcomm, "RETRY_DELAY", 0.1):
        await establish_rfcomm_connection(connection)
    assert _open_now(connection)
    await connection.close()
