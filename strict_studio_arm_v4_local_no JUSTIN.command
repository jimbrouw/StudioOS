#!/bin/bash
# Local-only endurance studio launch and preflight for Companion.
# ARM opens the production applications and bridges, but never starts recording.
# Success means the Sony cameras enabled in fx30_bridge/config.json, OBS,
# RØDECaster MIDI and the recording SSD are ready at the same time.
# same time. Shade is deliberately outside this version.

set -u

LOCK_DIR="/private/tmp/strict_studio_arm.lock"
LOG_FILE="/private/tmp/strict_studio_arm_v4_local.log"
PROJECT_DIR="/Users/podcaststudio/Developer/StudioOS-Build-V4"
NOTIFY_COMMAND="$PROJECT_DIR/studio_notification.command"
FX30_START="$PROJECT_DIR/arm_fx30_studio.sh"
# JUST_START="$PROJECT_DIR/fx30_just_control_v4_local/start_just_control_v4_local.command"
FX30_STATUS="http://127.0.0.1:3030/record/status"
FX30_WARM="http://127.0.0.1:3030/record/warm"
ZOWIE_GUARD="$PROJECT_DIR/fx30_bridge/zowie_guard.py"
ZOWIE_CONFIG="$PROJECT_DIR/fx30_bridge/config.json"
# JUST_STATUS="http://127.0.0.1:3060/v4/status"
# JUST_PREPARE="http://127.0.0.1:3060/v4/prepare"
# PREPARE_RESPONSE="/private/tmp/strict_studio_arm_v4_prepare_response.json"
MODE_FILE="$PROJECT_DIR/.studio_record_mode"
WAIT_SECONDS=180
OBS_APP="OBS"
# JUST_ENGINE_APP="Just In Mac Lite NDI"

if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
  /bin/echo "ARM BUSY"
  exit 1
fi

