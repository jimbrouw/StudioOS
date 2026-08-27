#!/bin/bash

# Companion treats $(...) as its own variable syntax inside action fields.
# Keep the command substitution here, outside the Companion editor, so the
# status button receives a real result instead of an empty JSON document.

# Companion's local shell runner is isolated from the bridge's loopback
# listener, while the studio LAN address is reachable from the same Mac.
status=$(/usr/bin/curl --noproxy '*' -fsS --max-time 5 http://192.168.0.100:3030/record/status 2>/dev/null) || {
  /bin/echo OFFLINE
  exit 0
}

/bin/printf '%s' "$status" | /usr/bin/python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError, ValueError):
    print("OFFLINE")
    raise SystemExit(0)

cameras = payload.get("cameraStatus") or []
print(
    "ONLINE"
    if cameras and all(camera.get("warmConnection") is True for camera in cameras)
    else "WORKING"
)
'
