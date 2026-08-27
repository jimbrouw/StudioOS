#!/usr/bin/python3
"""Print a compact FX30 status value for a Bitfocus Companion variable."""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def format_time(seconds):
    if not isinstance(seconds, (int, float)):
        return "00:00:00"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def copy_display(state):
    progress = state.get("ingest_progress") or {}
    status = progress.get("status") or "idle"
    percent = progress.get("percent")
    file_name = progress.get("fileName") or progress.get("message") or ""
    eta = format_time(progress.get("etaSeconds"))
    if status in {"copying", "connecting", "starting", "switching", "waiting_for_ftp"}:
        percent_text = "--" if not isinstance(percent, (int, float)) else f"{percent:.0f}%"
        first = f"COPY {percent_text}"
        second = file_name[:28]
        third = f"ETA {eta}" if status == "copying" else status.upper()
        return "\n".join(line for line in (first, second, third) if line)
    if status == "complete":
        return "COPY DONE"
    if status == "failed":
        return "COPY FAILED"
    return "STOP COPY"


def local_storage_text(camera_id):
    """Read free space locally so this works while the bridge is not restarted."""
    config_path = Path(__file__).with_name("config.json")
    try:
        config = json.loads(config_path.read_text())
        camera = next(
            item for item in config.get("cameras", []) if item.get("id") == camera_id
        )
        folder = Path(camera.get("ingestFolder", "")).expanduser()
        if not folder.exists():
            return "Storage unavailable"
        free_gb = shutil.disk_usage(folder).free / 1_000_000_000
        return f"{free_gb:.1f} GB free"
    except (OSError, StopIteration, TypeError, ValueError, KeyError):
        return "Storage unavailable"


def card_display(state, camera_id):
    report = (state.get("card_status") or {}).get(camera_id) or {}
    if not report.get("ok"):
        return "CARD\nCHECK\nSTATUS"
    lines = ["CARD"]
    for slot in report.get("slots") or []:
        minutes = slot.get("remainingMinutes")
        if isinstance(minutes, (int, float)) and minutes >= 0:
            lines.append(f"S{slot.get('slot')} {format_time(minutes * 60)}")
    return "\n".join(lines[:3]) if len(lines) > 1 else "CARD\nTIME N/A"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3030/record/status")
    parser.add_argument("--camera", default="fx30-a")
    parser.add_argument(
        "--field",
        choices=("display", "recording", "time", "free-gb", "copy-display", "copy-percent", "copy-active", "card-display"),
        default="display",
    )
    args = parser.parse_args()

    try:
        with urlopen(args.url, timeout=2) as response:
            data = json.load(response)
    except (OSError, URLError, ValueError):
        print("0" if args.field in {"recording", "copy-percent", "copy-active"} else "OFFLINE")
        # Keep the shell action successful so Companion still stores the
        # visible OFFLINE/0 value instead of retaining stale data.
        return 0

    state = data.get("state") or {}
    storage = data.get("storage") or {}
    camera_storage = next(
        (
            item
            for item in storage.get("cameras", [])
            if item.get("id") == args.camera and item.get("ok")
        ),
        None,
    )
    if camera_storage:
        storage = camera_storage
    recording = bool(state.get("recording"))
    elapsed_seconds = state.get("recordingElapsedSeconds")
    if elapsed_seconds is None and recording:
        started_at = state.get("recording_started_at")
        if isinstance(started_at, (int, float)):
            elapsed_seconds = max(0, time.time() - started_at)
    elapsed = format_time(elapsed_seconds)
    free_text = storage.get("freeGBText") or local_storage_text(args.camera)

    if args.field == "recording":
        print("1" if recording else "0")
    elif args.field == "copy-display":
        print(copy_display(state))
    elif args.field == "copy-percent":
        progress = state.get("ingest_progress") or {}
        percent = progress.get("percent")
        print(f"{percent:.0f}" if isinstance(percent, (int, float)) else "0")
    elif args.field == "copy-active":
        print("1" if state.get("ingest_running") else "0")
    elif args.field == "card-display":
        print(card_display(state, args.camera))
    elif args.field == "time":
        print(elapsed)
    elif args.field == "free-gb":
        print(free_text)
    else:
        print("REC" if recording else "READY")
        print(elapsed)
        print(free_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
