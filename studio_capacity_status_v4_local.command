#!/bin/bash
# Read-only Companion helper for the V4 RØDECaster capacity guardian.
set -u
STATUS_URL="http://127.0.0.1:3060/v4/storage"
if [ "${1:-status}" = "open-dashboard" ]; then /usr/bin/open "http://127.0.0.1:3060/" >/dev/null 2>&1 || true; fi
payload="$(/usr/bin/curl -fsS --max-time 5 "$STATUS_URL" 2>/dev/null || true)"
if [ -z "$payload" ]; then /bin/echo "CAPACITY OFFLINE"; exit 1; fi
/bin/echo "$payload" | /usr/bin/python3 -c '
import json,sys
try: row=next(x for x in json.load(sys.stdin).get("surfaces",[]) if x.get("key")=="rodecaster-capacity")
except Exception: print("CAPACITY OFFLINE"); raise SystemExit(1)
labels={"confirmed":"CAPACITY CONFIRMED","override":"CAPACITY OVERRIDE","below-threshold":"CAPACITY BELOW","stale":"CAPACITY STALE","unavailable":"CAPACITY UNAVAILABLE"}
print(labels.get(row.get("state"),"CAPACITY UNAVAILABLE")); raise SystemExit(0 if row.get("gateOpen") else 1)
'
