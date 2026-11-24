"""A serialx handler for Bluetooth Classic RFCOMM services: ``rfcomm://`` URLs.

``rfcomm://00:11:22:33:44:55?service=1101`` opens that device's Serial Port
service (``1101``) as a serial connection. ``service`` is a UUID, or its
16- or 32-bit short form; ``adapter`` (such as ``hci0``) chooses the local
adapter.

BlueZ makes the connection, through its profile API, and hands the
connected socket over D-Bus; see :mod:`bluetooth_classic.profile`. BlueZ
finds the service's channel itself. Bytes given as ``write_on_connect`` are
written the moment BlueZ hands the socket over, before anything else runs:
some devices change state soon after a connection opens and only answer a
request sent before then.

An open link reports the channel, service and adapter it used through
``get_extra_info()`` (``rfcomm_channel``, ``rfcomm_service``,
``rfcomm_adapter``), and is listed by :func:`open_rfcomm_links` while open.

Importing this module (or ``bluetooth_classic``) registers the handler with
serialx's asyncio API (``serialx.create_serial_connection()``); its blocking
``serial_for_url()`` refuses ``rfcomm://`` URLs. Everything after the link
opens (reading, writing, closing) is serialx's own socket handling.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from serialx import (
    BaseSerial,
    Parity,
    StopBits,
    create_serial_connection,
    register_uri_handler,
)
from serialx.platforms.serial_socket import SocketSerial, SocketSerialTransport

from .client import normalize_address
from .profile import connect_profile

_LOGGER = logging.getLogger(__name__)

SCHEME = "rfcomm://"
DEFAULT_CONNECT_TIMEOUT = 10.0

# The Bluetooth Base UUID, which 16- and 32-bit UUIDs are short forms of.
_BASE_UUID = UUID("00000000-0000-1000-8000-00805f9b34fb")
#: The Serial Port Profile.
SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")

_ADAPTER_RE = re.compile(r"hci\d+")


def service_uuid(service: UUID | str | int) -> UUID:
    """Return a service UUID from a UUID, its string, or a 16/32-bit short form.

    ``"1101"``, ``0x1101`` and ``"00001101-0000-1000-8000-00805f9b34fb"`` all
    give the Serial Port Profile.

    Raises:
        ValueError: Not a UUID.
    """
    if isinstance(service, UUID):
        return service
    if isinstance(service, int):
        return _from_short(service)
    text = service.strip().lower().removeprefix("0x")
    if len(text) in (4, 8):
        return _from_short(int(text, 16))
    return UUID(text)


def _from_short(value: int) -> UUID:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"Not a 16- or 32-bit UUID: {value:#x}")
    return UUID(int=_BASE_UUID.int | value << 96)


@dataclass(frozen=True, slots=True)
class RfcommTarget:
    """What an ``rfcomm://`` URL connects to."""

    address: str
    service: UUID
    #: The local adapter, such as ``hci0``; ``None`` lets BlueZ choose.
    adapter: str | None = None


@dataclass(frozen=True, slots=True)
class RfcommLink:
    """An open RFCOMM link."""

    address: str
    #: The RFCOMM channel BlueZ connected to, if known.
    channel: int | None
    #: The adapter it is on, such as ``hci0``.
    adapter: str | None
    service: UUID
    #: When it opened, in ``time.monotonic()`` seconds.
    since: float = field(default_factory=time.monotonic)


def rfcomm_url(
    address: str,
    *,
    service: UUID | str | int,
    adapter: str | None = None,
) -> str:
    """Return the ``rfcomm://`` URL of a device's service.

    Raises:
        ValueError: A service or adapter that is not valid.
    """
    target = RfcommTarget(normalize_address(address), service_uuid(service), adapter)
    _check(target, repr(target))
    params = {"service": str(target.service)}
    if adapter is not None:
        params["adapter"] = adapter
    return f"{SCHEME}{target.address}?{urllib.parse.urlencode(params)}"


