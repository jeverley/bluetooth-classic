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

Method names follow [bleak](https://github.com/hbldh/bleak)'s where there is
an equivalent.
- **Connect** to a device's RFCOMM service, such as its serial port, as an
  asyncio transport and protocol.
  [aio-rfcomm](https://github.com/dlech/aio-rfcomm) makes the connection
  (through BlueZ's profile API); this library is the one aio-rfcomm owner
  in the process, shared by every caller, and bytes to send on connect go
  out from a thread of its own the moment the channel opens.

It covers what a Classic device needs that the usual Python Bluetooth
libraries do not, and nothing about any one kind of device.

## Installation

Install this via pip (or your favourite package manager):

`pip install bluetooth-classic`

It needs Linux with BlueZ 5, and Python 3.11 or newer.

## Usage

```python
import asyncio

from bluetooth_classic import SERIAL_PORT, BluetoothClassic, open_rfcomm


async def main() -> None:
    async with BluetoothClassic() as bluetooth:
        for device in await bluetooth.discover(timeout=10):
            print(device.address, device.name, device.rssi)

        device = await bluetooth.pair("00:11:22:33:44:55")
        await bluetooth.trust(device.address)

        found = await bluetooth.find_device_by_address(device.address)
        print("on" if found else "off")

    # The device's Serial Port service, connected by BlueZ, with a protocol
    # attached as asyncio's create_connection() does.
    transport, protocol = await open_rfcomm(
        "00:11:22:33:44:55", asyncio.Protocol, service=SERIAL_PORT
    )
    transport.close()


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
