"""Models for Bluetooth Classic devices and what a scan found."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ClassicDevice:
    """A Bluetooth device as BlueZ knows it."""

    #: Address in upper-case colon form, such as ``"00:11:22:33:44:55"``.
    address: str
    #: The device's name, or its alias when it has not sent a name.
    name: str | None
    #: BlueZ's object path for the device.
    path: str
    #: The adapter it is known on, such as ``"hci0"``.
    adapter: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    #: Service class UUIDs, lower case. Before pairing, only those the
    #: device advertises in a scan; the full list is read when it is paired.
    uuids: frozenset[str] = frozenset()
    #: The Classic Class of Device, which only Classic devices report.
    device_class: int | None = None
    #: Signal strength from the latest scan answer, if BlueZ has one.
    rssi: int | None = None

    def offers(self, service: str) -> bool:
        """Return whether the device lists a service, by its UUID."""
        return service.lower() in self.uuids


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Whether a device answered a short Classic scan, and how."""

    #: Whether it answered within the time allowed.
    found: bool
    #: Seconds until it answered.
    after: float | None = None
    #: Device properties BlueZ changed up to the answer, summarised for logs.
    #: A Classic inquiry answer changes the signal strength (``RSSI``), often
    #: with ``TxPower``.
    changed: dict[str, Any] = field(default_factory=dict)
    #: Whether BlueZ already had a link to the device when the scan began.
    linked: bool = False
