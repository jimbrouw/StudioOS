#!/bin/bash
# Select the guarded production path or a temporary endurance-test path.

set -u

MODE_FILE="/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_record_mode"

case "${1:-status}" in
  ndi-test)
    /bin/echo "ndi-test" >"$MODE_FILE"
    /bin/echo "ARM NDI TEST"
    ;;
  fx30-test)
    /bin/echo "fx30-test" >"$MODE_FILE"
    /bin/echo "ARM FX30 TEST"
    ;;
  strict)
    /bin/echo "strict" >"$MODE_FILE"
    /bin/echo "ARM STRICT"
    ;;
  status)
    current_mode="$(/bin/cat "$MODE_FILE" 2>/dev/null || true)"
    case "$current_mode" in
      ndi-test) /bin/echo "NDI TEST" ;;
      fx30-test) /bin/echo "FX30 TEST" ;;
      *) /bin/echo "STRICT" ;;
    esac
    ;;
  *)
    /bin/echo "Usage: $0 <ndi-test|fx30-test|strict|status>" >&2
    exit 2
    ;;
esac
