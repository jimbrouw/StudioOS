#!/usr/bin/env python3
"""Safely copy one RØDECaster take to SSD and LucidLink with SHA-256 proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_STAGING_ROOT = Path("/Volumes/Extreme SSD/TEST-RECS/AUDIO/RODECASTER")
DEFAULT_LUCID_ROOT = Path("/Volumes/specialized2026/studio/Studio Recordings/RØDECaster Audio")
DEFAULT_STATE_FILE = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_storage_state.json")
DEFAULT_MOUNT_CANDIDATES = (
    Path("/Volumes/RodeCaster"),
    Path("/Volumes/RODECaster"),
)
VOLUMES_ROOT = Path("/Volumes")


class IngestError(RuntimeError):
    """A safe, operator-facing ingest failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy the newest RØDECaster take from USB Transfer Mode to the "
            "external SSD and LucidLink, then verify every file by SHA-256."
        )
    )
    parser.add_argument(
        "--mount",
        type=Path,
        help="Mounted RØDECaster volume. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Override the recordings root (useful for a controlled test).",
    )
    parser.add_argument(
        "--take",
        help="Exact take-folder name. The newest take is selected when omitted.",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=DEFAULT_STAGING_ROOT,
        help=f"External SSD destination (default: {DEFAULT_STAGING_ROOT}).",
    )
    parser.add_argument(
        "--lucid-root",
        type=Path,
        default=DEFAULT_LUCID_ROOT,
        help=f"LucidLink destination (default: {DEFAULT_LUCID_ROOT}).",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=2.0,
        help="Seconds used to prove source files are no longer changing.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0,
        help="Wait this long for the RØDECaster Transfer Mode mount.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and report the selected take without copying.",
    )
    parser.add_argument(
        "--staging-only",
        action="store_true",
        help="Copy and verify only the external SSD destination.",
    )
    parser.add_argument(
        "--all-takes",
        action="store_true",
        help="Process every valid take oldest-first instead of only the newest take.",
    )
    parser.add_argument(
        "--delete-verified",
        action="store_true",
        help="Delete each source take only after SSD and LucidLink manifests both match.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Capacity cache updated after a mounted-card scan (default: {DEFAULT_STATE_FILE}).",
    )
    return parser.parse_args()


def locate_mount(explicit_mount: Path | None, wait_seconds: float) -> Path:
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        if explicit_mount:
            candidates = (explicit_mount,)
        else:
            candidates = list(DEFAULT_MOUNT_CANDIDATES)
            try:
                candidates.extend(VOLUMES_ROOT.iterdir())
            except OSError:
                pass

        matching_mounts: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                locate_recordings_root(candidate)
            except (IngestError, OSError):
                continue
            matching_mounts.append(candidate)

        if len(matching_mounts) == 1:
            return matching_mounts[0]
        if len(matching_mounts) > 1:
            mounts = ", ".join(str(path) for path in matching_mounts)
            raise IngestError(
                f"Multiple mounted volumes contain RODECaster recordings: {mounts}. "
                "Use --mount to select the intended card."
            )
        if time.monotonic() >= deadline:
            expected = explicit_mount or (
                "a volume under /Volumes containing a RODECaster folder"
            )
            raise IngestError(
                f"RØDECaster card was not found at {expected}. "
                "Enter Transfer Mode over USB 1 and try again."
            )
        time.sleep(1)


def locate_recordings_root(mount: Path) -> Path:
    preferred = mount / "RODECaster"
    if preferred.is_dir():
        return preferred
    for child in mount.iterdir():
        if child.is_dir() and child.name.casefold() == "rodecaster":
            return child
    raise IngestError(f"No RODECaster recordings folder was found on {mount}.")


def select_take(source_root: Path, requested_take: str | None) -> Path:
    if requested_take:
        take = source_root / requested_take
        if not take.is_dir():
            raise IngestError(f"Requested take does not exist: {take}")
        return take

    takes = [
        item
        for item in source_root.iterdir()
        if item.is_dir() and not item.name.startswith(".")
    ]
    if not takes:
        raise IngestError(f"No take folders were found under {source_root}.")
    return max(takes, key=lambda item: item.stat().st_mtime_ns)


def take_duration_seconds(take: Path) -> float | None:
    meta = take / "Meta.xml"
    if not meta.is_file():
        return None
    try:
        duration = ET.parse(meta).findtext(".//duration")
        value = float(duration) if duration is not None else 0
        return value if value > 0 else None
    except (ET.ParseError, OSError, TypeError, ValueError):
        return None