def parse_rfcomm_url(url: str) -> RfcommTarget:
    """Return what an ``rfcomm://`` URL connects to.

    A Bluetooth address has colons, so the URL cannot be parsed as host and
    port; the rest are query parameters instead.

    Raises:
        ValueError: Not an ``rfcomm://`` URL with an address and a service,
            each parameter given once, known and valid.
    """
    if not url.startswith(SCHEME):
        raise ValueError(f"Not an rfcomm:// URL: {url}")
    address, _, query = url.removeprefix(SCHEME).partition("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    for name, values in params.items():
        if name not in ("service", "adapter"):
            raise ValueError(f"Unknown rfcomm:// parameter {name}: {url}")
        if len(values) != 1:
            raise ValueError(f"rfcomm:// {name} given more than once: {url}")
        if not values[0]:
            raise ValueError(f"rfcomm:// {name} is empty: {url}")
    if "service" not in params:
        raise ValueError(f"rfcomm:// needs a service: {url}")
    try:
        service = service_uuid(params["service"][0])
    except ValueError as err:
        raise ValueError(f"rfcomm:// service is not a UUID: {url}") from err
    adapter = params.get("adapter", [None])[0]
    target = RfcommTarget(normalize_address(address), service, adapter)
    _check(target, url)
    return target


def _check(target: RfcommTarget, url: str) -> None:
    if target.adapter is not None and not _ADAPTER_RE.fullmatch(target.adapter):
        raise ValueError(f"rfcomm:// adapter is a name such as hci0: {url}")


# The link registry.

_links: dict[int, RfcommLink] = {}
_links_lock = threading.Lock()
_listeners: list[Callable[[], None]] = []


def open_rfcomm_links() -> list[RfcommLink]:
    """Return the RFCOMM links this process has open through the handler."""
    with _links_lock:
        return list(_links.values())


def on_rfcomm_links_changed(callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` whenever a link opens or closes; returns an unsubscribe.

    It is called on the event loop.
    """
    _listeners.append(callback)

    def unsubscribe() -> None:
        if callback in _listeners:
            _listeners.remove(callback)

    return unsubscribe


def _add_link(key: int, link: RfcommLink) -> None:
    with _links_lock:
        _links[key] = link
    _notify()


def _remove_link(key: int) -> None:
    with _links_lock:
        if _links.pop(key, None) is None:
            return
    _notify()


def _notify() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:
            _LOGGER.exception("RFCOMM link listener failed")


# Opening links.

# Tasks dropping BlueZ's side of closed links; kept so they finish.
_releasing: set[asyncio.Task[None]] = set()


def _adapter_of(device_path: str) -> str | None:
    """Return the adapter in a device's object path, such as ``hci0``."""
    parts = device_path.split("/")
    return parts[3] if len(parts) > 3 and _ADAPTER_RE.fullmatch(parts[3]) else None


class RfcommSerial(SocketSerial):
    """Serial settings for an ``rfcomm://`` URL; opening it blocking is refused.

    BlueZ's profile API needs an event loop: use serialx's asyncio API.
    """

    def __init__(
        self,
        *args: Any,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        write_on_connect: bytes = b"",
        **kwargs: Any,
    ) -> None:
        """Initialise from an ``rfcomm://`` URL; the baud rate is ignored."""
        # SocketSerial's own initialiser parses a host and port.
        BaseSerial.__init__(self, *args, **kwargs)
        self._target = parse_rfcomm_url(str(self._path))
        self._connect_timeout = connect_timeout
        self._write_on_connect = bytes(write_on_connect)
        self._socket: socket.socket | None = None

    def _open(self) -> None:
        """Refuse: only serialx's asyncio API can open ``rfcomm://`` URLs."""
        raise io.UnsupportedOperation(
            "rfcomm:// supports only serialx's asyncio API (create_serial_connection)"
        )


class _RfcommProxyProtocol(asyncio.Protocol):
    """Passes the socket transport's events to the serial transport."""

    def __init__(self, serial_transport: RfcommSerialTransport) -> None:
        self._serial_transport = serial_transport

    def data_received(self, data: bytes) -> None:
        self._serial_transport._data_received(data)

    def pause_writing(self) -> None:
        self._serial_transport._pause_writing()

    def resume_writing(self) -> None:
        self._serial_transport._resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        self._serial_transport._tcp_connection_lost()
        self._serial_transport._connection_lost(exc)


class RfcommSerialTransport(SocketSerialTransport):
    """Serial transport over an RFCOMM service that BlueZ connected."""

    transport_name = "rfcomm"

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        """Initialise the transport."""
        super().__init__(loop, protocol)
        # Bytes received before the protocol is told the connection opened.
        self._early: list[bytes] = []
        # Why the link dropped, if it did before the protocol was attached.
        self._lost_before_attached: Exception | None = None
        # Drops BlueZ's side of the link once it closes; set while open.
        self._release: Callable[[], Any] | None = None

    async def _connect(  # type: ignore[override]
        self,
        *,
        path: str,
        baudrate: int,
        parity: Parity = Parity.NONE,
        stopbits: StopBits | int | float = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        byte_size: int = 8,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        write_on_connect: bytes = b"",
        **kwargs: Any,
    ) -> None:
        serial = RfcommSerial(
            path=path,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            byte_size=byte_size,
            connect_timeout=connect_timeout,
            write_on_connect=write_on_connect,
        )
        self._serial = serial
        self._extra["serial"] = serial
        self._tcp_connection_lost_waiter = self._loop.create_future()

        target = serial._target
        profile_link = await connect_profile(
            target.address,
            str(target.service),
            adapter=target.adapter,
            timeout=connect_timeout,
            write_on_connect=serial._write_on_connect,
        )
        handover = profile_link.handover
        self._release = profile_link.release
        try:
            sock = socket.socket(fileno=handover.fd)
        except BaseException:
            socket.close(handover.fd)
            await self._release_now()
            raise
        try:
            try:
                channel: int | None = sock.getpeername()[1]
            except (OSError, IndexError, TypeError):
                channel = None
            link = RfcommLink(
                target.address,
                channel,
                target.adapter or _adapter_of(handover.device_path),
                target.service,
            )
            sock.setblocking(False)
            transport, _ = await self._loop.create_connection(
                lambda: _RfcommProxyProtocol(self), sock=sock
            )
        except BaseException:
            sock.close()
            await self._release_now()
            raise
        self._tcp_transport = transport
        self._extra.update(
            rfcomm_channel=link.channel,
            rfcomm_service=link.service,
            rfcomm_adapter=link.adapter,
        )

        if self._connection_lost_called:
            # The device dropped the link before the protocol was attached:
            # fail the connect, as a plain socket connect would have.
            transport.close()
            await self._tcp_connection_lost_waiter
            self._tcp_transport = None
            raise self._lost_before_attached or ConnectionResetError(
                "The device closed the connection as it opened"
            )

        _add_link(id(self), link)
        self._connection_made()

    async def _release_now(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            await release()

    def _connection_made(self) -> None:
        """Tell the protocol the connection opened, then pass on held bytes."""
        self._call_protocol_connection_made()
        early, self._early = self._early, []
        for data in early:
            self._protocol.data_received(data)

    def _connection_lost(self, exc: Exception | None) -> None:
        """Drop BlueZ's side; note why if the protocol was not attached yet."""
        _remove_link(id(self))
        if self._release is not None:
            task = self._loop.create_task(self._release_now())
            _releasing.add(task)
            task.add_done_callback(_releasing.discard)
        if not self._connection_made_called and exc is not None:
            self._lost_before_attached = exc
        super()._connection_lost(exc)

    def _data_received(self, data: bytes) -> None:
        """Pass on received bytes, holding any that come before connection_made."""
        if not self._connection_made_called:
            self._early.append(data)
            return
        self._protocol.data_received(data)


def _register() -> None:
    """Register the handler, unless another copy of it already has."""
    try:
        register_uri_handler(
            scheme=SCHEME,
            unique_scheme=SCHEME,
            sync_cls=RfcommSerial,
            async_transport_cls=RfcommSerialTransport,
            connect_kwargs=frozenset({"connect_timeout", "write_on_connect"}),
        )
    except ValueError:
        # serialx refuses a second handler for a scheme; one registered by
        # another copy of this module (imported under another name, or
        # vendored) serves just as well.
        _LOGGER.debug("An rfcomm:// handler is already registered")


_register()


async def open_rfcomm(
    address: str,
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    service: UUID | str | int,
    adapter: str | None = None,
    timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    write_on_connect: bytes = b"",
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    """Open a device's RFCOMM service through serialx; attach a protocol.

    A shortcut for ``serialx.create_serial_connection()`` with an
    ``rfcomm://`` URL.

    Args:
        address: The device's address. BlueZ must know it (paired).
        protocol_factory: Returns the protocol to attach.
        service: The service to connect to, such as ``SERIAL_PORT``.
        adapter: The local adapter, such as ``hci0``; ``None`` lets BlueZ
            choose.
        timeout: Seconds to wait for the link to open.
        write_on_connect: Bytes written the moment BlueZ hands the
            connection over, before the protocol is attached. Replies wait
            until it is.

    Raises:
        ValueError: A service or adapter that is not valid.
        ProfileInUseError: Another program has registered the service.
        RfcommConnectError: BlueZ could not connect.
        TimeoutError: No connection within ``timeout`` seconds.
    """
    return await create_serial_connection(
        asyncio.get_running_loop(),
        protocol_factory,
        rfcomm_url(address, service=service, adapter=adapter),
        baudrate=9600,  # Required by serialx; RFCOMM has no baud rate.
        connect_timeout=timeout,
        write_on_connect=write_on_connect,
    )
