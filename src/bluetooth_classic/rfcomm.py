"""A serialx handler for Bluetooth Classic RFCOMM: ``rfcomm://`` URLs.

RFCOMM carries a device's serial-style services, each on its own channel
(1 to 30). A URL names the device and the service, the channel, or both:

- ``rfcomm://00:11:22:33:44:55?service=1101`` looks the Serial Port
  service's channel up in the device's SDP records, then connects to it.
- ``rfcomm://00:11:22:33:44:55?channel=1`` connects to channel 1.
- ``rfcomm://00:11:22:33:44:55?service=1101&channel=1`` connects to
  channel 1, and if the device refuses it, looks the service up once and
  connects to the channel found. Callers that keep the channel a
  connection used (``get_extra_info("rfcomm_channel")``) pass it this way
  next time, so a lookup, itself a short connection, is only made when
  needed.

``service`` is a UUID, or its 16- or 32-bit short form. ``adapter`` (such
as ``hci0``) chooses the local adapter. ``security`` asks Linux for a
level of link security: ``low`` is Linux's default; ``medium`` needs an
authenticated (paired) and encrypted link; ``high`` also needs a PIN or
passkey to have been used. Without it, Linux's default is left alone.

The link is opened in a worker thread. Bytes given as ``write_on_connect``
are written by that thread the moment the link opens, without waiting for
the event loop: some devices change state soon after a connection opens
and only answer a request sent before then.

An open link reports the channel, service and adapter it used through
``get_extra_info()`` (``rfcomm_channel``, ``rfcomm_service``,
``rfcomm_adapter``), and is listed by :func:`open_rfcomm_links` while open.

Importing this module (or ``bluetooth_classic``) registers the handler, so
``serialx.create_serial_connection()`` and ``serialx.serial_for_url()``
accept ``rfcomm://`` URLs.

Everything after the link opens (reading, writing, timeouts, closing) is
serialx's own socket handling.

Python's socket module supports RFCOMM on Linux when it is built against the
BlueZ headers. No PyBluez or other compiled dependency is needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
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
from .exceptions import RfcommUnsupportedError, ServiceLookupError
from .sdp import (
    adapter_address,
    adapter_name,
    find_rfcomm_channel_blocking,
    service_uuid,
)

_LOGGER = logging.getLogger(__name__)

SCHEME = "rfcomm://"
DEFAULT_CONNECT_TIMEOUT = 10.0

# From <bluetooth/bluetooth.h>. Python's socket module has no names for these.
SOL_BLUETOOTH = 274
BT_SECURITY = 4

_ADAPTER_RE = re.compile(r"hci\d+")


class Security(IntEnum):
    """Link security levels, as Linux numbers them (BT_SECURITY_*)."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True, slots=True)
class RfcommTarget:
    """What an ``rfcomm://`` URL connects to."""

    address: str
    #: The service to look up the channel of.
    service: UUID | None = None
    #: The channel to try first, or only, if no service is given.
    channel: int | None = None
    security: Security | None = None
    #: The local adapter, such as ``hci0``; ``None`` lets Linux choose.
    adapter: str | None = None


@dataclass(frozen=True, slots=True)
class RfcommLink:
    """An open RFCOMM link."""

    address: str
    channel: int
    #: The adapter it is on, such as ``hci0``, if known.
    adapter: str | None
    #: The service it was opened for, if one was given.
    service: UUID | None
    #: When it opened, in ``time.monotonic()`` seconds.
    since: float = field(default_factory=time.monotonic)


def rfcomm_supported() -> bool:
    """Return whether this Python can open RFCOMM sockets."""
    return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


def rfcomm_url(
    address: str,
    *,
    service: UUID | str | int | None = None,
    channel: int | None = None,
    security: Security | None = None,
    adapter: str | None = None,
) -> str:
    """Return the ``rfcomm://`` URL of a device's service or channel.

    Raises:
        ValueError: Neither a service nor a channel, or one not valid.
    """
    target = RfcommTarget(
        normalize_address(address),
        None if service is None else service_uuid(service),
        channel,
        security,
        adapter,
    )
    _check(target, repr(target))
    params: dict[str, str] = {}
    if target.service is not None:
        params["service"] = str(target.service)
    if channel is not None:
        params["channel"] = str(channel)
    if security is not None:
        params["security"] = security.name.lower()
    if adapter is not None:
        params["adapter"] = adapter
    return f"{SCHEME}{target.address}?{urllib.parse.urlencode(params)}"


