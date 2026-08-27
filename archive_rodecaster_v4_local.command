#!/bin/bash
# Queue the optional SSD-only RØDECaster archive from Companion page 4.

set -u

START_COMMAND="/Users/jim/Developer/build/fx30_just_control_v4_local/start_just_control_v4_local.command"
STATUS_URL="http://127.0.0.1:3060/v4/status"
ARCHIVE_URL="http://127.0.0.1:3060/v4/storage/rodecaster-archive"

if ! /usr/bin/curl -fsS --max-time 3 "$STATUS_URL" >/dev/null 2>&1; then
  /usr/bin/open -a Terminal "$START_COMMAND" >/dev/null 2>&1 || {
    /bin/echo "LOCAL RØDE ARCHIVE FAILED: V4 bridge could not start"
    exit 1
  }
  for attempt in {1..30}; do
    /usr/bin/curl -fsS --max-time 3 "$STATUS_URL" >/dev/null 2>&1 && break
    /bin/sleep 1
  done
fi

if ! /usr/bin/curl -fsS --max-time 10 -X POST \
  -H 'Content-Type: application/json' -d '{}' "$ARCHIVE_URL" >/dev/null
then
  /bin/echo "LOCAL RØDE ARCHIVE FAILED"
  exit 1
fi

/bin/echo "LOCAL RØDE ARCHIVE STARTED"
