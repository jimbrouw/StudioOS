#!/bin/bash
# Production FX30 bridge wrapper for launchd. It serves port 3030 and keeps
# the existing bridge/API as the single source of truth, but suppresses
# automatic OBS and browser launches at login.

set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(cd "$SERVICE_DIR/.." && pwd)"
CONFIG_PATH="$HOME/Library/Application Support/StudioOS/Config/fx30-bridge.json"
LOG_DIR="/Users/podcaststudio/Library/Logs/BuildStudio/FX30"
SERVICE_LOG="$LOG_DIR/production-service.log"

read -r HOST PORT < <(/usr/bin/python3 -c '
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
print(config.get("host", ""), config.get("port", ""))
' "$CONFIG_PATH")

if [ "$PORT" != "3030" ] || { [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "0.0.0.0" ]; }; then
  echo "Refusing unexpected production FX30 config. Require port 3030 and host 127.0.0.1 or 0.0.0.0." >&2
  exit 64
fi

/bin/mkdir -p "$LOG_DIR"
umask 077

exec /usr/bin/python3 "$PACKAGE_DIR/bridge.py" \
  --config "$CONFIG_PATH" \
  --background-service >>"$SERVICE_LOG" 2>&1
