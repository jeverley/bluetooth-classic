"""The pairing agent BlueZ asks during pairing.

This module does not use ``from __future__ import annotations``: dbus-fast
reads the D-Bus signatures from the methods' annotations, such as ``"o"``.
"""

# ruff: noqa: F821, N802
# mypy: disable-error-code="name-defined"

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method

from .const import AGENT_INTERFACE, DEFAULT_PIN

REJECTED = "org.bluez.Error.Rejected"


class PairingAgent(ServiceInterface):
    """Accepts pairing for one device only, for the length of one pairing.

    It suits devices without a screen or keyboard: "just works" pairing is
    confirmed, and a device that asks for a PIN gets ``pin``. Requests about
    any other device are rejected, so pairing one device cannot also accept
    another that asks at the same time.
    """

    def __init__(self, device_path: str, pin: str = DEFAULT_PIN) -> None:
        """Create an agent for the device at ``device_path``."""
        super().__init__(AGENT_INTERFACE)
        self._device_path = device_path
        self._pin = pin

    def _check(self, device: str) -> None:
        if device != self._device_path:
            raise DBusError(REJECTED, "Not the device being paired")

    @method()
    def Release(self) -> None:
        """Release the agent; nothing to clean up."""

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        """Return the PIN for a device that asks for one."""
        self._check(device)
        return self._pin

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:
        """Accept a PIN the device shows."""
        self._check(device)

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        """Return a passkey: the PIN as a number, or 0."""
        self._check(device)
        return int(self._pin) if self._pin.isdigit() else 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        """Accept a passkey the device shows."""
        self._check(device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        """Confirm "just works" pairing."""
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o") -> None:
        """Authorise pairing that the device started."""
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        """Authorise a service connection made during pairing."""
        self._check(device)

    @method()
    def Cancel(self) -> None:
        """Note that BlueZ cancelled a request; nothing to undo."""
