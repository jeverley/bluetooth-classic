"""Tests for the rfcomm:// serialx handler, with a fake BlueZ profile API."""

from __future__ import annotations

import asyncio
import io
import socket
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import serialx

from bluetooth_classic import (
    SERIAL_PORT,
    ProfileInUseError,
    RfcommConnectError,
    service_uuid,
)
from bluetooth_classic.rfcomm import (
    RfcommLink,
    RfcommSerialTransport,
    RfcommTarget,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
    parse_rfcomm_url,
    rfcomm_url,
)

from .conftest import SPEAKER, SPEAKER_ADDRESS, SPP, FakeBus

URL = f"rfcomm://{SPEAKER_ADDRESS}?service=1101"
REQUEST = b"hello"

# serialx uses whichever copy of the module registered first; patch that one.
rfcomm = sys.modules[serialx.common.get_uri_handler(URL).async_transport_cls.__module__]
profile_module = sys.modules[rfcomm.__name__.rsplit(".", 1)[0] + ".profile"]


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


@pytest.fixture
def bluez(bus: FakeBus) -> Iterator[FakeBus]:
    """BlueZ on the fake bus, as the handler's bus."""

    async def connect_bus() -> FakeBus:
        return bus

    profiles = profile_module._Profiles()
    with (
        patch.object(profiles, "_connect_bus", connect_bus),
        patch.object(profile_module, "_profiles", profiles),
    ):
        yield bus
    bus.close()


async def _open(url: str = URL, **kwargs: Any) -> tuple[Any, _Recorder]:
    transport, protocol = await serialx.create_serial_connection(
        asyncio.get_running_loop(), _Recorder, url, baudrate=9600, **kwargs
    )
    assert isinstance(protocol, _Recorder)
    return transport, protocol


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_service_uuids() -> None:
    for form in ("1101", "0x1101", 0x1101, str(SERIAL_PORT), SERIAL_PORT):
        assert service_uuid(form) == SERIAL_PORT
    assert service_uuid("00001101") == SERIAL_PORT
    with pytest.raises(ValueError):
        service_uuid("serial")


def test_urls() -> None:
    url = rfcomm_url("00-11-22-33-44-55", service="1101", adapter="hci1")
    assert url == (
        f"rfcomm://{SPEAKER_ADDRESS}?service=00001101-0000-1000-8000-00805f9b34fb"
        "&adapter=hci1"
    )
    assert parse_rfcomm_url(url) == RfcommTarget(SPEAKER_ADDRESS, SERIAL_PORT, "hci1")
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
        "rfcomm://00:11:22:33:44:55?service=1101&adapter=eth0",
        "rfcomm://not-an-address?service=1101",
    ],
)
def test_bad_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_rfcomm_url(url)


async def test_bluez_connects_and_the_request_goes_first(bluez: FakeBus) -> None:
    bluez.reply_on_connect = b"reply"
    transport, protocol = await _open(write_on_connect=REQUEST)
    assert isinstance(transport, RfcommSerialTransport)
    # Registered as a client, then connected by service.
    assert bluez.members()[-1] == "ConnectProfile"
    assert ("ConnectProfile", SPEAKER, [SPP]) in bluez.calls
    [(_, uuid)] = bluez.profiles.items()
    assert uuid == SPP
    device = bluez.device_ends[0]
    device.settimeout(1)
    # Written inside BlueZ's handover, before anything else.
    assert device.recv(64) == REQUEST
    transport.write(b"next")
    assert device.recv(64) == b"next"
    await _settle()
    assert protocol.events[:2] == [("made",), ("data", b"reply")]
    assert transport.get_extra_info("rfcomm_service") == SERIAL_PORT
    assert transport.get_extra_info("rfcomm_adapter") == "hci0"
    transport.close()
    await asyncio.wait_for(protocol.lost.wait(), 1)
    await _settle()
    # BlueZ drops its side, and the registration goes once unused.
    assert bluez.members()[-2:] == ["DisconnectProfile", "UnregisterProfile"]
    assert bluez.profiles == {}


async def test_the_device_closing_the_link_is_reported(bluez: FakeBus) -> None:
    _, protocol = await _open()
    bluez.device_ends[0].close()
    await asyncio.wait_for(protocol.lost.wait(), 1)
    await _settle()
    assert bluez.profiles == {}


