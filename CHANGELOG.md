# CHANGELOG


## v0.4.0 (2026-11-24)

### Features

- Follow bleak's names: async with, find_device_by_address(), unpair()
  ([`3317db0`](https://github.com/jeverley/bluetooth-classic/commit/3317db0cd6c447cc8369077e800c1be338e5ad90))

BluetoothClassic is constructed directly and opened with async with, as bleak's scanner and client
  are, instead of with connect(). discover() takes timeout. scan_for() becomes
  find_device_by_address(), which returns the device or None, stops as soon as it answers, and also
  finds devices BlueZ did not know; ScanResult is removed. unpair() removes a device from BlueZ, as
  bleak's does on Linux. DISCOVERY_SECONDS and SCAN_FOR_SECONDS become DISCOVERY_TIMEOUT and
  FIND_TIMEOUT. The old names are removed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.3.0 (2026-11-24)

### Features

- Rename check_presence() to scan_for()
  ([`4d73f62`](https://github.com/jeverley/bluetooth-classic/commit/4d73f62bb1c1fac3a62681fb6d4ffd5915c27e7d))

"Presence" reads as people being home, which this cannot tell: it finds only discoverable devices,
  and phones usually are not. scan_for() says what it does. PresenceResult becomes ScanResult, with
  found in place of present, and PRESENCE_SECONDS becomes SCAN_FOR_SECONDS. The old names are
  removed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.2.0 (2026-11-24)

### Bug Fixes

- Pass the pre-commit type check for the RFCOMM handler
  ([`547fe07`](https://github.com/jeverley/bluetooth-classic/commit/547fe07d99e6974b2580d679dc67488ca055f5fa))

Pre-commit's mypy now has serialx installed, and the Bluetooth socket names, which not every
  platform's type stubs define, are allowed to be missing.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Open RFCOMM serial connections with rfcomm:// URLs
  ([`2d42e5f`](https://github.com/jeverley/bluetooth-classic/commit/2d42e5f0c9c167f8ea98d5706e1c0b45ed47e45f))

Importing bluetooth_classic registers an rfcomm:// handler with serialx:
  rfcomm://<address>?channel=<n>&security=low|medium|high. A worker thread opens the link and writes
  the write_on_connect bytes the moment it opens, before the event loop gets to it; a cancelled
  connect closes the socket once the thread is done. Reading, writing and closing reuse serialx's
  socket handling. open_rfcomm() builds the URL and connects.

find_rfcomm_channel() looks up the channel a device offers a service on, with sdptool, returning
  None when it cannot.

The library's scope grows from discovery, pairing and presence to what Classic devices need that
  Home Assistant's Bluetooth libraries lack.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.1.1 (2026-11-24)

### Bug Fixes

- Make scans and BlueZ calls more robust
  ([`a4512c2`](https://github.com/jeverley/bluetooth-classic/commit/a4512c28751a2dbac24426fb29fc92b4d8dc45ce))

- discover() returns only devices with a Classic device class, so a Low Energy device whose only
  change is a signal strength is left out. - The discovery filter is cleared when a scan ends, so it
  does not shape later scans. - Calls to BlueZ wait at most 10 seconds for an answer; pairing keeps
  its own timeout. - A scan or pairing on a powered-off adapter raises NoAdapterError.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Continuous Integration

- Publish an already tagged release to PyPI on request
  ([`017b741`](https://github.com/jeverley/bluetooth-classic/commit/017b74103296b93499c2b1a1442fe0cd9bf1c22d))

If publishing fails after the release is tagged, running the workflow again makes no new release, so
  nothing is published. A manual run with the tag now builds that release and publishes it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.1.0 (2026-11-23)

### Continuous Integration

- Use the Node 24 versions of the checkout and setup-python actions
  ([`969aae6`](https://github.com/jeverley/bluetooth-classic/commit/969aae69ec98aecaca28e54904d49072848b00ac))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Add Bluetooth Classic discovery, pairing, trust and presence through BlueZ
  ([`6ba4bd0`](https://github.com/jeverley/bluetooth-classic/commit/6ba4bd048be33c2681962da07c9ba89b098fc0a7))

A generic asyncio library for the housekeeping around Bluetooth Classic (BR/EDR) devices on Linux,
  over BlueZ's D-Bus interface with dbus-fast:

- discover nearby Classic devices, with names, services and signal strength, using a Classic-only
  discovery filter; - pair with a temporary agent that answers for the one device only, for devices
  without a screen or keyboard, and trust devices; - check whether a known device is on with a short
  Classic scan that never connects to it, ignoring signal strengths that come from Low Energy
  advertising or link changes.

Tests run against a fake BlueZ on a fake bus. The layout and tooling follow the Bluetooth-Devices
  libraries: Poetry, src layout, typed, mypy, ruff, pre-commit, python-semantic-release and Read the
  Docs.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt
