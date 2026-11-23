"""Constants for talking to BlueZ about Bluetooth Classic devices."""

from __future__ import annotations

from typing import Final

BLUEZ_SERVICE: Final = "org.bluez"
BLUEZ_PATH: Final = "/org/bluez"
ADAPTER_INTERFACE: Final = "org.bluez.Adapter1"
DEVICE_INTERFACE: Final = "org.bluez.Device1"
AGENT_INTERFACE: Final = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE: Final = "org.bluez.AgentManager1"
OBJECT_MANAGER_INTERFACE: Final = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_INTERFACE: Final = "org.freedesktop.DBus.Properties"
DBUS_SERVICE: Final = "org.freedesktop.DBus"
DBUS_PATH: Final = "/org/freedesktop/DBus"

# Where the pairing agent is exported on this library's bus connection.
AGENT_PATH: Final = "/org/bluetooth_classic/agent"
# The agent's capability: pairing needs no display or keyboard, which suits
# speakers, headphones and most other Classic devices without a screen.
AGENT_CAPABILITY: Final = "NoInputNoOutput"
# Answered when a device asks for a PIN; the usual default of older audio
# devices.
DEFAULT_PIN: Final = "0000"

# How long a discovery runs by default.
DISCOVERY_SECONDS: Final = 12.0
# How long a presence check waits for a device to answer. A device that is on
# and discoverable answers a Classic inquiry within a few seconds.
PRESENCE_SECONDS: Final = 5.0
# How long pairing may take before it is cancelled.
PAIR_TIMEOUT: Final = 45.0

# The discovery filter value that limits discovery to Bluetooth Classic.
TRANSPORT_BREDR: Final = "bredr"

# Device properties that come with a signal strength for reasons other than
# answering a Classic inquiry: Low Energy advertising, or a link changing.
NOT_INQUIRY: Final = frozenset(
    {"ManufacturerData", "AdvertisingFlags", "ServiceData", "Connected"}
)
