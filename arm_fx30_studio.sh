#!/bin/bash

# Companion entry point for ARM / READY.
# Open one visible Studio terminal while the bridge starts, and ignore
# repeated presses while startup is in progress or the bridge is running.

HOST="127.0.0.1"
PORT="3030"
LOCK_DIR="/tmp/fx30_studio_arm_start.lock"
STUDIO_COMMAND="/Users/jim/Developer/build/start_fx30_studio.command"
LOCK_TIMEOUT_SECONDS=180

bridge_is_running() {
  /usr/bin/nc -z "$HOST" "$PORT" >/dev/null 2>&1
}

if bridge_is_running; then
  exit 0
fi

# Recover automatically from a launcher that failed before opening the port.
if [ -d "$LOCK_DIR" ]; then
  now=$(/bin/date +%s)
  modified=$(/usr/bin/stat -f %m "$LOCK_DIR" 2>/dev/null || /bin/echo 0)
  if [ $((now - modified)) -ge "$LOCK_TIMEOUT_SECONDS" ]; then
    /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
fi

# mkdir is atomic: only one rapid button press can acquire the startup lock.
if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi

if ! /usr/bin/open -a Terminal "$STUDIO_COMMAND"; then
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
  exit 1
fi

# Remove the lock once the bridge is listening, or after the startup timeout.
(
  elapsed=0
  while [ "$elapsed" -lt "$LOCK_TIMEOUT_SECONDS" ]; do
    if bridge_is_running; then
      break
    fi
    /bin/sleep 2
    elapsed=$((elapsed + 2))
  done
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
) >/dev/null 2>&1 &

exit 0
