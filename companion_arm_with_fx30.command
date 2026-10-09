#!/bin/bash
set -u

KASA_CONTROL="/Users/podcaststudio/Developer/build/kasa_plug_control/companion_kasa.command"
FX30_LAUNCHER="/Users/podcaststudio/Developer/StudioOS-Build-V4/arm_fx30_studio.sh"
PACKAGED_ARM="/Applications/StudioOS.app/Contents/Resources/Controls/strict_studio_arm_v4_local.command"
WARM_URL="http://127.0.0.1:3030/record/warm"
CONTROL_HELPER="/Applications/StudioOS.app/Contents/Resources/Controls/studioos-control-helper"
NOTIFY_COMMAND="/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_notification.command"
ZOWIE_GUARD="/Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge/zowie_guard.py"
ZOWIE_CONFIG="/Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge/config.json"
ZOWIE_STATUS="/Users/podcaststudio/Developer/StudioOS-Build-V4/zowie_main_stream_status.py"
JUST_ENGINE_APP="/Applications/Just In Mac Lite NDI.app"
OBS_APP="/Applications/OBS.app"
WAIT_SECONDS=180
JUST_API_URL="http://127.0.0.1:8080/ingest/allChannels"
STRICT_ARM_LOG="/private/tmp/strict_studio_arm_v4_local.log"
RUN_DIR="$HOME/Library/Application Support/StudioOS/Run"
LOCK_DIR="$RUN_DIR/companion-arm-fx30.lock"
STARTING_FILE="$RUN_DIR/companion-arm-fx30.starting"
STATUS_FILE="$RUN_DIR/companion-arm-fx30.status"
LOG_FILE="$HOME/Library/Logs/StudioOS/companion-arm-fx30.log"

write_status() {
  phase="$1"
  detail="$2"
  status_temp="$STATUS_FILE.$$"
  /usr/bin/printf '%s\n%s\n' "$phase" "$detail" >"$status_temp"
  /bin/mv "$status_temp" "$STATUS_FILE"
}

port_ready() {
  /usr/bin/nc -z 127.0.0.1 3030 >/dev/null 2>&1 ||
    /usr/sbin/lsof -nP -iTCP:3030 -sTCP:LISTEN >/dev/null 2>&1
}

warm_ready() {
  local timeout_seconds="$1"
  /usr/bin/curl -sS --max-time "$timeout_seconds" -X POST "$WARM_URL" |
    "$CONTROL_HELPER" warm-ready 2>/dev/null
}

launch_studio_apps() {
  /usr/bin/open "$JUST_ENGINE_APP" >/dev/null 2>&1 &&
    /usr/bin/open --env "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      "$OBS_APP" >/dev/null 2>&1
}

start_zowie_check() {
  ZOWIE_OUT="$RUN_DIR/zowie-check.$$.out"
  /bin/bash "$NOTIFY_COMMAND" "arm-zowie-check" "Studio ARM is waiting" \
    "Checking that each Zowie Main Stream is 4K. If one is HD, change and save it on that Zowie web page before RECORD." >/dev/null 2>&1 || true
  ( /usr/bin/python3 "$ZOWIE_STATUS" --check --fix --config "$ZOWIE_CONFIG" >"$ZOWIE_OUT" 2>&1; /bin/echo "$?" >"$ZOWIE_OUT.rc" ) &
  ZOWIE_PID=$!
}

just_api_alive() {
  local code
  code="$(/usr/bin/curl -s -m 5 -o /dev/null -w '%{http_code}' "$JUST_API_URL" 2>/dev/null)"
  [ "$code" = "200" ]
}

