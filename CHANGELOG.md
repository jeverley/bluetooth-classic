# CHANGELOG


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
