"""Bluetooth Classic discovery, pairing and RFCOMM links on Linux."""

__version__ = "0.9.1"

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
    RfcommConnection,
    establish_rfcomm_connection,
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
    "RfcommConnection",
    "establish_rfcomm_connection",
    "normalize_address",
    "service_uuid",
]
