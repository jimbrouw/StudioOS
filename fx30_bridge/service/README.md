# FX30 background service: test-only Phase 2 package

This package is inert until a person installs its plist with `launchctl`. It
does not alter the existing Terminal launcher, Companion, V4, OBS, RODECaster,
or port 3030.

## Safety boundary

- The test configuration binds only `127.0.0.1:3064`.
- It contains zero cameras, has camera warm-up disabled by construction, and
  cannot use a real camera command.
- Dashboard opening, notifications, ingest, delivery, email, RODECaster, and
  Zowie behaviour are disabled in the test configuration.
- The wrapper rejects every configuration unless it is loopback, not port
  3030, and has zero cameras.

## Files

- `run_fx30_service.command`: guarded wrapper, lifecycle records, and log
  rotation.
- `com.buildstudio.fx30-bridge.test.plist`: uninstalled test LaunchAgent.
- `fx30_service_diagnostics.command`: read-only launchd, listener, status, and
  log report.
- `fx30-service-test-config.json`: isolated configuration.

No test service has been started or installed. A later fixture-only launch on
port 3064 requires an explicit idle-window approval. Production activation on
3030 is separately gated.

## Production port 3030 package

The production files are prepared but are not installed automatically:

- `run_fx30_production_service.command` runs the real `config.json` on port
  3030 with `--background-service`, so it does not open OBS or a browser at
  login.
- `com.buildstudio.fx30-bridge.plist` is the real user LaunchAgent definition.
- `install_fx30_production_service.command --prepare` validates without
  installing or starting anything.
- `install_fx30_production_service.command --install` is the explicit live
  activation step. It may establish the configured warm camera connection and
  therefore requires an idle studio and operator approval.
- `install_fx30_production_service.command --uninstall` stops the agent and
  retains the installed plist with a disabled timestamp for recovery.

Do not install the production service during a take. After installation, first
prove `/health`, the dashboard, and read-only `/record/status`; do not use a
green dashboard alone as recording proof.