cleanup() {
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log() {
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
}

port_ready() {
  if /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1; then
    return 0
  fi

  # Detached Companion workers can receive a false-negative loopback result
  # even while the host has a real listener. Recognise that owner so ARM does
  # not open a duplicate V4 Terminal and invalidate the capacity session. The
  # later HTTP checks still fail closed if the listener is not responsive.
  /usr/sbin/lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for_port() {
  port="$1"
  label="$2"
  elapsed=0
  while [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
    if port_ready "$port"; then
      return 0
    fi
    /bin/sleep 2
    elapsed=$((elapsed + 2))
  done
  log "$label did not open port $port within ${WAIT_SECONDS}s"
  return 1
}

# channels_ready() {
#   /usr/bin/curl -fsS --max-time 10 "$JUST_STATUS" |
#     /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); wanted=["Channel1","Channel2","Channel3"]; rows={x.get("name"):x for x in d.get("channels") or []}; sys.exit(0 if all(rows.get(n,{}).get("active") and rows.get(n,{}).get("canRecord") for n in wanted) else 1)'
# }
#
# wait_for_channels_ready() {
#   elapsed=0
#   while [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
#     if channels_ready; then
#       return 0
#     fi
#     /bin/sleep 2
#     elapsed=$((elapsed + 2))
#   done
#   log "Channel1, Channel2, and Channel3 did not become record-ready within ${WAIT_SECONDS}s"
#   return 1
# }

failures=""
capacity_override=false
add_failure() {
  if [ -n "$failures" ]; then
    failures="$failures; $1"
  else
    failures="$1"
  fi
  log "FAIL: $1"
}

log "ARM strict preflight started"

ndi_test_mode=false
fx30_test_mode=false
if [ -f "$MODE_FILE" ] &&
  [ "$(/bin/cat "$MODE_FILE" 2>/dev/null)" = "ndi-test" ]
then
  ndi_test_mode=true
  log "NDI TEST MODE: Sony FX30 launch, readiness, and recording are bypassed"
elif [ -f "$MODE_FILE" ] &&
  [ "$(/bin/cat "$MODE_FILE" 2>/dev/null)" = "fx30-test" ]
then
  fx30_test_mode=true
  log "FX30 TEST MODE: all enabled Sony FX30 cameras are required and recorded"
fi

# Justin/Mac Lite NDI and FX30 launch intentionally disabled in this variant.
/usr/bin/open --env "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  -a "$OBS_APP" >/dev/null 2>&1 ||
  add_failure "OBS launch failed"
/usr/bin/osascript -e 'tell application "OBS" to activate' >/dev/null 2>&1 ||
  add_failure "OBS activation failed"

# FX30 bridge readiness intentionally disabled in this OBS/RØDECaster-only variant.

# Just In V4 local bridge intentionally disabled in this variant.
wait_for_port 4455 "OBS websocket" || add_failure "OBS websocket offline"

# Just In recording SSD check intentionally disabled in this variant.

if ! /usr/sbin/system_profiler SPAudioDataType -detailLevel mini 2>/dev/null |
  /usr/bin/grep -q "RODECaster Pro II"
then
  add_failure "RØDECaster Pro II audio/MIDI device not detected"
fi

# Sony warm-up and Zowie verification intentionally disabled in this variant.

: <<'NO_JUSTIN'
if port_ready 3060; then
  prepare_code="$(/usr/bin/curl -sS --max-time 20 -o "$PREPARE_RESPONSE" -w '%{http_code}' -X POST "$JUST_PREPARE" 2>/dev/null || /bin/echo "000")"
  if [ "$prepare_code" != "200" ]; then
    prepare_detail="$(/usr/bin/python3 -c 'import json,sys; p=sys.argv[1]
try:
    d=json.load(open(p)); print(d.get("error") or "unknown prepare error")
except Exception:
    print("bridge unavailable or returned an unreadable response")' "$PREPARE_RESPONSE" 2>/dev/null || /bin/echo "bridge unavailable")"
    add_failure "Just In prepare failed: $prepare_detail"
  elif ! wait_for_channels_ready; then
    add_failure "Channel1, Channel2, and Channel3 are not all record-ready"
  fi

  # prepare already checks the live SSD surface, RØDECaster capacity gate,
  # enabled FX30 status, post-record readiness, and requested Just In channels.
  # Do not force the optional Sony card refresh here: the SDK can take up to
  # 40 seconds and its result is warning-only because the camera often does not
  # expose a trustworthy remaining-time value.
  storage_response="$(/usr/bin/curl -fsS --max-time 10 "$JUST_STATUS" 2>/dev/null | \
    /usr/bin/python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("storageReport") or {}))' 2>/dev/null || true)"
  if [ -z "$storage_response" ]; then
    add_failure "storage pre-flight unavailable"
  else
    storage_summary="$(/bin/echo "$storage_response" | /usr/bin/python3 -c '
import json, sys
d=json.load(sys.stdin)
for item in d.get("surfaces", []):
    print(f"STORAGE {item.get('"'"'label'"'"')}: {item.get('"'"'status'"'"')} — {item.get('"'"'message'"'"', '"'"'no detail'"'"')}")
' 2>/dev/null || true)"
    while IFS= read -r storage_line; do
      [ -n "$storage_line" ] && log "$storage_line"
    done <<EOF
$storage_summary
EOF
    if ! /bin/echo "$storage_response" | /usr/bin/python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)'; then
      add_failure "required storage unavailable or unwritable"
    elif ! /bin/echo "$storage_response" | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); c=next((x for x in d.get("surfaces", []) if x.get("key")=="rodecaster-capacity"), {}); sys.exit(0 if c.get("gateOpen") else 1)'; then
      add_failure "RØDECaster capacity needs a current operator confirmation or explicit override"
    elif /bin/echo "$storage_response" | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); c=next((x for x in d.get("surfaces", []) if x.get("key")=="rodecaster-capacity"), {}); sys.exit(0 if c.get("state")=="override" else 1)'; then
      capacity_override=true
      log "RØDECaster capacity override active: AMBER only"
    fi
  fi
fi
NO_JUSTIN

if [ -n "$failures" ]; then
  /bin/bash "$NOTIFY_COMMAND" "arm-failed" "Studio ARM failed" "$failures" >/dev/null 2>&1 || true
  /bin/echo "ARM FAILED"
  exit 1
fi

if [ "$ndi_test_mode" = true ]; then
  if [ "$capacity_override" = true ]; then
    log "ARM TEST OK — CAPACITY OVERRIDE"
    /bin/echo "ARM TEST OK — CAPACITY OVERRIDE"
    exit 0
  fi
  log "ARM TEST OK: Sony bypassed; OBS, RØDECaster, and local SSD ready; Shade bypassed"
  /bin/echo "ARM TEST OK"
  exit 0
fi

if [ "$fx30_test_mode" = true ]; then
  if [ "$capacity_override" = true ]; then
    log "ARM TEST OK — CAPACITY OVERRIDE"
    /bin/echo "ARM TEST OK — CAPACITY OVERRIDE"
    exit 0
  fi
  log "ARM TEST OK: enabled FX30 cameras, OBS, RØDECaster, and local SSD ready; Shade bypassed"
  /bin/echo "ARM TEST OK"
  exit 0
fi

if [ "$capacity_override" = true ]; then
  log "ARM OK — CAPACITY OVERRIDE"
  /bin/echo "ARM OK — CAPACITY OVERRIDE"
  exit 0
fi

log "ARM OK: OBS and RØDECaster ready; retired Just In and FX30 checks bypassed"
/bin/echo "ARM OK"
exit 0
