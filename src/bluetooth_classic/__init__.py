"""Bluetooth Classic discovery, pairing and RFCOMM serial on Linux."""

__version__ = "0.7.0"

from .client import BluetoothClassic, normalize_address
from .const import DEFAULT_PIN, DISCOVERY_TIMEOUT, FIND_TIMEOUT, PAIR_TIMEOUT
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
    RfcommConnectError,
)
from .models import ClassicDevice
from .rfcomm import (
    SERIAL_PORT,
    RfcommLink,
    RfcommTarget,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
    parse_rfcomm_url,
    rfcomm_url,
    service_uuid,
)

__all__ = [
    "DEFAULT_PIN",
    "DISCOVERY_TIMEOUT",
    "FIND_TIMEOUT",
    "PAIR_TIMEOUT",
    "SERIAL_PORT",
    "BluetoothClassic",
    "BluetoothClassicError",
    "ClassicDevice",
    "DeviceNotFoundError",
    "NoAdapterError",
    "PairingError",
    "RfcommConnectError",
    "RfcommLink",
    "RfcommTarget",
    "normalize_address",
    "on_rfcomm_links_changed",
    "open_rfcomm",
    "open_rfcomm_links",
    "parse_rfcomm_url",
    "rfcomm_url",
    "service_uuid",
]
