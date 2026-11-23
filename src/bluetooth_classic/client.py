"""Bluetooth Classic discovery, pairing and presence through BlueZ."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import replace
from types import TracebackType
from typing import Any, Self, TypeAlias

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .agent import PairingAgent
from .const import (
    ADAPTER_INTERFACE,
    AGENT_CAPABILITY,
    AGENT_MANAGER_INTERFACE,
    AGENT_PATH,
    BLUEZ_PATH,
    BLUEZ_SERVICE,
    DBUS_PATH,
    DBUS_SERVICE,
    DEFAULT_PIN,
    DEVICE_INTERFACE,
    DISCOVERY_SECONDS,
    NOT_INQUIRY,
    OBJECT_MANAGER_INTERFACE,
    PAIR_TIMEOUT,
    PRESENCE_SECONDS,
    PROPERTIES_INTERFACE,
    TRANSPORT_BREDR,
)
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
)
from .models import ClassicDevice, PresenceResult

_LOGGER = logging.getLogger(__name__)

_ADDRESS_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

_ManagedObjects: TypeAlias = dict[str, dict[str, dict[str, Any]]]


def normalize_address(address: str) -> str:
    """Return an address in upper-case colon form, or raise ValueError."""
    candidate = address.strip().upper().replace("-", ":")
    if not _ADDRESS_RE.match(candidate):
        raise ValueError(f"Not a Bluetooth address: {address}")
    return candidate


def _unwrap(value: Any) -> Any:
    return value.value if isinstance(value, Variant) else value


def _summary(value: Any) -> Any:
    """Return a property value short enough to log."""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return "{" + ", ".join(str(key) for key in value) + "}"
    if isinstance(value, list | tuple | bytes | bytearray):
        return f"[{len(value)}]"
    return type(value).__name__


def _is_inquiry_answer(properties: dict[str, Any]) -> bool:
    """Return whether changed properties are an answer to a Classic inquiry.

    A Classic inquiry answer brings a signal strength. Low Energy advertising
    and a link changing can bring one too, but with other properties.
    """
    return "RSSI" in properties and not properties.keys() & NOT_INQUIRY


def _device(path: str, properties: dict[str, Any]) -> ClassicDevice:
    props = {key: _unwrap(value) for key, value in properties.items()}
    rssi = props.get("RSSI")
    device_class = props.get("Class")
    return ClassicDevice(
        address=str(props.get("Address", "")).upper(),
        name=props.get("Name") or props.get("Alias"),
        path=path,
        adapter=path.split("/")[3] if path.count("/") >= 3 else "",
        paired=bool(props.get("Paired", False)),
        trusted=bool(props.get("Trusted", False)),
        connected=bool(props.get("Connected", False)),
        uuids=frozenset(uuid.lower() for uuid in props.get("UUIDs", [])),
        device_class=int(device_class) if device_class is not None else None,
        rssi=int(rssi) if rssi is not None else None,
    )


class BluetoothClassic:
    """A connection to BlueZ for Bluetooth Classic devices on one adapter.

    Use :meth:`connect`, ideally as an async context manager::

        async with await BluetoothClassic.connect() as bluetooth:
            devices = await bluetooth.discover()

    Discovery and presence checks on one connection run one at a time.
    A Classic scan uses the same radio as Low Energy scanning and interrupts
    it while it runs, so keep them short and let one owner schedule them.
    """

    def __init__(self, bus: MessageBus, adapter_path: str, *, owns_bus: bool) -> None:
        """Wrap a connected bus; use :meth:`connect` instead."""
        self._bus = bus
        self._adapter_path = adapter_path
        self._owns_bus = owns_bus
        self._scan_lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls, adapter: str | None = None, *, bus: MessageBus | None = None
    ) -> Self:
        """Connect to BlueZ and choose an adapter.

        Args:
            adapter: The adapter's name, such as ``"hci0"``. ``None`` picks the
                first powered adapter, or the first adapter if none is powered.
            bus: A system bus connection to use instead of opening one. It is
                left open when this connection closes.

        Raises:
            BluetoothClassicError: The system bus could not be reached.
            NoAdapterError: There is no adapter, or not the one asked for.
        """
        owns_bus = bus is None
        if bus is None:
            try:
                bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            except Exception as err:
                raise BluetoothClassicError(
                    f"Could not connect to the system bus: {err}"
                ) from err
        try:
            path = _choose_adapter(await _managed_objects(bus), adapter)
        except BaseException:
            if owns_bus:
                bus.disconnect()
            raise
        return cls(bus, path, owns_bus=owns_bus)

    @property
    def adapter(self) -> str:
        """Return the adapter's name, such as ``"hci0"``."""
        return self._adapter_path.rsplit("/", 1)[-1]

    def close(self) -> None:
        """Close the connection, unless it was given an existing bus."""
        if self._owns_bus:
            self._bus.disconnect()

    async def __aenter__(self) -> Self:
        """Use as an async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on leaving the block."""
        self.close()

    async def devices(self, *, service: str | None = None) -> list[ClassicDevice]:
        """Return the devices BlueZ knows on this adapter.

        Known devices are those paired, and those seen by a recent scan.

        Args:
            service: Only devices that list this service UUID. Before pairing,
                a device lists only the services it advertises in a scan.
        """
        devices = [
            _device(path, interfaces[DEVICE_INTERFACE])
            for path, interfaces in (await _managed_objects(self._bus)).items()
            if DEVICE_INTERFACE in interfaces
            and path.startswith(f"{self._adapter_path}/")
        ]
        if service is not None:
            devices = [device for device in devices if device.offers(service)]
        return sorted(devices, key=lambda device: device.address)

    async def device(self, address: str) -> ClassicDevice | None:
        """Return the device with this address, if BlueZ knows it."""
        address = normalize_address(address)
        return next((d for d in await self.devices() if d.address == address), None)

    async def discover(self, seconds: float = DISCOVERY_SECONDS) -> list[ClassicDevice]:
        """Scan for Bluetooth Classic devices and return those that answered.

        Devices that answer become known to BlueZ, so they can be paired.
        Only discoverable devices answer.
        """
        answered: dict[str, int | None] = {}

        def on_answer(path: str, properties: dict[str, Any]) -> None:
            # Low Energy devices found by other programs' scans also come
            # and go meanwhile; only Classic devices report a device class.
            if "Class" in properties or _is_inquiry_answer(properties):
                answered[path] = _unwrap(properties.get("RSSI"))

        async with (
            self._scan_lock,
            self._listening(on_answer, new_devices=True),
            self._discovery(),
        ):
            await asyncio.sleep(seconds)
        found = []
        for device in await self.devices():
            if device.path in answered:
                rssi = answered[device.path]
                if rssi is not None:
                    device = replace(device, rssi=int(rssi))
                found.append(device)
        _LOGGER.debug("Discovery on %s found %s devices", self.adapter, len(found))
        return found

    async def check_presence(
        self, address: str, seconds: float = PRESENCE_SECONDS
    ) -> PresenceResult:
        """Return whether a known device answers a short Classic scan.

        This does not connect to the device, so it does not wake one that
        wakes when connected to. A device answers only while it is on and
        discoverable.

        Raises:
            DeviceNotFoundError: BlueZ does not know the device; discover or
                pair it first.
        """
        device = await self.device(address)
        if device is None:
            raise DeviceNotFoundError(f"{address} is not known on {self.adapter}")
        answered = asyncio.Event()
        changed: dict[str, Any] = {}

        def on_change(path: str, properties: dict[str, Any]) -> None:
            if path != device.path or answered.is_set():
                return
            for key, value in properties.items():
                changed[key] = _summary(_unwrap(value))
            if _is_inquiry_answer(properties):
                answered.set()

        loop = asyncio.get_running_loop()
        async with self._scan_lock, self._listening(on_change, path=device.path):
            started = loop.time()
            async with self._discovery():
                try:
                    async with asyncio.timeout(seconds):
                        await answered.wait()
                except TimeoutError:
                    return PresenceResult(False, linked=device.connected)
                return PresenceResult(
                    True,
                    after=loop.time() - started,
                    changed=changed,
                    linked=device.connected,
                )

    async def pair(
        self,
        address: str,
        *,
        pin: str = DEFAULT_PIN,
        timeout: float = PAIR_TIMEOUT,
    ) -> ClassicDevice:
        """Pair with a known device, if not paired already.

        A temporary agent answers for this device only: it confirms "just
        works" pairing and gives ``pin`` if the device asks for one.

        Raises:
            DeviceNotFoundError: BlueZ does not know the device; discover it
                first.
            PairingError: Pairing failed or timed out.
        """
        device = await self._known(address)
        if device.paired:
            return device
        agent = PairingAgent(device.path, pin)
        self._bus.export(AGENT_PATH, agent)
        try:
            await self._call(
                BLUEZ_PATH,
                AGENT_MANAGER_INTERFACE,
                "RegisterAgent",
                "os",
                [AGENT_PATH, AGENT_CAPABILITY],
            )
            try:
                async with asyncio.timeout(timeout):
                    await self._call(device.path, DEVICE_INTERFACE, "Pair")
            except TimeoutError as err:
                with contextlib.suppress(BluetoothClassicError):
                    await self._call(device.path, DEVICE_INTERFACE, "CancelPairing")
                raise PairingError(f"Pairing with {device.address} timed out") from err
            except BluetoothClassicError as err:
                if "AlreadyExists" not in str(err):
                    raise PairingError(str(err)) from err
            finally:
                try:
                    await self._call(
                        BLUEZ_PATH,
                        AGENT_MANAGER_INTERFACE,
                        "UnregisterAgent",
                        "o",
                        [AGENT_PATH],
                    )
                except BluetoothClassicError as err:
                    _LOGGER.debug("Could not unregister the pairing agent: %s", err)
        finally:
            self._bus.unexport(AGENT_PATH, agent)
        return await self._known(address)

    async def trust(self, address: str) -> ClassicDevice:
        """Mark a known device as trusted, if it is not already.

        BlueZ then accepts the device's connections without asking. Some
        devices drop a connection from a host that does not trust them, as
        phones trust what they pair with.

        Raises:
            DeviceNotFoundError: BlueZ does not know the device.
        """
        device = await self._known(address)
        if not device.trusted:
            await self._call(
                device.path,
                PROPERTIES_INTERFACE,
                "Set",
                "ssv",
                [DEVICE_INTERFACE, "Trusted", Variant("b", True)],
            )
        return await self._known(address)

    async def _known(self, address: str) -> ClassicDevice:
        if (device := await self.device(address)) is None:
            raise DeviceNotFoundError(f"{address} is not known on {self.adapter}")
        return device

    @contextlib.asynccontextmanager
    async def _discovery(self) -> AsyncIterator[None]:
        """Run Classic-only discovery on the adapter for the block's length.

        BlueZ keeps a discovery filter per client and merges them, so this
        does not stop Low Energy scanning other programs have asked for.
        """
        await self._call(
            self._adapter_path,
            ADAPTER_INTERFACE,
            "SetDiscoveryFilter",
            "a{sv}",
            [{"Transport": Variant("s", TRANSPORT_BREDR)}],
        )
        await self._call(self._adapter_path, ADAPTER_INTERFACE, "StartDiscovery")
        try:
            yield
        finally:
            try:
                await self._call(self._adapter_path, ADAPTER_INTERFACE, "StopDiscovery")
            except BluetoothClassicError as err:
                _LOGGER.debug("Could not stop discovery: %s", err)

    @contextlib.asynccontextmanager
    async def _listening(
        self,
        callback: Callable[[str, dict[str, Any]], None],
        *,
        path: str | None = None,
        new_devices: bool = False,
    ) -> AsyncIterator[None]:
        """Call back with each device's changed properties during the block.

        With ``path``, only that device's; otherwise every device on the
        adapter. With ``new_devices``, devices BlueZ adds are reported too.
        """
        where = f"path='{path}'" if path else f"path_namespace='{self._adapter_path}'"
        rules = [
            f"type='signal',interface='{PROPERTIES_INTERFACE}',"
            f"member='PropertiesChanged',{where}"
        ]
        if new_devices:
            rules.append(
                f"type='signal',interface='{OBJECT_MANAGER_INTERFACE}',"
                "member='InterfacesAdded'"
            )
        prefix = f"{self._adapter_path}/"

        def handler(message: Message) -> None:
            if message.message_type != MessageType.SIGNAL or not message.body:
                return
            if (
                message.member == "PropertiesChanged"
                and message.body[0] == DEVICE_INTERFACE
                and message.path
                and (message.path == path if path else message.path.startswith(prefix))
            ):
                callback(message.path, message.body[1])
            elif (
                new_devices
                and message.member == "InterfacesAdded"
                and str(message.body[0]).startswith(prefix)
                and DEVICE_INTERFACE in message.body[1]
            ):
                callback(message.body[0], message.body[1][DEVICE_INTERFACE])

        added: list[str] = []
        self._bus.add_message_handler(handler)
        try:
            for rule in rules:
                await self._bus_call("AddMatch", rule)
                added.append(rule)
            yield
        finally:
            self._bus.remove_message_handler(handler)
            for rule in added:
                with contextlib.suppress(BluetoothClassicError):
                    await self._bus_call("RemoveMatch", rule)

    async def _bus_call(self, member: str, rule: str) -> None:
        await _call(
            self._bus, DBUS_SERVICE, DBUS_PATH, DBUS_SERVICE, member, "s", [rule]
        )

    async def _call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Iterable[Any] = (),
    ) -> list[Any]:
        return await _call(
            self._bus, BLUEZ_SERVICE, path, interface, member, signature, body
        )


