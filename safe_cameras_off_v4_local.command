#!/bin/bash
# Turn off the camera Kasa plug through a hidden Companion button only when
# the strict recording lock is clear, or every recorder and copy job proves safe.

set -u

LOCK_FILE="/private/tmp/strict_studio_recording.lock"
FX30_STATUS="http://127.0.0.1:3030/record/status"
JUST_STATUS="http://127.0.0.1:3060/v4/status"
COMPANION_CAMERAS_OFF="http://127.0.0.1:8000/api/location/4/2/4"

if [ -e "$LOCK_FILE" ]; then
  if ! /usr/bin/curl -fsS --max-time 5 "$FX30_STATUS" |
    /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("state") or {}; sys.exit(0 if not s.get("recording") and not s.get("record_action_running") and not s.get("ingest_running") and (s.get("ingest_progress") or {}).get("status") in ("idle","complete") else 1)'
  then
    /bin/echo "CAMS OFF BLOCKED: FX30 recording or delivery is not safely complete"
    exit 1
  fi

  if ! /usr/bin/curl -fsS --max-time 5 "$JUST_STATUS" |
    /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); state=(d.get("postRecord") or {}).get("state","idle"); sys.exit(0 if not d.get("recording") and state in ("idle","complete") else 1)'
  then
    /bin/echo "CAMS OFF BLOCKED: Just In recording or delivery is not safely complete"
    exit 1
  fi

  if ! /usr/bin/python3 /Users/jim/Developer/build/fx30_bridge/obs_verify.py \
    --folder "/Volumes/Extreme SSD/TEST-RECS/OBS-MASTERS" \
    --expected-count 3 \
    --stable-check-seconds 2 >/dev/null
  then
    /bin/echo "CAMS OFF BLOCKED: three stable OBS camera files were not verified"
    exit 1
  fi
fi

/usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_CAMERAS_OFF/press" >/dev/null
/bin/sleep 1
/usr/bin/curl -fsS --max-time 5 -X POST "$COMPANION_CAMERAS_OFF/release" >/dev/null
/bin/rm -f "$LOCK_FILE"
/bin/echo "CAMS OFF SENT"