def parse_rfcomm_url(url: str) -> RfcommTarget:
    """Return what an ``rfcomm://`` URL connects to.

    A Bluetooth address has colons, so the URL cannot be parsed as host and
    port; the rest are query parameters instead.

    Raises:
        ValueError: Not an ``rfcomm://`` URL with an address and a service
            or a channel from 1 to 30, each parameter given at most once and
            valid.
    """
    if not url.startswith(SCHEME):
        raise ValueError(f"Not an rfcomm:// URL: {url}")
    address, _, query = url.removeprefix(SCHEME).partition("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    for name, values in params.items():
        if name not in ("service", "channel", "security", "adapter"):
            raise ValueError(f"Unknown rfcomm:// parameter {name}: {url}")
        if len(values) != 1:
            raise ValueError(f"rfcomm:// {name} given more than once: {url}")
        if not values[0]:
            raise ValueError(f"rfcomm:// {name} is empty: {url}")
    value = {name: values[0] for name, values in params.items()}
    try:
        service = service_uuid(value["service"]) if "service" in value else None
    except ValueError as err:
        raise ValueError(f"rfcomm:// service is not a UUID: {url}") from err
    channel = value.get("channel")
    if channel is not None and not channel.isdigit():
        raise ValueError(f"rfcomm:// channel is a number: {url}")
    level = value.get("security")
    if level is not None and level.upper() not in Security.__members__:
        raise ValueError(f"rfcomm:// security is low, medium or high: {url}")
    target = RfcommTarget(
        normalize_address(address),
        service,
        None if channel is None else int(channel),
        None if level is None else Security[level.upper()],
        value.get("adapter"),
    )
    _check(target, url)
    return target


def _check(target: RfcommTarget, url: str) -> None:
    if target.service is None and target.channel is None:
        raise ValueError(f"rfcomm:// needs a service or a channel: {url}")
    if target.channel is not None and not 1 <= target.channel <= 30:
        raise ValueError(f"RFCOMM channels are 1 to 30: {url}")
    if target.adapter is not None and not _ADAPTER_RE.fullmatch(target.adapter):
        raise ValueError(f"rfcomm:// adapter is a name such as hci0: {url}")


# The link registry.

_links: dict[int, RfcommLink] = {}
_links_lock = threading.Lock()
_listeners: list[Callable[[], None]] = []


def open_rfcomm_links() -> list[RfcommLink]:
    """Return the RFCOMM links this process has open through the handler.

    An async link is removed when it drops or is closed. A blocking
    ``RfcommSerial`` link is listed until the port is closed, even if the
    device has dropped it.
    """
    with _links_lock:
        return list(_links.values())


def on_rfcomm_links_changed(callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` whenever a link opens or closes; returns an unsubscribe.

    It is called in the thread that opened or closed the link: the event
    loop's for connections made with ``open_rfcomm()`` or serialx's async
    API.
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


def _rfcomm_socket(security: Security | None) -> socket.socket:
    """Return a new RFCOMM socket, at the security level asked for."""
    if not rfcomm_supported():
        raise RfcommUnsupportedError(
            "This Python was built without Bluetooth socket support"
        )
    # Only defined where Python was built with Bluetooth support, so not in
    # every platform's type stubs.
    sock = socket.socket(
        socket.AF_BLUETOOTH,  # type: ignore[attr-defined, unused-ignore]
        socket.SOCK_STREAM,
        socket.BTPROTO_RFCOMM,  # type: ignore[attr-defined, unused-ignore]
    )
    if security is not None:
        try:
            # struct bt_security: the level, and the key size (0: any).
            sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", security, 0))
        except BaseException:
            # Asked for, so not to be skipped quietly.
            sock.close()
            raise
    return sock


def _connect(
    target: RfcommTarget, channel: int, timeout: float | None
) -> socket.socket:
    """Return a socket connected to one channel."""
    sock = _rfcomm_socket(target.security)
    try:
        sock.settimeout(timeout)
        if target.adapter is not None:
            sock.bind((adapter_address(target.adapter), 0))
        sock.connect((target.address, channel))
    except BaseException:
        sock.close()
        raise
    return sock


def _look_up(target: RfcommTarget, timeout: float | None) -> int:
    """Return the channel of the target's service."""
    assert target.service is not None
    channel = find_rfcomm_channel_blocking(
        target.address,
        target.service,
        adapter=target.adapter,
        timeout=10.0 if timeout is None else timeout,
    )
    if channel is None:
        raise ServiceLookupError(
            f"{target.address} does not offer service {target.service}"
        )
    _LOGGER.debug(
        "%s offers service %s on channel %s", target.address, target.service, channel
    )
    return channel


def _open_link(
    target: RfcommTarget, timeout: float | None, write_on_connect: bytes
) -> tuple[socket.socket, RfcommLink]:
    """Open the link and write write_on_connect at once; runs in a worker thread.

    Returns the connected socket and the link it is. ``timeout`` bounds the
    whole open, a lookup included. Describing the link reads sysfs, so it is
    done here, off the event loop.
    """
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining() -> float | None:
        if deadline is None:
            return None
        if (left := deadline - time.monotonic()) <= 0:
            raise TimeoutError(f"Timed out opening a link to {target.address}")
        return left

    looked_up = target.channel is None
    channel = _look_up(target, remaining()) if looked_up else target.channel
    assert channel is not None
    try:
        sock = _connect(target, channel, remaining())
    except ConnectionRefusedError:
        # Nothing listens on the channel given: the device may have moved
        # the service. Look it up once.
        if looked_up or target.service is None:
            raise
        _LOGGER.debug(
            "%s refused channel %s; looking up service %s",
            target.address,
            channel,
            target.service,
        )
        found = _look_up(target, remaining())
        if found == channel:
            raise
        channel = found
        sock = _connect(target, channel, remaining())
    try:
        if write_on_connect:
            sock.sendall(write_on_connect)
        link = _link(target, sock, channel)
    except BaseException:
        sock.close()
        raise
    return sock, link


def _link(target: RfcommTarget, sock: socket.socket, channel: int) -> RfcommLink:
    """Describe a newly opened link."""
    try:
        local = sock.getsockname()[0]
    except (OSError, IndexError, TypeError):
        local = None
    adapter = target.adapter or (adapter_name(local) if local else None)
    return RfcommLink(target.address, channel, adapter, target.service)


def _close_after(job: asyncio.Future[tuple[socket.socket, RfcommLink]]) -> None:
    """Close the socket of an abandoned open once its thread is done."""
    if job.cancelled():
        return
    if (err := job.exception()) is not None:
        _LOGGER.debug("Abandoned connection attempt ended: %r", err)
        return
    job.result()[0].close()


class RfcommSerial(SocketSerial):
    """Blocking serial interface over an RFCOMM channel."""

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
        #: The link while open.
        self.link: RfcommLink | None = None

    def _open(self) -> None:
        """Open the link and write write_on_connect."""
        sock, link = _open_link(
            self._target, self._connect_timeout, self._write_on_connect
        )
        self._socket = sock
        self.link = link
        _add_link(id(self), link)

    def _close(self) -> None:
        """Close the link."""
        super()._close()
        self.link = None
        _remove_link(id(self))


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
    """Serial transport over an RFCOMM channel."""

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
        # Fails here, not in the thread, when Python has no Bluetooth sockets.
        if not rfcomm_supported():
            raise RfcommUnsupportedError(
                "This Python was built without Bluetooth socket support"
            )
        job = self._loop.run_in_executor(
            None, _open_link, target, connect_timeout, serial._write_on_connect
        )
        try:
            # Shielded: a cancelled open is left to finish, then closed.
            sock, link = await asyncio.shield(job)
        except BaseException:
            # Also when the open finished just as this was cancelled: the
            # callback then runs at once and closes the socket.
            job.add_done_callback(_close_after)
            raise
        try:
            sock.setblocking(False)
            transport, _ = await self._loop.create_connection(
                lambda: _RfcommProxyProtocol(self), sock=sock
            )
        except BaseException:
            sock.close()
            raise
        self._tcp_transport = transport
        self._extra.update(
            rfcomm_channel=link.channel,
            rfcomm_service=target.service,
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

    def _connection_made(self) -> None:
        """Tell the protocol the connection opened, then pass on held bytes."""
        self._call_protocol_connection_made()
        early, self._early = self._early, []
        for data in early:
            self._protocol.data_received(data)

    def _connection_lost(self, exc: Exception | None) -> None:
        """Note why the link dropped if the protocol was not attached yet."""
        _remove_link(id(self))
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
    service: UUID | str | int | None = None,
    channel: int | None = None,
    adapter: str | None = None,
    timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    write_on_connect: bytes = b"",
    security: Security | None = None,
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    """Open an RFCOMM service or channel through serialx; attach a protocol.

    A shortcut for ``serialx.create_serial_connection()`` with an
    ``rfcomm://`` URL. The transport's ``get_extra_info("rfcomm_channel")``
    is the channel used, to pass as ``channel`` next time.

    Args:
        address: The device's address.
        protocol_factory: Returns the protocol to attach.
        service: The service to connect to; its channel is looked up if no
            channel is given, or if the device refuses the one given.
        channel: The RFCOMM channel to connect to, 1 to 30.
        adapter: The local adapter, such as ``hci0``; ``None`` lets Linux
            choose.
        timeout: Seconds to wait for the link to open, a lookup included.
        write_on_connect: Bytes written the moment the link opens, before the
            protocol is attached. Replies wait until it is.
        security: The link security to ask for; ``None`` leaves Linux's
            default.

    Raises:
        ValueError: Neither a service nor a channel, or one not valid.
        NoAdapterError: The adapter asked for does not exist.
        RfcommUnsupportedError: This Python cannot open RFCOMM sockets.
        ServiceLookupError: The service's channel could not be looked up, or
            the device does not offer it.
        OSError: The link could not be opened.
    """
    return await create_serial_connection(
        asyncio.get_running_loop(),
        protocol_factory,
        rfcomm_url(
            address,
            service=service,
            channel=channel,
            security=security,
            adapter=adapter,
        ),
        baudrate=9600,  # Required by serialx; RFCOMM has no baud rate.
        connect_timeout=timeout,
        write_on_connect=write_on_connect,
    )
