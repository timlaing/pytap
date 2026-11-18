# Changelog

## [Unreleased]

### Added

- **Stale reading behaviour** option (**Configure → Change stale reading behaviour**): choose what live panel readings do once a panel stops sending valid reports — **Unavailable** (default), **Zero** (power, voltage, current and performance report 0; other live readings become unavailable) or **Hold last value** (previous behaviour, restored after a restart) — and the timeout (60–600 s, default 120 s). Energy totals are never affected.
- Per-optimizer diagnostic entities from previously undecoded power-report bytes: **High output mode** binary sensor (byte 7 top bit, set almost only above ~0.5 A input current), **Status code** sensor (byte 9 raw value shown as hex, e.g. `0x64` normal, `0x68` low current around dawn/dusk, `0xE4` low input voltage) and **Low voltage flag** binary sensor (byte 9 top bit). Names describe captured behaviour; Tigo has not documented these bits. They only record a state when their value changes.
- **Reading fault** problem binary sensor per optimizer. It turns on when a report is physically impossible (output voltage more than 2 V above input, either voltage outside 0–100 V, or temperature outside −40…125 °C) and clears on the next valid report. Impossible readings are discarded: they don't replace the last good values, add energy, or count as a fresh report. Attributes `fault_reasons` and `last_fault_at`; the state is kept across restarts.
- Optimizer devices, and string devices without optimizers, can be deleted from the Home Assistant UI. Deleting an optimizer excludes its barcode from automatic discovery.
- `pytap.delete_capture` admin action deletes a stopped capture session and its exported archives.
- Opt-in, admin-only raw capture with bounded asynchronous writing, timestamp/connection indexes, private archives, rotation/storage/duration limits, and explicit loss/error reporting. Off after every restart/reload.
- Standalone frame inspection/replay retaining unsupported frames, bad CRCs, unknown/extra payload bytes, and using recorded timestamps rather than replay time. Each frame is flagged when the live parser would drop it (`live_parser_would_drop`, `giant`).
- Bundled Tigo icons and logos, including high-resolution variants, served locally by Home Assistant's native brands API.
- Passive automatic optimizer discovery, enabled by default for new setups. Setup can finish before any modules are seen; sensors and aggregate membership grow dynamically as barcodes arrive.
- Discovery status diagnostics, module metadata editing, and persistent ignore rules for explicitly removed optimizers.
- Home Assistant ESPHome serial proxies and local serial adapters, selectable with the native serial-port picker during setup and connection changes.
- Transport diagnostics with serial endpoints redacted, and bounded serial open/read/close operations using `serialx`.
- **Configurable reporting mode** — New "Live reporting" toggle (default on) under **Configure → Change reporting settings**. When disabled, readings are cached and delivered as a time-weighted average every _reporting interval_ (5–300 s, default 30), reducing CPU and recorder load on installations with many panels. Energy totals are always integrated from raw readings, so only the instantaneous values are averaged.

### Changed

