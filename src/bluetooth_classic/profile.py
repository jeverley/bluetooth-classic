"""Connecting to RFCOMM services through BlueZ's profile API.

BlueZ makes the connection and hands the connected socket over D-Bus, as
bleak's connections are made by BlueZ too:

1. Register as a client of the service (``ProfileManager1.RegisterProfile``),
   once per process and only while a connection uses it.
2. Ask BlueZ to connect it on the device (``Device1.ConnectProfile``).
3. BlueZ calls ``Profile1.NewConnection`` with the connected socket. Bytes
   to send at once are written inside that call, before anything else runs.
4. When the link closes, ask BlueZ to drop its side
   (``Device1.DisconnectProfile``): BlueZ keeps its own reference to the
   connection, so closing ours alone leaves the link open.

BlueZ accepts one registration per service UUID, so while this process holds
one, other programs cannot connect to that service through BlueZ (as found by
others on BlueZ 5.72 and 5.85; unconfirmed here). Registering only while in
use keeps that short.
"""

# Do not add ``from __future__ import annotations``: dbus-fast reads the
# D-Bus signatures from the method annotations. D-Bus method names are
# BlueZ's, in CamelCase.
# ruff: noqa: N802

import asyncio
import itertools
import logging
import os
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusDict, DBusObjectPath, DBusUnixFd
from dbus_fast.service import ServiceInterface, method

from .client import (
    _call,
    _choose_adapter,
    _managed_objects,
    _unwrap,
    normalize_address,
)
from .const import ADAPTER_INTERFACE, BLUEZ_PATH, BLUEZ_SERVICE, DEVICE_INTERFACE
from .exceptions import (
    BluetoothClassicError,
    NoAdapterError,
    ProfileInUseError,
    RfcommConnectError,
)

_LOGGER = logging.getLogger(__name__)

PROFILE_MANAGER_INTERFACE = "org.bluez.ProfileManager1"
_PROFILE_PATHS = itertools.count()


@dataclass(frozen=True, slots=True)
class Handover:
    """A connected socket BlueZ handed over."""

    fd: int
    #: The device's object path, which names the adapter it is on.
    device_path: str
    #: When the bytes to send at once were written, in time.monotonic().
    written_at: float


# Background tasks (dropping BlueZ's side of links); kept so they finish.
_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class _Profile(ServiceInterface):
    """The object BlueZ calls with each connected socket for one service."""

    def __init__(self, on_stray: Callable[[str], None]) -> None:
        super().__init__("org.bluez.Profile1")
        # Per device path, in order: bytes to write at once, and the waiter.
        self._waiting: dict[str, list[tuple[bytes, asyncio.Future[Handover]]]] = {}
        # Called with the device path of a connection nobody waits for.
        self._on_stray = on_stray

    def expect(self, device_path: str, write: bytes) -> asyncio.Future[Handover]:
        """Wait for the next connection to a device."""
        future: asyncio.Future[Handover] = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(device_path, []).append((write, future))
        return future

    def forget(self, device_path: str, future: asyncio.Future[Handover]) -> None:
        """Stop waiting; a socket that arrives anyway is closed."""
        waiting = [w for w in self._waiting.get(device_path, []) if w[1] is not future]
        if waiting:
            self._waiting[device_path] = waiting
        else:
            self._waiting.pop(device_path, None)

    def _stray(self, device: str, fd: int) -> None:
        # Nobody waits: the attempt was abandoned, or the device connected by
        # itself. Close ours, and have BlueZ drop its side too, as it keeps
        # its own reference to the link.
        _LOGGER.debug("Unexpected connection from %s; closing it", device)
        os.close(fd)
        self._on_stray(device)

    @method()
    def Release(self) -> None:
        _LOGGER.debug("BlueZ released a profile")

    @method()
    def NewConnection(
        self, device: DBusObjectPath, fd: DBusUnixFd, properties: DBusDict
    ) -> None:
        waiting = self._waiting.get(device)
        if not waiting:
            self._stray(device, fd)
            return
        write, future = waiting.pop(0)
        if not waiting:
            del self._waiting[device]
        if future.done():
            # Given up already: nothing is sent to the device.
            self._stray(device, fd)
            return
        try:
            if write:
                # At once, before anything else runs on the event loop: some
                # devices change state soon after a connection opens.
                os.write(fd, write)
        except OSError as err:
            os.close(fd)
            self._on_stray(device)
            future.set_exception(err)
            return
        future.set_result(Handover(fd, device, time.monotonic()))

    @method()
    def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # The link's owner closes it; closing the socket here would pull it
        # out from under them.
        _LOGGER.debug("BlueZ asked to disconnect %s", device)


