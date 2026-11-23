"""Errors raised by bluetooth-classic."""

from __future__ import annotations


class BluetoothClassicError(Exception):
    """BlueZ could not be reached, or refused a request."""


class NoAdapterError(BluetoothClassicError):
    """No Bluetooth adapter is available, or the one asked for is missing."""


class DeviceNotFoundError(BluetoothClassicError):
    """BlueZ does not know the device."""


class PairingError(BluetoothClassicError):
    """The device could not be paired."""
