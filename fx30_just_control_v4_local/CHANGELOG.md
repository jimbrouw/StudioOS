# Changelog

## Unreleased - Headless NDI backup recovery V1 (local-test only)

- Added canonical one-to-one NDI/Just In/FX30 recovery mappings, with Camera 2
  and Camera 3 retained as disabled configuration entries.
- Added atomic per-session manifests and append-only, idempotent authoritative
  events for a read-only remote notifier.
- Added a read-only liveness observer and fail-closed terminal recovery gate.
- Added fixture-only Sony recovery staging, SHA-256 receipts, verified delivery
  and the 30-second duration-review threshold.
- Left the recovery worker disabled in the active config because ZowieBox
  recording errors and real Sony catalog/selected-ingest proof remain open.

## v4.1.0 - 2026-08-11

- Added the fail-closed, operator-confirmed RØDECaster capacity guardian.
- Added short-lived, single-use amber override evidence and capacity recovery.
- Made ARM and bridge recording start require the same current capacity gate.
- Added dashboard and Companion capacity controls without changing archive or delete behavior.

## v4.0.0 - 2026-08-10

- Forked V4 beside Shade V3 for a reversible long-record endurance test.
- Moved the bridge to port 3060 and the API to `/v4/*`.
- Kept masters on the Extreme SSD without a second copy or full-file hash pass.
- Moved proxies to `/Volumes/Extreme SSD/Proxies` and verifies them with FFprobe.
- Removed every Shade mount, capacity, copy, and completion dependency.
- Made RØDECaster transfer manual, SSD-only, and source-retaining.
- Added separate local ARM, RECORD, STOP, dashboard, and Companion page 4 wiring.
