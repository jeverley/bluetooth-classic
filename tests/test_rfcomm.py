"""Tests for the rfcomm:// serialx handler, with a socket pair for the link."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import serialx

from bluetooth_classic import SERIAL_PORT, ServiceLookupError
from bluetooth_classic.rfcomm import (
    RfcommLink,
    RfcommSerialTransport,
    RfcommTarget,
    Security,
    _rfcomm_socket,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
    parse_rfcomm_url,
    rfcomm_url,
)

ADDRESS = "00:11:22:33:44:55"
URL = f"rfcomm://{ADDRESS}?channel=1"
SERVICE_URL = f"rfcomm://{ADDRESS}?service=1101"
REQUEST = b"hello"

# serialx uses whichever copy of the module registered first; patch that one.
rfcomm = sys.modules[serialx.common.get_uri_handler(URL).async_transport_cls.__module__]


class _Recorder(asyncio.Protocol):
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.lost = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.events.append(("made",))

    def data_received(self, data: bytes) -> None:
        self.events.append(("data", data))

    def connection_lost(self, exc: Exception | None) -> None:
        self.events.append(("lost", exc))
        self.lost.set()


class _Link:
    """One end stands in for the RFCOMM socket; the other is the device."""

    def __init__(self) -> None:
        self.ours, self.device = socket.socketpair()
        self.connected_to: tuple[str, int] | None = None
        self.reply_on_connect = b""
        # Channels nothing listens on, and the channel the service is on.
        self.refused: set[int] = set()
        self.offered: int | None = 1
        self.lookups: list[tuple[str, Any, str | None]] = []
        self.timeouts: list[float | None] = []

    def connect(
        self, target: RfcommTarget, channel: int, timeout: float | None
    ) -> socket.socket:
        self.timeouts.append(timeout)
        if channel in self.refused:
            raise ConnectionRefusedError(111, "Connection refused")
        # The pair is connected already; record where it would have gone.
        self.connected_to = (target.address, channel)
        if self.reply_on_connect:
            self.device.sendall(self.reply_on_connect)
        return self.ours

    def look_up(
        self, address: str, service: Any, *, adapter: str | None, timeout: float
    ) -> int | None:
        self.lookups.append((address, service, adapter))
        return self.offered

    def close(self) -> None:
        self.ours.close()
        self.device.close()


@pytest.fixture
def link() -> Iterator[_Link]:
    link = _Link()
    with (
        patch.object(rfcomm, "rfcomm_supported", return_value=True),
        patch.object(rfcomm, "_connect", link.connect),
        patch.object(rfcomm, "find_rfcomm_channel_blocking", link.look_up),
    ):
        yield link
    link.close()


def test_urls() -> None:
    assert rfcomm_url("00-11-22-33-44-55", channel=1) == URL
    assert parse_rfcomm_url(URL) == RfcommTarget(ADDRESS, channel=1)
    secure = f"rfcomm://{ADDRESS}?channel=30&security=medium"
    assert rfcomm_url(ADDRESS, channel=30, security=Security.MEDIUM) == secure
    assert parse_rfcomm_url(secure) == RfcommTarget(
        ADDRESS, channel=30, security=Security.MEDIUM
    )
    assert parse_rfcomm_url(f"{URL}&security=HIGH").security == Security.HIGH


def test_service_urls() -> None:
    url = rfcomm_url(ADDRESS, service="1101", channel=10, adapter="hci1")
    assert url == (
        f"rfcomm://{ADDRESS}?service=00001101-0000-1000-8000-00805f9b34fb"
        "&channel=10&adapter=hci1"
    )
    assert parse_rfcomm_url(url) == RfcommTarget(
        ADDRESS, service=SERIAL_PORT, channel=10, adapter="hci1"
    )
    assert parse_rfcomm_url(f"rfcomm://{ADDRESS}?service=0x1101") == RfcommTarget(
        ADDRESS, service=SERIAL_PORT
    )
    with pytest.raises(ValueError):
        rfcomm_url(ADDRESS)


@pytest.mark.parametrize(
    "url",
    [
        "socket://00:11:22:33:44:55?channel=1",
        "rfcomm://00:11:22:33:44:55",
        "rfcomm://00:11:22:33:44:55?security=low",
        "rfcomm://00:11:22:33:44:55?channel=1&security=none",
        "rfcomm://00:11:22:33:44:55?channel=1&security=low&security=high",
        "rfcomm://00:11:22:33:44:55?channel=x",
        "rfcomm://00:11:22:33:44:55?channel=0",
        "rfcomm://00:11:22:33:44:55?channel=31",
        "rfcomm://00:11:22:33:44:55?channel=1&channel=2",
        "rfcomm://00:11:22:33:44:55?service=serial",
        "rfcomm://00:11:22:33:44:55?channel=1&adapter=eth0",
        "rfcomm://00:11:22:33:44:55?channel=1&colour=blue",
        "rfcomm://not-an-address?channel=1",
    ],
)
def test_bad_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_rfcomm_url(url)


class _NewSocket:
    def __init__(self, *args: object, fail: bool = False) -> None:
        self.options: list[tuple[Any, ...]] = []
        self.closed = False
        self.fail = fail

    def setsockopt(self, *args: object) -> None:
        if self.fail:
            raise OSError("refused")
        self.options.append(args)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def new_socket() -> Iterator[_NewSocket]:
    sock = _NewSocket()
    with (
        patch.object(rfcomm, "rfcomm_supported", return_value=True),
        patch.object(socket, "socket", return_value=sock),
        patch.object(socket, "AF_BLUETOOTH", 31, create=True),
        patch.object(socket, "BTPROTO_RFCOMM", 3, create=True),
    ):
        yield sock


def test_security_is_set_only_when_asked(new_socket: _NewSocket) -> None:
    _rfcomm_socket(None)
    assert new_socket.options == []
    _rfcomm_socket(Security.MEDIUM)
    assert new_socket.options == [(274, 4, b"\x02\x00")]


def test_security_that_cannot_be_set_fails_the_connect(
    new_socket: _NewSocket,
) -> None:
    new_socket.fail = True
    with pytest.raises(OSError):
        _rfcomm_socket(Security.HIGH)
    assert new_socket.closed


def test_connect_binds_to_the_adapter_asked_for() -> None:
    calls: list[tuple[Any, ...]] = []

    class _Socket:
        def settimeout(self, timeout: float) -> None:
            calls.append(("settimeout", timeout))

        def bind(self, address: tuple[str, int]) -> None:
            calls.append(("bind", address))

        def connect(self, address: tuple[str, int]) -> None:
            calls.append(("connect", address))

    with (
        patch.object(rfcomm, "_rfcomm_socket", return_value=_Socket()),
        patch.object(rfcomm, "adapter_address", return_value="AA:BB:CC:DD:EE:FF"),
    ):
        rfcomm._connect(RfcommTarget(ADDRESS, channel=1, adapter="hci1"), 1, 5.0)
    assert calls == [
        ("settimeout", 5.0),
        ("bind", ("AA:BB:CC:DD:EE:FF", 0)),
        ("connect", (ADDRESS, 1)),
    ]


async def test_connection_through_serialx(link: _Link) -> None:
    link.reply_on_connect = b"reply"
    loop = asyncio.get_running_loop()
    transport, protocol = await serialx.create_serial_connection(
        loop, _Recorder, URL, baudrate=9600, write_on_connect=REQUEST
    )
    assert isinstance(transport, RfcommSerialTransport)
    assert isinstance(protocol, _Recorder)
    assert link.connected_to == (ADDRESS, 1)
    # The device got the request before anything else.
    link.device.settimeout(1)
    assert link.device.recv(64) == REQUEST
    transport.write(b"next")
    assert link.device.recv(64) == b"next"
    # The reply sent as the link opened reaches the protocol after it is told
    # the connection opened.
    for _ in range(100):
        if len(protocol.events) >= 2:
            break
        await asyncio.sleep(0.01)
    assert protocol.events[:2] == [("made",), ("data", b"reply")]
    # The device closing the link is reported.
    link.device.close()
    await asyncio.wait_for(protocol.lost.wait(), 1)
    assert protocol.events[-1][0] == "lost"


async def test_open_rfcomm_connects_as_the_client_expects(link: _Link) -> None:
    transport, protocol = await open_rfcomm(
        "00-11-22-33-44-55",
        _Recorder,
        channel=1,
        write_on_connect=REQUEST,
        security=Security.MEDIUM,
    )
    assert isinstance(transport, RfcommSerialTransport)
    assert isinstance(protocol, _Recorder)
    assert link.connected_to == (ADDRESS, 1)
    link.device.settimeout(1)
    assert link.device.recv(64) == REQUEST
    transport.close()
    await asyncio.wait_for(protocol.lost.wait(), 1)


async def test_a_link_dropped_as_it_opens_fails_the_connect(link: _Link) -> None:
    loop = asyncio.get_running_loop()
    original = loop.create_connection

    async def dropping(factory: Any, **kwargs: Any) -> Any:
        # The device resets the link before the protocol is attached.
        transport, proxy = await original(factory, **kwargs)
        proxy.connection_lost(ConnectionResetError(104, "reset by peer"))
        return transport, proxy

    protocol = _Recorder()
    with (
        patch.object(loop, "create_connection", dropping),
        pytest.raises(ConnectionResetError),
    ):
        await serialx.create_serial_connection(
            loop, lambda: protocol, URL, baudrate=9600
        )
    assert protocol.events == []


async def test_bytes_before_connection_made_are_held() -> None:
    loop = asyncio.get_running_loop()
    protocol = _Recorder()
    transport = RfcommSerialTransport(loop, protocol)
    transport._data_received(b"early")
    assert protocol.events == []
    transport._connection_made()
    transport._data_received(b"late")
    assert protocol.events == [("made",), ("data", b"early"), ("data", b"late")]


async def test_write_on_connect_is_only_for_rfcomm() -> None:
    # serialx drops it for other backends rather than failing.
    handler = serialx.common.get_uri_handler("socket://localhost:1")
    routed = serialx.common.route_backend_kwargs(handler, {"write_on_connect": REQUEST})
    assert routed == {}


async def test_cancelling_a_connect_closes_the_socket_after_the_thread() -> None:
    release = threading.Event()
    closed = threading.Event()

    class _Socket:
        def close(self) -> None:
            closed.set()

    def blocking_open(*args: object) -> tuple[_Socket, int]:
        release.wait(5)
        return _Socket(), 1

    loop = asyncio.get_running_loop()
    with (
        patch.object(rfcomm, "rfcomm_supported", return_value=True),
        patch.object(rfcomm, "_open_link", blocking_open),
    ):
        task = asyncio.ensure_future(
            serialx.create_serial_connection(loop, _Recorder, URL, baudrate=9600)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The thread is still opening the link: nothing to close yet.
        assert not closed.is_set()
        release.set()
        for _ in range(100):
            if closed.is_set():
                break
            await asyncio.sleep(0.01)
        assert closed.is_set()


async def _open(link: _Link, url: str) -> tuple[asyncio.Transport, _Recorder]:
    transport, protocol = await serialx.create_serial_connection(
        asyncio.get_running_loop(), _Recorder, url, baudrate=9600
    )
    assert isinstance(protocol, _Recorder)
    return transport, protocol


async def _close(transport: asyncio.Transport, protocol: _Recorder) -> None:
    transport.close()
    await asyncio.wait_for(protocol.lost.wait(), 1)


async def test_a_channel_given_is_used_without_a_lookup(link: _Link) -> None:
    transport, protocol = await _open(link, f"{SERVICE_URL}&channel=3")
    assert link.lookups == []
    assert link.connected_to == (ADDRESS, 3)
    assert transport.get_extra_info("rfcomm_channel") == 3
    assert transport.get_extra_info("rfcomm_service") == SERIAL_PORT
    await _close(transport, protocol)


async def test_a_service_alone_is_looked_up(link: _Link) -> None:
    link.offered = 10
    transport, protocol = await _open(link, f"{SERVICE_URL}&adapter=hci1")
    assert link.lookups == [(ADDRESS, SERIAL_PORT, "hci1")]
    assert link.connected_to == (ADDRESS, 10)
    assert transport.get_extra_info("rfcomm_channel") == 10
    assert transport.get_extra_info("rfcomm_adapter") == "hci1"
    await _close(transport, protocol)


async def test_a_refused_channel_is_looked_up_once(link: _Link) -> None:
    link.refused = {3}
    link.offered = 10
    transport, protocol = await _open(link, f"{SERVICE_URL}&channel=3")
    assert len(link.lookups) == 1
    assert link.connected_to == (ADDRESS, 10)
    assert transport.get_extra_info("rfcomm_channel") == 10
    await _close(transport, protocol)


async def test_a_refused_channel_without_a_service_fails(link: _Link) -> None:
    link.refused = {1}
    with pytest.raises(ConnectionRefusedError):
        await _open(link, URL)
    assert link.lookups == []


async def test_a_refused_channel_the_lookup_confirms_fails(link: _Link) -> None:
    link.refused = {3}
    link.offered = 3
    with pytest.raises(ConnectionRefusedError):
        await _open(link, f"{SERVICE_URL}&channel=3")
    assert len(link.lookups) == 1


async def test_a_service_not_offered_fails(link: _Link) -> None:
    link.offered = None
    with pytest.raises(ServiceLookupError):
        await _open(link, SERVICE_URL)
    assert link.connected_to is None


async def test_open_links_are_listed(link: _Link) -> None:
    changes: list[list[RfcommLink]] = []
    unsubscribe = on_rfcomm_links_changed(lambda: changes.append(open_rfcomm_links()))
    try:
        transport, protocol = await _open(link, URL)
        [listed] = open_rfcomm_links()
        assert (listed.address, listed.channel, listed.service) == (ADDRESS, 1, None)
        await _close(transport, protocol)
        assert open_rfcomm_links() == []
        assert changes == [[listed], []]
    finally:
        unsubscribe()


def test_blocking_serial_writes_as_it_opens(link: _Link) -> None:
    # serialx types only its own options; handlers add theirs.
    port = serialx.serial_for_url(
        URL,
        baudrate=9600,
        write_on_connect=REQUEST,  # type: ignore[call-arg]
    )
    port.open()
    assert [open.channel for open in open_rfcomm_links()] == [1]
    try:
        link.device.settimeout(1)
        assert link.device.recv(64) == REQUEST
        port.write(b"next")
        assert link.device.recv(64) == b"next"
        link.device.sendall(b"answer")
        port.timeout = 1
        assert port.read(6) == b"answer"
    finally:
        port.close()
    assert open_rfcomm_links() == []
