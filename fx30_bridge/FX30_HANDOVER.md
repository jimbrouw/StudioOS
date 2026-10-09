# FX30 Three-Camera Fix — Handover

> The newest update is first. The original 2026-08-26 handover follows below it.

# Update 2026-10-09 — Faster, self-healing ARM

## Summary
ARM (the Companion button that runs `companion_arm_with_fx30.command`) used to
fail after ~3 minutes with `CAMERA WARM`, or fail and then work on a second
press. Five separate causes were found and fixed. Verified live, including
three full camera power cycles: all three cameras warm, all three Zowie boxes
`4K READY`, ARM ends `ready / PRESS RECORD` in ~20-30 s with cameras already on.

## What was wrong, and the fix

### 1. Sony SDK could not discover the cameras (CAMERA WARM failure)
- Symptom: `Enumeration of device information failed` for every camera, in
  `sony-rec` and in Imaging Edge Desktop Remote. The cameras were fine: ping
  0% loss, port 15740 open, and their SSDP multicast broadcasts were visible
  with `tcpdump` on `vlan0`. Local Network permission was already granted.
- Cause: multicast discovery packets never reach user-space apps on this Mac.
  CrowdStrike Falcon, BeyondTrust and the Cato helper all run as system
  extensions; this is outside our control and IT cannot be asked to change it.
- Fix: `sony-rec` now connects **directly by IP + MAC** using
  `CreateCameraObjectInfoEthernetConnection` (no discovery). The MAC identity
  rule is unchanged: `bridge.py` still asserts the MAC reported in
  `SONY_REC_READY`.
  - IP comes from `--ip` (set by `bridge.py` from the camera `ip` field in
    `config.json`), else from the system ARP table, else it falls back to the
    old multicast discovery.
  - Current camera IPs: fx30-a `192.168.50.118`, fx30-b `192.168.50.119`,
    fx30-c `192.168.50.120`. **Set DHCP reservations on the router** for these
    MACs; if an IP changes, update `ip` in `config.json`.
  - Connect time is now 2-3 s per camera.
- Files: `sony_rec_src/SonyRec.cpp` (rebuild with `sony_rec_src/build.sh`),
  `bridge.py` (`_server_command` passes `--ip`), `config.json` (`ip` fields;
  `config.json` is git-ignored, so add the `ip` fields by hand on a new Mac).
- Not fixed: Imaging Edge Desktop Remote still cannot see the cameras.

### 2. Camera MACs have changed since the 2026-08-26 handover
Current MACs (from `config.json`, confirmed against the ARP table):
- fx30-a: `9C:69:D3:3A:8C:9F`
- fx30-b: `9C:69:D3:21:76:61`
- fx30-c: `9C:69:D3:3A:8C:6F`

The MACs in the older section below are historical.

### 3. ARM script was slow and retried blindly
`companion_arm_with_fx30.command` now:
- runs the Zowie 4K check **in parallel** while the cameras boot;
- waits until each camera answers on port 15740 instead of calling the slow
  `/record/warm` every few seconds;
- treats `Warm reconnect already in progress` (the bridge's own startup warm
  holds the lock) as "keep waiting", not a failure;
- fails fast with a **named camera and error** if the same real error repeats
  4 times (e.g. `fx30-b: Enumeration failed`), and names any camera that never
  came online within `WAIT_SECONDS` (180);
- cleans up its `zowie-check.*.out` temp files on exit.

### 4. Zowie boxes 2 and 3 fell back to HD after a camera restart
- Cause (found by comparing the boxes): box 1 (never failed) had
  `edid_mode = 1` (4K30); boxes 2 and 3 had `edid_mode = 0` (Auto). With Auto,
  a camera that restarts can come up on a smaller picture first and the box
  follows it down.
- Fix: boxes 2 and 3 set to **EDID 4K30**. API: `POST /video?option=setinfo`
  with `{"group":"hdmi","opt":"set_input_info","data":<result of get_input_info
  with gsv2001.edid_mode set to 1>}`. Verified after three camera power cycles.
  To undo, set the box's Video Input EDID back to Auto in its web UI.
- Also fixed in `fx30_bridge/zowie_guard.py`:
  - Zowie **rejects the whole encoder save** when the sub-stream keyinterval is
    below 20, yet still returns status `00000` (the failure is only in the text
    field `rsp`). The old code wrote both streams and never read `rsp`. It now
    writes **only the Main Stream** and raises if `rsp` is not `succeed`.
  - A Main Stream GOP above its frame rate (boxes 2/3 had GOP 60 at 30 fps)
    makes Zowie ignore 4K changes, so it is corrected even while the box is
    currently 4K.
