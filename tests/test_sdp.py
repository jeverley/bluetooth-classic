"""Tests for looking up RFCOMM services with SDP."""

from __future__ import annotations

import struct
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from bluetooth_classic import (
    SERIAL_PORT,
    RfcommService,
    ServiceLookupError,
    find_rfcomm_channel,
    find_rfcomm_services,
    sdp,
    service_uuid,
)

ADDRESS = "00:11:22:33:44:55"
VENDOR = UUID("12345678-1234-5678-1234-56789abcdef0")


# Data elements, encoded here independently of the module.


def _seq(*items: bytes) -> bytes:
    body = b"".join(items)
    return bytes([0x35, len(body)]) + body


def _u8(value: int) -> bytes:
    return bytes([0x08, value])


def _u16(value: int) -> bytes:
    return b"\x09" + struct.pack(">H", value)


def _uuid16(value: int) -> bytes:
    return b"\x19" + struct.pack(">H", value)


def _uuid128(value: UUID) -> bytes:
    return b"\x1c" + value.bytes


def _text(value: str) -> bytes:
    data = value.encode()
    return bytes([0x25, len(data)]) + data


def _record(classes: bytes, channel: int | None, name: str | None = None) -> bytes:
    protocols = [_seq(_uuid16(0x0100))]
    if channel is not None:
        protocols.append(_seq(_uuid16(0x0003), _u8(channel)))
    attributes = [
        _u16(0x0001),
        classes,
        _u16(0x0004),
        _seq(*protocols),
    ]
    if name is not None:
        attributes += [_u16(0x0100), _text(name)]
    return _seq(*attributes)


def _reply(transaction: int, lists: bytes, continuation: bytes = b"") -> bytes:
    parameters = (
        struct.pack(">H", len(lists))
        + lists
        + bytes([len(continuation)])
        + continuation
    )
    return struct.pack(">BHH", 0x07, transaction, len(parameters)) + parameters


class _Socket:
    """Stands in for the L2CAP socket to the device's SDP server."""

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = replies
        self.sent: list[bytes] = []
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.calls.append(("settimeout", timeout))

    def bind(self, address: tuple[str, int]) -> None:
        self.calls.append(("bind", address))

    def connect(self, address: tuple[str, int]) -> None:
        self.calls.append(("connect", address))

    def send(self, data: bytes) -> int:
        self.sent.append(data)
        return len(data)

    def recv(self, size: int) -> bytes:
        if not self.replies:
            raise TimeoutError("timed out")
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


def _patched(sock: _Socket) -> Any:
    return patch.object(sdp, "_l2cap_socket", return_value=sock)


RECORDS = _seq(
    _record(_seq(_uuid16(0x1101)), 10, "Serial Port"),
    _record(_seq(_uuid128(VENDOR), _uuid16(0x1101)), 4),
    # Not carried by RFCOMM.
    _record(_seq(_uuid16(0x110A)), None),
)


async def test_finds_the_channel() -> None:
    sock = _Socket([_reply(1, RECORDS)])
    with _patched(sock):
        assert await find_rfcomm_channel("00-11-22-33-44-55") == 10
    assert ("connect", (ADDRESS, 1)) in sock.calls
    assert sock.closed
    # A Service Search Attribute request for the Serial Port UUID.
    request = sock.sent[0]
    assert request[0] == 0x06
    assert _uuid16(0x1101) in request
    # Attributes 0x0001, 0x0004 and 0x0100.
    assert _seq(_u16(0x0001), _u16(0x0004), _u16(0x0100)) in request


async def test_lists_every_service() -> None:
    sock = _Socket([_reply(1, RECORDS)])
    with _patched(sock):
        services = await find_rfcomm_services(ADDRESS)
    assert services == [
        RfcommService(SERIAL_PORT, 10, "Serial Port"),
        RfcommService(VENDOR, 4),
    ]
    # Every RFCOMM record: the search is for the RFCOMM protocol UUID.
    assert _uuid16(0x0003) in sock.sent[0]