def valid_takes(source_root: Path) -> list[Path]:
    takes = []
    for item in source_root.iterdir():
        if item.is_symlink() or not item.is_dir() or item.name.startswith("."):
            continue
        if take_duration_seconds(item) is None:
            continue
        takes.append(item)
    return sorted(takes, key=lambda item: item.stat().st_mtime_ns)


def file_snapshot(root: Path) -> list[dict[str, int | str]]:
    snapshot: list[dict[str, int | str]] = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise IngestError(f"Refusing unexpected symbolic link: {item}")
        if not item.is_file() or item.name.startswith("._"):
            continue
        stat = item.stat()
        snapshot.append(
            {
                "path": item.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtimeNs": stat.st_mtime_ns,
            }
        )
    if not snapshot:
        raise IngestError(f"No recording files were found in {root}.")
    return snapshot


def prove_stable(root: Path, seconds: float) -> list[dict[str, int | str]]:
    before = file_snapshot(root)
    if seconds > 0:
        time.sleep(seconds)
    after = file_snapshot(root)
    if before != after:
        raise IngestError(
            "The RØDECaster take is still changing. Wait for recording "
            "finalisation, then run the ingest again."
        )
    return after


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.name.startswith("._"):
            continue
        result[item.relative_to(root).as_posix()] = {
            "size": item.stat().st_size,
            "sha256": sha256(item),
        }
    if not result:
        raise IngestError(f"No files were available to verify in {root}.")
    return result


def copy_directory_once(source: Path, destination: Path) -> str:
    if destination.exists():
        if not destination.is_dir():
            raise IngestError(f"Destination is not a folder: {destination}")
        if manifest(source) != manifest(destination):
            raise IngestError(
                f"Destination already exists but does not match: {destination}. "
                "Nothing was overwritten."
            )
        return "already_verified"

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / f".{destination.name}.partial-{uuid.uuid4().hex}"
    shutil.copytree(source, partial, copy_function=shutil.copy2)
    if manifest(source) != manifest(partial):
        raise IngestError(
            f"Copy verification failed before finalising {destination}. "
            f"The partial copy remains at {partial} for inspection."
        )
    partial.rename(destination)
    return "copied"


def safe_receipt_name(take_name: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in take_name
    )
    return cleaned.strip("._") or "rodecaster-take"


