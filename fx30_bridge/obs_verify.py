#!/usr/bin/env python3
import os
import sys
import time
import json
import argparse
from pathlib import Path

def get_mp4_duration(file_path):
    """
    Parses the MP4 file header directly to find the timescale and duration
    from the 'mvhd' atom. Zero external dependencies.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return None
        file_size = path.stat().st_size
        if file_size < 8:
            return None

        with open(path, "rb") as f:
            # First try: check first 10MB of the file (common for fast-start / moov-at-beginning)
            data = f.read(min(10 * 1024 * 1024, file_size))
            idx = data.find(b"mvhd")
            
            # Second try: check last 10MB of the file (common if moov is at the end)
            if idx == -1 and file_size > 10 * 1024 * 1024:
                f.seek(file_size - 10 * 1024 * 1024)
                data = f.read(10 * 1024 * 1024)
                idx = data.find(b"mvhd")
                
            if idx == -1:
                return None
                
            # version is 1 byte at idx + 4
            version = data[idx + 4]
            if version == 0:
                # timescale is 4 bytes at idx + 16
                # duration is 4 bytes at idx + 20
                timescale = int.from_bytes(data[idx + 16 : idx + 20], "big")
                duration = int.from_bytes(data[idx + 20 : idx + 24], "big")
            elif version == 1:
                # timescale is 4 bytes at idx + 24
                # duration is 8 bytes at idx + 28
                timescale = int.from_bytes(data[idx + 24 : idx + 28], "big")
                duration = int.from_bytes(data[idx + 28 : idx + 36], "big")
            else:
                return None

            if timescale > 0:
                return round(duration / timescale, 2)
    except Exception as e:
        # Silently fail or return None if parsing fails
        pass
    return None

def verify_obs_recordings(folder_path, expected_count=1, stable_check_seconds=1.0):
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return {
            "ok": False,
            "error": f"Folder {folder_path} does not exist or is not a directory.",
            "expectedCameraCount": expected_count,
            "actualCameraCount": 0,
            "files": []
        }

    # Find movie files
    extensions = {".mp4", ".mkv", ".mov", ".mxf"}
    movie_files = []
    for item in folder.iterdir():
        if item.is_file() and item.suffix.lower() in extensions:
            if item.name.startswith("._"):
                continue
            stat = item.stat()
            movie_files.append({
                "name": item.name,
                "path": str(item),
                "sizeBytes": stat.st_size,
                "modifiedAt": stat.st_mtime,
            })

    if not movie_files:
        return {
            "ok": False,
            "error": f"No movie files found in {folder_path}.",
            "expectedCameraCount": expected_count,
            "actualCameraCount": 0,
            "files": []
        }

    # Group files by session (modification times within 90 seconds of each other)
    # 90 seconds accommodates multiple cameras stop timing differences.
    sorted_files = sorted(movie_files, key=lambda x: x["modifiedAt"])
    sessions = []
    current_session = []
    for f in sorted_files:
        if not current_session:
            current_session.append(f)
        else:
            if f["modifiedAt"] - current_session[-1]["modifiedAt"] <= 90:
                current_session.append(f)
            else:
                sessions.append(current_session)
                current_session = [f]
    if current_session:
        sessions.append(current_session)

    if not sessions:
        return {
            "ok": False,
            "error": "No sessions identified.",
            "expectedCameraCount": expected_count,
            "actualCameraCount": 0,
            "files": []
        }

    # The latest session is the last one in sorted order
    latest_session_files = sessions[-1]
    
    # Check if files are still growing
    # Measure size at start of check
    initial_sizes = {f["path"]: Path(f["path"]).stat().st_size for f in latest_session_files}
    time.sleep(stable_check_seconds)
    
    verified_files = []
    for f in latest_session_files:
        path = Path(f["path"])
        current_size = path.stat().st_size
        growing = current_size > initial_sizes[f["path"]]
        
        # Calculate duration if MP4
        duration = None
        if path.suffix.lower() == ".mp4":
            duration = get_mp4_duration(path)

        verified_files.append({
            "name": f["name"],
            "path": f["path"],
            "sizeBytes": current_size,
            "modifiedAt": int(f["modifiedAt"]),
            "modifiedAtStr": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["modifiedAt"])),
            "growing": growing,
            "durationSeconds": duration
        })

    actual_count = len(verified_files)
    ok = (actual_count >= expected_count) and all(not f["growing"] and f["sizeBytes"] > 0 for f in verified_files)

    return {
        "ok": ok,
        "expectedCameraCount": expected_count,
        "actualCameraCount": actual_count,
        "files": verified_files
    }

def main():
    parser = argparse.ArgumentParser(description="Verify OBS / NDI recordings")
    parser.add_argument("--folder", required=True, help="Path to OBS recordings folder")
    parser.add_argument("--expected-count", type=int, default=1, help="Expected number of camera files")
    parser.add_argument("--stable-check-seconds", type=float, default=1.0, help="Seconds to wait to detect file growth")
    args = parser.parse_args()

    report = verify_obs_recordings(args.folder, args.expected_count, args.stable_check_seconds)
    print(json.dumps(report, indent=2))
    
    if not report.get("ok", False):
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
