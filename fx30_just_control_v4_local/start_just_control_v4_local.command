#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
exec /usr/bin/python3 v4_bridge.py --config config.json --open-dashboard