@dataclass(slots=True, eq=False)
class _Registration:
    service: str
    path: str
    profile: _Profile
    #: The bus it was made on: BlueZ drops it when that bus disconnects.
    bus: MessageBus
    users: int = 1
    #: Devices with a link of this process to the service, open or opening.
    devices: set[str] = field(default_factory=set)


class _Profiles:
    """One bus and one registration per service, shared and counted."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._registered: dict[str, _Registration] = {}

    def _the_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            # A bus and lock belong to one event loop.
            self._lock = asyncio.Lock()
            self._loop = loop
            self._bus = None
            self._registered = {}
        return self._lock

    async def _connect_bus(self) -> MessageBus:
        return await MessageBus(
            bus_type=BusType.SYSTEM, negotiate_unix_fd=True
        ).connect()

    async def _the_bus(self) -> MessageBus:
        """Return the shared bus, connecting it if needed; call with the lock."""
        if self._bus is None or not self._bus.connected:
            try:
                self._bus = await self._connect_bus()
            except Exception as err:
                raise RfcommConnectError(
                    f"Could not reach BlueZ over D-Bus: {err}"
                ) from err
            # Registrations on the old bus went with it.
            self._registered = {}
        return self._bus

    async def acquire(self, service: str) -> _Registration:
        """Return the registration for a service, registering it if unused."""
        async with self._the_lock():
            bus = await self._the_bus()
            if (registration := self._registered.get(service)) is not None:
                registration.users += 1
                return registration
            path = f"/org/bluetooth_classic/profile{next(_PROFILE_PATHS)}"
            profile = _Profile(
                lambda device: _spawn(disconnect_profile(bus, device, service))
            )
            bus.export(path, profile)
            try:
                await _call(
                    bus,
                    BLUEZ_SERVICE,
                    BLUEZ_PATH,
                    PROFILE_MANAGER_INTERFACE,
                    "RegisterProfile",
                    "osa{sv}",
                    [
                        path,
                        service,
                        {
                            "Role": Variant("s", "client"),
                            "AutoConnect": Variant("b", False),
                        },
                    ],
                )
            except BluetoothClassicError as err:
                bus.unexport(path, profile)
                if "AlreadyExists" in str(err) or "already registered" in str(err):
                    raise ProfileInUseError(
                        f"Another program has registered service {service} with BlueZ"
                    ) from err
                raise RfcommConnectError(
                    f"BlueZ refused to register service {service}: {err}"
                ) from err
            _LOGGER.debug("Registered service %s with BlueZ", service)
            registration = _Registration(service, path, profile, bus)
            self._registered[service] = registration
            return registration

    async def release(self, registration: _Registration) -> None:
        """Stop using a registration; unregister it once unused."""
        async with self._the_lock():
            registration.users -= 1
            if registration.users > 0:
                return
            if self._registered.get(registration.service) is registration:
                del self._registered[registration.service]
            bus = registration.bus
            if not bus.connected:
                # BlueZ dropped it with the bus.
                return
            try:
                await _call(
                    bus,
                    BLUEZ_SERVICE,
                    BLUEZ_PATH,
                    PROFILE_MANAGER_INTERFACE,
                    "UnregisterProfile",
                    "o",
                    [registration.path],
                )
            except BluetoothClassicError as err:
                _LOGGER.debug(
                    "Could not unregister service %s: %s", registration.service, err
                )
            finally:
                bus.unexport(registration.path, registration.profile)
            _LOGGER.debug("Unregistered service %s from BlueZ", registration.service)


_profiles = _Profiles()


async def _device_path(bus: MessageBus, address: str, adapter: str | None) -> str:
    """Return the object path of a device BlueZ knows, on the adapter asked for."""
    try:
        objects = await _managed_objects(bus)
        adapter_path = _choose_adapter(objects, adapter) if adapter else None
    except NoAdapterError as err:
        raise RfcommConnectError(str(err)) from err
    except BluetoothClassicError as err:
        raise RfcommConnectError(f"Could not ask BlueZ for devices: {err}") from err
    powered = {
        path
        for path, interfaces in objects.items()
        if ADAPTER_INTERFACE in interfaces
        and _unwrap(interfaces[ADAPTER_INTERFACE].get("Powered"))
    }
    # Under a powered adapter first, when none was asked for.
    matches = sorted(
        (path.rsplit("/", 1)[0] not in powered, path)
        for path, interfaces in objects.items()
        if DEVICE_INTERFACE in interfaces
        and normalize_address(str(_unwrap(interfaces[DEVICE_INTERFACE]["Address"])))
        == address
        and (adapter_path is None or path.startswith(f"{adapter_path}/"))
    )
    if not matches:
        raise RfcommConnectError(f"BlueZ does not know {address}; pair it first")
    return matches[0][1]


async def disconnect_profile(bus: MessageBus, device_path: str, service: str) -> None:
    """Ask BlueZ to drop its side of a service's connection to a device."""
    try:
        await _call(
            bus,
            BLUEZ_SERVICE,
            device_path,
            DEVICE_INTERFACE,
            "DisconnectProfile",
            "s",
            [service],
        )
    except BluetoothClassicError as err:
        # Usually: nothing was connected.
        _LOGGER.debug("DisconnectProfile %s on %s: %s", service, device_path, err)


