#!/bin/bash
# Owns the detached RECORD run and releases the single-flight reservation.

set -u

RUN_LOCK="/private/tmp/strict_studio_record_v4_local_run.lock"
COORDINATOR="/Users/podcaststudio/Developer/StudioOS-Build-V4/strict_studio_record_v4_local.command"

cleanup() {
  /bin/rmdir "$RUN_LOCK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$COORDINATOR"
