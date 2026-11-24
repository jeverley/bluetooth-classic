"""Finding the RFCOMM channels a device offers its services on, with SDP.

A device lists its services in its Service Discovery Protocol (SDP)
directory: for each, the service's UUID and, for services carried by
RFCOMM, the channel it listens on. This module asks that directory
directly, over an L2CAP connection, with Python's own Bluetooth sockets.

A lookup is a short Bluetooth connection to the device. Like any
connection, it can wake a device that wakes when connected to; callers
that keep the channel from an earlier connection avoid looking up again.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from .client import normalize_address
from .exceptions import NoAdapterError, RfcommUnsupportedError, ServiceLookupError

_LOGGER = logging.getLogger(__name__)

# The Bluetooth Base UUID, which 16- and 32-bit UUIDs are short forms of.
_BASE_UUID = UUID("00000000-0000-1000-8000-00805f9b34fb")
# The Serial Port Profile.
SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")
_RFCOMM = UUID("00000003-0000-1000-8000-00805f9b34fb")

LOOKUP_TIMEOUT = 10.0
# The L2CAP PSM every device's SDP server listens on.
_SDP_PSM = 1
_SYSFS_BLUETOOTH = Path("/sys/class/bluetooth")

# PDU IDs.
_ERROR_RESPONSE = 0x01
_SERVICE_SEARCH_ATTRIBUTE_REQUEST = 0x06
_SERVICE_SEARCH_ATTRIBUTE_RESPONSE = 0x07
# Attribute IDs.
_SERVICE_CLASS_ID_LIST = 0x0001
_PROTOCOL_DESCRIPTOR_LIST = 0x0004
# The service name in the primary language (language base 0x0100).
_SERVICE_NAME = 0x0100
_ATTRIBUTES = (_SERVICE_CLASS_ID_LIST, _PROTOCOL_DESCRIPTOR_LIST, _SERVICE_NAME)
# The most attribute bytes to ask for in one reply; longer replies continue.
_MAX_ATTRIBUTE_BYTES = 0x0400
# Data element types.
_UINT, _SINT, _UUID, _TEXT, _BOOL, _SEQUENCE, _ALTERNATIVE, _URL = range(1, 9)
_FIXED_SIZES = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16}
# Service records nest a few levels; deeper replies are malformed.
_MAX_DEPTH = 16
# Continued replies to accept for one lookup, and the longest continuation
# state the specification allows.
_MAX_ROUNDS = 64
_MAX_CONTINUATION = 16


@dataclass(frozen=True, slots=True)
class RfcommService:
    """A service a device offers over RFCOMM."""

    #: The service's class UUID, the most specific its record lists.
    uuid: UUID
    #: The RFCOMM channel it listens on.
    channel: int
    #: The name the device gives it, if any.
    name: str | None = None
    #: Every class its record lists, the most specific first.
    classes: tuple[UUID, ...] = field(default=(), compare=False)


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


def _short(uuid: UUID) -> int | None:
    """Return the 16-bit short form of a Bluetooth UUID, if it has one."""
    value = uuid.int >> 96
    if uuid.int & ((1 << 96) - 1) == _BASE_UUID.int and value <= 0xFFFF:
        return value
    return None


def adapter_address(adapter: str) -> str:
    """Return an adapter's address, such as ``hci0``'s.

    Raises:
        NoAdapterError: No such adapter.
    """
    try:
        return (_SYSFS_BLUETOOTH / adapter / "address").read_text().strip().upper()
    except OSError as err:
        raise NoAdapterError(f"Bluetooth adapter {adapter} not found") from err


def adapter_name(address: str) -> str | None:
    """Return the name of the adapter with this address, such as ``"hci0"``."""
    address = address.upper()
    try:
        for path in sorted(_SYSFS_BLUETOOTH.glob("hci*")):
            if (path / "address").read_text().strip().upper() == address:
                return path.name
    except OSError:
        return None
    return None


# Requests.


def _element(type_: int, data: bytes) -> bytes:
    """Encode a data element whose size needs a length byte or two."""
    if len(data) < 0x100:
        return bytes([type_ << 3 | 5, len(data)]) + data
    return bytes([type_ << 3 | 6]) + struct.pack(">H", len(data)) + data


def _uuid_element(uuid: UUID) -> bytes:
    if (short := _short(uuid)) is not None:
        return bytes([_UUID << 3 | 1]) + struct.pack(">H", short)
    return bytes([_UUID << 3 | 4]) + uuid.bytes


def _request(transaction: int, uuid: UUID, continuation: bytes) -> bytes:
    """Return a Service Search Attribute request for one UUID."""
    pattern = _element(_SEQUENCE, _uuid_element(uuid))
    attributes = _element(
        _SEQUENCE,
        b"".join(bytes([_UINT << 3 | 1]) + struct.pack(">H", a) for a in _ATTRIBUTES),
    )
    parameters = (
        pattern
        + struct.pack(">H", _MAX_ATTRIBUTE_BYTES)
        + attributes
        + bytes([len(continuation)])
        + continuation
    )
    return (
        struct.pack(
            ">BHH", _SERVICE_SEARCH_ATTRIBUTE_REQUEST, transaction, len(parameters)
        )
        + parameters
    )


# Replies.


def _parse_element(data: bytes, offset: int, depth: int = 0) -> tuple[Any, int]:
    """Return a data element's value and the offset after it."""
    if depth > _MAX_DEPTH:
        raise ValueError("data elements nested too deeply")
    try:
        header = data[offset]
        type_, size_index = header >> 3, header & 7
        offset += 1
        if type_ == 0:
            return None, offset
        if size_index in _FIXED_SIZES:
            size = _FIXED_SIZES[size_index]
        else:
            width = {5: 1, 6: 2, 7: 4}[size_index]
            size = int.from_bytes(data[offset : offset + width], "big")
            offset += width
        value = data[offset : offset + size]
        if len(value) < size:
            raise ValueError("data element runs past the end")
    except (IndexError, KeyError) as err:
        raise ValueError("malformed data element") from err
    end = offset + size
    if type_ == _UINT:
        return int.from_bytes(value, "big"), end
    if type_ == _SINT:
        return int.from_bytes(value, "big", signed=True), end
    if type_ == _UUID:
        if size == 16:
            return UUID(bytes=bytes(value)), end
        return _from_short(int.from_bytes(value, "big")), end
    if type_ in (_TEXT, _URL):
        return bytes(value).decode("utf-8", "replace").rstrip("\x00"), end
    if type_ == _BOOL:
        return bool(value[0]), end
    if type_ in (_SEQUENCE, _ALTERNATIVE):
        items = []
        position = offset
        while position < end:
            item, position = _parse_element(data, position, depth + 1)
            if position > end:
                raise ValueError("data element runs past its sequence")
            items.append(item)
        return items, end
    raise ValueError(f"unknown data element type {type_}")


