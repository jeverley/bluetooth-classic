"""Bluetooth Classic discovery, finding a device and pairing, through BlueZ."""

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
    CALL_TIMEOUT,
    DBUS_PATH,
    DBUS_SERVICE,
    DEFAULT_PIN,
    DEVICE_INTERFACE,
    DISCOVERY_TIMEOUT,
    ERROR_NOT_READY,
    FIND_TIMEOUT,
    NOT_INQUIRY,
    OBJECT_MANAGER_INTERFACE,
    PAIR_TIMEOUT,
    PROPERTIES_INTERFACE,
    TRANSPORT_BREDR,
)
from .exceptions import (
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
)
from .models import ClassicDevice

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

    Use it as an async context manager, as bleak's scanner and client are::

        async with BluetoothClassic() as bluetooth:
            devices = await bluetooth.discover()

    Discovery and finding a device on one connection run one at a time.
    A Classic scan uses the same radio as Low Energy scanning and interrupts
    it while it runs, so keep them short and let one owner schedule them.
    """

    def __init__(
        self, adapter: str | None = None, *, bus: MessageBus | None = None
    ) -> None:
        """Prepare a connection; it opens on entering the ``async with`` block.

        Args:
            adapter: The adapter's name, such as ``"hci0"``. ``None`` picks
                BlueZ's first adapter, as aio-rfcomm does for RFCOMM links, so
                a device paired here can be connected; if it is powered off,
                scans and pairing raise NoAdapterError.
            bus: A system bus connection to use instead of opening one. It is
                left open when this connection closes.
        """
        self._requested_adapter = adapter
        self._given_bus = bus
        self._bus: MessageBus | None = None
        self._adapter_path = ""
        self._scan_lock = asyncio.Lock()
        # One pairing at a time: the agent is exported at one path.
        self._pair_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        """Connect to BlueZ and choose the adapter.

        Raises:
            BluetoothClassicError: The system bus could not be reached.
            NoAdapterError: There is no adapter, or not the one asked for.
        """
        if self._bus is not None:
            raise BluetoothClassicError("This BluetoothClassic is already open")
        bus = self._given_bus
        if bus is None:
            try:
                bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            except Exception as err:
                raise BluetoothClassicError(
                    f"Could not connect to the system bus: {err}"
                ) from err
        try:
            path = _choose_adapter(await _managed_objects(bus), self._requested_adapter)
        except BaseException:
            if self._given_bus is None:
                bus.disconnect()
            raise
        self._bus = bus
        self._adapter_path = path
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection, unless it was given an existing bus."""
        if self._bus is not None and self._given_bus is None:
            self._bus.disconnect()
        self._bus = None

    @property
    def adapter(self) -> str:
        """Return the adapter's name, such as ``"hci0"``."""
        self._require_open()
        return self._adapter_path.rsplit("/", 1)[-1]

    def _require_open(self) -> MessageBus:
        if self._bus is None:
            raise BluetoothClassicError(
                "Not connected to BlueZ; use BluetoothClassic in async with"
            )
        return self._bus

    async def devices(self, *, service: str | None = None) -> list[ClassicDevice]:
        """Return the devices BlueZ knows on this adapter.

        Known devices are those paired, and those seen by a recent scan.

        Args:
            service: Only devices that list this service UUID. Before pairing,
                a device lists only the services it advertises in a scan.
        """
        devices = [
            _device(path, interfaces[DEVICE_INTERFACE])
            for path, interfaces in (
                await _managed_objects(self._require_open())
            ).items()
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

    async def discover(self, timeout: float = DISCOVERY_TIMEOUT) -> list[ClassicDevice]:
        """Scan for Bluetooth Classic devices and return those that answered.

        Like bleak's ``BleakScanner.discover()``. Devices that answer become
        known to BlueZ, so they can be paired. Only discoverable devices
        answer.

        Raises:
            NoAdapterError: The adapter is powered off.
        """
        answered: dict[str, int | None] = {}

        def on_answer(path: str, properties: dict[str, Any], added: bool) -> None:
            # Low Energy devices found by other programs' scans also come
            # and go meanwhile; only Classic devices report a device class.
            if "Class" in properties or _is_inquiry_answer(properties):
                answered[path] = _unwrap(properties.get("RSSI"))

        async with (
            self._scan_lock,
            self._listening(on_answer, new_devices=True),
            self._discovery(),
        ):
            await asyncio.sleep(timeout)
        found = []
        for device in await self.devices():
            # A Low Energy device can bring a signal strength alone, without
            # the properties that mark advertising; only Classic devices
            # have a device class.
            if device.path in answered and device.device_class is not None:
                rssi = answered[device.path]
                if rssi is not None:
                    device = replace(device, rssi=int(rssi))
                found.append(device)
        _LOGGER.debug("Discovery on %s found %s devices", self.adapter, len(found))
        return found

    async def find_device_by_address(
        self, address: str, timeout: float = FIND_TIMEOUT
    ) -> ClassicDevice | None:
        """Scan until a device answers, and return it; ``None`` if it does not.

        Like bleak's ``BleakScanner.find_device_by_address()``: the scan stops
        as soon as the device answers. It does not connect to the device, so
        it does not wake one that wakes when connected to. The device need not
        be known to BlueZ beforehand.

        A device answers only while it is on and discoverable. Phones and
        computers are usually not discoverable, so this does not tell
        whether a person is home.

        Raises:
            NoAdapterError: The adapter is powered off.
        """
        address = normalize_address(address)
        self._require_open()
        target = f"{self._adapter_path}/dev_{address.replace(':', '_')}"
        answered = asyncio.Event()
        rssi: list[Any] = []

        def on_answer(path: str, properties: dict[str, Any], added: bool) -> None:
            if path != target or answered.is_set():
                return
            # A device BlueZ adds brings its class with the signal strength;
            # for a known one, only a signal strength from an inquiry counts,
            # not a class change made some other way.
            if (added and "Class" in properties) or _is_inquiry_answer(properties):
                rssi.append(_unwrap(properties.get("RSSI")))
                answered.set()

        async with (
            self._scan_lock,
            self._listening(on_answer, new_devices=True),
            self._discovery(),
        ):
            try:
                async with asyncio.timeout(timeout):
                    await answered.wait()
            except TimeoutError:
                return None
        device = await self.device(address)
        if device is not None and rssi and rssi[0] is not None:
            device = replace(device, rssi=int(rssi[0]))
        return device

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
            NoAdapterError: The adapter is powered off.
            PairingError: Pairing failed or timed out.
        """
        async with self._pair_lock:
            return await self._pair(address, pin, timeout)

    async def _pair(self, address: str, pin: str, timeout: float) -> ClassicDevice:
        device = await self._known(address)
        if device.paired:
            return device
        agent = PairingAgent(device.path, pin)
        self._require_open().export(AGENT_PATH, agent)
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
                    await self._call(
                        device.path, DEVICE_INTERFACE, "Pair", timeout=None
                    )
            except TimeoutError as err:
                with contextlib.suppress(BluetoothClassicError):
                    await self._call(device.path, DEVICE_INTERFACE, "CancelPairing")
                raise PairingError(f"Pairing with {device.address} timed out") from err
            except NoAdapterError:
                raise
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
            self._require_open().unexport(AGENT_PATH, agent)
        return await self._known(address)

    async def unpair(self, address: str) -> None:
        """Remove a device from BlueZ, forgetting its pairing and trust.

        Like bleak's ``BleakClient.unpair()``, which also removes the device
        on Linux. Nothing happens if BlueZ does not know the device.
        """
        device = await self.device(address)
        if device is not None:
            await self._call(
                self._adapter_path,
                ADAPTER_INTERFACE,
                "RemoveDevice",
                "o",
                [device.path],
            )

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
        does not stop Low Energy scanning other programs have asked for. The
        filter is cleared afterwards.
        """
        await self._call(
            self._adapter_path,
            ADAPTER_INTERFACE,
            "SetDiscoveryFilter",
            "a{sv}",
            [{"Transport": Variant("s", TRANSPORT_BREDR)}],
        )
        try:
            await self._call(self._adapter_path, ADAPTER_INTERFACE, "StartDiscovery")
            try:
                yield
            finally:
                try:
                    await self._call(
                        self._adapter_path, ADAPTER_INTERFACE, "StopDiscovery"
                    )
                except BluetoothClassicError as err:
                    _LOGGER.debug("Could not stop discovery: %s", err)
        finally:
            # Clear this client's filter, so it does not shape later scans.
            try:
                await self._call(
                    self._adapter_path,
                    ADAPTER_INTERFACE,
                    "SetDiscoveryFilter",
                    "a{sv}",
                    [{}],
                )
            except BluetoothClassicError as err:
                _LOGGER.debug("Could not clear the discovery filter: %s", err)

    @contextlib.asynccontextmanager
    async def _listening(
        self,
        callback: Callable[[str, dict[str, Any], bool], None],
        *,
        new_devices: bool = False,
    ) -> AsyncIterator[None]:
        """Call back with each device's changed properties during the block.

        The callback gets the device's path, the properties, and whether
        BlueZ has just added the device. With ``new_devices``, devices BlueZ
        adds are reported too.
        """
        rules = [
            f"type='signal',interface='{PROPERTIES_INTERFACE}',"
            f"member='PropertiesChanged',path_namespace='{self._adapter_path}'"
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
                and message.path.startswith(prefix)
            ):
                callback(message.path, message.body[1], False)
            elif (
                new_devices
                and message.member == "InterfacesAdded"
                and str(message.body[0]).startswith(prefix)
                and DEVICE_INTERFACE in message.body[1]
            ):
                callback(message.body[0], message.body[1][DEVICE_INTERFACE], True)

        added: list[str] = []
        self._require_open().add_message_handler(handler)
        try:
            for rule in rules:
                await self._bus_call("AddMatch", rule)
                added.append(rule)
            yield
        finally:
            self._require_open().remove_message_handler(handler)
            for rule in added:
                with contextlib.suppress(BluetoothClassicError):
                    await self._bus_call("RemoveMatch", rule)

    async def _bus_call(self, member: str, rule: str) -> None:
        await _call(
            self._require_open(),
            DBUS_SERVICE,
            DBUS_PATH,
            DBUS_SERVICE,
            member,
            "s",
            [rule],
        )

    async def _call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Iterable[Any] = (),
        *,
        timeout: float | None = CALL_TIMEOUT,
    ) -> list[Any]:
        return await _call(
            self._require_open(),
            BLUEZ_SERVICE,
            path,
            interface,
            member,
            signature,
            body,
            timeout=timeout,
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
    # The first by object path, powered or not: the one aio-rfcomm opens
    # RFCOMM links on, so pairing and links use the same adapter.
    return adapters[0][0]


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
    *,
    timeout: float | None = CALL_TIMEOUT,
) -> list[Any]:
    """Call a D-Bus method and return the reply's body.

    Raises:
        NoAdapterError: BlueZ says the adapter is not ready (powered off).
        BluetoothClassicError: The call failed, or had no answer within
            ``timeout`` seconds.
    """
    message = Message(
        destination=destination,
        path=path,
        interface=interface,
        member=member,
        signature=signature,
        body=list(body),
    )
    try:
        async with asyncio.timeout(timeout):
            reply = await bus.call(message)
    except TimeoutError as err:
        raise BluetoothClassicError(
            f"{member} had no answer within {timeout} s"
        ) from err
    except Exception as err:
        raise BluetoothClassicError(f"{member} failed: {err}") from err
    if reply is None:
        raise BluetoothClassicError(f"{member} got no reply")
    if reply.message_type == MessageType.ERROR and reply.error_name == ERROR_NOT_READY:
        raise NoAdapterError(f"{member} failed: the adapter is powered off")
    if reply.message_type == MessageType.ERROR:
        raise BluetoothClassicError(f"{member} failed: {reply.error_name} {reply.body}")
    return list(reply.body)
