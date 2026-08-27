#!/bin/bash
# ARM-only no-FX30 readiness test. This wrapper makes the Sony bypass
# temporary, preserves the previous mode on every exit path, and never creates
# a no-camera recording entitlement.

set -u

MODE_FILE="/Users/jim/Developer/build/.studio_record_mode"
ARM_COMMAND="/Users/jim/Developer/build/strict_studio_arm_v4_local.command"
V4_STATUS="http://127.0.0.1:3060/v4/status"
RECORDING_LOCK="/private/tmp/strict_studio_recording.lock"
STOP_SENT="/private/tmp/strict_studio_stop_v4_local.sent"
RECEIPT_DIR="/Users/jim/Developer/build/no_camera_test_receipts"
STAMP="$(/bin/date -u '+%Y%m%dT%H%M%SZ')-$$"
RECEIPT="$RECEIPT_DIR/$STAMP-arm-no-camera.txt"

/bin/mkdir -p "$RECEIPT_DIR" || {
  /bin/echo "ARM FAILED: NO CAMERA - could not create test receipt"
  exit 1
}

had_mode=false
saved_mode=""
if [ -e "$MODE_FILE" ]; then
  had_mode=true
  saved_mode="$(/bin/cat "$MODE_FILE" 2>/dev/null || true)"
fi

restore_mode() {
  if [ "$had_mode" = true ]; then
    /usr/bin/printf '%s\n' "$saved_mode" >"$MODE_FILE"
  else
    /bin/rm -f "$MODE_FILE"
  fi
}
trap restore_mode EXIT HUP INT TERM

receipt() {
  /usr/bin/printf '%s %s\n' "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$RECEIPT"
}

fail() {
  receipt "result=failed detail=$1"
  /bin/echo "ARM FAILED: NO CAMERA - $1"
  exit 1
}

receipt "test=arm-no-camera prior_mode=${saved_mode:-strict}"

if ! status_json="$(/usr/bin/curl -fsS --max-time 5 "$V4_STATUS" 2>/dev/null)"; then
  fail "V4 status is unavailable"
fi

if ! status_detail="$(/usr/bin/printf '%s' "$status_json" | /usr/bin/python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    raise SystemExit("V4 status is unreadable")
if status.get("recording"):
    raise SystemExit("a V4 recording is active")
post = status.get("postRecordSummary") or {}
state = post.get("state", "idle")
if state in {"running", "checking", "attention", "failed"}:
    raise SystemExit("post-record work is " + state)
print("V4 stopped; post-record=" + state)
' 2>&1)"; then
  fail "$status_detail"
fi
receipt "preflight=$status_detail"

# A lock without its one-shot STOP receipt is ambiguous. Never reinterpret it
# as idle. A lock plus the STOP receipt remains historical evidence and is
# safe only because V4 just reported recording=false.
if [ -e "$RECORDING_LOCK" ] && [ ! -d "$STOP_SENT" ]; then
  fail "guarded recording lock is present without a STOP receipt"
fi
if [ -e "$RECORDING_LOCK" ]; then
  receipt "historical_recording_lock=present stop_receipt=present"
fi

/usr/bin/printf 'ndi-test\n' >"$MODE_FILE" || fail "could not enter temporary test mode"
receipt "mode=ndi-test temporary=true"

if arm_output="$("$ARM_COMMAND" 2>&1)"; then
  receipt "strict_arm_exit=0 output=${arm_output//$'\n'/ | }"
  if /usr/bin/printf '%s\n' "$arm_output" | /usr/bin/grep -qx 'ARM TEST OK\|ARM TEST OK — CAPACITY OVERRIDE'; then
    receipt "result=success mode_restored_on_exit=true"
    /bin/echo "ARM OK: NO CAMERA"
    exit 0
  fi
  fail "unexpected ARM result"
fi

failed_check="$(/usr/bin/printf '%s\n' "$arm_output" | /usr/bin/grep '^ARM FAILED' | /usr/bin/tail -n 1 | /usr/bin/sed 's/^ARM FAILED[: ]*//' || true)"
[ -n "$failed_check" ] || failed_check="guarded ARM preflight failed"
receipt "strict_arm_exit=nonzero output=${arm_output//$'\n'/ | }"
fail "$failed_check"
