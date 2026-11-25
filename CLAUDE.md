# CLAUDE.md

`bluetooth-classic`: Bluetooth Classic (BR/EDR) discovery, pairing, trust
and finding one device on Linux, through BlueZ over D-Bus with dbus-fast, and RFCOMM
links through aio-rfcomm, as the one aio-rfcomm owner in a process. It holds
what Classic devices need that Home Assistant's Bluetooth libraries do not,
standing in for Home Assistant's `bluetooth` integration, which is meant to
absorb it (discovery, scans, pairing and the aio-rfcomm owner). Generic:
it knows nothing about any one kind of device or application. It is meant to
fit alongside Home Assistant's Bluetooth libraries (the Bluetooth-Devices
organisation), so it follows their layout and tooling: Poetry, `src/`
layout, typed, pytest, mypy, ruff, pre-commit, python-semantic-release,
Read the Docs.

## Rules

- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
  `test:`, `chore:`); releases are made from them. Do not bump the version
  by hand.
- Commit as `jeverley <46714706+jeverley@users.noreply.github.com>`.
- Work on a feature branch; ask before merging anything more than trivial.
- No device-specific behaviour: callers filter devices themselves. Docs,
  comments and tests use generic devices and example addresses only
  (such as `00:11:22:33:44:55`), never real ones.
- Device operations follow bleak's names and parameters where there is an
  equivalent (`discover(timeout=)`, `find_device_by_address()`, `pair()`,
  `unpair()`, `async with`). RFCOMM connections use the RFCOMM terms
  instead: a connection is opened and closed (`open()`, `close()`,
  `is_open`, `closed_callback`), as it is one service connection on a link
  BlueZ owns, not the device's connection.
- Connections: only generic RFCOMM connections: `RfcommConnection` (open,
  write, close; data and closing as callbacks; named for what the RFCOMM
  specification calls it) and `establish_rfcomm_connection()`, shaped like
  bleak-retry-connector's `establish_connection()`. A byte stream, with no
  framing: no device protocols; callers bring their own.
- Links come from aio-rfcomm (BlueZ's profile API on Linux). aio-rfcomm
  allows one Bluetooth object per process: this library owns it, on its
  own event loop in a background thread, and every caller in the process
  shares it, so `write_on_open` is sent the moment the link opens however
  busy the caller's loop is. No Bluetooth or D-Bus code of our own for
  links, and no serialx.
- `agent.py` must not use
  `from __future__ import annotations`: dbus-fast reads the D-Bus
  signatures from the method annotations.
- Before committing: `ruff check .`, `ruff format --check .`, `mypy src
  tests`, `pytest`.
- No em dashes, and no `--` in their place.
