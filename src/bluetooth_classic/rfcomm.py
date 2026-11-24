"""A serialx handler for Bluetooth Classic RFCOMM: ``rfcomm://`` URLs.

``rfcomm://00:11:22:33:44:55?channel=1`` opens RFCOMM channel 1 of that
device as a serial connection, with Python's own Bluetooth sockets.

``security`` asks Linux for a level of link security:
``rfcomm://00:11:22:33:44:55?channel=1&security=medium``. ``low`` is
Linux's default; ``medium`` needs an authenticated (paired) and encrypted
link; ``high`` also needs a PIN or passkey to have been used. Without it,
Linux's default is left alone.

The link is opened in a worker thread. Bytes given as ``write_on_connect``
are written by that thread the moment the link opens, without waiting for
the event loop: some devices change state soon after a connection opens
and only answer a request sent before then.

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
import socket
import struct
import urllib.parse
from collections.abc import Callable
from enum import IntEnum
from typing import Any

from serialx import (
    BaseSerial,
    Parity,
    StopBits,
    create_serial_connection,
    register_uri_handler,
)
from serialx.platforms.serial_socket import SocketSerial, SocketSerialTransport

from .client import normalize_address
from .exceptions import RfcommUnsupportedError

_LOGGER = logging.getLogger(__name__)

SCHEME = "rfcomm://"
DEFAULT_CONNECT_TIMEOUT = 10.0

# From <bluetooth/bluetooth.h>. Python's socket module has no names for these.
SOL_BLUETOOTH = 274
BT_SECURITY = 4


class Security(IntEnum):
    """Link security levels, as Linux numbers them (BT_SECURITY_*)."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


def rfcomm_supported() -> bool:
    """Return whether this Python can open RFCOMM sockets."""
    return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


def rfcomm_url(address: str, channel: int, security: Security | None = None) -> str:
    """Return the ``rfcomm://`` URL of a device's RFCOMM channel."""
    url = f"{SCHEME}{normalize_address(address)}?channel={channel}"
    if security is not None:
        url += f"&security={security.name.lower()}"
    return url


def parse_rfcomm_url(url: str) -> tuple[str, int, Security | None]:
    """Return the address, channel and security of an ``rfcomm://`` URL.

    A Bluetooth address has colons, so the URL cannot be parsed as host and
    port; the channel is a query parameter instead.

    Raises:
        ValueError: Not an ``rfcomm://`` URL with an address, a channel from
            1 to 30, and at most one known security level.
    """
    if not url.startswith(SCHEME):
        raise ValueError(f"Not an rfcomm:// URL: {url}")
    address, _, query = url.removeprefix(SCHEME).partition("?")
    params = urllib.parse.parse_qs(query)
    channels = params.get("channel", [])
    if len(channels) != 1 or not channels[0].isdigit():
        raise ValueError(f"rfcomm:// URL needs one channel: {url}")
    channel = int(channels[0])
    if not 1 <= channel <= 30:
        raise ValueError(f"RFCOMM channels are 1 to 30: {url}")
    levels = params.get("security", [])
    if len(levels) > 1 or (levels and levels[0].upper() not in Security.__members__):
        raise ValueError(f"rfcomm:// security is low, medium or high: {url}")
    security = Security[levels[0].upper()] if levels else None
    return normalize_address(address), channel, security


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


def _connect_and_write(
    sock: socket.socket,
    address: str,
    channel: int,
    timeout: float | None,
    write_on_connect: bytes,
) -> None:
    """Connect, then write write_on_connect at once; runs in a worker thread."""
    sock.settimeout(timeout)
    sock.connect((address, channel))
    if write_on_connect:
        sock.sendall(write_on_connect)


def _close_after(job: asyncio.Future[None], sock: socket.socket) -> None:
    """Close the socket of an abandoned connect once its thread is done."""
    if not job.cancelled() and (err := job.exception()) is not None:
        _LOGGER.debug("Abandoned connection attempt ended: %r", err)
    sock.close()


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
        self._address, self._channel, self._security = parse_rfcomm_url(str(self._path))
        self._connect_timeout = connect_timeout
        self._write_on_connect = bytes(write_on_connect)
        self._socket: socket.socket | None = None

    def _open(self) -> None:
        """Open the link and write write_on_connect."""
        sock = _rfcomm_socket(self._security)
        try:
            _connect_and_write(
                sock,
                self._address,
                self._channel,
                self._connect_timeout,
                self._write_on_connect,
            )
        except BaseException:
            sock.close()
            raise
        self._socket = sock


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

        sock = _rfcomm_socket(serial._security)
        job = self._loop.run_in_executor(
            None,
            _connect_and_write,
            sock,
            serial._address,
            serial._channel,
            connect_timeout,
            serial._write_on_connect,
        )
        try:
            # Shielded: cancelling must not close the socket under the thread.
            await asyncio.shield(job)
            sock.setblocking(False)
            transport, _ = await self._loop.create_connection(
                lambda: _RfcommProxyProtocol(self), sock=sock
            )
        except BaseException:
            if job.done():
                sock.close()
            else:
                # Cancelled while the thread still uses the socket: close it
                # once the thread has finished with it.
                job.add_done_callback(lambda done: _close_after(done, sock))
            raise
        self._tcp_transport = transport

        if self._connection_lost_called:
            transport.close()
            await self._tcp_connection_lost_waiter
            self._tcp_transport = None
            return

        self._connection_made()

    def _connection_made(self) -> None:
        """Tell the protocol the connection opened, then pass on held bytes."""
        self._call_protocol_connection_made()
        early, self._early = self._early, []
        for data in early:
            self._protocol.data_received(data)

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
    channel: int,
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    write_on_connect: bytes = b"",
    security: Security | None = None,
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    """Open an RFCOMM channel through serialx and attach a protocol to it.

    A shortcut for ``serialx.create_serial_connection()`` with an
    ``rfcomm://`` URL.

    Args:
        address: The device's address.
        channel: The RFCOMM channel, 1 to 30.
        protocol_factory: Returns the protocol to attach.
        timeout: Seconds to wait for the link to open.
        write_on_connect: Bytes written the moment the link opens, before the
            protocol is attached. Replies wait until it is.
        security: The link security to ask for; ``None`` leaves Linux's
            default.

    Raises:
        RfcommUnsupportedError: This Python cannot open RFCOMM sockets.
        OSError: The link could not be opened.
    """
    return await create_serial_connection(
        asyncio.get_running_loop(),
        protocol_factory,
        rfcomm_url(address, channel, security),
        baudrate=9600,  # Required by serialx; RFCOMM has no baud rate.
        connect_timeout=timeout,
        write_on_connect=write_on_connect,
    )
