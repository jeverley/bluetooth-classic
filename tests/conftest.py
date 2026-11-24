"""A fake system bus that behaves like BlueZ, for tests without hardware."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from dbus_fast import MessageType, Variant

from bluetooth_classic.agent import PairingAgent

ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
HCI0 = "/org/bluez/hci0"
SPEAKER = f"{HCI0}/dev_00_11_22_33_44_55"
SPEAKER_ADDRESS = "00:11:22:33:44:55"
SPP = "00001101-0000-1000-8000-00805f9b34fb"
A2DP = "0000110b-0000-1000-8000-00805f9b34fb"


def device_properties(address: str, name: str, **extra: Any) -> dict[str, Variant]:
    properties = {
        "Address": Variant("s", address),
        "Name": Variant("s", name),
        "Paired": Variant("b", False),
        "Trusted": Variant("b", False),
        "Connected": Variant("b", False),
        "UUIDs": Variant("as", [A2DP]),
        "Class": Variant("u", 0x240414),
    }
    properties.update(extra)
    return properties


class FakeBus:
    """Answers BlueZ calls from an in-memory object tree."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            HCI0: {ADAPTER: {"Powered": Variant("b", True)}},
            SPEAKER: {DEVICE: device_properties(SPEAKER_ADDRESS, "Speaker")},
        }
        self.calls: list[tuple[str, str, list[Any]]] = []
        self.handlers: list[Callable[[Any], None]] = []
        self.exported: dict[str, Any] = {}
        self.matches: list[str] = []
        self.disconnected = False
        # Signals sent once discovery starts: (path, properties) changes, and
        # devices added.
        self.on_discovery: list[tuple[str, dict[str, Variant]]] = []
        self.added_on_discovery: dict[str, dict[str, Variant]] = {}
        # Pairing: None succeeds; a string is a BlueZ error name; "hang" waits.
        self.pair_outcome: str | None = None
        self.agent_answers: list[Any] = []
        # Calls that fail with a BlueZ error name, and calls never answered.
        self.errors: dict[str, str] = {}
        self.unanswered: set[str] = set()

    async def call(self, message: Any) -> SimpleNamespace:
        self.calls.append((message.member, message.path, list(message.body)))
        if message.member in self.unanswered:
            await asyncio.sleep(10)
        if message.member in self.errors:
            return self._error(self.errors[message.member])
        handler = getattr(self, f"_{message.member}", None)
        if handler is None:
            return self._reply()
        return await handler(message)

    def export(self, path: str, interface: Any) -> None:
        # dbus-fast refuses a second interface of the same name at a path.
        if path in self.exported:
            raise ValueError(f"An interface is already exported at {path}")
        self.exported[path] = interface

    def unexport(self, path: str, interface: Any) -> None:
        self.exported.pop(path, None)

    def add_message_handler(self, handler: Callable[[Any], None]) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Callable[[Any], None]) -> None:
        self.handlers.remove(handler)

    def disconnect(self) -> None:
        self.disconnected = True

    def members(self) -> list[str]:
        return [member for member, _, _ in self.calls]

    def emit(self, member: str, path: str, body: list[Any]) -> None:
        signal = SimpleNamespace(
            message_type=MessageType.SIGNAL, member=member, path=path, body=body
        )
        for handler in list(self.handlers):
            handler(signal)

    @staticmethod
    def _reply(*body: Any) -> SimpleNamespace:
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=list(body))

    @staticmethod
    def _error(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            message_type=MessageType.ERROR, error_name=name, body=["refused"]
        )

    async def _GetManagedObjects(self, message: Any) -> SimpleNamespace:
        return self._reply(self.objects)

    async def _AddMatch(self, message: Any) -> SimpleNamespace:
        self.matches.append(message.body[0])
        return self._reply()

    async def _RemoveMatch(self, message: Any) -> SimpleNamespace:
        self.matches.remove(message.body[0])
        return self._reply()

    async def _StartDiscovery(self, message: Any) -> SimpleNamespace:
        loop = asyncio.get_running_loop()
        for path, properties in self.on_discovery:
            loop.call_soon(
                self.emit, "PropertiesChanged", path, [DEVICE, properties, []]
            )
        for path, properties in self.added_on_discovery.items():
            self.objects[path] = {DEVICE: properties}
            loop.call_soon(
                self.emit, "InterfacesAdded", "/", [path, {DEVICE: properties}]
            )
        return self._reply()

    async def _Pair(self, message: Any) -> SimpleNamespace:
        # Pairing takes a while, so other calls can run meanwhile.
        await asyncio.sleep(0.01)
        if self.pair_outcome == "hang":
            await asyncio.sleep(10)
        if self.pair_outcome is not None:
            return self._error(self.pair_outcome)
        agent = self.exported["/org/bluetooth_classic/agent"]
        # BlueZ asks the agent to confirm "just works" pairing.
        wrapped: Any = PairingAgent
        wrapped.RequestConfirmation.__wrapped__(agent, message.path, 0)
        self.agent_answers.append(
            wrapped.RequestPinCode.__wrapped__(agent, message.path)
        )
        properties = self.objects[message.path][DEVICE]
        properties["Paired"] = Variant("b", True)
        properties["UUIDs"] = Variant("as", [A2DP, SPP])
        return self._reply()

    async def _RemoveDevice(self, message: Any) -> SimpleNamespace:
        self.objects.pop(message.body[0], None)
        return self._reply()

    async def _Set(self, message: Any) -> SimpleNamespace:
        interface, name, value = message.body
        self.objects[message.path][interface][name] = value
        return self._reply()


@pytest.fixture
def bus() -> FakeBus:
    """Return a fake system bus with one adapter and one speaker."""
    return FakeBus()
