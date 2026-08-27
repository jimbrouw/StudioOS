#!/bin/bash
# Guarded OBS and RODECaster stop command.
# Stops the OBS and RODECaster-only Companion control, then runs V4 ingest.

set -u

PROJECT_DIR="/Users/podcaststudio/Developer/StudioOS-Build-V4"
COMPANION_STOP="http://127.0.0.1:8000/api/location/4/2/5"
V4_STATUS="http://127.0.0.1:3060/v4/status"
V4_INGEST="http://127.0.0.1:3060/v4/ingest"
LOCK_COMMAND="$PROJECT_DIR/studio_recording_lock.command"
NOTIFY_COMMAND="$PROJECT_DIR/studio_notification.command"
LOG_FILE="/private/tmp/stop_obs_rodecaster_v4_local.log"
RECORDING_LOCK="/private/tmp/strict_studio_recording.lock"
STOP_SENT="/private/tmp/stop_obs_rodecaster_v4_local.sent"
RUN_LOCK="/private/tmp/stop_obs_rodecaster_v4_local.lock"

log() {
  /bin/echo "$(${DATE_COMMAND:-/bin/date} -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
}

fail() {
  reason="$1"
  log "STOP BLOCKED: $reason"
  /bin/bash "$NOTIFY_COMMAND" "stop-obs-rodecaster-failed" "Stop needs checking" "$reason" >/dev/null 2>&1 || true
  /bin/echo "STOP BLOCKED: $reason"
  exit 1
}

port_ready() {
  /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

[ -x "$LOCK_COMMAND" ] || fail "recording lock command is unavailable"
[ -f "$NOTIFY_COMMAND" ] || fail "notification command is unavailable"
[ -e "$RECORDING_LOCK" ] || fail "no guarded recording lock is present"
port_ready 3060 || fail "V4 bridge is offline"
port_ready 4455 || fail "OBS websocket is offline"

# A new guarded start owns a newer lock than any marker left by an earlier
# stop attempt. Allow that new session to receive one deliberate STOP.
marker_mtime="$(/usr/bin/stat -f '%m' "$STOP_SENT" 2>/dev/null || /bin/echo 0)"
lock_mtime="$(/usr/bin/stat -f '%m' "$RECORDING_LOCK" 2>/dev/null || /bin/echo 0)"
if [ "$lock_mtime" -gt "$marker_mtime" ]; then
  /bin/rmdir "$STOP_SENT" 2>/dev/null || true
  log "cleared stale stop marker because recording lock is newer"
fi

if ! /bin/mkdir "$RUN_LOCK" 2>/dev/null; then
  fail "another OBS and RODECaster stop is already running"
fi
cleanup() {
  /bin/rmdir "$RUN_LOCK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if ! /bin/mkdir "$STOP_SENT" 2>/dev/null; then
  fail "stop was already sent"
fi

log "OBS and RODECaster stop requested"
if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_STOP/down" >/dev/null; then
  fail "Companion did not accept the OBS and RODECaster stop press"
fi

/bin/sleep 1
release_ok=true
if ! /usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_STOP/up" >/dev/null; then
  release_ok=false
  log "Companion stop release failed; continuing to post-record ingest"
  /bin/bash "$NOTIFY_COMMAND" "stop-obs-rodecaster-release-failed" "Stop release needs checking" \
    "The OBS and RODECaster stop was sent, but Companion release failed. Post-record work will continue." >/dev/null 2>&1 || true
fi

log "OBS and RODECaster stopped; starting verified post-record ingest"
ingest_response=""
if ! ingest_response="$(/usr/bin/curl -fsS --max-time 300 -X POST "$V4_INGEST" 2>&1)"; then
  log "post-record ingest failed: $ingest_response"
  fail "post-record proxy or LucidLink delivery failed"
fi

"$LOCK_COMMAND" clear >/dev/null 2>&1 || fail "could not clear recording lock after verified ingest"
log "post-record ingest completed: $ingest_response"
if [ "$release_ok" = true ]; then
  /bin/echo "STOPPED: OBS AND RODECASTER; POST-RECORD COMPLETE"
else
  /bin/echo "STOPPED: OBS AND RODECASTER; POST-RECORD COMPLETE; RELEASE WARNING"
fi
