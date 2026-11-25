"""Tests for the rfcomm:// serialx handler, with a fake aio-rfcomm."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import serialx
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    ChannelClosedError,
    ConnectionFailedError,
)

from bluetooth_classic import (
    SERIAL_PORT,
    RfcommConnectError,
    service_uuid,
)
from bluetooth_classic.rfcomm import (
    RfcommLink,
    RfcommTarget,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
    parse_rfcomm_url,
    rfcomm_url,
)

from .conftest import SPEAKER_ADDRESS

URL = f"rfcomm://{SPEAKER_ADDRESS}?service=1101"
OTHER = "66:77:88:99:AA:BB"
REQUEST = b"hello"

# serialx uses whichever copy of the module registered first; patch that one.
rfcomm = sys.modules[serialx.common.get_uri_handler(URL).async_transport_cls.__module__]


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
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
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
            channel.closed.set()

    def on_loop(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback`` on the handler's background loop."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(callback, *args)

    def from_device(self, channel: FakeChannel, data: bytes | None) -> None:
        """The device sends bytes, or ends the link with None."""
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


async def _open(url: str = URL, **kwargs: Any) -> tuple[Any, _Recorder]:
    transport, protocol = await serialx.create_serial_connection(
        asyncio.get_running_loop(), _Recorder, url, baudrate=9600, **kwargs
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


def test_urls() -> None:
    url = rfcomm_url("00-11-22-33-44-55", service="1101")
    assert url == (
        f"rfcomm://{SPEAKER_ADDRESS}?service=00001101-0000-1000-8000-00805f9b34fb"
    )
    assert parse_rfcomm_url(url) == RfcommTarget(SPEAKER_ADDRESS, SERIAL_PORT)
    assert parse_rfcomm_url(URL) == RfcommTarget(SPEAKER_ADDRESS, SERIAL_PORT)


@pytest.mark.parametrize(
    "url",
    [
        "socket://00:11:22:33:44:55?service=1101",
        "rfcomm://00:11:22:33:44:55",
        "rfcomm://00:11:22:33:44:55?channel=1",
        "rfcomm://00:11:22:33:44:55?service=1101&security=medium",
        "rfcomm://00:11:22:33:44:55?service=serial",
        "rfcomm://00:11:22:33:44:55?service=",
        "rfcomm://00:11:22:33:44:55?service=1101&service=1102",
        "rfcomm://00:11:22:33:44:55?service=1101&adapter=hci0",
        "rfcomm://not-an-address?service=1101",
    ],
)
def test_bad_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_rfcomm_url(url)


def test_the_service_is_a_uuid() -> None:
    assert isinstance(parse_rfcomm_url(URL).service, UUID)


async def test_the_request_goes_first(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open(write_on_connect=REQUEST)
    [channel] = bluetooth.channels
    assert (channel.address, channel.service) == (SPEAKER_ADDRESS, SERIAL_PORT)
    assert channel.sent == [REQUEST]
    assert transport.get_extra_info("rfcomm_service") == SERIAL_PORT
    transport.write(b"more")
    await _until(lambda: channel.sent == [REQUEST, b"more"])
    bluetooth.from_device(channel, b"reply")
    await asyncio.wait_for(protocol.data.wait(), 1)
    await _close(transport, protocol)
    assert protocol.events == [("made",), ("data", b"reply"), ("lost", None)]
    assert channel.closed.is_set()


async def test_aio_rfcomm_is_opened_once(bluetooth: FakeBluetooth) -> None:
    first = await _open()
    second = await _open(f"rfcomm://{OTHER}?service=1101")
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
    assert isinstance(protocol.events[-1][1], ConnectionResetError)
    assert open_rfcomm_links() == []
    await _until(channel.closed.is_set)


async def test_open_rfcomm_connects_by_service(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await open_rfcomm(
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
    await _close(*await _open(f"rfcomm://{OTHER}?service=1101"))
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


async def test_aio_rfcomm_open_elsewhere(bluetooth: FakeBluetooth) -> None:
    bluetooth.already_open = True
    with pytest.raises(RfcommConnectError, match="one owner"):
        await _open()


async def test_no_connection_in_time(bluetooth: FakeBluetooth) -> None:
    bluetooth.hang = True
    with pytest.raises(TimeoutError):
        await _open(connect_timeout=0.05)
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


async def test_flush_waits_until_sent(bluetooth: FakeBluetooth) -> None:
    transport, protocol = await _open()
    [channel] = bluetooth.channels
    transport.write(b"data")
    await asyncio.wait_for(transport.flush(), 1)
    assert channel.sent == [b"data"]
    assert transport.get_write_buffer_size() == 0
    await _close(transport, protocol)


def test_blocking_use_is_refused() -> None:
    port = serialx.serial_for_url(URL, baudrate=9600)
    with pytest.raises(io.UnsupportedOperation):
        port.open()


async def test_write_on_connect_is_only_for_rfcomm() -> None:
    # serialx drops it for other backends rather than failing.
    handler = serialx.common.get_uri_handler("socket://localhost:1")
    routed = serialx.common.route_backend_kwargs(handler, {"write_on_connect": REQUEST})
    assert routed == {}
