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

from bluetooth_classic.rfcomm import (
    RfcommSerialTransport,
    Security,
    _connect_and_write,
    _rfcomm_socket,
    open_rfcomm,
    parse_rfcomm_url,
    rfcomm_url,
)

ADDRESS = "00:11:22:33:44:55"
URL = f"rfcomm://{ADDRESS}?channel=1"
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

    def connect_and_write(
        self,
        sock: socket.socket,
        address: str,
        channel: int,
        timeout: float | None,
        write_on_connect: bytes,
    ) -> None:
        # The pair is connected already; record where it would have gone.
        assert sock is self.ours
        self.connected_to = (address, channel)
        if self.reply_on_connect:
            self.device.sendall(self.reply_on_connect)
        if write_on_connect:
            sock.sendall(write_on_connect)

    def close(self) -> None:
        self.ours.close()
        self.device.close()


@pytest.fixture
def link() -> Iterator[_Link]:
    link = _Link()
    with (
        patch.object(rfcomm, "_rfcomm_socket", return_value=link.ours),
        patch.object(rfcomm, "_connect_and_write", link.connect_and_write),
    ):
        yield link
    link.close()


def test_urls() -> None:
    assert rfcomm_url("00-11-22-33-44-55", 1) == URL
    assert parse_rfcomm_url(URL) == (ADDRESS, 1, None)
    secure = f"rfcomm://{ADDRESS}?channel=30&security=medium"
    assert rfcomm_url(ADDRESS, 30, Security.MEDIUM) == secure
    assert parse_rfcomm_url(secure) == (ADDRESS, 30, Security.MEDIUM)
    assert parse_rfcomm_url(f"{URL}&security=HIGH")[2] == Security.HIGH


@pytest.mark.parametrize(
    "url",
    [
        "socket://00:11:22:33:44:55?channel=1",
        "rfcomm://00:11:22:33:44:55",
        "rfcomm://00:11:22:33:44:55?channel=1&security=none",
        "rfcomm://00:11:22:33:44:55?channel=1&security=low&security=high",
        "rfcomm://00:11:22:33:44:55?channel=x",
        "rfcomm://00:11:22:33:44:55?channel=0",
        "rfcomm://00:11:22:33:44:55?channel=31",
        "rfcomm://00:11:22:33:44:55?channel=1&channel=2",
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


def test_connect_and_write_writes_as_it_opens() -> None:
    calls: list[tuple[Any, ...]] = []

    class _Socket:
        def settimeout(self, timeout: float) -> None:
            calls.append(("settimeout", timeout))

        def connect(self, address: tuple[str, int]) -> None:
            calls.append(("connect", address))

        def sendall(self, data: bytes) -> None:
            calls.append(("sendall", data))

    _connect_and_write(_Socket(), ADDRESS, 1, 5.0, REQUEST)  # type: ignore[arg-type]
    assert calls == [
        ("settimeout", 5.0),
        ("connect", (ADDRESS, 1)),
        ("sendall", REQUEST),
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
        1,
        _Recorder,
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

    def blocking_connect(*args: object) -> None:
        release.wait(5)

    loop = asyncio.get_running_loop()
    with (
        patch.object(rfcomm, "_rfcomm_socket", return_value=_Socket()),
        patch.object(rfcomm, "_connect_and_write", blocking_connect),
    ):
        task = asyncio.ensure_future(
            serialx.create_serial_connection(loop, _Recorder, URL, baudrate=9600)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The thread still has the socket: not closed yet.
        assert not closed.is_set()
        release.set()
        for _ in range(100):
            if closed.is_set():
                break
            await asyncio.sleep(0.01)
        assert closed.is_set()


def test_blocking_serial_writes_as_it_opens(link: _Link) -> None:
    # serialx types only its own options; handlers add theirs.
    port = serialx.serial_for_url(
        URL,
        baudrate=9600,
        write_on_connect=REQUEST,  # type: ignore[call-arg]
    )
    port.open()
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
