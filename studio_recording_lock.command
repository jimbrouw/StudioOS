#!/bin/bash
# Fail-closed marker used by the production Companion page.

set -u

LOCK_FILE="/private/tmp/strict_studio_recording.lock"

case "${1:-}" in
  start)
    /usr/bin/touch "$LOCK_FILE"
    /bin/echo "RECORDING LOCKED"
    ;;
  clear)
    /bin/rm -f "$LOCK_FILE"
    /bin/echo "RECORDING LOCK CLEARED"
    ;;
  status)
    if [ -e "$LOCK_FILE" ]; then
      /bin/echo "LOCKED"
      exit 1
    fi
    /bin/echo "CLEAR"
    ;;
  *)
    /bin/echo "Usage: $0 <start|clear|status>" >&2
    exit 2
    ;;
esac