- By default, live panel readings (power, voltages, currents, temperature, duty cycle, RSSI, performance and the status entities) now become unavailable 120 s after the last valid report instead of holding the last value overnight; see the stale reading option above. In averaged reporting mode the reporting interval is added to the timeout. String and installation power and performance count only panels that are currently reporting. After a restart, saved live readings are not shown as current (they stay unavailable, or 0 with **Zero**) until the first new report, unless **Hold last value** is selected. Energy totals (daily, total, readings today) stay available.
- String and installation energy totals now add up each member's increase since it was last seen (restored across restarts), so moving a panel between strings or discovering a new panel no longer adds its lifetime energy. The daily aggregate resets at local midnight and reports the start of its own cycle as `last_reset`.
- Packet captures are stored in the Home Assistant configuration directory (`pytap_captures/<entry_id>`), so they are included in Home Assistant backups, and are deleted when the PyTap entry is removed.
- Exported capture archives count toward the capture storage budget; `export_capture` accepts an optional `max_storage_mib` and refuses an export that doesn't fit.
- Capture manifests are rewritten only when they change (at most every 10 s while recording, plus at rotation/stop), after the data they describe has been flushed to disk. `events.jsonl` now has a checksum in the manifest.
- When a panel starts or stops reporting, only that panel's entities and the string/installation aggregates containing it are updated, instead of every entity.
- An unreachable endpoint in the setup or connection-change form now shows "cannot connect"; submitting the same settings again saves them and PyTap keeps retrying in the background.
- Local serial paths must be under `/dev/`.
- Connection loss is logged once (with the error type and errno) instead of on every retry.
- Config entries are migrated to version 7: existing connections are marked as TCP, and existing installations keep manual module selection (automatic discovery stays off until enabled in options). Automatically discovered modules use barcode-based names and unknown panel ratings until edited; performance is not guessed from a default rating.
- Home Assistant 2026.11+ is now required; existing TCP connections migrate automatically without changing entity IDs or energy history.
- Connection settings now offer TCP or serial/proxy; serial defaults to 38400 baud, 8N1, with no flow control or bus writes.
- Reporting interval default changed from 5 to 30 seconds; allowed range tightened from 1–300 to 5–300 seconds.
- Config entry schema bumped to version 5 (adds `live_reporting`, default `True`, via automatic migration).
- The reporting interval is now collected on its own follow-up screen that appears only when live reporting is turned off, so the interval is never shown (effectively greyed out) while live reporting is enabled.

### Fixed

- The listener thread now keeps its own state and Home Assistant only receives published copies, so averaged reporting mode no longer occasionally shows raw readings.
- Unloading or reloading no longer gets stuck when the listener is slow to stop: setup is retried until the old listener has released its port, so it is never shared.
- Stopping a TCP connection wakes the blocked socket read immediately.
- A running packet capture is finalised when Home Assistant shuts down.
- The midnight reset of daily values is DST-safe.
- Symlinked serial paths (e.g. `/dev/serial/by-id/…`) are recognised as the same port as their target.
- Invalid saved connection settings (e.g. an out-of-range port from an older version) now give a clear setup error instead of retrying forever.
- Devices of removed modules and of strings without modules are removed at setup.
- Automatic discovery no longer adopts an optimizer already tracked by another PyTap entry.
- An invalid slot counter in a receive-response header no longer discards the whole frame; only the slot-clock update is skipped. Power reports with an invalid slot counter are dropped. Both increment the parser's `invalid_slots` counter.
- Interrupted captures whose index ends in corrupt rows can still be exported; the archive's `recovery.json` lists the warnings and marks it unverified.
- Capture action errors are shown as translated messages; diagnostics no longer include the capture directory path.
- Finish setup in empty manual mode now shows a validation error instead of silently returning to the menu.
- Discovery saves do not trigger reconnections, and module edits preserve discoveries arriving while the options flow is open.
- Live reporting no longer buffers readings internally, preventing unbounded memory growth over time in the default mode.
- Live reporting now publishes an immutable copy of node data to Home Assistant, avoiding rare races where consumers (sensors, diagnostics) could observe the data changing mid-read.
- Averaged interval mode now flushes buffered readings on schedule during quiet periods (e.g. at dusk), instead of waiting for the next reading or a reconnect.
- The version 5 migration clamps any pre-existing reporting interval below 5 seconds up to the new minimum, so the reporting settings form stays submittable after upgrade.

## [1.2.0] - 2026-03-07

### Changed

- Refactor code structure for improved readability and maintainability

## [1.1.1] - 2026-03-05

### Added

- Proactive midnight reset for daily energy accumulators

### Changed

- Reorder datetime imports for improved clarity
- Refactor code structure for improved readability and maintainability

### Documentation

- Document `sensor.tigo_max_power` and Page 3 (Panel Power) in pv_production dashboard

## [1.1.0] - Previous release

See [v1.1.0 tag](https://github.com/azebro/pytap/releases/tag/v1.1.0).

## [1.0.0] - Initial release

See [v1.0.0 tag](https://github.com/azebro/pytap/releases/tag/v1.0.0).
