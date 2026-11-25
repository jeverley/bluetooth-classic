# Usage

## Connecting

{class}`~bluetooth_classic.BluetoothClassic` is used as an async context
manager, like bleak's `BleakScanner` and `BleakClient`:

```python
async with BluetoothClassic(adapter="hci0") as bluetooth:
    ...
```

Entering the block connects to BlueZ on the system bus and chooses the
adapter: the one you name, or else BlueZ's first, powered or not, which is
the one aio-rfcomm opens RFCOMM links on. Leaving it closes
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

## RFCOMM links

RFCOMM carries a device's serial-style services: the Serial Port Profile
(`1101`), Dial-up Networking (`1103`), the OBEX services and vendors' own
services with 128-bit UUIDs. Audio, input devices and networking (A2DP,
AVRCP, HID, PAN) use L2CAP directly, not RFCOMM. Leave Headset and
Hands-Free (`1108`, `111e`) to BlueZ and the audio system.

{class}`~bluetooth_classic.RfcommClient` connects to a device's RFCOMM
service, shaped like bleak's `BleakClient`: data the device sends arrives at
`data_callback` from the first byte, and `disconnected_callback` is called
with the client when the link ends (`disconnect_reason` says why).

```python
client = RfcommClient(
    "00:11:22:33:44:55",
    service=SERIAL_PORT,
    data_callback=on_data,
    disconnected_callback=on_disconnected,
)
await establish_rfcomm_connection(client, write_on_connect=QUERY)
client.write_nowait(b"...")  # queue and return
await client.write(b"...")  # wait until it is on its way
await client.disconnect()
```

{func}`~bluetooth_classic.establish_rfcomm_connection` connects it, as
bleak-retry-connector's `establish_connection()` does: just after a link
closes, BlueZ can refuse a new one for a moment, and it tries again (four
times, a second apart) while the error says the device is busy. Such
errors are raised as {class}`~bluetooth_classic.RfcommConnectError` with
`errno` `EBUSY`; which error BlueZ gives is unconfirmed. `client.connect()`
tries once.

Underneath, {func}`~bluetooth_classic.create_rfcomm_connection` attaches an
asyncio protocol instead, as `loop.create_connection()` does for a socket,
and returns `(transport, protocol)`.

`service` is the service's UUID, or its 16- or 32-bit short form (`1101`).
The device must be known to BlueZ (paired).

aio-rfcomm uses BlueZ's first adapter; there is no option to choose one.
{class}`~bluetooth_classic.BluetoothClassic` uses the same one unless you
name another, so a device it pairs can be connected. Pairing on another
adapter you name leaves the device unreachable by RFCOMM links.

Link security is BlueZ's for client profiles: there is no option for it.
Whether BlueZ asks for an encrypted link by default is unconfirmed.

### How it connects

[aio-rfcomm](https://github.com/dlech/aio-rfcomm) makes the connection. On
Linux it registers with BlueZ as a client of the service, asks BlueZ to
connect it on the device, and BlueZ finds the channel and hands over the
connected socket over D-Bus. Links within one process share the
registration; BlueZ accepts one per service UUID, so while one is open,
another program cannot connect to that service through BlueZ.

aio-rfcomm allows one Bluetooth object per process. This library opens it on
first use and keeps it, on an event loop of its own in a background thread,
and every caller in the process shares it: open links through
this library rather than opening aio-rfcomm
directly. If something else in the process has already opened aio-rfcomm,
connecting raises {class}`~bluetooth_classic.RfcommConnectError`. A process has at
most one link to a device's service at a time: opening a second, while the
first is open or opening, raises it too.

Two options are also accepted: `timeout`, which bounds the whole open, and
`write_on_connect`, bytes sent from the library's own loop the moment the channel opens, however busy the application's loop is.
Use it for a device that changes state soon after a connection opens and
only answers a request sent before then.

When the device or Bluetooth ends a link, the protocol's `connection_lost()`
gets a `ConnectionResetError` (`ECONNRESET`): aio-rfcomm does not say why a
link ended, as RFCOMM reports even an orderly hang-up as a reset. Bluetooth
itself going away gives a `ConnectionAbortedError` instead.

Closing sends what is queued first, waiting at most two seconds for a
device that has stopped reading; `abort()` drops it.

An open link reports its address and service through `get_extra_info()`
(`rfcomm_address`, `rfcomm_service`). {func}`~bluetooth_classic.open_rfcomm_links`
lists the open links, and
{func}`~bluetooth_classic.on_rfcomm_links_changed` calls back whenever one
opens or closes.
