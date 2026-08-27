#!/bin/bash
# Guarded entry point for the visible RECORD Companion button.
# Recording is kept separate from ARM. RECORD performs a fast live preflight;
# the V4 start endpoint then enforces storage, capacity, post-record readiness,
# and all requested Just In channels before any independent recorder is sent.

set -u

LOCK_COMMAND="/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_recording_lock.command"
MODE_FILE="/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_record_mode"
JUST_START="http://127.0.0.1:3060/v4/record/start"
JUST_STOP="http://127.0.0.1:3060/v4/record/stop"
COMPANION_RECORD_STRICT="http://127.0.0.1:8000/api/location/4/1/2"
# This off-surface Companion button contains OBS and RØDECaster MIDI only.
# Do not point it at 4/1/3: that visible control is the manual RØDE archive.
COMPANION_RECORD_NDI_TEST="http://127.0.0.1:8000/api/location/4/1/6"
NOTIFY_COMMAND="/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_notification.command"
LOG_FILE="/private/tmp/strict_studio_record_v4_local.log"
STOP_SENT="/private/tmp/strict_studio_stop_v4_local.sent"

log() {
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
}

port_ready() {
  /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

block_record() {
  log "FAST PREFLIGHT BLOCKED: $1"
  /bin/bash "$NOTIFY_COMMAND" "record-fast-preflight-failed" "Studio RECORD blocked" \
    "$1. Press ARM, resolve the red item, then press RECORD once." >/dev/null 2>&1 || true
  /bin/echo "RECORD BLOCKED"
  exit 1
}

COMPANION_RECORD="$COMPANION_RECORD_STRICT"
COMPANION_ROUTE_LABEL="4/1/2 (FX30, OBS, and RØDECaster CC17)"
if [ -f "$MODE_FILE" ] &&
  [ "$(/bin/cat "$MODE_FILE" 2>/dev/null)" = "ndi-test" ]
then
  COMPANION_RECORD="$COMPANION_RECORD_NDI_TEST"
  COMPANION_ROUTE_LABEL="4/1/6 (NO CAMERA: OBS and RØDECaster CC17 only)"
fi

log "RECORD request received; fast preflight started"

port_ready 3060 || block_record "Just In V4 bridge is offline"
port_ready 4455 || block_record "OBS websocket is offline"
[ -d "/Volumes/Extreme SSD/JUSTCONTROL_RECORDING" ] || block_record "recording SSD is not mounted"

if ! /usr/sbin/system_profiler SPAudioDataType -detailLevel mini 2>/dev/null |
  /usr/bin/grep -q "RODECaster Pro II"
then
  block_record "RØDECaster Pro II is not detected"
fi

if [ "$COMPANION_RECORD" = "$COMPANION_RECORD_STRICT" ]; then
  port_ready 3030 || block_record "FX30 bridge is offline"
  if ! /usr/bin/curl -sS --max-time 120 -X POST http://127.0.0.1:3030/record/warm |
    /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("cameraResults") or []; sys.exit(0 if d.get("ok") and r and all(x.get("ok") and x.get("warmConnection") for x in r) else 1)' 2>/dev/null
  then
    block_record "physical FX30 Sony control connection is not ready"
  fi
fi

log "Fast device preflight passed"

# Start and verify Just In/NDI before dispatching any independent recorder
# actions. Companion runs every action in a button concurrently, even when an
# earlier HTTP action fails, so leaving this request inside the hidden button
# can start the RØDECaster alone after a capacity 409.
just_start_response=""
if ! just_start_response="$(/usr/bin/curl -fsS --max-time 30 -X POST "$JUST_START" 2>&1)"; then
  # A timeout can leave the start outcome unknown. Ask V4 to stop its owned
  # channels before returning; this does not dispatch Sony, OBS, or MIDI.
  /usr/bin/curl -fsS --max-time 20 -X POST "$JUST_STOP" >/dev/null 2>&1 || true
  /bin/bash "$NOTIFY_COMMAND" "record-guard-failed" "Studio RECORD blocked" \
    "Just In/NDI did not confirm start. Sony, OBS, and RØDECaster were not sent a start command." >/dev/null 2>&1 || true
  /bin/echo "RECORD BLOCKED: JUST IN/NDI DID NOT START"
  exit 1
fi

log "Just In/NDI start confirmed"
log "Dispatching Companion $COMPANION_ROUTE_LABEL"

"$LOCK_COMMAND" start >/dev/null
/bin/rmdir "$STOP_SENT" 2>/dev/null || true

if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_RECORD/down" >/dev/null; then
  /usr/bin/curl -fsS --max-time 20 -X POST "$JUST_STOP" >/dev/null 2>&1 || true
  "$LOCK_COMMAND" clear >/dev/null 2>&1 || true
  /bin/bash "$NOTIFY_COMMAND" "record-dispatch-failed" "Studio RECORD not sent" \
    "Companion did not accept the RECORD press. Nothing is confirmed recording." >/dev/null 2>&1 || true
  /bin/echo "RECORD FAILED"
  exit 1
fi

log "Companion start dispatched through $COMPANION_ROUTE_LABEL"

/bin/sleep 1
if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_RECORD/up" >/dev/null; then
  /bin/bash "$NOTIFY_COMMAND" "record-release-failed" "Studio RECORD needs checking" \
    "The RECORD press was sent, but its release failed. Check the recorders." >/dev/null 2>&1 || true
  /bin/echo "RECORD RELEASE WARNING"
  exit 1
fi

/bin/echo "RECORD SENT"
