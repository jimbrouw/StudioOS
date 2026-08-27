#!/bin/bash
# Throttled, one-shot macOS studio notification. Notification failure never
# changes the result of the recording workflow that called this helper.

set -u

event_key="${1:-}"
title="${2:-}"
message="${3:-}"
cooldown_seconds="${STUDIO_NOTIFICATION_COOLDOWN_SECONDS:-30}"
state_dir="${STUDIO_NOTIFICATION_STATE_DIR:-/private/tmp/studio_notification_state}"
log_file="${STUDIO_NOTIFICATION_LOG_FILE:-/private/tmp/studio_notifications.log}"
osascript_path="${STUDIO_OSASCRIPT_PATH:-/usr/bin/osascript}"

case "$event_key" in
  ""|*[!A-Za-z0-9._-]*)
    /bin/echo "invalid-event-key"
    exit 2
    ;;
esac

log_receipt() {
  status="$1"
  clean_title="$(/bin/echo "$title" | /usr/bin/tr '\t\r\n' '   ')"
  clean_message="$(/bin/echo "$message" | /usr/bin/tr '\t\r\n' '   ')"
  /bin/mkdir -p "$(/usr/bin/dirname "$log_file")" 2>/dev/null || true
  /bin/echo "$(${DATE_COMMAND:-/bin/date} -u '+%Y-%m-%dT%H:%M:%SZ')\t$status\t$event_key\t$clean_title\t$clean_message" >>"$log_file" 2>/dev/null || true
  /bin/echo "$status"
}

if [ "${STUDIO_NOTIFICATIONS_DISABLED:-0}" = "1" ]; then
  log_receipt "disabled"
  exit 0
fi

if [ "${STUDIO_NOTIFICATION_DRY_RUN:-0}" = "1" ]; then
  log_receipt "dry-run"
  exit 0
fi

/bin/mkdir -p "$state_dir" 2>/dev/null || {
  log_receipt "state-failed"
  exit 1
}

lock_dir="$state_dir/$event_key.lock"
if ! /bin/mkdir "$lock_dir" 2>/dev/null; then
  log_receipt "suppressed"
  exit 0
fi
cleanup() {
  /bin/rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

now="$(/bin/date '+%s')"
last_file="$state_dir/$event_key.last"
last=0
if [ -f "$last_file" ]; then
  last="$(/bin/cat "$last_file" 2>/dev/null || /bin/echo 0)"
fi
case "$last" in
  ""|*[!0-9]*) last=0 ;;
esac
case "$cooldown_seconds" in
  ""|*[!0-9]*) cooldown_seconds=30 ;;
esac

if [ $((now - last)) -lt "$cooldown_seconds" ]; then
  log_receipt "suppressed"
  exit 0
fi

if "$osascript_path" \
  -e 'on run argv' \
  -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
  -e 'end run' \
  "$title" "$message" >/dev/null 2>&1
then
  /bin/echo "$now" >"$last_file"
  log_receipt "sent"
  exit 0
fi

log_receipt "failed"
exit 1
