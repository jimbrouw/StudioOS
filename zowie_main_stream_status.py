#!/usr/bin/env python3
"""Read Zowie Main Stream settings and persist per-box Companion status."""

import argparse
import json
import time
from pathlib import Path

from fx30_bridge.zowie_guard import (
    ZowieGuardError,
    _configured_password,
    ensure_resolution,
    verify_resolution,
)


RUN_DIR = Path.home() / "Library/Application Support/StudioOS/Run"
DEVICE_DETAILS = {
    "fx30-a": ("ZOWIE 1", "101"),
    "fx30-b": ("ZOWIE 2", "102"),
    "fx30-c": ("ZOWIE 3", "103"),
}


def status_path(device_id, status_dir=RUN_DIR):
    return Path(status_dir) / f"zowie-main-stream-{device_id}.json"


def write_status(device_id, state, detail, status_dir=RUN_DIR):
    target = status_path(device_id, status_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"state": state, "detail": detail}), encoding="utf-8"
    )
    temporary.replace(target)


def read_status(device_id, status_dir=RUN_DIR):
    try:
        return json.loads(status_path(device_id, status_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "unknown", "detail": "Press ZOWIE 4K"}


def check_devices(config, status_dir=RUN_DIR, fix=False, retry_seconds=0, retry_interval=3):
    guard = config.get("zowieResolutionGuard") or {}
    results = []
    deadline = time.monotonic() + retry_seconds
    for device in guard.get("devices", []):
        if not device.get("enabled", True):
            continue
        device_id = device.get("cameraId", "")
        if device_id not in DEVICE_DETAILS:
            continue
        name, short_address = DEVICE_DETAILS[device_id]
        while True:
            try:
                password = _configured_password(device)
                if not password:
                    raise ZowieGuardError("login password is unavailable")
                check = ensure_resolution if fix else verify_resolution
                outcome = check(
                    device["host"],
                    device.get("username", "admin"),
                    password,
                    int(device.get("requiredWidth", guard.get("requiredWidth", 3840))),
                    int(device.get("requiredHeight", guard.get("requiredHeight", 2160))),
                    float(guard.get("timeoutSeconds", 5)),
                )
                state = "ready"
                detail = "4K FIXED" if fix and (outcome or {}).get("changed") is True else "4K READY"
            except (KeyError, TypeError, ValueError, ZowieGuardError) as exc:
                message = str(exc)
                state = "needs-4k" if "Main Stream is" in message or "read back" in message else "unavailable"
                detail = f"OPEN {short_address}" if state == "needs-4k" else "CHECK CONNECTION"
            # A box that is still booting is unreachable, not HD: keep trying.
            if state != "unavailable" or time.monotonic() >= deadline:
                break
            time.sleep(retry_interval)
        write_status(device_id, state, detail, status_dir)
        results.append({"id": device_id, "name": name, "state": state, "detail": detail})
    return results


def print_device_status(device_id, field, status_dir=RUN_DIR):
    name, _ = DEVICE_DETAILS[device_id]
    current = read_status(device_id, status_dir)
    state = current.get("state", "unknown")
    detail = current.get("detail", "Press ZOWIE 4K")
    if field == "phase":
        print(f"{name} {state.upper().replace('-', ' ')}")
    else:
        print(f"{name}\n{detail}")


def main():
    parser = argparse.ArgumentParser(description="Read Zowie Main Stream status")
    parser.add_argument("--config")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--fix", action="store_true", help="set Main Stream to 4K when a box is not already 4K")
    parser.add_argument("--device", choices=tuple(DEVICE_DETAILS))
    parser.add_argument("--field", choices=("display", "phase"))
    args = parser.parse_args()

    if args.check:
        if not args.config:
            parser.error("--check requires --config")
        with open(args.config, encoding="utf-8") as config_file:
            config_data = json.load(config_file)
        guard_retry = (config_data.get("zowieResolutionGuard") or {}).get("retrySeconds", 90)
        results = check_devices(
            config_data,
            fix=args.fix,
            retry_seconds=float(guard_retry) if args.fix else 0,
        )
        for result in results:
            print(f"{result['name']}: {result['detail']}")
        return 0 if results and all(result["state"] == "ready" for result in results) else 1

    if not args.device or not args.field:
        parser.error("use --check --config or --device with --field")
    print_device_status(args.device, args.field)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())