def _rfcomm_channel(descriptors: Any) -> int | None:
    """Return the RFCOMM channel in a protocol descriptor list, if any."""
    if not isinstance(descriptors, list):
        return None
    # An alternative of several lists: take the first that names a channel.
    if descriptors and all(
        isinstance(d, list) and d and isinstance(d[0], list) for d in descriptors
    ):
        for alternative in descriptors:
            if (channel := _rfcomm_channel(alternative)) is not None:
                return channel
        return None
    for descriptor in descriptors:
        if (
            isinstance(descriptor, list)
            and len(descriptor) >= 2
            and descriptor[0] == _RFCOMM
            and isinstance(descriptor[1], int)
        ):
            return descriptor[1]
    return None


def _services(attribute_lists: bytes) -> list[RfcommService]:
    """Return the RFCOMM services in a reply's attribute lists."""
    records, end = _parse_element(attribute_lists, 0)
    if end != len(attribute_lists) or not isinstance(records, list):
        raise ValueError("malformed attribute lists")
    services = []
    for record in records:
        if not isinstance(record, list) or len(record) % 2:
            raise ValueError("malformed service record")
        if not all(isinstance(key, int) for key in record[::2]):
            raise ValueError("malformed attribute ID")
        attributes = dict(zip(record[::2], record[1::2], strict=True))
        channel = _rfcomm_channel(attributes.get(_PROTOCOL_DESCRIPTOR_LIST))
        classes = attributes.get(_SERVICE_CLASS_ID_LIST)
        if channel is None or not isinstance(classes, list) or not classes:
            continue
        uuid = classes[0]
        name = attributes.get(_SERVICE_NAME)
        if isinstance(uuid, UUID):
            services.append(
                RfcommService(
                    uuid,
                    channel,
                    name if isinstance(name, str) else None,
                    tuple(c for c in classes if isinstance(c, UUID)),
                )
            )
    return services


# Lookups.


