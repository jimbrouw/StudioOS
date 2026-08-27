#!/bin/bash
# Companion's shell action has a short timeout. Detach the guarded no-camera
# ARM process and let its own lock prevent concurrent preflights.

set -u

ARM_COMMAND="/Users/jim/Developer/build/arm_no_camera_v4_local.command"
LOG_FILE="/private/tmp/strict_studio_arm_v4_local.log"

/usr/bin/nohup "$ARM_COMMAND" </dev/null >>"$LOG_FILE" 2>&1 &
worker_pid=$!

if ! /bin/kill -0 "$worker_pid" 2>/dev/null; then
  /bin/echo "ARM LAUNCH FAILED"
  exit 1
fi

/bin/echo "ARM STARTING: NO CAMERA"