- `zowie_main_stream_status.py`:
  - new `--fix` mode (used by the ARM script) sets 4K instead of only warning;
    detail reads `4K FIXED` when it had to change something.
  - with `--fix`, an unreachable box is retried every 3 s for up to
    `zowieResolutionGuard.retrySeconds` (90), with one shared deadline, and is
    not reported as HD. The record-time check stays one-shot.

### 5. Just In's API hung, so the packaged ARM check failed on the first press
- Symptom in `/private/tmp/strict_studio_arm_v4_local.log`:
  `FAIL: Just In prepare failed: ... Could not reach Just In Mac Lite NDI at
  http://127.0.0.1:8080: timed out`, then `storage pre-flight unavailable`.
  Pressing ARM again worked once Just In recovered. Just In accepts the
  connection on port 8080 but never replies. The Mac was heavily loaded (VS
  Code, OBS, a BeyondTrust remote-desktop session, three Just In channels).
- Fix in `companion_arm_with_fx30.command`:
  - the health probe is `GET /ingest/allChannels` (the call V4 uses). `/`
    still answers when the API is stuck, so it is not a valid probe.
  - the packaged `strict_studio_arm_v4_local.command` gets **up to 3
    attempts**. Between attempts the failure reason is read from the packaged
    log; Just In or storage failures restart Just In (quit, relaunch) and wait
    for the API. Log lines: `ARM RETRY n: ...`.
  - Just In is never restarted while the FX30 bridge reports a recording.
- Not changed: the V4 service (`StudioOS V4 Service.app`) is a packaged app with
  a 5 s per-call timeout and no retry.
- The restart path was not exercised on a truly hung Just In; only the healthy
  path and the retry logic were run live.

## How to diagnose ARM failures
1. `~/Library/Logs/StudioOS/companion-arm-fx30.log`: look for `ARM FAILED`,
   `ARM RETRY`, `ARM WAIT`, `ZOWIE n: ...`.
2. `/private/tmp/strict_studio_arm_v4_local.log`: the packaged ARM's own
   `FAIL:` reasons.
3. `~/Library/Application Support/StudioOS/Run/companion-arm-fx30.status`.
4. Per camera: `ping`, `nc -z <ip> 15740`, then
   `./sony-rec --camera fx30-a --mac <MAC> --action status` from `fx30_bridge/`.
5. Zowie: `python3 zowie_main_stream_status.py --check --config
   fx30_bridge/config.json` (add `--fix` to correct). `4K FIXED` on every ARM
   means a box keeps reverting; check its EDID (should be 4K30).

## Rollback
Pre-change copies sit next to the originals (only the script backups and the
`sony-rec` backup are not part of the PR):
`sony-rec.pre-direct-ip-20261009`, `sony_rec_src/SonyRec.cpp.pre-direct-ip`,
`bridge.py.pre-direct-ip`, `config.json.pre-direct-ip`,
`zowie_guard.py.pre-fast-arm`, `zowie_main_stream_status.py.pre-fast-arm`,
`companion_arm_with_fx30.command.pre-fast-arm`,
`companion_arm_with_fx30.command.pre-retry`.

## Open items
- Zowie boxes 2/3 used to revert after camera power-off. EDID 4K30 held over
  three power cycles; keep watching for `4K FIXED` in the ARM log.
- Camera IPs must stay stable (DHCP reservations).
- A cold-start timing (cameras off to ARM OK) was not measured; expect roughly
  45-60 s plus a possible Just In restart and retry.
- IT-managed security software blocks multicast discovery. Direct-IP connect
  works around it; Imaging Edge Desktop Remote does not.

---

# Original handover (2026-08-26)

## Goal
Deterministic 3-camera Sony FX30 support in StudioOS. Root cause (proven):
the old `sony-rec` binary connects to "whichever FX30 the SDK enumerates
first" — `--camera fx30-a/b/c` was only a cosmetic label, not a real camera
selector.

## What's done and proven
- Rebuilt `sony-rec` from Sony's Camera Remote SDK sample as a scoped MVP:
  status / start / stop / `--server` warm protocol only. `ingest-*`,
  `format-card`, `delete-file`, `proxy-*`, `ftp-test` are explicit
  "not implemented in this MVP build" stubs — they never touch card media.
- Core fix: requires `--mac <AA:BB:...>`, enumerates every camera the SDK
  sees, connects **only** to the exact MAC match. Refuses (non-zero exit,
  never falls back) if no match, or if the connection can't be confirmed.
- **Tested live against real hardware**, individually and simultaneously:
  fx30-a → Cam1 `78:F5:05:44:97:0E`, fx30-b → Cam2 `78:F5:05:45:AB:9A`
  (needed a pairing retry once, then stable), fx30-c → Cam3
  `78:F5:05:46:64:E2`. All three connected correctly and concurrently
  through bridge.py's real `WarmCameraController` — no cross-grabbing.
