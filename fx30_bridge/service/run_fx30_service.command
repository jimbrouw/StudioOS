#!/bin/bash
# Test-only, launchd-friendly FX30 wrapper. It cannot connect to or command a
# camera because it accepts only an empty-camera loopback test configuration.

set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(cd "$SERVICE_DIR/.." && pwd)"
CONFIG_PATH="${1:-$SERVICE_DIR/fx30-service-test-config.json}"
LOG_DIR="/Users/podcaststudio/Library/Logs/BuildStudio/FX30"
SERVICE_LOG="$LOG_DIR/fx30-service.log"
MAX_LOG_BYTES=5242880
KEEP_LOGS=5

if [ ! -f "$CONFIG_PATH" ]; then
  echo "FX30 service config not found: $CONFIG_PATH" >&2
  exit 66
fi

read -r HOST PORT CAMERA_COUNT < <(/usr/bin/python3 -c '
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
print(config.get("host", ""), config.get("port", ""), len(config.get("cameras") or []))
' "$CONFIG_PATH")

if [ "$HOST" != "127.0.0.1" ] || [ "$PORT" = "3030" ] || [ "$CAMERA_COUNT" != "0" ]; then
  echo "Refusing a non-isolated FX30 configuration. Require loopback, a non-3030 port, and zero cameras." >&2
  exit 64
fi

/bin/mkdir -p "$LOG_DIR"

rotate_log() {
  local file="$1"
  local size=0
  [ -f "$file" ] && size="$(/usr/bin/stat -f '%z' "$file" 2>/dev/null || echo 0)"
  [ "$size" -lt "$MAX_LOG_BYTES" ] && return 0
  local index
  for ((index=KEEP_LOGS - 1; index>=1; index--)); do
    [ -f "$file.$index" ] && /bin/mv -f "$file.$index" "$file.$((index + 1))"
  done
  /bin/mv -f "$file" "$file.1"
}

log_event() {
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') event=$1 port=$PORT config=$CONFIG_PATH" >>"$SERVICE_LOG"
}

on_signal() {
  log_event "signal"
  exit 143
}

rotate_log "$SERVICE_LOG"
rotate_log "$LOG_DIR/launchd.out.log"
rotate_log "$LOG_DIR/launchd.err.log"
trap on_signal HUP INT TERM

umask 077
log_event "launch"
set +e
/usr/bin/python3 "$PACKAGE_DIR/bridge.py" --config "$CONFIG_PATH" >>"$SERVICE_LOG" 2>&1
status=$?
set -e
log_event "exit-$status"
exit "$status"
