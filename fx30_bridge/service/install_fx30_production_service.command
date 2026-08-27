#!/bin/bash
# Prepare, install, or uninstall the real port-3030 FX30 LaunchAgent.

set -euo pipefail

ACTION="${1:---prepare}"
SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_PLIST="$SERVICE_DIR/com.buildstudio.fx30-bridge.plist"
TARGET_PLIST="/Users/podcaststudio/Library/LaunchAgents/com.buildstudio.fx30-bridge.plist"
LABEL="com.buildstudio.fx30-bridge"
DOMAIN="gui/$(/usr/bin/id -u)"

/usr/bin/plutil -lint "$SOURCE_PLIST"
/bin/bash -n "$SERVICE_DIR/run_fx30_production_service.command"
/usr/bin/python3 "$SERVICE_DIR/../bridge.py" --help >/dev/null

case "$ACTION" in
  --prepare)
    echo "PREPARE OK: production FX30 service files validate. Nothing installed or started."
    ;;
  --install)
    /bin/mkdir -p /Users/podcaststudio/Library/LaunchAgents /Users/podcaststudio/Library/Logs/BuildStudio/FX30
    /bin/cp "$SOURCE_PLIST" "$TARGET_PLIST"
    /bin/launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    /bin/launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
    /bin/launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "INSTALLED: $LABEL. Verify http://127.0.0.1:3030/health before using controls."
    ;;
  --uninstall)
    /bin/launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    if [ -f "$TARGET_PLIST" ]; then
      /bin/mv "$TARGET_PLIST" "$TARGET_PLIST.disabled-$(/bin/date +%Y%m%d-%H%M%S)"
    fi
    echo "UNINSTALLED: $LABEL. The previous plist was retained with a disabled timestamp."
    ;;
  *)
    echo "Usage: $0 --prepare|--install|--uninstall" >&2
    exit 64
    ;;
esac
