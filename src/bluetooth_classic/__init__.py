"""Bluetooth Classic discovery, pairing and RFCOMM links on Linux."""

__version__ = "0.7.2"

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
    RfcommTransport,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
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
    "RfcommTransport",
    "normalize_address",
    "on_rfcomm_links_changed",
    "open_rfcomm",
    "open_rfcomm_links",
    "service_uuid",
]
