"""Bluetooth Classic discovery, pairing and RFCOMM serial on Linux."""

__version__ = "0.3.0"

from .client import BluetoothClassic, normalize_address
from .const import DEFAULT_PIN, DISCOVERY_TIMEOUT, FIND_TIMEOUT, PAIR_TIMEOUT
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
    RfcommUnsupportedError,
)
from .models import ClassicDevice
from .rfcomm import (
    Security,
    open_rfcomm,
    parse_rfcomm_url,
    rfcomm_supported,
    rfcomm_url,
)
from .sdp import SERIAL_PORT, find_rfcomm_channel

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
    "RfcommUnsupportedError",
    "Security",
    "find_rfcomm_channel",
    "normalize_address",
    "open_rfcomm",
    "parse_rfcomm_url",
    "rfcomm_supported",
    "rfcomm_url",
]
