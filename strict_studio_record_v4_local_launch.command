#!/bin/bash
# Fast Companion entry point. The internal shell action has a hard 20-second
# timeout, while a guarded FX30 warm-up can legitimately take longer. Reserve
# one run, detach the coordinator, and return before Companion kills it.

set -u

RUN_LOCK="/private/tmp/strict_studio_record_v4_local_run.lock"
WORKER="/Users/podcaststudio/Developer/StudioOS-Build-V4/strict_studio_record_v4_local_worker.command"
LOG_FILE="/private/tmp/strict_studio_record_v4_local.log"

if ! /bin/mkdir "$RUN_LOCK" 2>/dev/null; then
  /bin/echo "RECORD BUSY"
  exit 1
fi

/usr/bin/nohup "$WORKER" </dev/null >"$LOG_FILE" 2>&1 &
worker_pid=$!

if ! /bin/kill -0 "$worker_pid" 2>/dev/null; then
  /bin/rmdir "$RUN_LOCK" 2>/dev/null || true
  /bin/echo "RECORD LAUNCH FAILED"
  exit 1
fi

/bin/echo "RECORD STARTING"

