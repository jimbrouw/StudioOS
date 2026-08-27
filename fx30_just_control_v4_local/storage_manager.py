#!/usr/bin/env python3
"""Capacity reporting for the V4 local endurance pre-flight."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


GB = 1_000_000_000
DEFAULT_STATE_FILE = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_storage_state.json")
DEFAULT_MODE_FILE = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/.studio_record_mode")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def existing_storage_path(requested: Path) -> Path | None:
    path = requested.expanduser()
    if len(path.parts) >= 3 and path.parts[1] == "Volumes":
        volume = Path("/Volumes") / path.parts[2]
        if not volume.exists():
            return None
    while not path.exists() and path != path.parent:
        path = path.parent
    return path if path.exists() else None


class StorageManager:
    def __init__(self, config: dict, *, capacity_session_id: str | None = None):
        policy = config.get("storagePolicy") or {}
        self.config = config
        self.target_hours = float(policy.get("targetHours", 3))
        self.margin = float(policy.get("safetyMarginPercent", 15)) / 100
        self.stale_seconds = float(policy.get("rodecasterStaleHours", 24)) * 3600
        # The RØDECaster has no supported automation API for remaining time.
        # The operator reading is a session-bound baseline, not a hardware poll.
        self.capacity_session_id = capacity_session_id
        self.rodecaster_override_seconds = float(policy.get("rodecasterOverrideMinutes", 5)) * 60
        self.rodecaster_override_phrase = str(policy.get("rodecasterOverridePhrase", "OVERRIDE RØDECASTER CAPACITY"))
        self.rodecaster_required_minutes = self.target_hours * 60 * (1 + self.margin)
        self.ssd_rate = float(policy.get("extremeSsdBaselineGBPerHour", 607)) * GB
        self.proxy_rate = float(policy.get("proxyBaselineGBPerHour", 14)) * GB
        self.rode_fallback_rate = policy.get("rodecasterBaselineGBPerHour")
        self.rode_fallback_rate = float(self.rode_fallback_rate) * GB if self.rode_fallback_rate else None
        self.local_only = bool((config.get("postRecord") or {}).get("localOnly", False))
        self.include_rodecaster_in_preflight = bool(policy.get("includeRodecasterInPreflight", True)) and not self.local_only
        self.state_file = Path(policy.get("stateFile", DEFAULT_STATE_FILE))
        self.mode_file = Path(policy.get("modeFile", DEFAULT_MODE_FILE))
        self.fx30_status_url = str(policy.get("fx30StatusUrl", "http://127.0.0.1:3030/record/status"))
        self.fx30_refresh_url = str(policy.get("fx30RefreshUrl", "http://127.0.0.1:3030/record/card-status"))
        self.ssd_root = Path(config.get("sourceRoot", "/Volumes/Extreme SSD/JUSTCONTROL_RECORDING"))
        self.proxy_root = Path(config.get("proxyWorkingRoot", str(Path.home() / "Movies/JUSTCONTROL_PROXIES")))
        self.lucidlink_root = Path(config.get("deliveryRoot", "/Volumes/specialized2026/studio/Studio Recordings"))
        self.ffprobe_path = str((config.get("postRecord") or {}).get("ffprobePath", "/opt/homebrew/bin/ffprobe"))
        self._rate_cache = {"checked": None, "value": self.ssd_rate}

    def _write_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(f".{self.state_file.name}.partial")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.state_file)

    def confirm_rodecaster_capacity(self, remaining_minutes: float, source: str) -> dict:
        if remaining_minutes < 0:
            raise ValueError("RØDECaster remaining minutes must be zero or greater")
        source = source.strip()
        if not source:
            raise ValueError("RØDECaster confirmation source is required")
        now = time.time()
        confirmed_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        state = "confirmed" if remaining_minutes >= self.rodecaster_required_minutes else "below-threshold"
        evidence = {
            "state": state,
            "evidence": "operator-confirmed-device-reading",
            "remainingMinutes": round(float(remaining_minutes), 1),
            "source": source,
            "confirmedAt": confirmed_at,
            "confirmationDate": datetime.now().astimezone().date().isoformat(),
            "sessionId": self.capacity_session_id,
            "lastVerifiedTake": None,
            "attentionReason": None,
        }
        persisted = self._state()
        persisted["rodecasterCapacity"] = evidence
        self._write_state(persisted)
        return self.rodecaster_capacity()

    def override_rodecaster_capacity(self, phrase: str, reason: str) -> dict:
        if phrase != self.rodecaster_override_phrase:
            raise ValueError("RØDECaster override phrase did not match")
        if not reason.strip():
            raise ValueError("RØDECaster override reason is required")
        now = time.time()
        persisted = self._state()
        persisted["rodecasterCapacity"] = {
            "state": "override",
            "evidence": "operator-override",
            "remainingMinutes": None,
            "source": "operator override",
            "reason": reason.strip(),
            "confirmedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "expiresAt": datetime.fromtimestamp(now + self.rodecaster_override_seconds, timezone.utc).isoformat(),
            "usedAt": None,
        }
        self._write_state(persisted)
        return self.rodecaster_capacity()

    def recover_rodecaster_capacity(self) -> dict:
        persisted = self._state()
        persisted["rodecasterCapacity"] = {
            "state": "unavailable", "evidence": "operator-recovery-cleared", "remainingMinutes": None,
            "source": "operator recovery", "confirmedAt": None, "expiresAt": None, "usedAt": None,
        }
        self._write_state(persisted)
        return self.rodecaster_capacity()

    def consume_rodecaster_override(self) -> None:
        persisted = self._state()
        evidence = persisted.get("rodecasterCapacity")
        if not evidence or evidence.get("state") != "override":
            return
        evidence["usedAt"] = utc_now()
        persisted["rodecasterCapacity"] = evidence
        self._write_state(persisted)

    def mark_rodecaster_take_complete(self, started_at: float, stopped_at: float) -> dict:
        """Charge a trusted baseline only after every recorder stop was verified."""
        if not isinstance(started_at, (int, float)) or not isinstance(stopped_at, (int, float)) or stopped_at < started_at:
            return self.mark_rodecaster_take_attention("Verified take timing is unavailable; reconfirm capacity")
        persisted = self._state()
        evidence = (persisted.get("rodecasterCapacity") or {}).copy()
        if evidence.get("state") != "confirmed" or not isinstance(evidence.get("remainingMinutes"), (int, float)):
            return self.mark_rodecaster_take_attention("Capacity baseline was unavailable at verified stop; reconfirm capacity")
        elapsed_seconds = stopped_at - started_at
        charged_minutes = math.ceil(elapsed_seconds / 60) if elapsed_seconds > 0 else 0
        remaining = max(0.0, float(evidence["remainingMinutes"]) - charged_minutes)
        evidence.update({
            "remainingMinutes": round(remaining, 1),
            "lastVerifiedTake": {
                "startedAt": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
                "stoppedAt": datetime.fromtimestamp(stopped_at, timezone.utc).isoformat(),
                "elapsedSeconds": round(elapsed_seconds, 3),
                "chargedMinutes": charged_minutes,
            },
            "attentionReason": None,
        })
        if remaining < self.rodecaster_required_minutes:
            evidence["state"] = "below-threshold"
        persisted["rodecasterCapacity"] = evidence
        self._write_state(persisted)
        return self.rodecaster_capacity()

    def mark_rodecaster_take_attention(self, reason: str) -> dict:
        """Fail closed when a take may have consumed unknown device capacity."""
        persisted = self._state()
        evidence = (persisted.get("rodecasterCapacity") or {}).copy()
        evidence.update({
            "state": "attention",
            "evidence": evidence.get("evidence", "operator-confirmed-device-reading"),
            "attentionReason": reason,
            "attentionAt": utc_now(),
        })
        persisted["rodecasterCapacity"] = evidence
        self._write_state(persisted)
        return self.rodecaster_capacity()

    def rodecaster_capacity(self) -> dict:
        evidence = (self._state().get("rodecasterCapacity") or {}).copy()
        now = time.time()
        state = evidence.get("state", "unavailable")
        expires = parse_time(evidence.get("expiresAt"))
        if state not in {"confirmed", "below-threshold", "override", "stale", "attention", "unavailable"}:
            state = "unavailable"
        if state in {"confirmed", "below-threshold"} and (
            not isinstance(evidence.get("remainingMinutes"), (int, float))
            or not str(evidence.get("source", "")).strip()
            or not parse_time(evidence.get("confirmedAt"))
        ):
            state = "attention"
            message = "Capacity evidence is malformed; reconfirm from the RØDECaster screen or app"
        current_date = datetime.now().astimezone().date().isoformat()
        if state == "attention" and "message" not in locals():
            message = evidence.get("attentionReason") or "Take outcome is incomplete or unknown; reconfirm capacity from the device screen or app"
        elif state in {"confirmed", "below-threshold"} and self.capacity_session_id and evidence.get("sessionId") != self.capacity_session_id:
            state = "stale"
            message = "Capacity baseline belongs to an earlier bridge session; reconfirm from the device screen or app"
        elif state in {"confirmed", "below-threshold"} and evidence.get("confirmationDate") != current_date:
            state = "stale"
            message = "Capacity baseline is from an earlier production day; reconfirm from the device screen or app"
        elif state == "override" and (evidence.get("usedAt") or expires is None or now >= expires):
            state = "stale"
            message = "Operator override was used or expired; reconfirm from the RØDECaster screen or app"
        elif state == "confirmed":
            take = evidence.get("lastVerifiedTake") or {}
            message = f"Projected {float(evidence.get('remainingMinutes', 0)):.0f} minutes remaining from operator baseline"
            if take:
                message += f"; last verified take charged {take.get('chargedMinutes', 0)} minutes"
        elif state == "below-threshold":
            message = f"Projected {float(evidence.get('remainingMinutes', 0)):.0f} minutes remaining; {self.rodecaster_required_minutes:.0f} minutes required"
        elif state == "override":
            message = "Operator override active: amber only; expires after one start or five minutes"
        elif state == "attention":
            message = evidence.get("attentionReason") or "Take outcome is incomplete or unknown; reconfirm capacity from the device screen or app"
        elif state == "stale":
            message = "RØDECaster capacity confirmation is stale; reconfirm from the device screen or app"
        else:
            message = "No RØDECaster capacity confirmation. Read the device screen or app and confirm it here."
        allowed = state in {"confirmed", "override"}
        return {
            "key": "rodecaster-capacity", "label": "RØDECaster recording capacity", "required": True,
            "status": "ready" if state == "confirmed" else "warning" if state in {"override", "below-threshold", "attention"} else state,
            "state": state, "gateOpen": allowed, "message": message,
            "requiredMinutes": round(self.rodecaster_required_minutes, 1),
            "confirmationScope": "current bridge session and production day",
            "overrideWindowMinutes": round(self.rodecaster_override_seconds / 60, 1),
            "overridePhrase": self.rodecaster_override_phrase,
            **{key: evidence.get(key) for key in ("evidence", "remainingMinutes", "source", "confirmedAt", "confirmationDate", "sessionId", "expiresAt", "usedAt", "reason", "lastVerifiedTake", "attentionReason", "attentionAt")},
        }

    def _observed_ssd_rate(self) -> float:
        if self._rate_cache["checked"] is not None and time.monotonic() - self._rate_cache["checked"] < 60:
            return float(self._rate_cache["value"])
        groups: dict[str, list[Path]] = {}
        try:
            files = sorted(
                (item for item in self.ssd_root.iterdir() if item.is_file() and not item.name.startswith("._")),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )[:15]
        except OSError:
            files = []
        for item in files:
            if time.time() - item.stat().st_mtime < 10:
                continue
            key = re.sub(r"_Channel\d+(?=\.[^.]+$)", "", item.name, flags=re.IGNORECASE)
            groups.setdefault(key, []).append(item)
        rates = []
        for members in groups.values():
            if len(members) < 3:
                continue
            sizes = 0
            durations = []
            for item in members:
                try:
                    result = subprocess.run(
                        [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(item)],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=True,
                    )
                    duration = float(result.stdout.strip())
                except (OSError, subprocess.SubprocessError, ValueError):
                    duration = 0
                if duration > 0:
                    durations.append(duration)
                    sizes += item.stat().st_size
            if len(durations) == len(members):
                rates.append(sizes / max(durations) * 3600)
        observed = max([self.ssd_rate, *rates])
        self._rate_cache = {"checked": time.monotonic(), "value": observed}
        return observed

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _required(self, bytes_per_hour: float | None) -> int | None:
        if not bytes_per_hour:
            return None
        return round(bytes_per_hour * self.target_hours * (1 + self.margin))

    def _disk_surface(self, key: str, label: str, requested: Path, rate: float, *, required: bool = True) -> dict:
        path = existing_storage_path(requested)
        needed = self._required(rate)
        result = {
            "key": key,
            "label": label,
            "requestedPath": str(requested),
            "targetHours": self.target_hours,
            "safetyMarginPercent": round(self.margin * 100, 1),
            "bytesPerHour": round(rate),
            "requiredBytes": needed,
            "required": required,
        }
        if path is None:
            return {**result, "status": "unavailable", "message": "Volume is not mounted"}
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            return {**result, "status": "unavailable", "message": str(exc)}
        try:
            writable = not bool(os.statvfs(path).f_flag & getattr(os, "ST_RDONLY", 1))
        except OSError:
            writable = False
        status = "ready"
        message = f"{usage.free / GB:.1f} GB free"
        if not writable:
            status = "unavailable"
            message = "Volume is not writable"
        elif needed is not None and usage.free < needed:
            status = "warning"
            message = f"{usage.free / GB:.1f} GB free; {needed / GB:.1f} GB needed for target"
        return {
            **result,
            "status": status,
            "message": message,
            "volumePath": str(path),
            "device": path.stat().st_dev,
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
            "estimatedHours": round(usage.free / rate / (1 + self.margin), 2) if rate else None,
            "writable": writable,
        }

    @staticmethod
    def _find_rodecaster_mount() -> Path | None:
        try:
            matches = [p for p in Path("/Volumes").iterdir() if (p / "RODECaster").is_dir()]
        except OSError:
            return None
        return matches[0] if len(matches) == 1 else None

    def _rodecaster(self) -> dict:
        cached = (self._state().get("rodecaster") or {}).copy()
        mount = self._find_rodecaster_mount()
        checked_at = cached.get("checkedAt")
        rate = cached.get("bytesPerHour") or self.rode_fallback_rate
        source = "cached"
        if mount:
            usage = shutil.disk_usage(mount)
            cached.update({
                "checkedAt": utc_now(),
                "mount": str(mount),
                "totalBytes": usage.total,
                "usedBytes": usage.used,
                "freeBytes": usage.free,
            })
            checked_at = cached["checkedAt"]
            source = "live"
        needed = self._required(float(rate)) if rate else None
        age = time.time() - parse_time(checked_at) if parse_time(checked_at) else None
        base = {
            "key": "rodecaster",
            "label": "RØDECaster microSD",
            "targetHours": self.target_hours,
            "safetyMarginPercent": round(self.margin * 100, 1),
            "required": True,
            "source": source,
            "checkedAt": checked_at,
            "bytesPerHour": round(float(rate)) if rate else None,
            "requiredBytes": needed,
        }
        if not cached.get("freeBytes"):
            return {**base, "status": "warning", "message": "Capacity has not been measured in Transfer Mode"}
        free = int(cached["freeBytes"])
        if source == "cached" and (age is None or age > self.stale_seconds):
            status = "stale"
            message = f"Last capacity check is stale; cached free space {free / GB:.1f} GB"
        elif needed is None:
            status = "warning"
            message = f"{free / GB:.1f} GB free; recording-rate estimate unavailable"
        elif free < needed:
            status = "warning"
            message = f"{free / GB:.1f} GB free; {needed / GB:.1f} GB needed for target"
        else:
            status = "ready"
            message = f"{free / GB:.1f} GB free"
        return {
            **base,
            "status": status,
            "message": message,
            "freeBytes": free,
            "totalBytes": cached.get("totalBytes"),
            "estimatedHours": round(free / float(rate) / (1 + self.margin), 2) if rate else None,
            "ageSeconds": round(age) if age is not None else None,
        }

    def _mode(self) -> str:
        try:
            return self.mode_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "strict"

    def _sony(self) -> list[dict]:
        if self._mode() == "ndi-test":
            return [{"key": f"fx30-{name}", "label": f"FX30 {name.upper()} card", "status": "bypassed", "required": False, "message": "Bypassed in ndi-test mode"} for name in ("a", "b", "c")]
        try:
            with urlopen(self.fx30_status_url, timeout=5) as response:
                payload = json.load(response)
        except (OSError, URLError, ValueError):
            return [{"key": "fx30-enabled", "label": "Enabled FX30 cards", "status": "warning", "required": True, "message": "Card capacity unavailable"}]
        reports = payload.get("cardStatus") or {}
        enabled_ids = [
            str(camera.get("id"))
            for camera in (payload.get("cameraStatus") or [])
            if camera.get("id")
        ]
        if not enabled_ids:
            return [{"key": "fx30-enabled", "label": "Enabled FX30 cards", "status": "warning", "required": True, "message": "No enabled camera status available"}]
        rows = []
        for camera_id in enabled_ids:
            key = camera_id
            report = reports.get(key) or {}
            slots = report.get("slots") or []
            minutes = [float(slot.get("remainingMinutes")) for slot in slots if slot.get("remainingMinutes") is not None]
            remaining = max(minutes) if minutes else None
            if remaining is None:
                status, message = "warning", "Remaining recording time unavailable"
            elif remaining < self.target_hours * 60:
                status, message = "warning", f"{remaining:.0f} minutes remaining"
            else:
                status, message = "ready", f"{remaining:.0f} minutes remaining"
            rows.append({"key": key, "label": f"{camera_id.upper()} card", "status": status, "required": True, "remainingMinutes": remaining, "message": message})
        return rows

    def refresh_sony(self) -> None:
        if self._mode() == "ndi-test":
            return
        request = Request(self.fx30_refresh_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        try:
            urlopen(request, timeout=40).read()
        except (OSError, URLError):
            return

    def report(self, *, refresh_sony: bool = False) -> dict:
        if refresh_sony:
            self.refresh_sony()
        ssd_rate = self._observed_ssd_rate()
        rode_rate = float(self.rode_fallback_rate) if self.include_rodecaster_in_preflight and self.rode_fallback_rate else 0
        lucidlink_rate = ssd_rate + self.proxy_rate + rode_rate
        if self.local_only:
            surfaces = [self._disk_surface(
                "extreme-ssd",
                "Extreme SSD — masters + proxies",
                self.ssd_root,
                ssd_rate + self.proxy_rate,
            )]
        else:
            surfaces = [
                self._disk_surface("extreme-ssd", "Extreme SSD", self.ssd_root, ssd_rate),
                self._disk_surface("mac-proxies", "Mac proxy workspace", self.proxy_root, self.proxy_rate),
                self._disk_surface("lucidlink", "LucidLink delivery", self.lucidlink_root, lucidlink_rate),
            ]
        # Always surface the fail-closed operator confirmation, including local-only mode.
        surfaces.append(self.rodecaster_capacity())
        surfaces.extend(self._sony())
        hard_failures = [item for item in surfaces if item.get("required") and item.get("status") == "unavailable"]
        warnings = [item for item in surfaces if item.get("status") in {"warning", "stale"}]
        capacity = next(item for item in surfaces if item["key"] == "rodecaster-capacity")
        capacity_blocked = not capacity["gateOpen"]
        return {
            "ok": not hard_failures and not capacity_blocked,
            "status": "unavailable" if hard_failures or capacity_blocked else "warning" if warnings else "ready",
            "targetHours": self.target_hours,
            "safetyMarginPercent": round(self.margin * 100, 1),
            "checkedAt": utc_now(),
            "warningOnly": False,
            "hardFailures": [item["key"] for item in hard_failures] + (["rodecaster-capacity"] if capacity_blocked and capacity["key"] not in [item["key"] for item in hard_failures] else []),
            "warnings": [item["key"] for item in warnings],
            "surfaces": surfaces,
        }
