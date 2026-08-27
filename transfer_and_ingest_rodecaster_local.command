#!/bin/bash
# V4 RØDECaster archive. Copies takes to the Extreme SSD and LucidLink,
# verifies them, retains the card copies, and returns to audio/MIDI mode.

set -euo pipefail
cd /Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge

STATE_FILE="/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_storage_state_v4_local.json"

find_rodecaster_mount() {
  /usr/bin/python3 -c 'from pathlib import Path; matches = [p for p in Path("/Volumes").iterdir() if (p / "RODECaster").is_dir()]; print(matches[0] if len(matches) == 1 else "")' 2>/dev/null
}

restore_normal_mode() {
  original_status=$?
  trap - EXIT
  set +e
  if [ -n "$(find_rodecaster_mount)" ]; then
    ./rodecaster_select_device --button "Back"
    for attempt in {1..30}; do
      [ -z "$(find_rodecaster_mount)" ] && break
      /bin/sleep 1
    done
  fi
  exit "$original_status"
}

trap restore_normal_mode EXIT

if /usr/bin/pgrep -q -x obs; then
  /bin/echo "Refusing Transfer Mode while OBS is running."
  exit 1
fi

if [ -z "$(find_rodecaster_mount)" ]; then
  /usr/bin/open -a "RODECaster App"
  ./rodecaster_select_device
  ./rodecaster_select_device --button "Transfer Recordings"
  ./rodecaster_select_device --button "Continue"
fi

/usr/bin/python3 rodecaster_ingest.py \
  --wait-seconds 60 \
  --all-takes \
  --state-file "$STATE_FILE"
