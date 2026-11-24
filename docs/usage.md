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

RFCOMM carries a device's serial-style services: the Serial Port Profile
(`1101`), Dial-up Networking (`1103`), the OBEX services and vendors' own
services with 128-bit UUIDs. Audio, input devices and networking (A2DP,
AVRCP, HID, PAN) use L2CAP directly, not RFCOMM. Leave Headset and
Hands-Free (`1108`, `111e`) to BlueZ and the audio system.

Importing `bluetooth_classic` registers an `rfcomm://` handler with
[serialx](https://github.com/puddly/serialx)'s asyncio API, so it opens a
device's RFCOMM service like any serial port:

```python
transport, protocol = await serialx.create_serial_connection(
    loop, MyProtocol, "rfcomm://00:11:22:33:44:55?service=1101", baudrate=9600
)
```

The baud rate is required by serialx and ignored. The URL takes:

- `service`: the service's UUID, or its 16- or 32-bit short form (`1101`).
- `adapter` (optional): the local adapter, such as `hci0`. Without it,
  BlueZ chooses.

The device must be known to BlueZ (paired). serialx's blocking
`serial_for_url()` refuses `rfcomm://` URLs.

Link security is BlueZ's for client profiles: there is no option for it.
Whether BlueZ asks for an encrypted link by default is unconfirmed.

### How it connects

BlueZ makes the connection, through its profile API, as it makes bleak's
connections: the handler registers as a client of the service, asks BlueZ
to connect it on the device, and BlueZ finds the channel and hands over the
connected socket over D-Bus. No Bluetooth sockets of Python's own are
needed. When the link closes, the handler asks BlueZ to drop its side too
(BlueZ keeps its own reference to the connection), and unregisters the
service once no link uses it.

BlueZ accepts one registration per service UUID, so while one of the
handler's links is open, another program cannot connect to that service
through BlueZ, and the other way round:
{class}`~bluetooth_classic.ProfileInUseError` says so. Links within one
process share the registration. A process has at most one link to a
device's service at a time: opening a second, while the first is open or
opening, raises {class}`~bluetooth_classic.RfcommConnectError`.

Two connection options are also accepted: `connect_timeout`, which bounds
the whole open, and
`write_on_connect`, bytes written the moment BlueZ hands the connection
over, before anything else runs. Use it for a device that changes state
soon after a connection opens and only answers a request sent before then.

An open connection reports the channel BlueZ connected to, the service and
the adapter through `get_extra_info()` (`rfcomm_channel`, `rfcomm_service`,
`rfcomm_adapter`). {func}`~bluetooth_classic.open_rfcomm_links` lists the
links open through the handler, and
{func}`~bluetooth_classic.on_rfcomm_links_changed` calls back whenever one
opens or closes.

{func}`~bluetooth_classic.open_rfcomm` is a shortcut that builds the URL.
