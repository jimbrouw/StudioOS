#!/bin/bash
# Read-only V4 service diagnostics. This script never starts, stops, or sends
# any recorder, camera, OBS, Companion, or RODECaster command.

set -u

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_PATH="${1:-$SERVICE_DIR/v4-service-test-config.json}"
PORT="${V4_SERVICE_DIAGNOSTICS_PORT:-3063}"
LABEL="com.buildstudio.just-control-v4-local.test"
BASE_URL="http://127.0.0.1:$PORT"
LOG_DIR="/Users/podcaststudio/Library/Logs/BuildStudio/V4"

echo "V4 service diagnostics (read-only)"
echo "label: $LABEL"
echo "port: $PORT"
echo "config: $CONFIG_PATH"

echo "\nlaunchd state:"
/bin/launchctl print "gui/$(/usr/bin/id -u)/$LABEL" 2>&1 || echo "not loaded"

echo "\nlistener:"
/usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1 || echo "no listener"

echo "\nV4 status summary:"
/usr/bin/curl -fsS --max-time 3 "$BASE_URL/v4/status" 2>/dev/null |
  /usr/bin/python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    print("status_unknown: endpoint unreachable or unreadable")
    raise SystemExit(0)
keys = ("ok", "service", "localOnly", "recording", "requestedChannels", "postRecord")
print(json.dumps({key: status.get(key) for key in keys}, indent=2, sort_keys=True))
' || echo "status_unknown: endpoint unreachable or unreadable"

echo "\nrecent service logs:"
for log in "$LOG_DIR/v4-service.log" "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log"; do
  echo "--- $log ---"
  [ -f "$log" ] && /usr/bin/tail -n 40 "$log" || echo "absent"
done