# A hung Just In keeps its port open but never answers, and "open" does nothing
# for an app that is already running, so restart it when its API stays silent.
ensure_just_in_api() {
  local attempt tries="${1:-8}"
  for attempt in $(/usr/bin/seq 1 "$tries"); do
    just_api_alive && return 0
    /bin/sleep 3
  done
  if /usr/bin/curl -s -m 3 "http://127.0.0.1:3030/record/status" 2>/dev/null | /usr/bin/grep -q '"recording": true'; then
    /bin/echo "ARM WARNING: Just In API is not responding but a recording is active; not restarting it"
    return 0
  fi
  /bin/echo "ARM WAIT: Just In API is not responding; restarting Just In"
  /usr/bin/osascript -e 'tell application "Just In Mac Lite NDI" to quit' >/dev/null 2>&1 || true
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    /usr/bin/pgrep -f "Just In Mac Lite NDI.app/Contents/MacOS" >/dev/null 2>&1 || break
    /bin/sleep 1
  done
  if /usr/bin/pgrep -f "Just In Mac Lite NDI.app/Contents/MacOS" >/dev/null 2>&1; then
    /usr/bin/pgrep -f "Just In Mac Lite NDI.app/Contents/MacOS" | /usr/bin/xargs /bin/kill 2>/dev/null || true
    /bin/sleep 2
  fi
  /usr/bin/open "$JUST_ENGINE_APP" >/dev/null 2>&1 || true
  for attempt in $(/usr/bin/seq 1 20); do
    just_api_alive && { /bin/echo "ARM WAIT: Just In API is back"; return 0; }
    /bin/sleep 2
  done
  /bin/echo "ARM WARNING: Just In API still not responding after restart"
}

run_strict_arm() {
  ensure_just_in_api
  write_status "checking" "4K CHECK"
  zowie_warning=false
  /bin/echo "ARM WAIT: collecting Zowie Main Stream 4K check"
  wait "$ZOWIE_PID" 2>/dev/null || true
  zowie_result="$(/bin/cat "$ZOWIE_OUT" 2>/dev/null)"
  zowie_rc="$(/bin/cat "$ZOWIE_OUT.rc" 2>/dev/null || /bin/echo 1)"
  /bin/rm -f "$ZOWIE_OUT" "$ZOWIE_OUT.rc"
  if [ "$zowie_rc" = "0" ]; then
    /bin/echo "$zowie_result"
  else
    zowie_warning=true
    /bin/echo "ARM WARNING: Zowie Main Stream needs manual 4K setting: $zowie_result"
    /bin/bash "$NOTIFY_COMMAND" "arm-zowie-warning" "Zowie 4K warning" \
      "Change and save the affected Zowie Main Stream to 3840x2160 on its web page. RECORD will remain blocked until it is confirmed." >/dev/null 2>&1 || true
  fi
  strict_ok=false
  for strict_attempt in 1 2 3; do
    strict_mark="$(/usr/bin/wc -l <"$STRICT_ARM_LOG" 2>/dev/null || /bin/echo 0)"
    if STUDIO_ZOWIE_ALREADY_HANDLED=1 "$PACKAGED_ARM"; then
      strict_ok=true
      break
    fi
    [ "$strict_attempt" -ge 3 ] && break
    reason="$(/usr/bin/tail -n +$((strict_mark + 1)) "$STRICT_ARM_LOG" 2>/dev/null | /usr/bin/grep "FAIL:" | /usr/bin/cut -d' ' -f3- | /usr/bin/tr '\n' ';')"
    /bin/echo "ARM RETRY $strict_attempt: packaged check failed: ${reason:-unknown}"
    case "$reason" in
      *"Just In"*|*"storage pre-flight"*) ensure_just_in_api 1 ;;
    esac
    /bin/sleep 3
  done
  if [ "$strict_ok" = true ]; then
    if [ "$zowie_warning" = true ]; then
      write_status "warning" "4K CHECK"
    else
      write_status "ready" "PRESS RECORD"
    fi
    return 0
  fi
  write_status "failed" "CHECK ARM"
  return 1
}

# "id ip" for each enabled warm camera that has a configured IP.
camera_targets() {
  /usr/bin/python3 - "$ZOWIE_CONFIG" <<'PY'
import json, sys
for cam in json.load(open(sys.argv[1])).get("cameras", []):
    if cam.get("enabled", True) and cam.get("warmConnection", True) and cam.get("ip"):
        print(cam["id"], cam["ip"])
PY
}

# Prints the ids of cameras whose Sony control port (15740) is not answering yet.
cameras_not_listening() {
  while read -r cam_id cam_ip; do
    /usr/bin/nc -z -G 2 "$cam_ip" 15740 >/dev/null 2>&1 || printf '%s ' "$cam_id"
  done <<EOF
$(camera_targets)
EOF
}

