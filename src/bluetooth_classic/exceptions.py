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


class RfcommUnsupportedError(BluetoothClassicError):
    """This Python cannot open RFCOMM sockets (built without Bluetooth)."""


class ServiceLookupError(BluetoothClassicError, OSError):
    """A device's services could not be looked up (SDP), or it lacks the one asked for.

    An ``OSError`` too, as the link it was needed for could not be opened.
    """
