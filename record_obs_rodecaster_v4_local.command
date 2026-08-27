#!/bin/bash
# Guarded OBS and RODECaster recording command.
# Dispatches the Companion control containing OBS and RODECaster CC17 only.

set -u

PROJECT_DIR="/Users/podcaststudio/Developer/StudioOS-Build-V4"
LOCK_COMMAND="$PROJECT_DIR/studio_recording_lock.command"
NOTIFY_COMMAND="$PROJECT_DIR/studio_notification.command"
COMPANION_RECORD="http://127.0.0.1:8000/api/location/4/1/6"
LOG_FILE="/private/tmp/record_obs_rodecaster_v4_local.log"
STOP_SENT="/private/tmp/strict_studio_stop_v4_local.sent"
OBS_RODECASTER_STOP_SENT="/private/tmp/stop_obs_rodecaster_v4_local.sent"

log() {
  /bin/echo "$(${DATE_COMMAND:-/bin/date} -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
}

fail() {
  reason="$1"
  log "RECORD BLOCKED: $reason"
  /bin/bash "$NOTIFY_COMMAND" "record-obs-rodecaster-failed" "Recording blocked" "$reason" >/dev/null 2>&1 || true
  /bin/echo "RECORD BLOCKED: $reason"
  exit 1
}

port_ready() {
  /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

[ -x "$LOCK_COMMAND" ] || fail "recording lock command is unavailable"
[ -f "$NOTIFY_COMMAND" ] || fail "notification command is unavailable"
port_ready 4455 || fail "OBS websocket is offline"

if ! /usr/sbin/system_profiler SPAudioDataType -detailLevel mini 2>/dev/null |
  /usr/bin/grep -q "RODECaster Pro II"
then
  fail "RODECaster Pro II is not detected"
fi

if ! /bin/mkdir /private/tmp/record_obs_rodecaster_v4_local.lock 2>/dev/null; then
  fail "another OBS and RODECaster recording start is already running"
fi
cleanup_start() {
  /bin/rmdir /private/tmp/record_obs_rodecaster_v4_local.lock 2>/dev/null || true
}
trap cleanup_start EXIT INT TERM

log "OBS and RODECaster recording requested; preflight passed"
"$LOCK_COMMAND" start >/dev/null || fail "could not create recording lock"
/bin/rmdir "$STOP_SENT" 2>/dev/null || true
/bin/rmdir "$OBS_RODECASTER_STOP_SENT" 2>/dev/null || true

if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_RECORD/down" >/dev/null; then
  "$LOCK_COMMAND" clear >/dev/null 2>&1 || true
  fail "Companion did not accept the OBS and RODECaster press"
fi

log "OBS and RODECaster recording start dispatched through Companion 4/1/6"
/bin/sleep 1
if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_RECORD/up" >/dev/null; then
  /bin/bash "$NOTIFY_COMMAND" "record-obs-rodecaster-release-failed" "Recording needs checking" \
    "The OBS and RODECaster press was sent, but its release failed." >/dev/null 2>&1 || true
  /bin/echo "RECORD RELEASE WARNING"
  exit 1
fi

/bin/echo "RECORD SENT: OBS AND RODECASTER"