# Prints "id: error" for each camera in a /record/warm JSON response.
warm_errors() {
  /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("bridge: no valid response"); sys.exit()
for c in d.get("cameraResults", []):
    if not c.get("ok"):
        print("%s: %s" % (c.get("id"), c.get("error")))
if d.get("error") and not d.get("cameraResults"):
    print("bridge: %s" % d["error"])
'
}

if [ "${1:-}" != "--worker" ]; then
  if ! launch_studio_apps; then
    /bin/echo "ARM FAILED: Just In or OBS launch failed"
    exit 1
  fi
  /bin/mkdir -p "$RUN_DIR" "$(/usr/bin/dirname "$LOG_FILE")"
  if [ -f "$LOCK_DIR/pid" ]; then
    worker_pid="$(/bin/cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$worker_pid" ] && /bin/kill -0 "$worker_pid" 2>/dev/null; then
      exit 0
    fi
    /bin/rm -rf "$LOCK_DIR"
  fi
  /usr/bin/touch "$STARTING_FILE"
  /usr/bin/nohup "$0" --worker </dev/null >>"$LOG_FILE" 2>&1 &
  /usr/bin/printf 'ARM\nCHECKING\n00:00\n'
  exit 0
fi

if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
  /bin/rm -f "$STARTING_FILE"
  exit 0
fi
/bin/rm -f "$STARTING_FILE"
/bin/echo "$$" >"$LOCK_DIR/pid"
/bin/date '+%s' >"$LOCK_DIR/started-at"
write_status "checking" "00:00"
trap '/bin/rm -rf "$LOCK_DIR"; /bin/rm -f "${ZOWIE_OUT:-/nonexistent}" "${ZOWIE_OUT:-/nonexistent}.rc"' EXIT HUP INT TERM

if ! "$KASA_CONTROL" on cameras; then
  write_status "failed" "CAMERA POWER"
  /bin/echo "ARM FAILED: camera smart plugs did not confirm ON"
  exit 1
fi

if ! port_ready; then
  "$FX30_LAUNCHER" || {
    write_status "failed" "FX30 START"
    /bin/echo "ARM FAILED: FX30 Terminal launch failed"
    exit 1
  }
fi

start_zowie_check

deadline=$((SECONDS + WAIT_SECONDS))
last_errors=""
same_error_count=0
last_note=-10
while [ "$SECONDS" -lt "$deadline" ]; do
  missing="$(cameras_not_listening)"
  if [ -n "$missing" ] || ! port_ready; then
    if [ $((SECONDS - last_note)) -ge 10 ]; then
      /bin/echo "ARM WAIT: waiting for ${missing:-FX30 bridge} to come online"
      last_note=$SECONDS
    fi
    /bin/sleep 2
    continue
  fi
  remaining=$((deadline - SECONDS))
  warm_timeout=60
  if [ "$remaining" -lt "$warm_timeout" ]; then
    warm_timeout="$remaining"
  fi
  warm_response="$(/usr/bin/curl -sS --max-time "$warm_timeout" -X POST "$WARM_URL" 2>/dev/null)"
  if /bin/echo "$warm_response" | "$CONTROL_HELPER" warm-ready >/dev/null 2>&1; then
    run_strict_arm
    exit "$?"
  fi
  errors="$(/bin/echo "$warm_response" | warm_errors)"
  /bin/echo "ARM WAIT: FX30 cameras not warm yet: $errors"
  case "$errors" in
    *"already in progress"*)
      # The bridge's own startup warm still holds the lock; this is not a failure.
      /bin/sleep 2
      continue
      ;;
  esac
  if [ "$errors" = "$last_errors" ]; then
    same_error_count=$((same_error_count + 1))
  else
    same_error_count=1
    last_errors="$errors"
  fi
  if [ "$same_error_count" -ge 4 ]; then
    write_status "failed" "CAMERA WARM"
    /bin/echo "ARM FAILED: cameras answer on the network but will not connect: $errors"
    exit 1
  fi
  /bin/sleep 2
done

write_status "failed" "CAMERA WARM"
if [ -n "${missing:-}" ]; then
  /bin/echo "ARM FAILED: ${missing}did not come online within ${WAIT_SECONDS}s (check power and PC Remote mode)"
else
  /bin/echo "ARM FAILED: all FX30 cameras did not become warm within ${WAIT_SECONDS}s"
fi
exit 1
