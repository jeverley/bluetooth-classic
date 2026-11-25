"""Tests for RFCOMM links, with a fake aio-rfcomm."""

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
    rfcomm,
    service_uuid,
)
from bluetooth_classic.rfcomm import (
    RfcommClient,
    RfcommLink,
    create_rfcomm_connection,
    establish_rfcomm_connection,
    on_rfcomm_links_changed,
    open_rfcomm_links,
)

from .conftest import SPEAKER_ADDRESS

OTHER = "66:77:88:99:AA:BB"
REQUEST = b"hello"


class _Recorder(asyncio.Protocol):
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.lost = asyncio.Event()
        self.data = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.events.append(("made",))

    def data_received(self, data: bytes) -> None:
        self.events.append(("data", data))
        self.data.set()

    def connection_lost(self, exc: Exception | None) -> None:
        self.events.append(("lost", exc))
        self.lost.set()


class FakeChannel:
    """An aio-rfcomm channel; used on the handler's background loop."""

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
        """Run ``callback`` on the handler's background loop."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(callback, *args)

    def from_device(self, channel: FakeChannel, data: bytes | Exception | None) -> None:
        """The device sends bytes, ends the link (None), or it fails."""
        self.on_loop(channel.incoming.put_nowait, data)


@pytest.fixture
def bluetooth() -> Iterator[FakeBluetooth]:
    """aio-rfcomm, faked; the handler's thread is stopped afterwards."""
    fake = FakeBluetooth()
    with patch.object(rfcomm.aio_rfcomm, "open_bluetooth", fake.open_bluetooth):
        yield fake
    rfcomm._owner.shutdown()
    rfcomm._busy.clear()
    rfcomm._links.clear()


async def _open(address: str = SPEAKER_ADDRESS, **kwargs: Any) -> tuple[Any, _Recorder]:
    transport, protocol = await create_rfcomm_connection(
        address, _Recorder, service=SERIAL_PORT, **kwargs
    )
    assert isinstance(protocol, _Recorder)
    return transport, protocol


