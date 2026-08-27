#!/usr/bin/env python3
"""Stable-file discovery and verified copy for Just In Mac Lite NDI recordings."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable


class OperationCancelled(RuntimeError):
    """Raised after a user requests cancellation of post-record work."""


ProgressCallback = Callable[[dict], None]


def _check_cancel(cancel_event=None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Post-record processing cancelled by user")


def sha256(path: Path, *, cancel_event=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            _check_cancel(cancel_event)
            digest.update(block)
    return digest.hexdigest()


def movie_files(root: Path, extensions: set[str]) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in extensions and not item.name.startswith("._")),
        key=lambda item: item.stat().st_mtime,
    )


def stable_files(
    root: Path,
    extensions: set[str],
    *,
    not_before: float | None = None,
    stable_seconds: float = 3.0,
    settle_timeout_seconds: float = 30.0,
) -> list[Path]:
    deadline = time.monotonic() + settle_timeout_seconds
    while True:
        files = [item for item in movie_files(root, extensions) if not_before is None or item.stat().st_mtime >= not_before]
        if files:
            before = {item: item.stat().st_size for item in files}
            time.sleep(stable_seconds)
            after = {item: item.stat().st_size for item in files if item.exists()}
            if before == after and all(size > 0 for size in after.values()):
                return files
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.5)


def stable_paths(
    files: list[Path],
    *,
    stable_seconds: float = 3.0,
    settle_timeout_seconds: float = 30.0,
    cancel_event=None,
) -> list[Path]:
    """Wait for a fixed session snapshot to finish writing."""
    # Keep the exact recorder-provided paths even if Just In has not published
    # them into the directory yet. Dropping an absent path here turns a normal
    # finalisation delay into a false "no files" failure.
    files = list(dict.fromkeys(files))
    deadline = time.monotonic() + settle_timeout_seconds
    while files:
        _check_cancel(cancel_event)
        before = {item: item.stat().st_size for item in files if item.exists()}
        wait_until = time.monotonic() + stable_seconds
        while time.monotonic() < wait_until:
            _check_cancel(cancel_event)
            time.sleep(min(0.2, max(0, wait_until - time.monotonic())))
        after = {item: item.stat().st_size for item in files if item.exists()}
        if before == after and len(after) == len(files) and all(size > 0 for size in after.values()):
            return files
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.5)
    return []


def copy_verified(
    files: list[Path],
    source_root: Path,
    destination_root: Path,
    *,
    cancel_event=None,
    progress: ProgressCallback | None = None,
) -> list[dict]:
    destination_root.mkdir(parents=True, exist_ok=True)
    receipts: list[dict] = []
    total_bytes = sum(source.stat().st_size for source in files)
    completed_bytes = 0
    for index, source in enumerate(files):
        _check_cancel(cancel_event)
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = source.stat().st_size
        update = {
            "currentFile": source.name,
            "completedFiles": index,
            "totalFiles": len(files),
            "bytesCopied": completed_bytes,
            "bytesTotal": total_bytes,
        }
        if progress:
            progress(update)
        if (
            destination.exists()
            and destination.stat().st_size == source_bytes
            and sha256(destination, cancel_event=cancel_event) == sha256(source, cancel_event=cancel_event)
        ):
            receipts.append({"source": str(source), "destination": str(destination), "bytes": source.stat().st_size, "skipped": True, "verified": True})
            completed_bytes += source_bytes
            continue
        temporary = destination.with_name(f".{destination.name}.partial")
        try:
            with source.open("rb") as source_handle, temporary.open("wb") as destination_handle:
                while True:
                    _check_cancel(cancel_event)
                    block = source_handle.read(4 * 1024 * 1024)
                    if not block:
                        break
                    destination_handle.write(block)
                    if progress:
                        progress({**update, "bytesCopied": completed_bytes + destination_handle.tell()})
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            shutil.copystat(source, temporary)
            _check_cancel(cancel_event)
        except OperationCancelled:
            temporary.unlink(missing_ok=True)
            raise
        try:
            verified = (
                temporary.stat().st_size == source_bytes
                and sha256(temporary, cancel_event=cancel_event) == sha256(source, cancel_event=cancel_event)
            )
        except OperationCancelled:
            temporary.unlink(missing_ok=True)
            raise
        if not verified:
            temporary.unlink(missing_ok=True)
            raise IOError(f"Verified copy failed for {source}")
        temporary.replace(destination)
        receipts.append({"source": str(source), "destination": str(destination), "bytes": source.stat().st_size, "skipped": False, "verified": True})
        completed_bytes += source_bytes
        if progress:
            progress(
                {
                    "currentFile": source.name,
                    "completedFiles": index + 1,
                    "totalFiles": len(files),
                    "bytesCopied": completed_bytes,
                    "bytesTotal": total_bytes,
                }
            )
    return receipts


def _valid_media(path: Path, ffprobe_path: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return result.returncode == 0 and float(result.stdout.strip()) > 0
    except ValueError:
        return False


def create_proxies(
    files: list[Path],
    source_root: Path,
    proxy_root: Path,
    *,
    ffmpeg_path: str = "/opt/homebrew/bin/ffmpeg",
    ffprobe_path: str = "/opt/homebrew/bin/ffprobe",
    height: int = 1080,
    video_bitrate: str = "10M",
    audio_bitrate: str = "192k",
    cancel_event=None,
    progress: ProgressCallback | None = None,
) -> list[dict]:
    """Create edit-friendly H.264 proxies locally and verify they can be probed."""
    receipts: list[dict] = []
    proxy_root.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(files):
        _check_cancel(cancel_event)
        relative = source.relative_to(source_root)
        destination = proxy_root / relative.parent / f"{source.stem}_PROXY.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        update = {
            "currentFile": source.name,
            "completedFiles": index,
            "totalFiles": len(files),
        }
        if progress:
            progress(update)
        if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime and _valid_media(destination, ffprobe_path):
            receipts.append({"source": str(source), "proxy": str(destination), "bytes": destination.stat().st_size, "skipped": True, "verified": True})
            if progress:
                progress({**update, "completedFiles": index + 1})
            continue

        temporary = destination.with_name(f".{destination.stem}.partial.mp4")
        temporary.unlink(missing_ok=True)
        common = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            f"scale=-2:{height}",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
        ]
        hardware = common + [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            video_bitrate,
            "-maxrate",
            video_bitrate,
            "-bufsize",
            "20M",
            "-tag:v",
            "avc1",
            str(temporary),
        ]
        try:
            result = _run_cancellable(hardware, cancel_event=cancel_event)
            if result.returncode != 0:
                temporary.unlink(missing_ok=True)
                software = common + [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    str(temporary),
                ]
                result = _run_cancellable(software, cancel_event=cancel_event)
        except OperationCancelled:
            temporary.unlink(missing_ok=True)
            raise
        if result.returncode != 0 or not _valid_media(temporary, ffprobe_path):
            temporary.unlink(missing_ok=True)
            error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown ffmpeg error"
            raise RuntimeError(f"Proxy creation failed for {source}: {error}")
        temporary.replace(destination)
        receipts.append({"source": str(source), "proxy": str(destination), "bytes": destination.stat().st_size, "skipped": False, "verified": True})
        if progress:
            progress({**update, "completedFiles": index + 1})
    return receipts


def _run_cancellable(command: list[str], *, cancel_event=None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise OperationCancelled("Post-record processing cancelled by user")
        time.sleep(0.2)
    stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
