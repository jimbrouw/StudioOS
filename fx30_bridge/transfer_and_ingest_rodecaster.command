#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

find_rodecaster_mount() {
  /usr/bin/python3 -c 'from pathlib import Path; matches = [p for p in Path("/Volumes").iterdir() if (p / "RODECaster").is_dir()]; print(matches[0] if len(matches) == 1 else "")' 2>/dev/null
}

restore_normal_mode() {
  original_status=$?
  trap - EXIT
  set +e
  if [ -n "$(find_rodecaster_mount)" ]; then
    echo "Returning RØDECaster to normal USB audio/MIDI mode."
    ./rodecaster_select_device --button "Back"
    for attempt in {1..30}; do
      if [ -z "$(find_rodecaster_mount)" ]; then
        echo "RØDECaster Transfer Mode unmounted."
        break
      fi
      /bin/sleep 1
    done
  fi
  exit "$original_status"
}

trap restore_normal_mode EXIT

set +e
/usr/bin/pgrep -q -x obs
obs_check_status=$?
set -e

case "$obs_check_status" in
  0)
    echo "Refusing Transfer Mode while OBS is running."
    echo "Stop the production recording and close OBS before RØDECaster ingest."
    exit 1
    ;;
  1)
    ;;
  *)
    echo "Cannot safely determine whether OBS is running."
    echo "Transfer Mode was not requested."
    exit 1
    ;;
  esac

if [ -z "$(find_rodecaster_mount)" ]; then
  /usr/bin/open -a "RODECaster App"
  ./rodecaster_select_device
  ./rodecaster_select_device --button "Transfer Recordings"
  ./rodecaster_select_device --button "Continue"
else
  echo "RØDECaster Transfer Mode is already mounted."
fi

/usr/bin/python3 rodecaster_ingest.py --wait-seconds 60 --all-takes --delete-verified
