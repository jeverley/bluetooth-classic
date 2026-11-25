"""Bluetooth Classic discovery, pairing and RFCOMM links on Linux."""

__version__ = "0.8.0"

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
    RfcommClient,
    RfcommLink,
    RfcommTransport,
    create_rfcomm_connection,
    establish_rfcomm_connection,
    on_rfcomm_links_changed,
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
    "RfcommClient",
    "RfcommConnectError",
    "RfcommLink",
    "RfcommTransport",
    "create_rfcomm_connection",
    "establish_rfcomm_connection",
    "normalize_address",
    "on_rfcomm_links_changed",
    "open_rfcomm_links",
    "service_uuid",
]
