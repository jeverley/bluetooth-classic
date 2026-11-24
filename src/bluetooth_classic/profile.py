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
from dataclasses import dataclass
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
from .const import BLUEZ_PATH, BLUEZ_SERVICE, DEVICE_INTERFACE
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


class _Profile(ServiceInterface):
    """The object BlueZ calls with each connected socket for one service."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Profile1")
        # Per device path, in order: bytes to write at once, and the waiter.
        self._waiting: dict[str, list[tuple[bytes, asyncio.Future[Handover]]]] = {}

    def expect(self, device_path: str, write: bytes) -> asyncio.Future[Handover]:
        """Wait for the next connection to a device."""
        future: asyncio.Future[Handover] = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(device_path, []).append((write, future))
        return future

    def forget(self, device_path: str, future: asyncio.Future[Handover]) -> None:
        """Stop waiting; a socket that arrives anyway is closed."""
        waiting = self._waiting.get(device_path, [])
        self._waiting[device_path] = [w for w in waiting if w[1] is not future]
        if not self._waiting[device_path]:
            del self._waiting[device_path]

    @method()
    def Release(self) -> None:
        _LOGGER.debug("BlueZ released a profile")

    @method()
    def NewConnection(
        self, device: DBusObjectPath, fd: DBusUnixFd, properties: DBusDict
    ) -> None:
        waiting = self._waiting.get(device)
        if not waiting:
            # Nobody waits: the attempt was abandoned, or the device
            # connected by itself. Closing is the only safe end.
            _LOGGER.debug("Unexpected connection from %s; closing it", device)
            os.close(fd)
            return
        write, future = waiting.pop(0)
        if not waiting:
            del self._waiting[device]
        try:
            if write:
                # At once, before anything else runs on the event loop: some
                # devices change state soon after a connection opens.
                os.write(fd, write)
        except OSError as err:
            os.close(fd)
            if not future.done():
                future.set_exception(err)
            return
        written_at = time.monotonic()
        if future.done():
            os.close(fd)
            return
        future.set_result(Handover(fd, device, written_at))

    @method()
    def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # The link's owner closes it; closing the socket here would pull it
        # out from under them.
        _LOGGER.debug("BlueZ asked to disconnect %s", device)


@dataclass(slots=True)
class _Registration:
    path: str
    profile: _Profile
    users: int


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

    async def bus(self) -> MessageBus:
        """Return the shared bus, connecting it if needed."""
        async with self._the_lock():
            if self._bus is None or not self._bus.connected:
                try:
                    self._bus = await self._connect_bus()
                except Exception as err:
                    raise RfcommConnectError(
                        f"Could not reach BlueZ over D-Bus: {err}"
                    ) from err
                self._registered = {}
            return self._bus

    async def acquire(self, service: str) -> _Profile:
        """Return the profile for a service, registering it if unused."""
        bus = await self.bus()
        async with self._the_lock():
            if (registration := self._registered.get(service)) is not None:
                registration.users += 1
                return registration.profile
            path = f"/org/bluetooth_classic/profile{next(_PROFILE_PATHS)}"
            profile = _Profile()
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
            self._registered[service] = _Registration(path, profile, 1)
            return profile

    async def release(self, service: str) -> None:
        """Stop using a service's profile; unregister it once unused."""
        async with self._the_lock():
            registration = self._registered.get(service)
            if registration is None:
                return
            registration.users -= 1
            if registration.users > 0:
                return
            del self._registered[service]
            bus = self._bus
            if bus is None or not bus.connected:
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
                _LOGGER.debug("Could not unregister service %s: %s", service, err)
            finally:
                bus.unexport(registration.path, registration.profile)
            _LOGGER.debug("Unregistered service %s from BlueZ", service)


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
    matches = sorted(
        path
        for path, interfaces in objects.items()
        if DEVICE_INTERFACE in interfaces
        and normalize_address(str(_unwrap(interfaces[DEVICE_INTERFACE]["Address"])))
        == address
        and (adapter_path is None or path.startswith(f"{adapter_path}/"))
    )
    if not matches:
        raise RfcommConnectError(f"BlueZ does not know {address}; pair it first")
    return matches[0]


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


async def connect_profile(
    address: str,
    service: str,
    *,
    adapter: str | None,
    timeout: float | None,
    write_on_connect: bytes,
) -> tuple[MessageBus, Handover]:
    """Have BlueZ connect a device's service and hand over the socket.

    The caller owns the socket and must call ``release_profile()`` once the
    link has closed.

    Raises:
        ProfileInUseError: Another program has registered the service.
        RfcommConnectError: BlueZ could not connect.
        TimeoutError: No connection within ``timeout`` seconds.
    """
    profile = await _profiles.acquire(service)
    try:
        bus = await _profiles.bus()
        device_path = await _device_path(bus, address, adapter)
        # A link BlueZ still holds would make the new one wait.
        await disconnect_profile(bus, device_path, service)
        future = profile.expect(device_path, write_on_connect)
        try:
            async with asyncio.timeout(timeout):
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
                waiting: set[asyncio.Future[Any]] = {reply, future}
                done, _ = await asyncio.wait(
                    waiting, return_when=asyncio.FIRST_COMPLETED
                )
                if future not in done:
                    try:
                        reply.result()
                    except BluetoothClassicError as err:
                        raise RfcommConnectError(
                            f"BlueZ could not connect to {address}: {err}"
                        ) from err
                    handover = await future
                else:
                    handover = future.result()
                    reply.add_done_callback(_ignore)
        except BaseException:
            profile.forget(device_path, future)
            if future.done() and not future.cancelled() and not future.exception():
                os.close(future.result().fd)
            asyncio.get_running_loop().create_task(
                disconnect_profile(bus, device_path, service)
            )
            raise
    except BaseException:
        await _profiles.release(service)
        raise
    return bus, handover


async def release_profile(bus: MessageBus, device_path: str, service: str) -> None:
    """Drop BlueZ's side of a closed link, and the registration once unused."""
    await disconnect_profile(bus, device_path, service)
    await _profiles.release(service)


def _ignore(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