def write_receipt(
    staging_root: Path,
    lucid_root: Path,
    receipt: dict,
) -> tuple[Path, Path]:
    filename = f"{safe_receipt_name(str(receipt['take']))}-archive.json"
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    paths = (
        staging_root / "_receipts" / filename,
        lucid_root / "_receipts" / filename,
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.partial-{uuid.uuid4().hex}")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    return paths


def safe_delete_take(source_root: Path, take: Path) -> None:
    resolved_root = source_root.resolve()
    resolved_take = take.resolve()
    if take.is_symlink() or resolved_take.parent != resolved_root:
        raise IngestError(f"Refusing unsafe take deletion target: {take}")
    if take_duration_seconds(take) is None:
        raise IngestError(f"Refusing to delete a folder without valid RØDECaster metadata: {take}")
    shutil.rmtree(take)
    if take.exists():
        raise IngestError(f"Verified take still exists after deletion attempt: {take}")


def write_capacity_state(state_file: Path, mount: Path, samples: list[tuple[int, float]]) -> dict:
    try:
        existing = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    usage = shutil.disk_usage(mount)
    rates = [size / duration * 3600 for size, duration in samples if size > 0 and duration > 0]
    rode = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "mount": str(mount),
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
        "bytesPerHour": max(rates) if rates else (existing.get("rodecaster") or {}).get("bytesPerHour"),
    }
    existing["rodecaster"] = rode
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(f".{state_file.name}.partial-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_file)
    return rode


def write_operation_state(state_file: Path, state: str, **details) -> None:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["rodecasterCleanup"] = {
        "state": state,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(f".{state_file.name}.partial-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_file)


def process_take(args: argparse.Namespace, mount: Path, source_root: Path, take: Path) -> dict:
    source_snapshot = prove_stable(take, max(0, args.stable_seconds))
    staging_destination = args.staging_root / take.name
    lucid_destination = args.lucid_root / take.name
    duration = take_duration_seconds(take)
    selection = {
        "ok": True,
        "dryRun": bool(args.dry_run),
        "mount": str(mount),
        "source": str(take),
        "take": take.name,
        "durationSeconds": duration,
        "fileCount": len(source_snapshot),
        "totalBytes": sum(int(item["size"]) for item in source_snapshot),
        "stagingDestination": str(staging_destination),
        "lucidDestination": str(lucid_destination),
        "cleanupState": "inventory_only" if args.dry_run else "not_requested",
    }
    if args.dry_run:
        return selection

    write_operation_state(args.state_file, "copying", take=take.name, destination="ssd")
    staging_result = copy_directory_once(take, staging_destination)
    write_operation_state(args.state_file, "verifying", take=take.name, destination="ssd")
    source_manifest = manifest(take)
    staging_manifest = manifest(staging_destination)
    if source_manifest != staging_manifest:
        raise IngestError("Final source and SSD manifests do not match.")

    if args.staging_only:
        return {
            **selection,
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "stagingResult": staging_result,
            "lucidResult": "skipped",
            "files": source_manifest,
        }

    write_operation_state(args.state_file, "copying", take=take.name, destination="lucidlink")
    lucid_result = copy_directory_once(staging_destination, lucid_destination)
    write_operation_state(args.state_file, "verifying", take=take.name, destination="lucidlink")
    lucid_manifest = manifest(lucid_destination)
    if not (source_manifest == staging_manifest == lucid_manifest):
        raise IngestError("Final source, SSD, and LucidLink manifests do not match.")

    receipt = {
        **selection,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "stagingResult": staging_result,
        "lucidResult": lucid_result,
        "cleanupState": "verified_pending_delete" if args.delete_verified else "verified_retained",
        "files": source_manifest,
    }
    staging_receipt, lucid_receipt = write_receipt(args.staging_root, args.lucid_root, receipt)
    receipt["stagingReceipt"] = str(staging_receipt)
    receipt["lucidReceipt"] = str(lucid_receipt)
    write_receipt(args.staging_root, args.lucid_root, receipt)

    if args.delete_verified:
        write_operation_state(args.state_file, "cleaning", take=take.name)
        safe_delete_take(source_root, take)
        receipt["cleanupState"] = "deleted"
        receipt["deletedAt"] = datetime.now(timezone.utc).isoformat()
        write_receipt(args.staging_root, args.lucid_root, receipt)
    return receipt


def run(args: argparse.Namespace) -> dict:
    if args.delete_verified and (args.dry_run or args.staging_only):
        raise IngestError("--delete-verified requires full SSD + LucidLink delivery and cannot be combined with --dry-run or --staging-only.")
    if args.source_root:
        source_root = args.source_root.resolve()
        mount = source_root.parent
    else:
        mount = locate_mount(args.mount, args.wait_seconds)
        source_root = locate_recordings_root(mount)

    write_operation_state(args.state_file, "checking")
    if args.take:
        takes = [select_take(source_root, args.take)]
    elif args.all_takes:
        takes = valid_takes(source_root)
    else:
        takes = [select_take(source_root, None)]
    if not takes:
        raise IngestError(f"No valid RØDECaster take folders were found under {source_root}.")

    results = []
    failures = []
    samples = []
    for take in takes:
        try:
            result = process_take(args, mount, source_root, take)
            results.append(result)
            if result.get("durationSeconds"):
                samples.append((int(result["totalBytes"]), float(result["durationSeconds"])))
        except (IngestError, OSError) as exc:
            failures.append({"take": take.name, "error": str(exc), "cleanupState": "retained_attention"})

    capacity = write_capacity_state(args.state_file, mount, samples)
    if not args.all_takes and len(results) == 1 and not failures:
        result = {**results[0], "cardCapacity": capacity}
        write_operation_state(
            args.state_file,
            "complete",
            processed=1,
            deleted=1 if result.get("cleanupState") == "deleted" else 0,
            retained=0 if result.get("cleanupState") == "deleted" else 1,
        )
        return result
    result = {
        "ok": not failures,
        "action": "inventory" if args.dry_run else "archive-clean" if args.delete_verified else "archive",
        "mount": str(mount),
        "processed": len(results),
        "deleted": sum(item.get("cleanupState") == "deleted" for item in results),
        "retained": len(failures) + sum(item.get("cleanupState") != "deleted" for item in results),
        "takes": results,
        "failures": failures,
        "cardCapacity": capacity,
    }
    write_operation_state(
        args.state_file,
        "complete" if result["ok"] else "attention",
        processed=result["processed"],
        deleted=result["deleted"],
        retained=result["retained"],
        failures=result["failures"],
    )
    return result


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (IngestError, OSError) as exc:
        try:
            write_operation_state(args.state_file, "attention", error=str(exc))
        except OSError:
            pass
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
