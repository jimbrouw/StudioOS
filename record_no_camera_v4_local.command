#!/bin/bash
# Guarded coordinator for one disposable no-camera recording test.
# It is invoked only by V4's guarded local no-camera endpoint. StudioOS never
# calls this coordinator's hidden Companion route or recorder controls itself.

set -u

MODE_FILE="/Users/jim/Developer/build/.studio_record_mode"
STRICT_RECORD="/Users/jim/Developer/build/strict_studio_record_v4_local.command"
ARM_RECEIPT_DIR="/Users/jim/Developer/build/no_camera_test_receipts"
SESSION_FILE="/private/tmp/strict_studio_no_camera_recording.session"
RECEIPT_DIR="/Users/jim/Developer/build/no_camera_test_receipts"
LOG_FILE="/private/tmp/strict_studio_no_camera_record.log"
MAX_ARM_AGE_SECONDS=900

stamp() { /bin/date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { /bin/echo "$(stamp) $*" >>"$LOG_FILE"; }

fail() {
  log "NO CAMERA RECORD BLOCKED: $1"
  /bin/echo "RECORD BLOCKED: NO CAMERA - $1"
  exit 1
}

saved_mode="strict"
if [ -f "$MODE_FILE" ]; then
  saved_mode="$(/bin/cat "$MODE_FILE" 2>/dev/null || /bin/echo strict)"
fi
restore_mode() {
  /usr/bin/printf '%s\n' "$saved_mode" >"$MODE_FILE"
}

completed=false
cleanup() {
  if [ "$completed" != true ]; then
    restore_mode
  fi
}
trap cleanup EXIT

[ -x "$STRICT_RECORD" ] || fail "guarded record coordinator is unavailable"
[ ! -e "$SESSION_FILE" ] || fail "a no-camera recording session already needs STOP verification"
/bin/mkdir -p "$RECEIPT_DIR" || fail "could not prepare no-camera receipt folder"

latest_arm_receipt="$(/usr/bin/find "$ARM_RECEIPT_DIR" -maxdepth 1 -type f -name '*-arm-no-camera.txt' -print 2>/dev/null | /usr/bin/sort | /usr/bin/tail -n 1)"
[ -n "$latest_arm_receipt" ] || fail "press ARM: NO CAMERA first"
/usr/bin/grep -q 'result=success mode_restored_on_exit=true' "$latest_arm_receipt" || fail "latest no-camera ARM did not pass"

now="$(/bin/date +%s)"
armed_at="$(/usr/bin/stat -f '%m' "$latest_arm_receipt" 2>/dev/null || /bin/echo 0)"
if [ $((now - armed_at)) -gt "$MAX_ARM_AGE_SECONDS" ]; then
  fail "ARM: NO CAMERA is older than 15 minutes; arm again before recording"
fi

# strict_studio_record selects the isolated Companion 4/1/6 route only while
# this temporary mode is present. Its order is V4 Just In start+verification,
# then OBS and a single RØDECaster CC17 press. On a Just In failure it sends
# no OBS or RØDECaster start action.
/usr/bin/printf 'ndi-test\n' >"$MODE_FILE" || fail "could not enter no-camera recording mode"
log "no-camera recording requested; fresh_arm_receipt=$latest_arm_receipt"

record_output=""
if ! record_output="$("$STRICT_RECORD" 2>&1)"; then
  log "guarded record failed: $record_output"
  fail "Just In, OBS or RØDECaster did not pass the guarded start"
fi

if ! /usr/bin/printf 'prior_mode=%s\narmed_receipt=%s\nstarted_at=%s\n' \
  "$saved_mode" "$latest_arm_receipt" "$(stamp)" >"$SESSION_FILE"; then
  log "RECORD SENT but no session marker could be written; leave no-camera mode set and use guarded STOP"
  /bin/echo "RECORD SENT: NO CAMERA - STOP NOW AND CHECK RECEIPTS"
  completed=true
  exit 0
fi

log "RECORD SENT: NO CAMERA; Companion isolated start accepted"
/bin/echo "RECORD SENT: NO CAMERA"
completed=true
