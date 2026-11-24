# CLAUDE.md

`bluetooth-classic`: Bluetooth Classic (BR/EDR) discovery, pairing, trust
and scans for one device on Linux, through BlueZ over D-Bus with dbus-fast, and RFCOMM
serial connections through a serialx `rfcomm://` handler. It holds what
Classic devices need that Home Assistant's Bluetooth libraries do not, so
each piece can later move to the library it belongs in (discovery and
scans to habluetooth, pairing to bleak, `rfcomm://` to serialx). Generic:
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
- Connections: only the generic `rfcomm://` serialx handler. No device
  protocols; callers bring their own.
- `agent.py` must not use `from __future__ import annotations`: dbus-fast
  reads the D-Bus signatures from the method annotations.
- Before committing: `ruff check .`, `ruff format --check .`, `mypy src
  tests`, `pytest`.
- No em dashes, and no `--` in their place.
