"""Bluetooth Classic discovery, pairing and presence on Linux, through BlueZ."""

__version__ = "0.1.1"

from .client import BluetoothClassic, normalize_address
from .const import DEFAULT_PIN, DISCOVERY_SECONDS, PAIR_TIMEOUT, PRESENCE_SECONDS
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
)
from .models import ClassicDevice, PresenceResult

__all__ = [
    "DEFAULT_PIN",
    "DISCOVERY_SECONDS",
    "PAIR_TIMEOUT",
    "PRESENCE_SECONDS",
    "BluetoothClassic",
    "BluetoothClassicError",
    "ClassicDevice",
    "DeviceNotFoundError",
    "NoAdapterError",
    "PairingError",
    "PresenceResult",
    "normalize_address",
]
