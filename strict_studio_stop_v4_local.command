#!/bin/bash
# Safety-critical stop for V4 local endurance mode. Stops every recorder and
# queues local SSD proxy creation, but starts no Shade or RØDECaster transfer.

set -u

MODE_FILE="/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_record_mode"
NO_CAMERA_RECORDING_SESSION="/private/tmp/strict_studio_no_camera_recording.session"
JUST_STOP="http://127.0.0.1:3060/v4/record/stop"
STOP_STRICT="http://127.0.0.1:8000/api/location/4/1/5"
STOP_NDI_TEST="http://127.0.0.1:8000/api/location/4/2/5"
LOG_FILE="/private/tmp/strict_studio_stop_v4_local.log"
NOTIFY_COMMAND="/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_notification.command"
RECORDING_LOCK="/private/tmp/strict_studio_recording.lock"
STOP_SENT="/private/tmp/strict_studio_stop_v4_local.sent"

# The RØDECaster action is a hardware toggle. Never send the hidden STOP chain
# unless the guarded RECORD path created the recording lock. Without this gate,
# STOP after a blocked start could turn an idle green RØDECaster into recording.
if [ ! -e "$RECORDING_LOCK" ]; then
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') STOP blocked: no guarded recording lock" >>"$LOG_FILE"
  /bin/bash "$NOTIFY_COMMAND" "stop-blocked-idle" "Studio STOP blocked" \
    "No guarded recording is active. No Sony, OBS, Just In, or RØDECaster stop command was sent." >/dev/null 2>&1 || true
  /bin/echo "STOP BLOCKED: NO GUARDED RECORDING"
  exit 1
fi

# A second STOP would repeat the RØDECaster hardware toggle. Keep an atomic
# sent marker even if later verification is partial, so repeated presses fail
# closed and require deliberate operator recovery.
if ! /bin/mkdir "$STOP_SENT" 2>/dev/null; then
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') STOP blocked: stop already sent" >>"$LOG_FILE"
  /bin/echo "STOP BLOCKED: ALREADY SENT"
  exit 1
fi

stop_button="$STOP_STRICT"
if [ -f "$MODE_FILE" ] &&
  [ "$(/bin/cat "$MODE_FILE" 2>/dev/null)" = "ndi-test" ]
then
  stop_button="$STOP_NDI_TEST"
fi

companion_stop_ok=true
if ! /usr/bin/curl -fsS --max-time 5 -X POST "$stop_button/press" >/dev/null; then
  companion_stop_ok=false
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') Companion hidden stop press failed: $stop_button" >>"$LOG_FILE"
fi

just_stop_response=""
if ! just_stop_response="$(/usr/bin/curl -fsS --max-time 20 -X POST "$JUST_STOP" 2>&1)"; then
  /bin/echo "JUST IN STOP FAILED: $just_stop_response" >>"$LOG_FILE"
  /bin/bash "$NOTIFY_COMMAND" "stop-incomplete" "Studio STOP needs checking" \
    "Just In stop confirmation is unavailable. Check all recorders." >/dev/null 2>&1 || true
  /bin/echo "STOP FAILED: JUST IN STILL NEEDS CHECKING"
  exit 1
fi

/bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') Local stop confirmed: $just_stop_response" >>"$LOG_FILE"

if [ "$companion_stop_ok" != true ]; then
  stop_attention="Just In stopped, but Companion stop actions were not sent. Check OBS and RØDECaster."
  if [ "$stop_button" = "$STOP_STRICT" ]; then
    stop_attention="Just In stopped, but Companion stop actions were not sent. Check OBS, RØDECaster, and the FX30s."
  fi
  /bin/bash "$NOTIFY_COMMAND" "stop-partial" "Studio STOP partial" \
    "$stop_attention" >/dev/null 2>&1 || true
  /bin/echo "STOP PARTIAL: COMPANION STOP ACTIONS NEED CHECKING"
  exit 1
fi

# A successful no-camera STOP restores the prior normal mode. On any failed
# STOP path above, keep its marker and mode in place so a retry cannot select
# the strict FX30 stop chain by accident.
if [ "$stop_button" = "$STOP_NDI_TEST" ] && [ -f "$NO_CAMERA_RECORDING_SESSION" ]; then
  prior_mode="$(/usr/bin/sed -n 's/^prior_mode=//p' "$NO_CAMERA_RECORDING_SESSION" | /usr/bin/head -n 1)"
  [ -n "$prior_mode" ] || prior_mode="strict"
  /usr/bin/printf '%s\n' "$prior_mode" >"$MODE_FILE"
  /bin/rm -f "$NO_CAMERA_RECORDING_SESSION"
fi

/bin/echo "STOPPED — LOCAL PROXIES STARTED"
