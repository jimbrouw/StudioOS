#!/bin/bash
# Companion's shell action returns promptly; the guarded coordinator retains
# the actual start receipt in its log and will not dispatch recorders until
# Just In has verified every requested channel.

set -u

RECORD_COMMAND="/Users/podcaststudio/Developer/StudioOS-Build-V4/record_no_camera_v4_local.command"
LOG_FILE="/private/tmp/strict_studio_no_camera_record.log"

[ -x "$RECORD_COMMAND" ] || {
  /bin/echo "RECORD BLOCKED: NO CAMERA - coordinator is unavailable"
  exit 1
}

/usr/bin/nohup "$RECORD_COMMAND" >>"$LOG_FILE" 2>&1 &
/bin/echo "RECORD STARTING: NO CAMERA"
