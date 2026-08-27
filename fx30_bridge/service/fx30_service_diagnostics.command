#!/bin/bash
# Read-only diagnostics for the isolated FX30 test bridge. It never calls a
# state-changing endpoint and therefore cannot warm, record, ingest, or delete.

set -u

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${FX30_SERVICE_DIAGNOSTICS_PORT:-3064}"
LABEL="com.buildstudio.fx30-bridge.test"
BASE_URL="http://127.0.0.1:$PORT"
LOG_DIR="/Users/podcaststudio/Library/Logs/BuildStudio/FX30"

echo "FX30 service diagnostics (read-only)"
echo "label: $LABEL"
echo "port: $PORT"

echo "\nlaunchd state:"
/bin/launchctl print "gui/$(/usr/bin/id -u)/$LABEL" 2>&1 || echo "not loaded"

echo "\nlistener:"
/usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1 || echo "no listener"

echo "\nbridge status summary:"
/usr/bin/curl -fsS --max-time 3 "$BASE_URL/record/status" 2>/dev/null |
  /usr/bin/python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    print("status_unknown: endpoint unreachable or unreadable")
    raise SystemExit(0)
state = status.get("state") or {}
print(json.dumps({"recording": state.get("recording"), "recordActionRunning": state.get("record_action_running"), "cameraStatus": status.get("cameraStatus")}, indent=2, sort_keys=True))
' || echo "status_unknown: endpoint unreachable or unreadable"

echo "\nrecent service logs:"
for log in "$LOG_DIR/fx30-service.log" "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log"; do
  echo "--- $log ---"
  [ -f "$log" ] && /usr/bin/tail -n 40 "$log" || echo "absent"
done
