#!/bin/bash
# Test-only, launchd-friendly V4 wrapper. It never selects port 3060 unless a
# later reviewed activation explicitly supplies both the port and override.

set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(cd "$SERVICE_DIR/.." && pwd)"
CONFIG_PATH="${1:-$SERVICE_DIR/v4-service-test-config.json}"
PORT="${2:-3063}"
LOG_DIR="$HOME/Library/Logs/BuildStudio/V4"
SERVICE_LOG="$LOG_DIR/v4-service.log"
MAX_LOG_BYTES=5242880
KEEP_LOGS=5

if [ "$PORT" = "3060" ] && [ "${V4_SERVICE_ALLOW_PRODUCTION_PORT:-}" != "approved" ]; then
  echo "Refusing production port 3060. Use the explicit reviewed activation procedure." >&2
  exit 64
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "V4 service config not found: $CONFIG_PATH" >&2
  exit 66
fi

/bin/mkdir -p "$LOG_DIR"
/usr/bin/python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$CONFIG_PATH"

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

rotate_log "$SERVICE_LOG"
rotate_log "$LOG_DIR/launchd.out.log"
rotate_log "$LOG_DIR/launchd.err.log"

log_event() {
  /bin/echo "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ') event=$1 port=$PORT config=$CONFIG_PATH" >>"$SERVICE_LOG"
}

umask 077
log_event "launch"
exec /usr/bin/python3 "$PACKAGE_DIR/v4_bridge.py" \
  --config "$CONFIG_PATH" \
  --port "$PORT" \
  >>"$SERVICE_LOG" 2>&1
