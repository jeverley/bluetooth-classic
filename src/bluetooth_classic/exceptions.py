"""Errors raised by bluetooth-classic."""

from __future__ import annotations


class BluetoothClassicError(Exception):
    """BlueZ could not be reached, or refused a request."""


class NoAdapterError(BluetoothClassicError):
    """No Bluetooth adapter is available: none, not the one asked for, or off."""


class DeviceNotFoundError(BluetoothClassicError):
    """BlueZ does not know the device."""


class PairingError(BluetoothClassicError):
    """The device could not be paired."""


class RfcommConnectError(BluetoothClassicError, OSError):
    """BlueZ could not open the RFCOMM service on the device.

    An ``OSError`` too, as the link could not be opened.
    """


class ProfileInUseError(RfcommConnectError):
    """Another program has registered the same service with BlueZ."""
