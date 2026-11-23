# Usage

## Connecting

{meth}`~bluetooth_classic.BluetoothClassic.connect` opens a connection to
BlueZ on the system bus and chooses an adapter: the one you name, such as
`"hci0"`, or else the first powered one. Use it as an async context manager
so the connection is closed afterwards. An application that already has a
system bus connection can pass it as `bus`; it is then left open.

## Discovering devices

{meth}`~bluetooth_classic.BluetoothClassic.discover` runs a Classic-only
scan and returns the devices that answered, with their signal strength.
Only discoverable devices answer. Devices found become known to BlueZ, so
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

## Checking presence

{meth}`~bluetooth_classic.BluetoothClassic.check_presence` runs a short
Classic scan and reports whether a known device answered, and how soon. It
never connects, so it does not wake a device that wakes when connected to.
A device answers only while it is on and discoverable.

Low Energy advertising and a link changing can also bring a signal strength
from BlueZ; those are not taken as an answer.

## Scanning and Low Energy

A Classic scan uses the same radio as Low Energy scanning and interrupts it
while it runs. BlueZ keeps a discovery filter for each client and merges
them, so a scan from this library does not stop another program's Low
Energy scanning, but it does pause it. Keep scans short, and in an
application that also scans for Low Energy devices, let one part of it
decide when they run.
