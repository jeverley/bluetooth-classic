"""Models for Bluetooth Classic devices and what a scan found."""

from __future__ import annotations

from dataclasses import dataclass


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
