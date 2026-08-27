# V4 background service: test-only Phase 1 package

This package is inert until a person installs its plist with `launchctl`.
It does not change the existing Terminal launcher, Page 4, Companion, FX30,
OBS, Just In, RODECaster, or port 3060.

## What it does

- Binds only `127.0.0.1:3063` by default. Port 3061 is already reserved by
  the loaded read-only watchdog, and port 3062 belongs to a separate native
  StudioOS test route.
- Uses `v4-service-test-config.json`, which points at a fixture-only Just In
  port (`18080`) and scratch state under `/private/tmp/buildstudio-v4-service-test`.
- Writes service and launchd logs to `~/Library/Logs/BuildStudio/V4`.
- Restarts only after an abnormal exit, with a 30-second throttle.
- Refuses port 3060 unless a later, separately reviewed activation passes the
  explicit `V4_SERVICE_ALLOW_PRODUCTION_PORT=approved` override.

## Files

- `run_v4_service.command`: guarded wrapper and log rotation.
- `com.buildstudio.just-control-v4-local.test.plist`: uninstalled test
  LaunchAgent definition.
- `v4_service_diagnostics.command`: read-only state, listener, status, and log
  report.
- `v4-service-test-config.json`: isolated non-production configuration.

## Verification completed by this package

Only static validation is appropriate while the current studio session is
unresolved: shell syntax, JSON parsing, and plist validation. Do not bootstrap
the plist or run the wrapper until an idle maintenance window is explicitly
approved.

## Later, separately approved test

Use a disposable fixture on port `18080`, then bootstrap only
`com.buildstudio.just-control-v4-local.test` and confirm one owner of port
`3063`. This test must not connect to port 8080, port 3060, or a real recorder.
Production transition is out of scope for this package and requires a distinct
review, backup, and idle-window approval.
