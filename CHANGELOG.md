# Changelog

## [Unreleased]

### Added

- Opt-in, admin-only raw capture with bounded asynchronous writing, timestamp/connection indexes, private archives, rotation/storage/duration limits, and explicit loss/error reporting. Off after every restart/reload.
- Standalone frame inspection/replay retaining unsupported frames, bad CRCs, unknown/extra payload bytes, and using recorded timestamps rather than replay time.
- Bundled Tigo icons and logos, including high-resolution variants, served locally by Home Assistant's native brands API.
- Passive automatic optimizer discovery, enabled by default for new setups. Setup can finish before any modules are seen; sensors and aggregate membership grow dynamically as barcodes arrive.
- Discovery status diagnostics, module metadata editing, and persistent ignore rules for explicitly removed optimizers.
- Home Assistant ESPHome serial proxies and local serial adapters, selectable with the native serial-port picker during setup and connection changes.
- Transport diagnostics with serial endpoints redacted, and bounded serial open/read/close operations using `serialx`.
- **Configurable reporting mode** — New "Live reporting" toggle (default on) under **Configure → Change reporting settings**. When disabled, readings are cached and delivered as a time-weighted average every _reporting interval_ (5–300 s, default 30), reducing CPU and recorder load on installations with many panels. Energy totals are always integrated from raw readings, so only the instantaneous values are averaged.

### Changed

- Config-entry version 7 preserves manual mode on existing installations. Automatically discovered modules use barcode-based names and unknown panel ratings until edited; performance is not guessed from a default rating.
- Home Assistant 2026.11+ is now required; existing TCP connections migrate automatically to config-entry version 6 without changing entity IDs or energy history.
- Connection settings now offer TCP or serial/proxy; serial defaults to 38400 baud, 8N1, with no flow control or bus writes.
- Reporting interval default changed from 5 to 30 seconds; allowed range tightened from 1–300 to 5–300 seconds.
- Config entry schema bumped to version 5 (adds `live_reporting`, default `True`, via automatic migration).
- The reporting interval is now collected on its own follow-up screen that appears only when live reporting is turned off, so the interval is never shown (effectively greyed out) while live reporting is enabled.

### Fixed

- Finish setup in empty manual mode now shows a validation error instead of silently returning to the menu.
- Discovery saves do not trigger reconnections, and module edits preserve discoveries arriving while the options flow is open.
- Listener shutdown now waits for the actual executor worker to release its port; a timed-out stop blocks replacement listeners rather than leaking a worker during reload.
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
