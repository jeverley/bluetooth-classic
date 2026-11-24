"""Finding a device's RFCOMM channel for a service, with sdptool."""

from __future__ import annotations

import asyncio
import logging
import re

from .client import normalize_address

_LOGGER = logging.getLogger(__name__)

SDPTOOL_TIMEOUT = 15.0
# The Serial Port Profile, by sdptool's name for it.
SERIAL_PORT = "SP"

_CHANNEL_RE = re.compile(r"Channel:\s*(\d+)")


async def find_rfcomm_channel(
    address: str, service: str = SERIAL_PORT, *, timeout: float = SDPTOOL_TIMEOUT
) -> int | None:
    """Look up the RFCOMM channel a device offers a service on.

    Asks the device's SDP server with ``sdptool``, from BlueZ's deprecated
    tools; BlueZ's D-Bus interface does not give a service's channel. Some
    systems, such as Home Assistant's container, still ship it.

    Args:
        address: The device's address.
        service: An sdptool service name, such as ``"SP"``, or a UUID.
        timeout: Seconds to wait for the answer.

    Returns:
        The channel, or ``None`` if sdptool is missing, the lookup fails or
        the device does not offer the service. Callers can then try likely
        channels in turn.
    """
    address = normalize_address(address)
    try:
        process = await asyncio.create_subprocess_exec(
            "sdptool",
            "search",
            "--bdaddr",
            address,
            service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError):
        _LOGGER.debug("sdptool is not available")
        return None
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await process.communicate()
    except TimeoutError:
        _LOGGER.debug("sdptool timed out looking up %s on %s", service, address)
        return None
    finally:
        # Timed out or cancelled: stop sdptool rather than leave it running.
        if process.returncode is None:
            process.kill()
            await process.wait()
    if match := _CHANNEL_RE.search(stdout.decode(errors="replace")):
        return int(match.group(1))
    _LOGGER.debug("sdptool found no %s service on %s", service, address)
    return None