async def test_open_rfcomm_connects_by_service(bluez: FakeBus) -> None:
    transport, protocol = await open_rfcomm(
        "00-11-22-33-44-55", _Recorder, service=SERIAL_PORT, write_on_connect=REQUEST
    )
    bluez.device_ends[0].settimeout(1)
    assert bluez.device_ends[0].recv(64) == REQUEST
    transport.close()
    assert isinstance(protocol, _Recorder)
    await asyncio.wait_for(protocol.lost.wait(), 1)


async def test_links_share_one_registration(bluez: FakeBus) -> None:
    first, first_protocol = await _open()
    second, second_protocol = await _open()
    assert bluez.members().count("RegisterProfile") == 1
    first.close()
    await asyncio.wait_for(first_protocol.lost.wait(), 1)
    await _settle()
    # Still in use by the second link.
    assert len(bluez.profiles) == 1
    second.close()
    await asyncio.wait_for(second_protocol.lost.wait(), 1)
    await _settle()
    assert bluez.profiles == {}


async def test_open_links_are_listed(bluez: FakeBus) -> None:
    changes: list[list[RfcommLink]] = []
    unsubscribe = on_rfcomm_links_changed(lambda: changes.append(open_rfcomm_links()))
    try:
        transport, protocol = await _open()
        [listed] = open_rfcomm_links()
        assert (listed.address, listed.service, listed.adapter) == (
            SPEAKER_ADDRESS,
            SERIAL_PORT,
            "hci0",
        )
        transport.close()
        await asyncio.wait_for(protocol.lost.wait(), 1)
        assert open_rfcomm_links() == []
        assert changes == [[listed], []]
    finally:
        unsubscribe()


async def test_bluez_failing_to_connect(bluez: FakeBus) -> None:
    bluez.handover = False
    bluez.errors["ConnectProfile"] = "org.bluez.Error.Failed"
    with pytest.raises(RfcommConnectError) as raised:
        await _open()
    # An OSError too, as the link could not be opened.
    assert isinstance(raised.value, OSError)
    await _settle()
    assert bluez.profiles == {}


async def test_another_program_holding_the_service(bluez: FakeBus) -> None:
    bluez.profiles["/elsewhere"] = SPP
    with pytest.raises(ProfileInUseError):
        await _open()
    assert "/org/bluetooth_classic/profile" not in "".join(bluez.exported)


async def test_an_unknown_device(bluez: FakeBus) -> None:
    with pytest.raises(RfcommConnectError, match="pair it first"):
        await _open("rfcomm://66:77:88:99:AA:BB?service=1101")
    with pytest.raises(RfcommConnectError):
        await _open(f"{URL}&adapter=hci1")
    await _settle()
    assert bluez.profiles == {}


async def test_no_connection_in_time(bluez: FakeBus) -> None:
    bluez.unanswered.add("ConnectProfile")
    with pytest.raises(TimeoutError):
        await _open(connect_timeout=0.05)
    await _settle()
    assert bluez.profiles == {}


async def test_an_abandoned_connection_is_closed() -> None:
    profile = profile_module._Profile()
    ours, device = socket.socketpair()
    profile.NewConnection.__wrapped__(profile, SPEAKER, ours.detach(), {})
    device.settimeout(1)
    # The handler's end was closed: the device reads end of file.
    assert device.recv(1) == b""
    device.close()


async def test_bytes_before_connection_made_are_held() -> None:
    loop = asyncio.get_running_loop()
    protocol = _Recorder()
    transport = RfcommSerialTransport(loop, protocol)
    transport._data_received(b"early")
    assert protocol.events == []
    transport._connection_made()
    transport._data_received(b"late")
    assert protocol.events == [("made",), ("data", b"early"), ("data", b"late")]


async def test_a_link_dropped_as_it_opens_fails_the_connect(bluez: FakeBus) -> None:
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
    await _settle()
    assert bluez.profiles == {}


def test_blocking_use_is_refused() -> None:
    port = serialx.serial_for_url(URL, baudrate=9600)
    with pytest.raises(io.UnsupportedOperation):
        port.open()


async def test_write_on_connect_is_only_for_rfcomm() -> None:
    # serialx drops it for other backends rather than failing.
    handler = serialx.common.get_uri_handler("socket://localhost:1")
    routed = serialx.common.route_backend_kwargs(handler, {"write_on_connect": REQUEST})
    assert routed == {}


def test_the_service_is_a_uuid() -> None:
    assert isinstance(parse_rfcomm_url(URL).service, UUID)