async def _until(condition: Callable[[], Any], timeout: float = 1.0) -> None:
    """Wait until ``condition()`` holds, here or on the handler's thread."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def _close(transport: Any, protocol: _Recorder) -> None:
    transport.close()
    await asyncio.wait_for(protocol.lost.wait(), 1)


def test_service_uuids() -> None:
    for form in ("1101", "0x1101", 0x1101, str(SERIAL_PORT), SERIAL_PORT):
        assert service_uuid(form) == SERIAL_PORT
    assert service_uuid("00001101") == SERIAL_PORT
    with pytest.raises(ValueError):
        service_uuid("serial")


async def test_the_request_goes_first(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open(write_on_connect=REQUEST)
    [channel] = bluetooth.channels
    assert (channel.address, channel.service) == (SPEAKER_ADDRESS, SERIAL_PORT)
    assert channel.sent == [REQUEST]
    assert transport.get_extra_info("rfcomm_service") == SERIAL_PORT
    assert transport.get_extra_info("rfcomm_address") == SPEAKER_ADDRESS
    transport.write(b"more")
    await _until(lambda: channel.sent == [REQUEST, b"more"])
    bluetooth.from_device(channel, b"reply")
    await asyncio.wait_for(protocol.data.wait(), 1)
    await _close(transport, protocol)
    assert protocol.events == [("made",), ("data", b"reply"), ("lost", None)]
    assert channel.closed.is_set()


async def test_aio_rfcomm_is_opened_once(bluetooth: FakeBluetooth) -> None:
    first = await _open()
    second = await _open(OTHER)
    assert bluetooth.opened == 1
    await _close(*first)
    await _close(*second)


async def test_the_device_ending_the_link_is_reported(
    bluetooth: FakeBluetooth,
) -> None:
    _, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.from_device(channel, None)
    await asyncio.wait_for(protocol.lost.wait(), 1)
    lost = protocol.events[-1][1]
    # As a socket reports it: RFCOMM reports even a hang-up as a reset.
    assert isinstance(lost, ConnectionResetError)
    assert lost.errno == errno.ECONNRESET
    assert open_rfcomm_links() == []
    await _until(channel.closed.is_set)


async def test_bluetooth_going_away_is_an_abort(bluetooth: FakeBluetooth) -> None:
    _, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.from_device(channel, BluetoothUnavailableError(Availability.SWITCHED_OFF))
    await asyncio.wait_for(protocol.lost.wait(), 1)
    lost = protocol.events[-1][1]
    assert isinstance(lost, ConnectionAbortedError)
    assert lost.errno == errno.ECONNABORTED


async def test_after_a_drop_the_device_is_freed_once_the_link_has_closed(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.close_delay = 0.2
    _, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.from_device(channel, None)
    # The drop is reported at once...
    await asyncio.wait_for(protocol.lost.wait(), 0.1)
    # ...but a new link waits for the old one to close.
    with pytest.raises(RfcommConnectError, match="already has a link"):
        await _open()
    await _until(channel.closed.is_set)
    await _until(lambda: not rfcomm._busy)
    bluetooth.close_delay = 0
    await _close(*await _open())


async def test_create_rfcomm_connection_connects_by_service(
    bluetooth: FakeBluetooth,
) -> None:
    transport, protocol = await create_rfcomm_connection(
        "00-11-22-33-44-55", _Recorder, service=SERIAL_PORT, write_on_connect=REQUEST
    )
    assert bluetooth.channels[0].sent == [REQUEST]
    assert isinstance(protocol, _Recorder)
    await _close(transport, protocol)


async def test_a_second_link_to_a_device_is_refused(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open()
    with pytest.raises(RfcommConnectError, match="already has a link"):
        await _open()
    assert len(bluetooth.channels) == 1
    assert not protocol.lost.is_set()
    # Another device can be connected meanwhile.
    await _close(*await _open(OTHER))
    await _close(transport, protocol)
    # Once closed, the device can be connected again.
    await _close(*await _open())


async def test_open_links_are_listed(bluetooth: FakeBluetooth) -> None:
    changes: list[list[RfcommLink]] = []
    unsubscribe = on_rfcomm_links_changed(lambda: changes.append(open_rfcomm_links()))
    try:
        transport, protocol = await _open()
        [listed] = open_rfcomm_links()
        assert (listed.address, listed.service) == (SPEAKER_ADDRESS, SERIAL_PORT)
        await _close(transport, protocol)
        assert open_rfcomm_links() == []
        assert changes == [[listed], []]
    finally:
        unsubscribe()


async def test_failing_to_connect(bluetooth: FakeBluetooth) -> None:
    bluetooth.fail = ConnectionFailedError("refused")
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    # An OSError too, as the link could not be opened.
    assert isinstance(raised.value, OSError)
    assert isinstance(raised.value.__cause__, ConnectionFailedError)
    # The device can be tried again.
    bluetooth.fail = None
    await _close(*await _open())


async def test_a_failure_is_logged_with_what_bluez_said(
    bluetooth: FakeBluetooth, caplog: pytest.LogCaptureFixture
) -> None:
    class BlueZError(Exception):
        type = "org.bluez.Error.InProgress"

    failure = ConnectionFailedError("refused")
    failure.__cause__ = BlueZError("br-connection-busy")
    bluetooth.fail = failure
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


async def test_an_unknown_device_says_which_adapter(bluetooth: FakeBluetooth) -> None:
    bluetooth.fail = DeviceNotFoundError(SPEAKER_ADDRESS)
    with pytest.raises(RfcommConnectError, match="first adapter"):
        await _open()


async def test_a_connect_given_up_leaves_nothing_unretrieved(
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


async def test_aio_rfcomm_open_elsewhere(bluetooth: FakeBluetooth) -> None:
    bluetooth.already_open = True
    with pytest.raises(RfcommConnectError, match="one owner"):
        await _open()


async def test_no_connection_in_time(bluetooth: FakeBluetooth) -> None:
    bluetooth.hang = True
    with pytest.raises(TimeoutError):
        await _open(timeout=0.05)
    bluetooth.hang = False
    await _close(*await _open())


async def test_the_request_goes_out_while_the_caller_is_busy(
    bluetooth: FakeBluetooth,
) -> None:
    bluetooth.delay = 0.05
    task = asyncio.ensure_future(_open(write_on_connect=REQUEST))
    await asyncio.sleep(0.01)
    # The caller's event loop is blocked (Home Assistant starting up) while
    # the channel opens.
    time.sleep(0.3)
    # Already sent, from the handler's own loop, before the caller's ran again.
    assert bluetooth.channels[0].sent == [REQUEST]
    await _close(*await task)


async def test_a_connect_given_up_midway_is_closed(bluetooth: FakeBluetooth) -> None:
    bluetooth.delay = 0.1
    task = asyncio.ensure_future(_open())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The channel it opened anyway is closed, and the device freed after.
    await _until(lambda: bluetooth.channels and bluetooth.channels[0].closed.is_set())
    await _until(lambda: not rfcomm._busy)
    bluetooth.delay = 0
    await _close(*await _open())


async def test_close_sends_what_is_queued_first(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    transport.write(b"last")
    assert transport.get_write_buffer_size() == 4
    transport.close()
    await asyncio.sleep(0.05)
    assert not protocol.lost.is_set()
    bluetooth.on_loop(channel.sending.set)
    await asyncio.wait_for(protocol.lost.wait(), 1)
    assert channel.sent == [b"last"]
    assert channel.closed.is_set()


async def test_abort_drops_what_is_queued(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    transport.write(b"dropped")
    transport.close()
    transport.abort()
    await asyncio.wait_for(protocol.lost.wait(), 1)
    assert channel.sent == []
    assert channel.closed.is_set()


async def test_a_bad_service_is_refused() -> None:
    with pytest.raises(ValueError):
        await create_rfcomm_connection(SPEAKER_ADDRESS, _Recorder, service="serial")


async def test_sent_bytes_leave_the_write_buffer(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open()
    [channel] = bluetooth.channels
    transport.write(b"data")
    assert transport.get_write_buffer_size() == 4
    await _until(lambda: transport.get_write_buffer_size() == 0)
    assert channel.sent == [b"data"]
    await _close(transport, protocol)


async def test_a_close_drops_what_a_stalled_device_does_not_take(
    bluetooth: FakeBluetooth,
) -> None:
    transport, protocol = await _open()
    [channel] = bluetooth.channels
    bluetooth.on_loop(channel.sending.clear)
    await asyncio.sleep(0.01)
    transport.write(b"stuck")
    with patch.object(rfcomm, "DRAIN_TIMEOUT", 0.05):
        transport.close()
        await asyncio.wait_for(protocol.lost.wait(), 1)
    assert channel.sent == []
    assert channel.closed.is_set()


def _connected(client: RfcommClient) -> bool:
    # Read through a function, so a type checker does not narrow it.
    return client.is_connected


async def test_the_client_connects_and_passes_data_on(bluetooth: FakeBluetooth) -> None:
    received: list[bytes] = []
    dropped: list[RfcommClient] = []
    client = RfcommClient(
        "00-11-22-33-44-55",
        service=SERIAL_PORT,
        data_callback=received.append,
        disconnected_callback=dropped.append,
    )
    assert not _connected(client)
    await client.connect(write_on_connect=REQUEST)
    assert _connected(client)
    assert client.connected_at is not None
    [channel] = bluetooth.channels
    assert channel.sent == [REQUEST]
    await client.write(b"one")
    assert channel.sent == [REQUEST, b"one"]
    client.write_nowait(b"two")
    await _until(lambda: channel.sent[-1] == b"two")
    bluetooth.from_device(channel, b"reply")
    await _until(lambda: received == [b"reply"])
    await client.disconnect()
    assert not _connected(client)
    assert dropped == [client]
    assert client.disconnect_reason is None


async def test_the_client_reports_a_drop(bluetooth: FakeBluetooth) -> None:
    dropped: list[RfcommClient] = []
    client = RfcommClient(
        SPEAKER_ADDRESS, service=SERIAL_PORT, disconnected_callback=dropped.append
    )
    await client.connect()
    bluetooth.from_device(bluetooth.channels[0], None)
    await _until(lambda: dropped == [client])
    assert isinstance(client.disconnect_reason, ConnectionResetError)
    with pytest.raises(RfcommConnectError) as raised:
        client.write_nowait(b"late")
    assert raised.value.errno == errno.ENOTCONN


async def test_the_client_as_a_context_manager(bluetooth: FakeBluetooth) -> None:
    async with RfcommClient(SPEAKER_ADDRESS, service=SERIAL_PORT) as client:
        assert _connected(client)
    assert not _connected(client)
    await _until(bluetooth.channels[0].closed.is_set)


class _BlueZError(Exception):
    def __init__(self, error_type: str, text: str) -> None:
        super().__init__(text)
        self.type = error_type


def _refusal(error_type: str, text: str) -> ConnectionFailedError:
    failure = ConnectionFailedError("refused")
    failure.__cause__ = _BlueZError(error_type, text)
    return failure


async def test_a_busy_device_is_marked_ebusy(bluetooth: FakeBluetooth) -> None:
    bluetooth.fail = _refusal("org.bluez.Error.InProgress", "Operation in progress")
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    assert raised.value.errno == errno.EBUSY
    bluetooth.fail = _refusal("org.bluez.Error.Failed", "br-connection-busy")
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    assert raised.value.errno == errno.EBUSY
    bluetooth.fail = _refusal("org.bluez.Error.Failed", "Host is down")
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    assert raised.value.errno is None


class _Flaky:
    """A client that is busy a few times, then connects."""

    def __init__(self, busy: int, error: OSError | None = None) -> None:
        self.busy = busy
        self.error = error or OSError(errno.EBUSY, "busy")
        self.attempts = 0

    async def connect(
        self, *, timeout: float | None = None, write_on_connect: bytes = b""
    ) -> None:
        self.attempts += 1
        if self.attempts <= self.busy:
            raise self.error


async def test_establish_retries_while_busy() -> None:
    client = _Flaky(busy=2)
    assert await establish_rfcomm_connection(client, retry_delay=0) is client
    assert client.attempts == 3


async def test_establish_gives_up_after_its_attempts() -> None:
    client = _Flaky(busy=10)
    with pytest.raises(OSError) as raised:
        await establish_rfcomm_connection(client, max_attempts=3, retry_delay=0)
    assert raised.value.errno == errno.EBUSY
    assert client.attempts == 3


async def test_establish_does_not_retry_other_failures() -> None:
    client = _Flaky(busy=1, error=OSError(errno.EHOSTDOWN, "Host is down"))
    with pytest.raises(OSError):
        await establish_rfcomm_connection(client, retry_delay=0)
    assert client.attempts == 1


async def test_establish_connects_a_real_client(bluetooth: FakeBluetooth) -> None:
    client = RfcommClient(SPEAKER_ADDRESS, service=SERIAL_PORT)
    await establish_rfcomm_connection(client, write_on_connect=REQUEST)
    assert bluetooth.channels[0].sent == [REQUEST]
    await client.disconnect()
