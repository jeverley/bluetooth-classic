# Usage

## Connecting

{class}`~bluetooth_classic.BluetoothClassic` is used as an async context
manager, like bleak's `BleakScanner` and `BleakClient`:

```python
async with BluetoothClassic(adapter="hci0") as bluetooth:
    ...
```

Entering the block connects to BlueZ on the system bus and chooses the
adapter: the one you name, or else the first powered one. Leaving it closes
the connection. An application that already has a system bus connection can
pass it as `bus`; it is then left open. The method names follow bleak's
where there is an equivalent.

Each call to BlueZ waits at most 10 seconds for an answer, except pairing,
which has its own timeout. A scan or pairing on a powered-off adapter raises
{class}`~bluetooth_classic.NoAdapterError`.

## Discovering devices

{meth}`~bluetooth_classic.BluetoothClassic.discover` runs a Classic-only
scan and returns the devices that answered, with their signal strength.
Only discoverable devices answer, and only devices with a Classic device
class are returned. Devices found become known to BlueZ, so
they can be paired, and {meth}`~bluetooth_classic.BluetoothClassic.devices`
lists them afterwards along with the paired ones.

Before pairing, a device lists only the services it advertises in a scan.
The full list is read when it is paired.

## Pairing and trust

{meth}`~bluetooth_classic.BluetoothClassic.pair` pairs with a known device.
A temporary agent answers BlueZ for that device only: it confirms "just
works" pairing and gives a PIN (`"0000"` by default) if the device asks for
one. Requests about any other device are rejected.

{meth}`~bluetooth_classic.BluetoothClassic.trust` marks the device trusted,
so BlueZ accepts its connections without asking. Some devices drop a
connection from a host that does not trust them.

{meth}`~bluetooth_classic.BluetoothClassic.unpair` removes a device from
BlueZ, forgetting its pairing and trust, as bleak's `unpair()` does on Linux.

## Finding one device

{meth}`~bluetooth_classic.BluetoothClassic.find_device_by_address` scans
until a device answers and returns it, or `None` if it does not answer in
time; the scan stops as soon as it answers. It never connects, so it does
not wake a device that wakes when connected to. A device answers only while
it is on and discoverable. Phones and computers usually are not, so this is
no way to tell whether a person is home.

Low Energy advertising and a link changing can also bring a signal strength
from BlueZ; those are not taken as an answer.

## Scanning and Low Energy

A Classic scan uses the same radio as Low Energy scanning and interrupts it
while it runs. BlueZ keeps a discovery filter for each client and merges
them, so a scan from this library does not stop another program's Low
Energy scanning, but it does pause it. The filter is cleared when the scan
ends. Keep scans short, and in an
application that also scans for Low Energy devices, let one part of it
decide when they run.

## Serial connections (RFCOMM)

RFCOMM carries a device's serial-style services, each on its own channel
(1 to 30): the Serial Port Profile (`1101`), Dial-up Networking (`1103`),
the OBEX services (object push, file transfer, phone book, messages) and
vendors' own services with 128-bit UUIDs. Audio, input devices and
networking (A2DP, AVRCP, HID, PAN) use L2CAP directly, not RFCOMM. Leave
Headset and Hands-Free (`1108`, `111e`) to BlueZ and the audio system.

Importing `bluetooth_classic` registers an `rfcomm://` handler with
[serialx](https://github.com/puddly/serialx), so its functions open a
device's RFCOMM service like any serial port:

```python
transport, protocol = await serialx.create_serial_connection(
    loop, MyProtocol, "rfcomm://00:11:22:33:44:55?service=1101", baudrate=9600
)
channel = transport.get_extra_info("rfcomm_channel")
```

The baud rate is required by serialx and ignored. The URL takes a service,
a channel, or both:

- `service`: the service's UUID, or its 16- or 32-bit short form (`1101`).
  Its channel is looked up in the device's SDP records.
- `channel`: the RFCOMM channel, 1 to 30. With a service as well, the
  channel is tried first; if the device refuses it, the service is looked
  up once and the channel found is used.
- `adapter` (optional): the local adapter, such as `hci0`. Without it,
  Linux chooses.
- `security` (optional): `low`, `medium` (an authenticated, encrypted link,
  so the device must be paired) or `high`. Without it, Linux's default is
  left alone. A level that cannot be set fails the connection.

The handler keeps nothing between connections. A lookup is itself a short
Bluetooth connection, and can wake a device that wakes when connected to,
so keep the channel a connection used (`get_extra_info("rfcomm_channel")`)
and pass it with the service next time: the lookup is then only made when
the device has moved the service.

An open connection also reports `rfcomm_service` and `rfcomm_adapter`
through `get_extra_info()`. {func}`~bluetooth_classic.open_rfcomm_links`
lists the links open through the handler, and
{func}`~bluetooth_classic.on_rfcomm_links_changed` calls back whenever one
opens or closes.

Two connection options are also accepted: `connect_timeout`, which covers a
lookup too, and `write_on_connect`, bytes written by the thread that opens
the link the moment it opens, before the event loop gets to it. Use it for
a device that changes state soon after a connection opens and only answers
a request sent before then.

{func}`~bluetooth_classic.open_rfcomm` is a shortcut that builds the URL.
{func}`~bluetooth_classic.rfcomm_supported` says whether this Python can
open RFCOMM sockets at all; it needs Linux and a Python built with
Bluetooth support.

## Looking up services

{func}`~bluetooth_classic.find_rfcomm_services` lists the services a device
offers over RFCOMM, with their channels and names, and
{func}`~bluetooth_classic.find_rfcomm_channel` returns one service's
channel, or `None` if the device does not offer it. They ask the device's
SDP server directly, over an L2CAP connection with Python's own Bluetooth
sockets; BlueZ's D-Bus interface does not give a service's channel. Each
has a blocking form (`find_rfcomm_services_blocking`,
`find_rfcomm_channel_blocking`) for code already in a worker thread. A
device that cannot be reached, or answers wrongly, raises
{class}`~bluetooth_classic.ServiceLookupError`.
