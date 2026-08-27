#!/bin/bash
# Companion/manual trigger: runs the FIXED FX30 bridge (mac-identity sony-rec)
# as a visible foreground Terminal process instead of a launchd background
# service, because launchd-launched processes are silently blocked from
# reaching the cameras on this Mac (see FX30_HANDOVER.md), while the exact
# same code works instantly when run interactively like this.

DEV="/Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge"
CONFIG="$HOME/Library/Application Support/StudioOS/Config/fx30-bridge.json"
TOOLS="/Applications/StudioOS.app/Contents/Resources/Tools"

echo "Stopping any launchd-managed fx30-bridge service so it can't fight for port 3030..."
launchctl bootout gui/$(id -u)/com.buildstudio.studioos.fx30-bridge 2>/dev/null
pkill -9 -f "sony-rec --camera" 2>/dev/null
sleep 2

cd "$DEV" || { echo "Cannot cd to $DEV"; exit 1; }
export PATH="$TOOLS:$PATH"
export DYLD_LIBRARY_PATH="$DEV:$DEV/CrAdapter${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

echo "Starting FX30 bridge (foreground) — keep this window open."
while true; do
  /usr/bin/python3 -u "$DEV/bridge.py" --config "$CONFIG"
  echo "bridge.py exited; restarting in 5 seconds... (Ctrl-C to stop)"
  sleep 5
done
