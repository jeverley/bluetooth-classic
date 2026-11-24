# CHANGELOG


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