def _choose_adapter(objects: _ManagedObjects, adapter: str | None) -> str:
    adapters = sorted(
        (path, interfaces[ADAPTER_INTERFACE])
        for path, interfaces in objects.items()
        if ADAPTER_INTERFACE in interfaces
    )
    if adapter is not None:
        path = f"{BLUEZ_PATH}/{adapter}"
        if any(candidate == path for candidate, _ in adapters):
            return path
        raise NoAdapterError(f"Bluetooth adapter {adapter} not found")
    if not adapters:
        raise NoAdapterError("No Bluetooth adapter found")
    powered = [path for path, props in adapters if _unwrap(props.get("Powered"))]
    return (powered or [adapters[0][0]])[0]


async def _managed_objects(bus: MessageBus) -> _ManagedObjects:
    reply = await _call(
        bus, BLUEZ_SERVICE, "/", OBJECT_MANAGER_INTERFACE, "GetManagedObjects"
    )
    return reply[0] if reply else {}


async def _call(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Iterable[Any] = (),
) -> list[Any]:
    try:
        reply = await bus.call(
            Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=list(body),
            )
        )
    except Exception as err:
        raise BluetoothClassicError(f"{member} failed: {err}") from err
    if reply is None:
        raise BluetoothClassicError(f"{member} got no reply")
    if reply.message_type == MessageType.ERROR:
        raise BluetoothClassicError(f"{member} failed: {reply.error_name} {reply.body}")
    return list(reply.body)