def _l2cap_socket() -> socket.socket:
    if not (hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_L2CAP")):
        raise RfcommUnsupportedError(
            "This Python was built without Bluetooth socket support"
        )
    # Only defined where Python was built with Bluetooth support, so not in
    # every platform's type stubs.
    return socket.socket(
        socket.AF_BLUETOOTH,  # type: ignore[attr-defined, unused-ignore]
        socket.SOCK_SEQPACKET,
        socket.BTPROTO_L2CAP,  # type: ignore[attr-defined, unused-ignore]
    )


def _query(
    address: str, uuid: UUID, *, adapter: str | None, timeout: float
) -> list[RfcommService]:
    """Ask a device's SDP server for the RFCOMM records listing a UUID."""
    sock = _l2cap_socket()
    try:
        sock.settimeout(timeout)
        if adapter is not None:
            sock.bind((adapter_address(adapter), 0))
        sock.connect((address, _SDP_PSM))
        attribute_lists = b""
        continuation = b""
        transaction = 0
        for _ in range(_MAX_ROUNDS):
            transaction = (transaction + 1) & 0xFFFF
            sock.send(_request(transaction, uuid, continuation))
            reply = sock.recv(0x10000)
            if len(reply) < 5:
                raise ValueError("short reply")
            pdu, answered, length = struct.unpack(">BHH", reply[:5])
            parameters = reply[5 : 5 + length]
            if answered != transaction or len(parameters) < length:
                raise ValueError("reply does not match the request")
            if pdu == _ERROR_RESPONSE:
                code = struct.unpack(">H", parameters[:2])[0] if length >= 2 else 0
                raise ServiceLookupError(f"{address} refused the lookup ({code:#x})")
            if pdu != _SERVICE_SEARCH_ATTRIBUTE_RESPONSE or length < 3:
                raise ValueError(f"unexpected reply {pdu:#x}")
            count = struct.unpack(">H", parameters[:2])[0]
            if 2 + count + 1 > length:
                raise ValueError("attribute byte count runs past the reply")
            attribute_lists += parameters[2 : 2 + count]
            state = parameters[2 + count :]
            if state[0] > _MAX_CONTINUATION or len(state) != 1 + state[0]:
                raise ValueError("malformed continuation state")
            if state[0] == 0:
                break
            continuation = state[1:]
        else:
            raise ValueError("reply continued too many times")
        return _services(attribute_lists) if attribute_lists else []
    except (ServiceLookupError, NoAdapterError):
        raise
    except (OSError, ValueError) as err:
        raise ServiceLookupError(
            f"Could not look up services on {address}: {err}"
        ) from err
    finally:
        sock.close()


def find_rfcomm_services_blocking(
    address: str,
    service: UUID | str | int = _RFCOMM,
    *,
    adapter: str | None = None,
    timeout: float = LOOKUP_TIMEOUT,
) -> list[RfcommService]:
    """Return the services a device offers over RFCOMM; blocks.

    By default, every RFCOMM service; with ``service``, only those whose
    record lists that UUID.

    Raises:
        ServiceLookupError: The device could not be reached, refused, or
            answered wrongly.
        RfcommUnsupportedError: This Python has no Bluetooth sockets.
    """
    address = normalize_address(address)
    services = _query(address, service_uuid(service), adapter=adapter, timeout=timeout)
    _LOGGER.debug("%s offers over RFCOMM: %s", address, services)
    return services


def find_rfcomm_channel_blocking(
    address: str,
    service: UUID | str | int = SERIAL_PORT,
    *,
    adapter: str | None = None,
    timeout: float = LOOKUP_TIMEOUT,
) -> int | None:
    """Return the RFCOMM channel a device offers a service on; blocks.

    Returns ``None`` if the device does not offer the service.

    Raises:
        ServiceLookupError: The device could not be reached, refused, or
            answered wrongly.
        RfcommUnsupportedError: This Python has no Bluetooth sockets.
    """
    uuid = service_uuid(service)
    found = find_rfcomm_services_blocking(
        address, uuid, adapter=adapter, timeout=timeout
    )
    # Prefer a record whose own class is the service; a record may also list
    # it as one of several classes. The search also matches records that only
    # mention the UUID elsewhere (a profile list): those are other services.
    for record in found:
        if record.uuid == uuid:
            return record.channel
    for record in found:
        if uuid in record.classes:
            return record.channel
    return None


async def find_rfcomm_services(
    address: str,
    service: UUID | str | int = _RFCOMM,
    *,
    adapter: str | None = None,
    timeout: float = LOOKUP_TIMEOUT,
) -> list[RfcommService]:
    """Return the services a device offers over RFCOMM.

    See :func:`find_rfcomm_services_blocking`; this runs it in an executor.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: find_rfcomm_services_blocking(
            address, service, adapter=adapter, timeout=timeout
        ),
    )


async def find_rfcomm_channel(
    address: str,
    service: UUID | str | int = SERIAL_PORT,
    *,
    adapter: str | None = None,
    timeout: float = LOOKUP_TIMEOUT,
) -> int | None:
    """Return the RFCOMM channel a device offers a service on.

    Returns ``None`` if the device does not offer the service. See
    :func:`find_rfcomm_channel_blocking`; this runs it in an executor.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: find_rfcomm_channel_blocking(
            address, service, adapter=adapter, timeout=timeout
        ),
    )
