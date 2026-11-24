"""Bluetooth Classic discovery, pairing and RFCOMM serial on Linux."""

__version__ = "0.5.0"

from .client import BluetoothClassic, normalize_address
from .const import DEFAULT_PIN, DISCOVERY_TIMEOUT, FIND_TIMEOUT, PAIR_TIMEOUT
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
    RfcommUnsupportedError,
    ServiceLookupError,
)
from .models import ClassicDevice
from .rfcomm import (
    RfcommLink,
    RfcommTarget,
    Security,
    on_rfcomm_links_changed,
    open_rfcomm,
    open_rfcomm_links,
    parse_rfcomm_url,
    rfcomm_supported,
    rfcomm_url,
)
from .sdp import (
    SERIAL_PORT,
    RfcommService,
    find_rfcomm_channel,
    find_rfcomm_channel_blocking,
    find_rfcomm_services,
    find_rfcomm_services_blocking,
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
    "RfcommLink",
    "RfcommService",
    "RfcommTarget",
    "RfcommUnsupportedError",
    "Security",
    "ServiceLookupError",
    "find_rfcomm_channel",
    "find_rfcomm_channel_blocking",
    "find_rfcomm_services",
    "find_rfcomm_services_blocking",
    "normalize_address",
    "on_rfcomm_links_changed",
    "open_rfcomm",
    "open_rfcomm_links",
    "parse_rfcomm_url",
    "rfcomm_supported",
    "rfcomm_url",
    "service_uuid",
]