async def test_binds_to_the_adapter_asked_for() -> None:
    sock = _Socket([_reply(1, RECORDS)])
    with (
        _patched(sock),
        patch.object(sdp, "adapter_address", return_value="AA:BB:CC:DD:EE:FF"),
    ):
        await find_rfcomm_channel(ADDRESS, adapter="hci1")
    assert sock.calls[1] == ("bind", ("AA:BB:CC:DD:EE:FF", 0))


async def test_prefers_the_record_of_the_service_itself() -> None:
    records = _seq(
        _record(_seq(_uuid128(VENDOR), _uuid16(0x1101)), 4),
        _record(_seq(_uuid16(0x1101)), 10),
    )
    with _patched(_Socket([_reply(1, records)])):
        assert await find_rfcomm_channel(ADDRESS) == 10
    records = _seq(_record(_seq(_uuid128(VENDOR), _uuid16(0x1101)), 4))
    with _patched(_Socket([_reply(1, records)])):
        assert await find_rfcomm_channel(ADDRESS) == 4


async def test_vendor_services_use_long_uuids() -> None:
    records = _seq(_record(_seq(_uuid128(VENDOR)), 7))
    sock = _Socket([_reply(1, records)])
    with _patched(sock):
        assert await find_rfcomm_channel(ADDRESS, str(VENDOR)) == 7
    assert _uuid128(VENDOR) in sock.sent[0]


async def test_follows_continuations() -> None:
    sock = _Socket([_reply(1, RECORDS[:20], b"\x01\x02"), _reply(2, RECORDS[20:])])
    with _patched(sock):
        assert await find_rfcomm_channel(ADDRESS) == 10
    assert len(sock.sent) == 2
    # The second request carries the continuation state.
    assert sock.sent[1].endswith(b"\x02\x01\x02")


async def test_no_such_service() -> None:
    with _patched(_Socket([_reply(1, _seq())])):
        assert await find_rfcomm_channel(ADDRESS, 0x1234) is None
    with _patched(_Socket([_reply(1, b"")])):
        assert await find_rfcomm_channel(ADDRESS) is None


@pytest.mark.parametrize(
    "replies",
    [
        [],
        [b"\x07\x00"],
        [_reply(2, RECORDS)],
        [struct.pack(">BHHH", 0x01, 1, 2, 0x0002)],
        [_reply(1, b"\x35\x10\x09")],
    ],
    ids=["timeout", "short", "wrong-transaction", "error", "malformed"],
)
async def test_bad_answers_fail_the_lookup(replies: list[bytes]) -> None:
    sock = _Socket(replies)
    with _patched(sock), pytest.raises(ServiceLookupError):
        await find_rfcomm_channel(ADDRESS)
    assert sock.closed


async def test_an_unreachable_device_fails_the_lookup() -> None:
    sock = _Socket([])
    with (
        patch.object(sock, "connect", side_effect=OSError(112, "Host is down")),
        _patched(sock),
        pytest.raises(ServiceLookupError) as raised,
    ):
        await find_rfcomm_channel(ADDRESS)
    # An OSError too, as the link it was for could not be opened.
    assert isinstance(raised.value, OSError)
    assert sock.closed


async def test_bad_address() -> None:
    with pytest.raises(ValueError):
        await find_rfcomm_channel("not an address")


def test_service_uuids() -> None:
    for form in ("1101", "0x1101", 0x1101, str(SERIAL_PORT), SERIAL_PORT):
        assert service_uuid(form) == SERIAL_PORT
    assert service_uuid("00001101") == SERIAL_PORT
    with pytest.raises(ValueError):
        service_uuid("serial")


def test_adapter_names(tmp_path: Any) -> None:
    (tmp_path / "hci0").mkdir()
    (tmp_path / "hci0" / "address").write_text("aa:bb:cc:dd:ee:ff\n")
    with patch.object(sdp, "_SYSFS_BLUETOOTH", tmp_path):
        assert sdp.adapter_address("hci0") == "AA:BB:CC:DD:EE:FF"
        assert sdp.adapter_name("AA:BB:CC:DD:EE:FF") == "hci0"
        assert sdp.adapter_name("00:11:22:33:44:55") is None
        with pytest.raises(ServiceLookupError):
            sdp.adapter_address("hci9")
