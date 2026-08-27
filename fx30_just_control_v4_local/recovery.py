"""Fail-closed, local-only NDI backup recovery primitives.

This module deliberately has no HTTP server and no recorder/camera control.
``v4_bridge`` supplies only terminal session evidence and, for a local rehearsal,
an explicit fixture candidate directory.  A remote notifier may consume the
append-only events written here, but it cannot issue commands back to V4.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class RecoveryAttention(RuntimeError):
    """Evidence was insufficient to continue safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def mapping_from_config(config: dict) -> list[dict]:
    recovery = config.get("recovery") or {}
    mappings = [dict(item) for item in recovery.get("mappings") or []]
    enabled_channels = {str(item["name"]) for item in config.get("channels") or [] if item.get("enabled", True)}
    seen_channels: set[str] = set()
    seen_cameras: set[str] = set()
    enabled: list[dict] = []
    for item in mappings:
        channel = str(item.get("justInChannel") or "")
        camera = str(item.get("fx30CameraId") or "")
        if not channel or not camera or not item.get("ndiLabel"):
            raise RecoveryAttention("Every recovery mapping needs justInChannel, ndiLabel, and fx30CameraId")
        if channel in seen_channels or camera in seen_cameras:
            raise RecoveryAttention("Recovery mappings must have unique Just In channels and FX30 camera IDs")
        seen_channels.add(channel)
        seen_cameras.add(camera)
        item["enabled"] = bool(item.get("enabled", True) and channel in enabled_channels)
        if item["enabled"]:
            enabled.append(item)
    if not enabled:
        raise RecoveryAttention("No enabled recovery mappings are configured")
    return enabled


class SessionLedger:
    """One manifest plus immutable, idempotent authoritative event receipts."""

    def __init__(self, root: Path, mapping: list[dict], *, session_id: str | None = None):
        self.root = Path(root)
        self.session_id = session_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.path = self.root / self.session_id
        self.manifest_path = self.path / "session.json"
        self.events_path = self.path / "events.jsonl"
        self.manifest: dict[str, Any] = {
            "schemaVersion": 1,
            "sessionId": self.session_id,
            "state": "creating",
            "createdAt": utc_now(),
            "mapping": mapping,
            "ndiMedia": {},
            "liveness": {},
            "recovery": {"state": "idle"},
            "zowiePrerequisite": {
                "state": "unresolved",
                "detail": "ZowieBox recording errors are unresolved; recovery is local-test only.",
            },
        }
        self._event_keys: set[str] = set()
        self.persist()
        self.emit("session.created", {"state": "creating"})

    @classmethod
    def load(cls, path: Path) -> "SessionLedger":
        manifest_path = Path(path) / "session.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        instance = cls.__new__(cls)
        instance.root = Path(path).parent
        instance.session_id = str(payload["sessionId"])
        instance.path = Path(path)
        instance.manifest_path = manifest_path
        instance.events_path = instance.path / "events.jsonl"
        instance.manifest = payload
        instance._event_keys = set()
        if instance.events_path.exists():
            for line in instance.events_path.read_text(encoding="utf-8").splitlines():
                if line:
                    instance._event_keys.add(json.loads(line)["idempotencyKey"])
        return instance

    def persist(self) -> None:
        atomic_json(self.manifest_path, self.manifest)

    def transition(self, state: str, **fields: Any) -> None:
        self.manifest["state"] = state
        self.manifest["updatedAt"] = utc_now()
        self.manifest.update(fields)
        self.persist()

    def emit(self, event_type: str, evidence: dict | None = None) -> dict:
        key = f"{self.session_id}:{event_type}:{self.manifest.get('state')}"
        if key in self._event_keys:
            return {"idempotencyKey": key, "duplicate": True}
        event = {
            "schemaVersion": 1,
            "idempotencyKey": key,
            "sessionId": self.session_id,
            "type": event_type,
            "state": self.manifest.get("state"),
            "at": utc_now(),
            "evidence": evidence or {},
        }
        self.path.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._event_keys.add(key)
        return event


def probe_media(path: Path, ffprobe_path: str) -> dict:
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return {"ok": False, "path": str(path), "error": "file is missing or empty"}
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "path": str(path), "error": (result.stderr or result.stdout or "ffprobe failed").strip()}
    try:
        payload = json.loads(result.stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError, json.JSONDecodeError):
        duration = 0
        payload = {}
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if duration <= 0 or video is None:
        return {"ok": False, "path": str(path), "error": "media has no usable video duration"}
    return {
        "ok": True,
        "path": str(path),
        "sizeBytes": path.stat().st_size,
        "durationSeconds": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "sha256": sha256(path),
    }


def select_unambiguous_candidate(candidates: list[dict], expected_duration: float, *, duration_tolerance: float = 30.0) -> dict:
    usable = [item for item in candidates if item.get("verification", {}).get("ok")]
    if len(usable) != 1:
        raise RecoveryAttention(f"Expected exactly one usable Sony candidate, found {len(usable)}")
    candidate = usable[0]
    observed = float(candidate["verification"]["durationSeconds"])
    candidate["durationDifferenceSeconds"] = round(abs(observed - expected_duration), 3)
    candidate["requiresReview"] = candidate["durationDifferenceSeconds"] > duration_tolerance
    return candidate


