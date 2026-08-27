#!/bin/bash
# Prevent overlapping ARM/READY runs from Companion or Stream Deck.
set -u

LOCK_DIR="/private/tmp/fx30_arm_ready.lock"
LOG_FILE="/private/tmp/fx30_arm_ready.log"

if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
  /bin/echo "ARM BUSY: studio setup is already running."
  exit 0
fi

cleanup() {
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

/bin/echo "ARM: preparing studio..."

# Start the NDI ingest channels, then confirm they are record-ready.
for channel in "NDI 1" "NDI 2" "NDI 3"; do
  /usr/bin/curl -fsS --max-time 4 -X POST "http://127.0.0.1:8080/ingest/startChannel" \
    -H "Content-Type: application/json" --data-raw "{\"channel\":\"$channel\"}" >/dev/null || true
done

/bin/sleep 3

ready=true
for channel in "NDI 1" "NDI 2" "NDI 3"; do
  /usr/bin/curl -fsS --max-time 4 "http://127.0.0.1:8080/ingest/activeChannels" | /usr/bin/grep -q "$channel" || ready=false
  /usr/bin/curl -fsS --max-time 4 -X POST "http://127.0.0.1:8080/ingest/requestCanRecord" \
    -H "Content-Type: application/json" --data-raw "{\"channel\":\"$channel\"}" | /usr/bin/grep -q '"rec" : true' || ready=false
done

# Keep the existing amplifier action, but do not let a slow smart-plug response hold the ARM lock.
nohup "/Users/jim/Documents/smart plug/g4-big-amp-on.command" >>"$LOG_FILE" 2>&1 &

# The bridge is already running. Open its dashboard and ask it to open/check LucidLink.
/usr/bin/open "http://127.0.0.1:3030/"
/usr/bin/curl -sS --max-time 10 -X POST "http://127.0.0.1:3030/studio/ready" >>"$LOG_FILE" 2>&1 || true

if [ "$ready" = true ]; then
  /bin/echo "ARM OK: NDI channels are active and record-ready."
else
  /bin/echo "ARM CHECK: dashboard and LucidLink opened; one or more NDI channels need attention."
fi
