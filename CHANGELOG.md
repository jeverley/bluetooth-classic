# CHANGELOG


## v0.9.0 (2026-11-25)

### Bug Fixes

- Abort while opening, and close between retries
  ([`b6313d1`](https://github.com/jeverley/bluetooth-classic/commit/b6313d146a2dbde385f531db948380201e52553a))

### Features

- RfcommConnection, driving aio-rfcomm's channel directly
  ([`f47686b`](https://github.com/jeverley/bluetooth-classic/commit/f47686b84e0b382ad9b63017bec012bfdb6495f8))

### Breaking Changes

- RfcommClient is replaced by RfcommConnection: connect() is open(), disconnect() is close(),
  write_on_connect is write_on_open, is_connected is is_open, connected_at is opened_at,
  disconnected_callback is closed_callback and disconnect_reason is close_reason.
  establish_rfcomm_connection() calls open(). create_rfcomm_connection(), RfcommTransport,
  RfcommLink, open_rfcomm_links() and on_rfcomm_links_changed() are removed.


## v0.8.2 (2026-11-25)

### Bug Fixes

- Retry while an old link closes, and close a link disconnected while opening
  ([`052637c`](https://github.com/jeverley/bluetooth-classic/commit/052637ce31ea6a180ac04ffa0eaae6843d6150ad))


## v0.8.1 (2026-11-25)

### Bug Fixes

- Pick BlueZ's first adapter, the one aio-rfcomm links use
  ([`6c587d4`](https://github.com/jeverley/bluetooth-classic/commit/6c587d40b84306481b0d5ddf968fcf90f896fb60))


## v0.8.0 (2026-11-25)

### Features

- Add RfcommClient and establish_rfcomm_connection(), shaped like bleak
  ([`1dc1480`](https://github.com/jeverley/bluetooth-classic/commit/1dc1480d2a265ff5e9a3131ac0efc4f509dffe91))

- Open RFCOMM links directly, without serialx
  ([`50212b7`](https://github.com/jeverley/bluetooth-classic/commit/50212b75bfd59088b24589716dffda99766aed6d))

- Read establish_rfcomm_connection()'s attempts and delay when called
  ([`e52db3b`](https://github.com/jeverley/bluetooth-classic/commit/e52db3b54ebadae31187a9148ca23660443042ac))


## v0.7.2 (2026-11-25)

### Bug Fixes

- Free a device after a drop only once its link has closed
  ([`0b7f4e1`](https://github.com/jeverley/bluetooth-classic/commit/0b7f4e1ef83b9b1eb54af163fc930fbeeb09f8e3))


## v0.7.1 (2026-11-25)

### Bug Fixes

- Log why an RFCOMM channel did not open
  ([`6e8722c`](https://github.com/jeverley/bluetooth-classic/commit/6e8722cda5c8926e27290af7f6854e695dfe308f))


## v0.7.0 (2026-11-25)

### Features

- Open RFCOMM links with aio-rfcomm
  ([`31cafa4`](https://github.com/jeverley/bluetooth-classic/commit/31cafa46f3d16f3a4a50e9bd92e73f78935475bb))

### Breaking Changes

- Rfcomm:// URLs and rfcomm_url() no longer take an adapter (aio-rfcomm uses BlueZ's first).
  RfcommLink and RfcommTarget lose their adapter and channel fields, links no longer report
  rfcomm_channel or rfcomm_adapter, and ProfileInUseError is removed: aio-rfcomm's errors are raised
  as RfcommConnectError.


## v0.6.2 (2026-11-24)

### Bug Fixes

- Log when BlueZ hands a connection over, and when the bytes go out
  ([`f4a153e`](https://github.com/jeverley/bluetooth-classic/commit/f4a153e240f5abefbc97f684e7e6982076038487))


## v0.6.1 (2026-11-24)

### Bug Fixes

- Handle BlueZ's handover on a D-Bus thread of its own
  ([`db3e18f`](https://github.com/jeverley/bluetooth-classic/commit/db3e18fef21b10409b91c9387eb50eee717c15ae))


## v0.6.0 (2026-11-24)

### Bug Fixes

- Drop stray BlueZ links, keep links to other devices, bound the open
  ([`67ffd29`](https://github.com/jeverley/bluetooth-classic/commit/67ffd29ce478d4ece520a62d451043b5b7e9ee5a))

### Features

- Connect RFCOMM services through BlueZ's profile API
  ([`686e65b`](https://github.com/jeverley/bluetooth-classic/commit/686e65b5f878b3ce91033ac158b56388bf289fbd))


## v0.5.0 (2026-11-24)

### Bug Fixes

- Harden RFCOMM opens and SDP reply parsing
  ([`af3fff7`](https://github.com/jeverley/bluetooth-classic/commit/af3fff7855312231d80f5e8ec2945063af1859f4))

### Features

- Connect to RFCOMM services by UUID, with our own SDP client
  ([`68e55c9`](https://github.com/jeverley/bluetooth-classic/commit/68e55c9b1b2977d3065adc875b50c97ffbce90ba))


## v0.4.1 (2026-11-24)

### Bug Fixes

- Fail early-dropped links, serialise pairing, and stop stray sdptool
  ([`36952f2`](https://github.com/jeverley/bluetooth-classic/commit/36952f2bfdf8b1cc94614ea49f26245cd8573f32))


## v0.4.0 (2026-11-24)

### Features

- Follow bleak's names: async with, find_device_by_address(), unpair()
  ([`3317db0`](https://github.com/jeverley/bluetooth-classic/commit/3317db0cd6c447cc8369077e800c1be338e5ad90))


## v0.3.0 (2026-11-24)

### Features

- Rename check_presence() to scan_for()
  ([`4d73f62`](https://github.com/jeverley/bluetooth-classic/commit/4d73f62bb1c1fac3a62681fb6d4ffd5915c27e7d))


## v0.2.0 (2026-11-24)

### Bug Fixes

- Pass the pre-commit type check for the RFCOMM handler
  ([`547fe07`](https://github.com/jeverley/bluetooth-classic/commit/547fe07d99e6974b2580d679dc67488ca055f5fa))

### Features

- Open RFCOMM serial connections with rfcomm:// URLs
  ([`2d42e5f`](https://github.com/jeverley/bluetooth-classic/commit/2d42e5f0c9c167f8ea98d5706e1c0b45ed47e45f))


## v0.1.1 (2026-11-24)

### Bug Fixes

- Make scans and BlueZ calls more robust
  ([`a4512c2`](https://github.com/jeverley/bluetooth-classic/commit/a4512c28751a2dbac24426fb29fc92b4d8dc45ce))


## v0.1.0 (2026-11-23)

### Features

- Add Bluetooth Classic discovery, pairing, trust and presence through BlueZ
  ([`6ba4bd0`](https://github.com/jeverley/bluetooth-classic/commit/6ba4bd048be33c2681962da07c9ba89b098fc0a7))
