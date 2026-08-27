#!/usr/bin/env python3
"""Read-only live check for the configured Just In Mac Lite NDI channels."""

from __future__ import annotations

import json
from pathlib import Path

from just_control_api import JustControlApiError, JustControlClient
from v4_bridge import load_config


def main() -> int:
    config = load_config(Path(__file__).with_name("config.json"))
    api_config = config.get("api", {})
    client = JustControlClient(api_config.get("host", "127.0.0.1"), int(api_config.get("port", 8080)), float(api_config.get("timeoutSeconds", 5)))
    requested = [item["name"] for item in config["channels"]]
    try:
        available = client.all_channels()
        active = client.active_channels()
        checks = []
        for channel in requested:
            check = {"channel": channel, "configured": channel in available, "active": channel in active}
            if channel in available:
                check["canRecord"] = client.can_record(channel).get("rec", False)
                check["recording"] = client.recording_status(channel).get("rec", False)
                check["configuration"] = client.recording_configuration(channel).get("configurations", [])
            checks.append(check)
        result = {"ok": all(item.get("configured") and item.get("canRecord") for item in checks), "available": available, "active": active, "requested": checks}
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 2
    except JustControlApiError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "status": exc.status}, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
