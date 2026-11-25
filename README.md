# Bluetooth Classic

<p align="center">
  <a href="https://github.com/jeverley/bluetooth-classic/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/jeverley/bluetooth-classic/ci.yml?branch=main&label=CI&logo=github&style=flat-square" alt="CI Status" >
  </a>
  <a href="https://bluetooth-classic.readthedocs.io">
    <img src="https://img.shields.io/readthedocs/bluetooth-classic.svg?logo=read-the-docs&logoColor=fff&style=flat-square" alt="Documentation Status">
  </a>
  <a href="https://codecov.io/gh/jeverley/bluetooth-classic">
    <img src="https://img.shields.io/codecov/c/github/jeverley/bluetooth-classic.svg?logo=codecov&logoColor=fff&style=flat-square" alt="Test coverage percentage">
  </a>
</p>
<p align="center">
  <a href="https://python-poetry.org/">
    <img src="https://img.shields.io/badge/packaging-poetry-299bd7?style=flat-square" alt="Poetry">
  </a>
  <a href="https://github.com/astral-sh/ruff">
    <img src="https://img.shields.io/badge/code%20style-ruff-000000.svg?style=flat-square" alt="ruff">
  </a>
  <a href="https://github.com/pre-commit/pre-commit">
    <img src="https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=flat-square" alt="pre-commit">
  </a>
</p>
<p align="center">
  <a href="https://pypi.org/project/bluetooth-classic/">
    <img src="https://img.shields.io/pypi/v/bluetooth-classic.svg?logo=python&logoColor=fff&style=flat-square" alt="PyPI Version">
  </a>
  <img src="https://img.shields.io/pypi/pyversions/bluetooth-classic.svg?style=flat-square&logo=python&amp;logoColor=fff" alt="Supported Python versions">
  <img src="https://img.shields.io/pypi/l/bluetooth-classic.svg?style=flat-square" alt="License">
</p>

<!-- docs-start -->

Bluetooth Classic (BR/EDR) discovery, pairing, scans and RFCOMM serial
connections on Linux.

Python has good libraries for Bluetooth Low Energy, such as
[Bleak](https://github.com/hbldh/bleak), but none of them handle Bluetooth
Classic devices: speakers, headphones, printers and serial adapters. This
library fills that gap. It talks to BlueZ over D-Bus with
[dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast), in your own
asyncio event loop, without compiled extensions.

- **Discover** nearby Classic devices, with their names, services and signal
  strength.
- **Pair** with a built-in agent for devices without a screen or keyboard,
  and **trust** them.
- **Find a device**: whether a device is on and discoverable, with a short
  Classic scan that never connects to it, so it does not wake a device that
  wakes when connected to.
- **Unpair** a device, removing it from BlueZ.
- **Open a connection** to one of a device's RFCOMM services, such as its
  serial port, made by [aio-rfcomm](https://github.com/dlech/aio-rfcomm)
  through BlueZ's profile API.

It covers what a Classic device needs that the usual Python Bluetooth
libraries do not, and nothing about any one kind of device. Method names
follow [bleak](https://github.com/hbldh/bleak)'s where there is an
equivalent.

## Devices, links and service connections

A Bluetooth Classic device is reached in layers, and each operation here
acts on one of them.

- **The device.** It is found by a scan, then paired and trusted, which
  BlueZ remembers. `BluetoothClassic` works at this level: `discover()`,
  `find_device_by_address()`, `pair()`, `trust()`, `unpair()`. None of them
  connects to the device; a scan only listens.
- **The link.** When anything needs to talk to the device, BlueZ connects a
  link to it (the device's connection). One link carries all the services
  in use on that device at once: an RFCOMM connection, audio, a keyboard.
  BlueZ owns it: it brings the link up when the first service needs it and
  takes it down a little after the last one is done. This library never
  connects or disconnects a device's link itself.
- **A service connection.** A device offers services, each identified by a
  UUID, such as its Serial Port (`1101`). An `RfcommConnection` is a
  connection to one of them (in the RFCOMM specification, a data link
  connection to the service's channel; BlueZ finds the channel). It is
  *opened* and *closed*: opening it brings the link up if it is not up
  already, which may wake a device that wakes when connected to; closing it
  ends only this connection, and leaves the link, and any other services on
  it, to BlueZ.

What a service connection carries is a byte stream. Data arrives in chunks
as the device sends it, with no message boundaries, so framing the bytes
into a device's messages is the caller's job. A write is done once the
bytes are on their way; the stream has no acknowledgement that the device
read them.

Two more things follow from how connections are made:

- **One owner per process.** aio-rfcomm allows one Bluetooth object in a
  process. This library opens it on first use and every caller shares it,
  so open connections through this library rather than opening aio-rfcomm
  directly. A device's service has at most one connection at a time.
- **Bytes on open.** Some devices change state soon after a connection
  opens and only answer a request sent before then. Bytes given as
  `write_on_open` are sent from this library's own thread the moment the
  connection opens, however busy the application's event loop is.

## Installation

Install this via pip (or your favourite package manager):

`pip install bluetooth-classic`

It needs Linux with BlueZ 5, and Python 3.11 or newer.

## Usage

```python
import asyncio

from bluetooth_classic import SERIAL_PORT, BluetoothClassic, RfcommConnection


async def main() -> None:
    async with BluetoothClassic() as bluetooth:
        for device in await bluetooth.discover(timeout=10):
            print(device.address, device.name, device.rssi)

        device = await bluetooth.pair("00:11:22:33:44:55")
        await bluetooth.trust(device.address)

        found = await bluetooth.find_device_by_address(device.address)
        print("on" if found else "off")

    # The device's Serial Port service, connected by BlueZ.
    async with RfcommConnection(
        "00:11:22:33:44:55", service=SERIAL_PORT, data_callback=print
    ) as connection:
        await connection.write(b"hello")


asyncio.run(main())
```

A Classic scan uses the same radio as Low Energy scanning and interrupts it
while it runs. Keep scans short, and in an application that also scans for
Low Energy devices, let one part of it decide when they run.

## Contributing

See [CONTRIBUTING.md](https://github.com/jeverley/bluetooth-classic/blob/main/CONTRIBUTING.md).

## Credits

The project layout and tooling follow the
[Bluetooth-Devices](https://github.com/Bluetooth-Devices) libraries, which
were created with
[browniebroke/cookiecutter-pypackage](https://github.com/browniebroke/cookiecutter-pypackage).