@dataclass(slots=True, eq=False)
class ProfileLink:
    """A link BlueZ connected; release it once the socket has closed."""

    handover: Handover
    _registration: _Registration
    _released: bool = False

    async def release(self) -> None:
        """Drop BlueZ's side of the closed link, and the registration once unused."""
        if self._released:
            return
        self._released = True
        registration = self._registration
        await disconnect_profile(
            registration.bus, self.handover.device_path, registration.service
        )
        registration.devices.discard(self.handover.device_path)
        await _profiles.release(registration)


async def connect_profile(
    address: str,
    service: str,
    *,
    adapter: str | None,
    timeout: float | None,
    write_on_connect: bytes,
) -> ProfileLink:
    """Have BlueZ connect a device's service and hand over the socket.

    The caller owns the socket and must call ``ProfileLink.release()`` once
    the link has closed. ``timeout`` bounds the whole open.

    Raises:
        ProfileInUseError: Another program has registered the service.
        RfcommConnectError: BlueZ could not connect, or this process already
            has a link to the device's service, open or opening.
        TimeoutError: No connection within ``timeout`` seconds.
    """
    async with asyncio.timeout(timeout):
        registration = await _profiles.acquire(service)
    device_path: str | None = None
    try:
        async with asyncio.timeout(timeout):
            bus = registration.bus
            device_path = await _device_path(bus, address, adapter)
            if device_path in registration.devices:
                found, device_path = device_path, None
                raise RfcommConnectError(
                    f"{address} already has a link to service {service}"
                    f" from this process ({found})"
                )
            registration.devices.add(device_path)
            # No link of ours is open: one BlueZ still holds from before
            # would make the new one wait.
            await disconnect_profile(bus, device_path, service)
            handover = await _connect(
                registration, device_path, address, service, write_on_connect
            )
    except BaseException:
        if device_path is not None:
            registration.devices.discard(device_path)
        await _profiles.release(registration)
        raise
    return ProfileLink(handover, registration)


async def _connect(
    registration: _Registration,
    device_path: str,
    address: str,
    service: str,
    write_on_connect: bytes,
) -> Handover:
    """Ask BlueZ to connect, and wait for the socket it hands over."""
    bus, profile = registration.bus, registration.profile
    future = profile.expect(device_path, write_on_connect)
    reply = asyncio.ensure_future(
        _call(
            bus,
            BLUEZ_SERVICE,
            device_path,
            DEVICE_INTERFACE,
            "ConnectProfile",
            "s",
            [service],
            timeout=None,
        )
    )
    # Whatever happens, the reply's outcome is collected.
    reply.add_done_callback(_ignore)
    try:
        waiting: set[asyncio.Future[Any]] = {reply, future}
        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if future not in done:
            try:
                reply.result()
            except BluetoothClassicError as err:
                raise RfcommConnectError(
                    f"BlueZ could not connect to {address}: {err}"
                ) from err
        return await future
    except BaseException:
        profile.forget(device_path, future)
        if future.done() and not future.cancelled() and not future.exception():
            os.close(future.result().fd)
        future.cancel()
        # A socket BlueZ hands over later is closed by the profile, which
        # then has BlueZ drop it; meanwhile drop any it already made.
        _spawn(disconnect_profile(bus, device_path, service))
        raise


def _ignore(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
