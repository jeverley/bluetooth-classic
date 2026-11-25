# CHANGELOG


## v0.8.2 (2026-11-25)

### Bug Fixes

- Retry while an old link closes, and close a link disconnected while opening
  ([`052637c`](https://github.com/jeverley/bluetooth-classic/commit/052637ce31ea6a180ac04ffa0eaae6843d6150ad))

- The one-link-per-service refusal carries errno EBUSY, so establish_rfcomm_connection() tries again
  while an old link is still closing after a drop. - RfcommClient.disconnect() called while
  connect() is opening the link waits for it and closes the link as soon as it opens; connect() then
  raises with ECONNABORTED. - establish_rfcomm_connection() refuses fewer than one attempt with
  ValueError. - A comment no longer names a device.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.8.1 (2026-11-25)

### Bug Fixes

- Pick BlueZ's first adapter, the one aio-rfcomm links use
  ([`6c587d4`](https://github.com/jeverley/bluetooth-classic/commit/6c587d40b84306481b0d5ddf968fcf90f896fb60))

With no adapter named, BluetoothClassic now uses BlueZ's first adapter by object path, powered or
  not, as aio-rfcomm does for RFCOMM links, instead of the first powered one. A device it pairs can
  then always be connected; if that adapter is off, scans and pairing raise NoAdapterError instead
  of moving to another adapter.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.8.0 (2026-11-25)

### Code Style

- Format the RFCOMM tests
  ([`90b98b0`](https://github.com/jeverley/bluetooth-classic/commit/90b98b0d986e12994be4de17def5786d96888c65))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Documentation

- End the usage guide with a single newline
  ([`8731928`](https://github.com/jeverley/bluetooth-classic/commit/873192834407a2afb4b4e91cd757beecee98c6ba))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Add RfcommClient and establish_rfcomm_connection(), shaped like bleak
  ([`1dc1480`](https://github.com/jeverley/bluetooth-classic/commit/1dc1480d2a265ff5e9a3131ac0efc4f509dffe91))

RfcommClient connects to a device's RFCOMM service as bleak's BleakClient does: connect(), write()
  and write_nowait(), disconnect(), a data callback from the first byte and a disconnected callback.
  establish_rfcomm_connection() connects one, retrying while BlueZ says the device is busy, as
  bleak-retry-connector's establish_connection() does; such failures are raised with errno EBUSY.
  Both sit on create_rfcomm_connection().

BREAKING CHANGE: open_rfcomm() is renamed create_rfcomm_connection(), following asyncio's
  create_connection(), which it is shaped like.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

- Open RFCOMM links directly, without serialx
  ([`50212b7`](https://github.com/jeverley/bluetooth-classic/commit/50212b75bfd59088b24589716dffda99766aed6d))

open_rfcomm() opens a device's RFCOMM service through this library's aio-rfcomm owner and attaches a
  protocol, as asyncio's create_connection() does, returning an RfcommTransport. Every caller in a
  process shares the one aio-rfcomm owner, so open links through here rather than opening aio-rfcomm
  directly. Closing sends what is queued first, for at most two seconds. Links report their address
  and service.

BREAKING CHANGE: the serialx rfcomm:// handler is removed, with rfcomm_url(), parse_rfcomm_url() and
  RfcommTarget, and serialx is no longer a dependency. Use open_rfcomm(); its timeout option is
  timeout.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

- Read establish_rfcomm_connection()'s attempts and delay when called
  ([`e52db3b`](https://github.com/jeverley/bluetooth-classic/commit/e52db3b54ebadae31187a9148ca23660443042ac))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.7.2 (2026-11-25)

### Bug Fixes

- Free a device after a drop only once its link has closed
  ([`0b7f4e1`](https://github.com/jeverley/bluetooth-classic/commit/0b7f4e1ef83b9b1eb54af163fc930fbeeb09f8e3))

- After the device or Bluetooth ends a link, the drop is still reported at once, but the device
  service can only be connected again once the old channel has closed, so a new link cannot race its
  teardown. - A link that ended is reported as ConnectionResetError with ECONNRESET, as a socket
  reports it; Bluetooth going away is ConnectionAbortedError. - A connect given up while opening no
  longer leaves an exception that is never retrieved. - A timeout is logged as one, without the
  cancellation it caused. - A device aio-rfcomm does not know says that it uses BlueZ's first
  adapter, and the docs say to pair the device on it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.7.1 (2026-11-25)

### Bug Fixes

- Log why an RFCOMM channel did not open
  ([`6e8722c`](https://github.com/jeverley/bluetooth-classic/commit/6e8722cda5c8926e27290af7f6854e695dfe308f))

When a connect fails, the handler logs aio-rfcomm's error and the BlueZ error it came from, with how
  long after asking; a connect given up is logged too.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.7.0 (2026-11-25)

### Chores

- Keep breaking changes on 0.x to minor releases
  ([`f59c37c`](https://github.com/jeverley/bluetooth-classic/commit/f59c37cba8276c4170c7fd67426dd2ea7c6b4022))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Open RFCOMM links with aio-rfcomm
  ([`31cafa4`](https://github.com/jeverley/bluetooth-classic/commit/31cafa46f3d16f3a4a50e9bd92e73f78935475bb))

The rfcomm:// handler now gets its connections from aio-rfcomm, which takes the same BlueZ profile
  route on Linux, instead of our own profile code. The handler owns aio-rfcomm's one Bluetooth
  object, on an event loop of its own in a background thread, so write_on_connect is sent the moment
  the channel opens however busy the caller's loop is. profile.py is removed.

BREAKING CHANGE: rfcomm:// URLs and rfcomm_url() no longer take an adapter (aio-rfcomm uses BlueZ's
  first). RfcommLink and RfcommTarget lose their adapter and channel fields, links no longer report
  rfcomm_channel or rfcomm_adapter, and ProfileInUseError is removed: aio-rfcomm's errors are raised
  as RfcommConnectError.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Breaking Changes

- Rfcomm:// URLs and rfcomm_url() no longer take an adapter (aio-rfcomm uses BlueZ's first).
  RfcommLink and RfcommTarget lose their adapter and channel fields, links no longer report
  rfcomm_channel or rfcomm_adapter, and ProfileInUseError is removed: aio-rfcomm's errors are raised
  as RfcommConnectError.


## v0.6.2 (2026-11-24)

### Bug Fixes

- Log when BlueZ hands a connection over, and when the bytes go out
  ([`f4a153e`](https://github.com/jeverley/bluetooth-classic/commit/f4a153e240f5abefbc97f684e7e6982076038487))

The D-Bus thread logs when it asks BlueZ to connect a service, and when BlueZ's NewConnection
  arrives: how long after asking, and how soon after that the bytes to send were written. A device
  that changes state soon after a connection opens can then be checked against where the time went:
  BlueZ connecting, or this process.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.6.1 (2026-11-24)

### Bug Fixes

- Handle BlueZ's handover on a D-Bus thread of its own
  ([`db3e18f`](https://github.com/jeverley/bluetooth-classic/commit/db3e18fef21b10409b91c9387eb50eee717c15ae))

The profile API's D-Bus work (registering, connecting, disconnecting and BlueZ's
  Profile1.NewConnection call) now runs on an event loop of its own, in a background thread.
  write_on_connect is written the moment BlueZ hands the connection over, however busy the caller's
  event loop is: a device that changes state soon after a connection opens (an LG soundbar moving to
  Bluetooth within 0.2 s) was answered too late while Home Assistant was starting up. The socket is
  then handed to the caller's loop. A connect the caller stops waiting for finishes on the D-Bus
  loop (within its timeout), and a link it opens is closed and released. The timeout is one deadline
  for the whole open.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Documentation

- Say that bytes sent on connect go out from the D-Bus thread
  ([`23741da`](https://github.com/jeverley/bluetooth-classic/commit/23741da2080ce85c59ef98d7fe98c095ba9b1c8f))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.6.0 (2026-11-24)

### Bug Fixes

- Drop stray BlueZ links, keep links to other devices, bound the open
  ([`67ffd29`](https://github.com/jeverley/bluetooth-classic/commit/67ffd29ce478d4ece520a62d451043b5b7e9ee5a))

- A socket BlueZ hands over after the connect was given up is closed without writing to it, and
  BlueZ is asked to drop the link too. - One link per device and service in a process: a second,
  while the first is open or opening, is refused instead of tearing down the first. -
  connect_timeout bounds the whole open, registration and lookups included. - Registrations are tied
  to the bus they were made on, so links opened before a D-Bus reconnect cannot release a newer
  registration. - Background DisconnectProfile calls are kept until they finish, and the
  ConnectProfile reply is always collected. - Without adapter=, a device under a powered adapter is
  preferred. - The docs say link security is BlueZ's for client profiles.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Connect RFCOMM services through BlueZ's profile API
  ([`686e65b`](https://github.com/jeverley/bluetooth-classic/commit/686e65b5f878b3ce91033ac158b56388bf289fbd))

The rfcomm:// handler no longer opens Bluetooth sockets of its own: it registers as a client of the
  service with BlueZ (only while a link uses it), asks BlueZ to connect it on the device, and takes
  the socket BlueZ hands over in Profile1.NewConnection, writing write_on_connect inside that call.
  When a link closes it asks BlueZ to drop its side as well, as BlueZ keeps its own reference to the
  connection. Links in one process share a registration; another program holding the service raises
  ProfileInUseError, and BlueZ failing to connect raises RfcommConnectError (an OSError).

URLs take service and adapter only: BlueZ finds the channel, so channel= and the channel hint are
  gone, as is security=. Only serialx's asyncio API opens rfcomm:// URLs; serial_for_url() refuses
  them. Our own SDP client (find_rfcomm_services, find_rfcomm_channel), Security,
  rfcomm_supported(), RfcommUnsupportedError and ServiceLookupError are removed. open_rfcomm() takes
  service= (required) and adapter=.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.5.0 (2026-11-24)

### Bug Fixes

- Harden RFCOMM opens and SDP reply parsing
  ([`af3fff7`](https://github.com/jeverley/bluetooth-classic/commit/af3fff7855312231d80f5e8ec2945063af1859f4))

- Close the socket when a connect is cancelled just as the open finishes. - Work out a link's
  adapter in the worker thread, off the event loop. - Reject malformed SDP replies (attribute IDs
  that are not numbers, deep nesting, elements running past their sequence, byte counts and
  continuation state that do not fit, endless continuations) as ServiceLookupError. - A missing
  adapter raises NoAdapterError. - find_rfcomm_channel only falls back to records that list the
  service among their classes. - Reject empty rfcomm:// parameters. - Fail with TimeoutError once
  the open's timeout has passed. - Document that blocking links are listed until closed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Features

- Connect to RFCOMM services by UUID, with our own SDP client
  ([`68e55c9`](https://github.com/jeverley/bluetooth-classic/commit/68e55c9b1b2977d3065adc875b50c97ffbce90ba))

rfcomm:// URLs and open_rfcomm() take a service (a UUID or its short form), a channel, or both, plus
  an optional adapter. With a service alone its channel is looked up in the device's SDP records;
  with both, the channel is tried first and the service is looked up once only if the device refuses
  it. The handler keeps no state between connections: a link reports the channel, service and
  adapter it used through get_extra_info(), for callers to pass back next time.

Open links are listed by open_rfcomm_links(), with on_rfcomm_links_changed() to follow them.

Service lookups no longer run sdptool: find_rfcomm_services() and find_rfcomm_channel() (with
  blocking forms) ask the device's SDP server directly over L2CAP, and raise ServiceLookupError when
  it cannot be reached or answers wrongly.

open_rfcomm() now takes the protocol factory second and the channel as a keyword, rfcomm_url() takes
  keyword options, and parse_rfcomm_url() returns an RfcommTarget.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt

### Testing

- Wait for the open's thread before cancelling, on every Python
  ([`7a46944`](https://github.com/jeverley/bluetooth-classic/commit/7a46944eb283b499e0169d27263a2f4ed040ee6b))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


## v0.4.1 (2026-11-24)

### Bug Fixes

- Fail early-dropped links, serialise pairing, and stop stray sdptool
  ([`36952f2`](https://github.com/jeverley/bluetooth-classic/commit/36952f2bfdf8b1cc94614ea49f26245cd8573f32))

- An RFCOMM link the device drops before the protocol is attached now fails the connect with the
  drop's error, instead of returning a dead transport whose protocol is never told it opened or
  closed. - pair() calls on one connection run one at a time, as the agent is exported at a single
  path. - find_device_by_address() no longer counts a known device's class change as an answer; only
  a new device's class, or an inquiry's signal strength, does. - Cancelling find_rfcomm_channel()
  stops sdptool instead of leaving it running. - Entering the same BluetoothClassic twice is refused
  rather than leaking a bus connection. - _listening() loses its unused per-path option.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CciW4UG1YNtAD3yvwo7sxt


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