def copy_preserving_source(source: Path, destination: Path) -> dict:
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=4 * 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if source.stat().st_size != temporary.stat().st_size or sha256(source) != sha256(temporary):
            raise RecoveryAttention(f"Copied recovery candidate does not match source: {source.name}")
        os.replace(temporary, destination)
    except Exception:
        # Preserve a failed partial for inspection; do not overwrite it on retry.
        if temporary.exists():
            temporary.rename(temporary.with_name(f"{temporary.name}.failed-{int(time.time())}"))
        raise
    return {"source": str(source), "destination": str(destination), "bytes": destination.stat().st_size, "sha256": sha256(destination)}


class RecoveryWorker:
    """A fixture-capable worker. It never contacts a camera or recorder."""

    def __init__(self, config: dict, *, probe: Callable[[Path, str], dict] = probe_media):
        recovery = config.get("recovery") or {}
        self.enabled = bool(recovery.get("enabled", False))
        self.local_test_only = bool(recovery.get("localTestOnly", True))
        self.staging_root = Path(recovery.get("stagingRoot", "/Volumes/Extreme SSD/STUDIO-RECOVERY"))
        self.delivery_root = Path(recovery.get("deliveryRoot", "/Volumes/specialized2026/studio/Studio Recordings/Studio Recovery"))
        self.fixture_root = Path(recovery["fixtureCandidatesRoot"]) if recovery.get("fixtureCandidatesRoot") else None
        self.tolerance = float(recovery.get("durationDifferenceSeconds", 30))
        self.ffprobe_path = str((config.get("postRecord") or {}).get("ffprobePath", "/opt/homebrew/bin/ffprobe"))
        self.probe = probe

    def fixture_candidates(self, mapping: dict) -> list[dict]:
        if self.fixture_root is None:
            return []
        camera_root = self.fixture_root / str(mapping["fx30CameraId"])
        if not camera_root.is_dir():
            return []
        candidates = []
        for item in sorted(camera_root.iterdir()):
            if item.is_file() and not item.name.startswith("."):
                candidates.append({"source": str(item), "sourceFilename": item.name, "verification": self.probe(item, self.ffprobe_path)})
        return candidates

    def recover(self, ledger: SessionLedger, mapping: dict, failed_ndi: dict) -> dict:
        if not self.enabled:
            raise RecoveryAttention("Recovery worker is disabled")
        if not self.local_test_only:
            raise RecoveryAttention("Automatic camera recovery is not enabled outside local-test mode")
        expected_duration = float(failed_ndi.get("expectedDurationSeconds") or 0)
        if expected_duration <= 0:
            raise RecoveryAttention("Expected session duration is unavailable")
        ledger.transition("cataloging_sony")
        ledger.emit("recovery.cataloging", {"camera": mapping["fx30CameraId"]})
        candidates = self.fixture_candidates(mapping)
        selected = select_unambiguous_candidate(candidates, expected_duration, duration_tolerance=10**9)
        failed_duration = float(failed_ndi.get("observedDurationSeconds") or 0)
        comparison_duration = failed_duration if failed_duration > 0 else expected_duration
        selected["durationDifferenceSeconds"] = round(
            abs(float(selected["verification"]["durationSeconds"]) - comparison_duration), 3
        )
        selected["durationComparisonBasis"] = "failed-ndi" if failed_duration > 0 else "expected-session"
        selected["requiresReview"] = selected["durationDifferenceSeconds"] > self.tolerance
        ledger.transition("ingesting_sony", recovery={"state": "ingesting_sony", "selected": selected})
        ledger.emit("recovery.started", {"camera": mapping["fx30CameraId"], "source": selected["sourceFilename"]})
        source = Path(selected["source"])
        package_root = self.staging_root / ledger.session_id / f"RECOVERED-{mapping['fx30CameraId'].upper()}"
        receipt = copy_preserving_source(source, package_root / source.name)
        verified = self.probe(Path(receipt["destination"]), self.ffprobe_path)
        if not verified.get("ok") or verified.get("sha256") != selected["verification"].get("sha256"):
            raise RecoveryAttention("Recovered local copy did not pass media/hash verification")
        delivery = None
        if not selected["requiresReview"]:
            delivery_destination = self.delivery_root / ledger.session_id / f"RECOVERED-{mapping['fx30CameraId'].upper()}" / source.name
            delivery = copy_preserving_source(Path(receipt["destination"]), delivery_destination)
            if delivery["sha256"] != receipt["sha256"]:
                raise RecoveryAttention("Delivered recovery copy does not match verified staging media")
        recovery = {
            "state": "validated",
            "mapping": mapping,
            "selected": selected,
            "copy": receipt,
            "delivery": delivery,
            "verification": verified,
        }
        ledger.manifest["recovery"] = recovery
        ledger.transition("attention" if selected["requiresReview"] else "editor_ready")
        ledger.emit("recovery.attention" if selected["requiresReview"] else "recovery.completed", {
            "durationDifferenceSeconds": selected["durationDifferenceSeconds"],
            "requiresReview": selected["requiresReview"],
        })
        return recovery