- `bridge.py` updated: `WarmCameraController` now asserts the MAC reported
  in `SONY_REC_READY` matches the camera's configured `mac`, and fails
  closed (refuses to be "ready") on any mismatch. `--mac` is passed on every
  command. Also fixed a real bug: a failed server-bind on startup used to
  leave the warm Sony connection orphaned, blocking the next restart attempt.
- Working files live at (NOT inside `/Applications`):
  `/Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge/`
  - `sony-rec` — the new binary (also `sony_rec_src/SonyRec.cpp` + `build.sh`)
  - `bridge.py`, `cleanup_manager.py`, `obs_verify.py` — updated source
  - `config.json` — dev/test config with all 3 cameras + mac fields (reference)

## Two blockers found (both are Mac/OS issues, not code bugs)

### 1. Enterprise EDR blocks new binaries inside `/Applications/StudioOS.app`
This Mac has CrowdStrike Falcon + BeyondTrust/Avecto Endpoint Security
installed as system extensions. Any newly-compiled `sony-rec` placed at
`/Applications/StudioOS.app/Contents/Resources/Services/FX30/sony-rec` gets
SIGKILL'd on execution. Confirmed not a signing issue (the whole
`StudioOS.app` is itself only ad-hoc signed and runs fine). Workaround:
**run `sony-rec` and `bridge.py` from the dev folder above instead** — that
location has never been blocked. A real fix would need a CrowdStrike/
BeyondTrust allow-list exception (by file hash) from whoever administers it.

### 2. launchd background service can't reach the cameras (Local Network permission)
The exact same `sony-rec` connects instantly when run interactively in
Terminal, but **hangs/fails every time when started via `launchd`**
(confirmed with an isolated `launchctl submit` test — even a plain one-shot
`--action status`, no PTY/warm mode involved, never completes under
launchd). This points to macOS's Local Network privacy permission being
denied/unprompted for the launchd "responsible process" identity, which is
different from Terminal's already-trusted identity.
**Current workaround: run it as a plain foreground Terminal process**
(see `start_fx30_bridge_terminal.command` below), not as a launchd service.

## Current safe state
Production launchd service (`com.buildstudio.studioos.fx30-bridge`) and
`/Applications/StudioOS.app/.../sony-rec` are back to the **original,
untouched, working single-camera (fx30-a) setup**. Nothing in
`/Applications` was left modified. Backups of everything touched are in:
`~/Library/Application Support/StudioOS/PreDeployBackups/20260826T235318Z/`

## How to run the fixed version right now
Trigger `start_fx30_bridge_terminal.command` (in the dev folder above) —
either double-click it, or have Companion run it directly. It:
1. Stops the launchd-managed service so it can't fight for port 3030.
2. Runs the fixed `bridge.py` (dev copy) in the foreground with the real
   production config at `~/Library/Application Support/StudioOS/Config/fx30-bridge.json`.
3. Auto-restarts it if it exits, and keeps logging to the terminal window.

**Note for whoever picks this up:** if Companion triggering it doesn't work,
Companion itself might hit the same Local Network permission issue as
launchd (untested) — if so, check System Settings → Privacy & Security →
Local Network for missing entries for Terminal/Python/Companion.

## To fully finish (either or both, in any order)
1. **CrowdStrike/BeyondTrust exception**, so `sony-rec` can live inside
   `/Applications/StudioOS.app` again like normal. Email already drafted
   earlier this session with the exact file hash — ask whoever administers
   those tools.
2. **Local Network permission for launchd**, so the bridge can go back to
   being a proper auto-starting background service instead of a manually
   triggered Terminal window. Check System Settings → Privacy & Security →
   Local Network; may need re-testing after any exception/grant to confirm
   `launchctl submit`/the real service can reach the cameras.

Once either is resolved, re-point `run-fx30-bridge`
(`/Applications/StudioOS.app/Contents/Resources/Services/FX30/run-fx30-bridge`)
back at the dev-folder `bridge.py`/`sony-rec`, update the production config
at `~/Library/Application Support/StudioOS/Config/fx30-bridge.json` with each
camera's `mac` field and `--mac` in its command arrays (same pattern as the
dev `config.json`), enable `fx30-b`/`fx30-c`, and re-run the same live test
sequence used today (status on each camera individually, then all three
concurrently through `WarmCameraController`) before trusting it in a show.

## Known camera MACs
- fx30-a (Cam1): `78:F5:05:44:97:0E`
- fx30-b (Cam2): `78:F5:05:45:AB:9A`
- fx30-c (Cam3): `78:F5:05:46:64:E2`

## Safety constraints (unchanged)
Never implement/enable `format-card` or `delete-file` beyond the current
safe stubs without deliberate, explicit testing. One Sony SDK controller per
camera. Never expose port 3030 publicly. Never commit/log camera passwords
(production password lives only in
`~/Library/Application Support/StudioOS/Config/fx30-bridge.json`, permission
0600).
