#!/usr/bin/env python3
"""Local HTTP bridge for a strict three-channel Just In Mac Lite NDI recording workflow."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlparse

from ingest import OperationCancelled, copy_verified, create_proxies, movie_files, stable_files, stable_paths
from just_control_api import JustControlApiError, JustControlClient
from recovery import RecoveryAttention, RecoveryWorker, SessionLedger, mapping_from_config, probe_media, utc_now
from storage_manager import StorageManager


DEFAULT_CHANNELS = ["Channel1", "Channel2", "Channel3"]
ACTIVE_POST_STATES = {
    "queued",
    "waiting_for_files",
    "backing_up",
    "copying_masters",
    "creating_proxies",
    "copying_proxies",
    "cancelling",
}
NOTIFICATION_COMMAND = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_notification.command")
DEFAULT_RECORDING_LOCK = Path("/private/tmp/strict_studio_recording.lock")
DEFAULT_STOP_MARKER = Path("/private/tmp/strict_studio_stop_v4_local.sent")
DEFAULT_NO_CAMERA_RECORD_COMMAND = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/record_no_camera_v4_local.command")
DEFAULT_NO_CAMERA_STOP_COMMAND = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/strict_studio_stop_v4_local.command")
DEFAULT_NO_CAMERA_ARM_RECEIPT_DIR = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/no_camera_test_receipts")
DEFAULT_NO_CAMERA_SESSION = Path("/private/tmp/strict_studio_no_camera_recording.session")


def notify_studio(event_key: str, title: str, message: str, *, enabled: bool = True) -> dict:
    if not enabled:
        return {"ok": True, "sent": False, "disabled": True, "eventKey": event_key}
    try:
        completed = subprocess.run(
            ["/bin/bash", str(NOTIFICATION_COMMAND), event_key, title, str(message)[:500]],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        status = completed.stdout.strip() or "failed"
        return {
            "ok": completed.returncode == 0,
            "sent": status == "sent",
            "status": status,
            "eventKey": event_key,
            **({"error": completed.stderr.strip()} if completed.stderr.strip() else {}),
        }
    except Exception as exc:
        print(f"macOS notification failed for {event_key}: {exc}", flush=True)
        return {"ok": False, "sent": False, "eventKey": event_key, "error": str(exc)}


def notify_request_failure(path: str, error: Exception | str, *, enabled: bool = True) -> None:
    if path == "/v4/record/start":
        notify_studio(
            "record-start-failed",
            "Studio RECORD failed",
            f"Just In did not start all three channels: {error}",
            enabled=enabled,
        )
    elif path == "/v4/record/stop":
        notify_studio(
            "stop-incomplete",
            "Studio STOP incomplete",
            f"Just In did not confirm stop on all channels: {error}",
            enabled=enabled,
        )


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _websocket_send(sock: socket.socket, payload: dict) -> None:
    """Send one client-masked JSON websocket frame; used only for OBS status."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = bytearray([0x81])
    length = len(raw)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.extend([0x80 | 126])
        header.extend(struct.pack("!H", length))
    else:
        header.extend([0x80 | 127])
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    header.extend(mask)
    header.extend(bytes(value ^ mask[index % 4] for index, value in enumerate(raw)))
    sock.sendall(header)


def _websocket_receive(sock: socket.socket) -> dict:
    """Read one unfragmented JSON websocket frame from OBS."""
    def read_exact(size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("OBS WebSocket closed while reading record status")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    first, second = read_exact(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(8))[0]
    masked = bool(second & 0x80)
    mask = read_exact(4) if masked else b""
    raw = read_exact(length)
    if masked:
        raw = bytes(value ^ mask[index % 4] for index, value in enumerate(raw))
    if opcode != 0x1:
        raise RuntimeError("OBS WebSocket returned a non-text record-status frame")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OBS WebSocket returned invalid record-status JSON") from exc


def obs_request(host: str, port: int, timeout: float, request_type: str, password: str | None = None) -> dict:
    """Send one OBS WebSocket request after a fresh authenticated session."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(1024)
            if not chunk:
                raise RuntimeError("OBS WebSocket closed during upgrade")
            response.extend(chunk)
            if len(response) > 16384:
                raise RuntimeError("OBS WebSocket upgrade response was too large")
        if not response.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("OBS WebSocket did not accept a read-only status connection")
        hello = _websocket_receive(sock)
        if hello.get("op") != 0:
            raise RuntimeError("OBS WebSocket did not provide its Hello message")
        authentication = (hello.get("d") or {}).get("authentication")
        identify = {"rpcVersion": 1}
        if authentication:
            if not password:
                raise RuntimeError("OBS status requires a configured read-only password")
            salt = str(authentication.get("salt", ""))
            challenge = str(authentication.get("challenge", ""))
            secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + challenge).encode("utf-8")).digest()
            ).decode("ascii")
        _websocket_send(sock, {"op": 1, "d": identify})
        identified = _websocket_receive(sock)
        if identified.get("op") != 2:
            raise RuntimeError("OBS WebSocket did not confirm read-only identification")
        request_id = uuid.uuid4().hex
        _websocket_send(sock, {
            "op": 6,
            "d": {"requestType": request_type, "requestId": request_id},
        })
        result = _websocket_receive(sock)
        data = result.get("d") or {}
        if result.get("op") != 7 or data.get("requestId") != request_id:
            raise RuntimeError(f"OBS did not confirm {request_type}")
        if not (data.get("requestStatus") or {}).get("result"):
            comment = (data.get("requestStatus") or {}).get("comment", "request failed")
            raise RuntimeError(f"OBS {request_type} failed: {comment}")
        return data.get("responseData") or {}


def obs_recording_status(host: str, port: int, timeout: float, password: str | None = None) -> bool:
    """Return OBS's explicit outputActive status without changing OBS state."""
    active = obs_request(host, port, timeout, "GetRecordStatus", password).get("outputActive")
    if active is not True and active is not False:
        raise RuntimeError("OBS did not provide an unambiguous recording status")
    return active


class Bridge:
    def __init__(self, config: dict):
        api_config = config.get("api", {})
        self.config = config
        self.recorder_mode = str(config.get("recorderMode", "just-in")).lower()
        if self.recorder_mode not in {"just-in", "obs"}:
            raise ValueError("recorderMode must be 'just-in' or 'obs'")
        self.api = JustControlClient(
            host=api_config.get("host", "127.0.0.1"),
            port=int(api_config.get("port", 8080)),
            timeout=float(api_config.get("timeoutSeconds", 5)),
        )
        self.channels = config.get("channels") or [{"name": name} for name in DEFAULT_CHANNELS]
        self.source_root = Path(config.get("sourceRoot", str(Path.home() / "Movies" / "JUSTCONTROL_RECORDING")))
        self.backup_root = Path(config["backupRoot"]) if config.get("backupRoot") else None
        self.delivery_root = Path(config.get("deliveryRoot", str(self.source_root)))
        self.proxy_working_root = Path(config.get("proxyWorkingRoot", str(self.source_root.parent / "JUSTCONTROL_PROXIES")))
        self.proxy_delivery_root = Path(config.get("proxyDeliveryRoot", str(self.proxy_working_root)))
        self.extensions = {str(ext).lower() for ext in config.get("extensions", [".mov", ".mp4", ".mxf"])}
        self.proxy_extensions = {str(ext).lower() for ext in config.get("proxyExtensions", [".mov", ".mp4", ".mxf"])}
        bridge_config = config.get("bridge", {})
        self.stable_seconds = float(bridge_config.get("stableSeconds", 3))
        self.settle_timeout_seconds = float(bridge_config.get("settleTimeoutSeconds", 30))
        post_config = config.get("postRecord", {})
        self.auto_post_record = bool(post_config.get("enabled", True))
        self.local_only = bool(post_config.get("localOnly", False))
        self.ffmpeg_path = str(post_config.get("ffmpegPath", "/opt/homebrew/bin/ffmpeg"))
        self.ffprobe_path = str(post_config.get("ffprobePath", "/opt/homebrew/bin/ffprobe"))
        self.proxy_height = int(post_config.get("proxyHeight", 1080))
        self.proxy_video_bitrate = str(post_config.get("proxyVideoBitrate", "10M"))
        self.proxy_audio_bitrate = str(post_config.get("proxyAudioBitrate", "192k"))
        notification_config = post_config.get("macNotifications") or {}
        self.mac_notifications = bool(notification_config.get("enabled", True))
        self.notify_post_success = bool(notification_config.get("postRecordSuccess", False))
        self.require_all = bool(config.get("requireAllChannels", True))
        obs_config = config.get("obs") or {}
        self.obs_host = str(obs_config.get("host", "127.0.0.1"))
        self.obs_port = int(obs_config.get("port", 4455))
        self.obs_timeout = float(obs_config.get("timeoutSeconds", 5))
        self.obs_password = os.environ.get(str(obs_config.get("passwordEnvironment", "OBS_WEBSOCKET_PASSWORD")))
        self.lock = threading.RLock()
        self.recording = False
        self.session_started_at: float | None = None
        self.session_files: list[Path] = []
        self.last_result: dict | None = None
        self.post_status: dict = {"state": "idle"}
        self.post_cancel_event = threading.Event()
        self.post_queue: queue.Queue[dict] = queue.Queue()
        self.post_worker = threading.Thread(target=self._post_worker, name="just-post-record", daemon=True)
        self.post_worker.start()
        # Capacity evidence must never survive an unobserved bridge restart.
        self.capacity_session_id = uuid.uuid4().hex
        self.storage_manager = StorageManager(config, capacity_session_id=self.capacity_session_id)
        policy = config.get("storagePolicy") or {}
        self.rodecaster_cleanup_command = policy.get("rodecasterCleanupCommand") or [
            "/Users/podcaststudio/Developer/StudioOS-Build-V4/transfer_and_ingest_rodecaster_local.command"
        ]
        self.include_rodecaster_in_completion = bool(policy.get("includeRodecasterInCompletion", True)) and not self.local_only
        self.storage_cleanup_lock = threading.Lock()
        self.recovery_worker = RecoveryWorker(config)
        self.recovery_mapping = mapping_from_config(config) if self.recovery_worker.enabled else []
        self.recovery_ledger: SessionLedger | None = None
        self.liveness_stop = threading.Event()
        self.liveness_thread: threading.Thread | None = None
        self.recovery_restart_attention = self._recovery_restart_attention()
        failed_stop_config = config.get("failedStopRecovery") or {}
        self.recording_lock_path = Path(failed_stop_config.get("recordingLockPath", DEFAULT_RECORDING_LOCK))
        self.stop_marker_path = Path(failed_stop_config.get("stopMarkerPath", DEFAULT_STOP_MARKER))
        no_camera_config = config.get("noCameraRecord") or {}
        self.no_camera_record_command = Path(no_camera_config.get("recordCommand", DEFAULT_NO_CAMERA_RECORD_COMMAND))
        self.no_camera_stop_command = Path(no_camera_config.get("stopCommand", DEFAULT_NO_CAMERA_STOP_COMMAND))
        self.no_camera_arm_receipt_dir = Path(no_camera_config.get("armReceiptDirectory", DEFAULT_NO_CAMERA_ARM_RECEIPT_DIR))
        self.no_camera_session_path = Path(no_camera_config.get("sessionPath", DEFAULT_NO_CAMERA_SESSION))
        self.no_camera_action_lock = threading.Lock()
        self.no_camera_action = {"state": "idle"}

    def _persist_runtime_state_required(self, key: str, value: dict) -> None:
        """Persist safety receipts before any local recovery marker is retired."""
        state = self.storage_manager._state()
        state[key] = value
        self.storage_manager._write_state(state)

    def _persist_runtime_state(self, key: str, value: dict) -> None:
        try:
            state_file = self.storage_manager.state_file
            state = self.storage_manager._state()
            state[key] = value
            state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_file.with_name(f".{state_file.name}.partial-{uuid.uuid4().hex}")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(state_file)
        except AttributeError:
            # Lightweight unit-test doubles may intentionally omit persistent storage.
            return
        except OSError as exc:
            print(f"WARNING could not persist {key}: {exc}", flush=True)

    def _no_camera_action_status(self) -> dict:
        """Return coordinator state only; it is never recorder-state evidence."""
        return dict(getattr(self, "no_camera_action", {"state": "unavailable"}))

    def _fresh_no_camera_arm_receipt(self) -> Path:
        receipt_dir = getattr(self, "no_camera_arm_receipt_dir", DEFAULT_NO_CAMERA_ARM_RECEIPT_DIR)
        receipts = sorted(receipt_dir.glob("*-arm-no-camera.txt"), key=lambda path: path.stat().st_mtime)
        if not receipts:
            raise RuntimeError("ARM: NO CAMERA must pass before a no-camera recording can start")
        receipt = receipts[-1]
        if "result=success mode_restored_on_exit=true" not in receipt.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError("The latest ARM: NO CAMERA check did not pass")
        if time.time() - receipt.stat().st_mtime > 15 * 60:
            raise RuntimeError("ARM: NO CAMERA is older than 15 minutes; arm again before recording")
        return receipt

    def _start_no_camera_worker(self, action: str, command: Path) -> None:
        """Run the existing guarded shell coordinator, never device controls here."""
        try:
            completed = subprocess.run(
                [str(command)],
                cwd="/Users/podcaststudio/Developer/StudioOS-Build-V4",
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()[-2000:]
            with self.no_camera_action_lock:
                if completed.returncode == 0:
                    self.no_camera_action = {
                        "state": "sent" if action == "start" else "stop-sent",
                        "action": action,
                        "finishedAt": utc_now(),
                        "detail": output or ("No-camera command completed"),
                    }
                else:
                    self.no_camera_action = {
                        "state": "attention",
                        "action": action,
                        "finishedAt": utc_now(),
                        "detail": output or "The guarded no-camera command did not complete",
                    }
        except (OSError, subprocess.TimeoutExpired) as exc:
            with self.no_camera_action_lock:
                self.no_camera_action = {"state": "attention", "action": action, "finishedAt": utc_now(), "detail": str(exc)}

    def _launch_no_camera_worker(self, action: str, command: Path) -> dict:
        with self.no_camera_action_lock:
            current = self._no_camera_action_status()
            if current.get("state") in {"starting", "stopping"}:
                raise RuntimeError("A no-camera recorder request is already in progress")
            self.no_camera_action = {
                "state": "starting" if action == "start" else "stopping",
                "action": action,
                "requestedAt": utc_now(),
            }
        # Start after releasing the state lock. This preserves the short
        # single-flight check and lets an immediate test worker publish its
        # completion without waiting on the request thread.
        threading.Thread(
            target=self._start_no_camera_worker,
            args=(action, command),
            name=f"no-camera-{action}",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "action": f"no-camera-{action}",
            "accepted": True,
            "detail": "The existing guarded no-camera coordinator is running. This is not recording or media proof.",
        }

    def start_no_camera_recording(self) -> dict:
        """Launch only the reviewed no-camera coordinator after local guard checks."""
        if self.recording:
            raise RuntimeError("V4 already reports a recording; no-camera RECORD was not sent")
        if self.post_status.get("state") in ACTIVE_POST_STATES:
            raise RuntimeError("No-camera RECORD is blocked while V4 post-record work is active")
        command = getattr(self, "no_camera_record_command", DEFAULT_NO_CAMERA_RECORD_COMMAND)
        if not command.is_file() or not os.access(command, os.X_OK):
            raise RuntimeError("The guarded no-camera record coordinator is unavailable")
        session_path = getattr(self, "no_camera_session_path", DEFAULT_NO_CAMERA_SESSION)
        if session_path.exists():
            raise RuntimeError("A no-camera recording session already needs STOP verification")
        receipt = self._fresh_no_camera_arm_receipt()
        result = self._launch_no_camera_worker("start", command)
        result["armReceipt"] = str(receipt)
        return result

    def stop_no_camera_recording(self) -> dict:
        """Launch only the reviewed no-camera STOP coordinator once, never retry a toggle."""
        if not self.recording:
            raise RuntimeError("V4 does not report an active recording; no-camera STOP was not sent")
        command = getattr(self, "no_camera_stop_command", DEFAULT_NO_CAMERA_STOP_COMMAND)
        if not command.is_file() or not os.access(command, os.X_OK):
            raise RuntimeError("The guarded no-camera STOP coordinator is unavailable")
        session_path = getattr(self, "no_camera_session_path", DEFAULT_NO_CAMERA_SESSION)
        if not session_path.exists():
            # If the post-start receipt write failed, the coordinator leaves
            # explicit mode + the normal guarded recording lock in place. That
            # is enough evidence to send the same one-shot no-camera STOP,
            # and avoids stranding a live recorder behind a missing receipt.
            try:
                mode = self.storage_manager.mode_file.read_text(encoding="utf-8").strip()
            except (AttributeError, OSError):
                mode = ""
            recording_lock = getattr(self, "recording_lock_path", DEFAULT_RECORDING_LOCK)
            if mode != "ndi-test" or not recording_lock.exists():
                raise RuntimeError("No-camera STOP is blocked because its guarded session marker is missing")
        return self._launch_no_camera_worker("stop", command)

    def _failed_stop_recovery_mode(self) -> tuple[str, bool]:
        try:
            mode = self.storage_manager.mode_file.read_text(encoding="utf-8").strip() or "strict"
        except (OSError, AttributeError):
            mode = "strict"
        return mode, mode != "ndi-test"

    def _failed_stop_recovery_just_in(self) -> tuple[dict[str, dict], bool]:
        """Require each requested channel to explicitly return rec=false."""
        observed: dict[str, dict] = {}
        try:
            configured = set(self.api.all_channels())
        except JustControlApiError as exc:
            return {name: {"state": "unknown", "error": str(exc)} for name in self.requested_names}, False
        all_stopped = True
        for name in self.requested_names:
            if name not in configured:
                observed[name] = {"state": "unknown", "error": "Requested channel is not configured"}
                all_stopped = False
                continue
            try:
                response = self.api.recording_status(name)
                recording = response.get("rec") if isinstance(response, dict) else None
                if recording is False:
                    observed[name] = {"state": "stopped", "recording": False}
                elif recording is True:
                    observed[name] = {"state": "recording", "recording": True}
                    all_stopped = False
                else:
                    observed[name] = {"state": "unknown", "error": "Just In did not provide rec=true or rec=false"}
                    all_stopped = False
            except JustControlApiError as exc:
                observed[name] = {"state": "unknown", "error": str(exc)}
                all_stopped = False
        return observed, all_stopped

    def _failed_stop_recovery_obs(self) -> dict:
        recovery_config = getattr(self, "config", {}).get("failedStopRecovery") or {}
        obs_config = recovery_config.get("obs") or {}
        password = None
        password_env = str(obs_config.get("passwordEnvironment", "OBS_WEBSOCKET_PASSWORD")).strip()
        if password_env:
            password = os.environ.get(password_env)
        try:
            recording = obs_recording_status(
                str(obs_config.get("host", "127.0.0.1")),
                int(obs_config.get("port", 4455)),
                float(obs_config.get("timeoutSeconds", 3)),
                password,
            )
            return {"state": "recording" if recording else "stopped", "recording": recording}
        except (OSError, RuntimeError, ValueError) as exc:
            return {"state": "unknown", "error": str(exc)}

    def _failed_stop_recovery_fx30(self) -> dict:
        try:
            with urlopen(self.storage_manager.fx30_status_url, timeout=3) as response:
                payload = json.load(response)
            state = payload.get("state") if isinstance(payload, dict) else None
            recording = state.get("recording") if isinstance(state, dict) else None
            action_running = state.get("record_action_running") if isinstance(state, dict) else None
            if recording is False and action_running is False:
                return {"state": "stopped", "recording": False, "recordActionRunning": False}
            if recording is True or action_running is True:
                return {"state": "recording", "recording": recording, "recordActionRunning": action_running}
            return {"state": "unknown", "error": "FX30 bridge did not provide explicit inactive recording state"}
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            return {"state": "unknown", "error": str(exc)}

    def failed_stop_recovery_status(self) -> dict:
        """Read-only eligibility for recovering V4's stale in-memory STOP state."""
        just_in, just_in_stopped = self._failed_stop_recovery_just_in()
        mode, cameras_required = self._failed_stop_recovery_mode()
        stale_state = bool(self.recording or getattr(self, "session_started_at", None) is not None)
        active_post = self.post_status.get("state") in ACTIVE_POST_STATES
        if not stale_state:
            reason = "V4 does not retain a recording or session flag"
        elif active_post:
            reason = "V4 is still performing post-record work"
        elif not just_in_stopped:
            reason = "Every requested Just In channel must explicitly report stopped"
        else:
            reason = "Ready for physical RØDECaster confirmation and final read-only checks"
        receipt = (self.storage_manager._state().get("failedStopRecovery") or {}).copy()
        return {
            "eligible": stale_state and not active_post and just_in_stopped,
            "reason": reason,
            "staleV4Session": stale_state,
            "postRecordActive": active_post,
            "justIn": just_in,
            "mode": mode,
            "camerasRequired": cameras_required,
            "receipt": receipt or None,
        }

    def _retire_failed_stop_markers(self) -> list[str]:
        failures = []
        retired: list[tuple[Path, bool]] = []
        for marker in (self.recording_lock_path, self.stop_marker_path):
            try:
                if marker.is_dir():
                    marker.rmdir()
                    retired.append((marker, True))
                else:
                    existed = marker.exists()
                    marker.unlink(missing_ok=True)
                    if existed:
                        retired.append((marker, False))
            except OSError as exc:
                failures.append(f"{marker}: {exc}")
                # Do not leave one guard cleared if the paired marker could
                # not be retired. These markers are intentionally empty.
                for previous, was_directory in reversed(retired):
                    try:
                        if was_directory:
                            previous.mkdir(exist_ok=True)
                        else:
                            previous.touch(exist_ok=True)
                    except OSError as restore_error:
                        failures.append(f"could not restore {previous}: {restore_error}")
                break
        return failures

    def recover_failed_stop(self, rodecaster_confirmed_off: bool, confirmation_source: str) -> dict:
        """Clear only a proven stale V4 state. It never sends a recorder command."""
        with self.lock:
            persisted = self.storage_manager._state()
            prior_receipt = (persisted.get("failedStopRecovery") or {}).copy()
            stale_state = bool(self.recording or getattr(self, "session_started_at", None) is not None)
            if prior_receipt.get("result") == "recovered" and not stale_state:
                return {"ok": True, "action": "recover-failed-stop", "alreadyRecovered": True, "receipt": prior_receipt}
            if not stale_state:
                raise RuntimeError("Failed STOP recovery is available only while V4 retains a recording or session flag")
            if self.post_status.get("state") in ACTIVE_POST_STATES:
                raise RuntimeError("Failed STOP recovery is blocked while V4 post-record work is active")

            just_in, just_in_stopped = self._failed_stop_recovery_just_in()
            if not just_in_stopped:
                raise RuntimeError("Failed STOP recovery is blocked: every requested Just In channel must explicitly report stopped")
            obs = self._failed_stop_recovery_obs()
            if obs.get("state") != "stopped":
                raise RuntimeError("Failed STOP recovery is blocked: OBS recording inactive was not positively confirmed")
            mode, cameras_required = self._failed_stop_recovery_mode()
            fx30 = {"state": "not-required"}
            if cameras_required:
                fx30 = self._failed_stop_recovery_fx30()
                if fx30.get("state") != "stopped":
                    raise RuntimeError("Failed STOP recovery is blocked: FX30 recording inactive was not positively confirmed")
            if rodecaster_confirmed_off is not True:
                raise RuntimeError("Failed STOP recovery requires explicit confirmation that the physical RØDECaster REC light and timer are off")
            if not confirmation_source.strip():
                raise RuntimeError("Failed STOP recovery requires a physical RØDECaster confirmation source")

            receipt = {
                "action": "recover-failed-stop",
                "validatedAt": utc_now(),
                "previousSession": {
                    "recording": self.recording,
                    "sessionStartedAt": self.session_started_at,
                    "lastResult": self.last_result,
                },
                "originalStopFailure": persisted.get("failedStop") or {
                    "state": "not-persisted",
                    "detail": "The stale session predates durable failed-STOP receipts.",
                },
                "observedStoppedStates": {"justIn": just_in, "obs": obs, "fx30": fx30, "mode": mode},
                "rodecasterConfirmation": {
                    "confirmedOff": True,
                    "source": confirmation_source.strip(),
                    "confirmedAt": utc_now(),
                },
                "result": "validated",
            }
            # This receipt is durable before any marker can be removed.
            self._persist_runtime_state_required("failedStopRecovery", receipt)
            marker_failures = self._retire_failed_stop_markers()
            if marker_failures:
                receipt.update({"result": "marker-retirement-failed", "markerErrors": marker_failures, "finishedAt": utc_now()})
                self._persist_runtime_state_required("failedStopRecovery", receipt)
                raise RuntimeError("Failed STOP recovery is blocked because safety markers could not be retired")

            self.recording = False
            self.session_started_at = None
            receipt.update({"result": "recovered", "recoveredAt": utc_now(), "retiredMarkers": [str(self.recording_lock_path), str(self.stop_marker_path)]})
            self._persist_runtime_state_required("failedStopRecovery", receipt)
            self.last_result = {"ok": True, "action": "recover-failed-stop", "recoveredAt": receipt["recoveredAt"]}
            return {"ok": True, "action": "recover-failed-stop", "alreadyRecovered": False, "receipt": receipt}

    def _post_record_summary(self) -> dict:
        persisted = self.storage_manager._state()
        verification = persisted.get("postRecordVerification") or {}
        cleanup = persisted.get("rodecasterCleanup") or {"state": "idle"}
        post_state = verification.get("state", "idle")
        cleanup_state = cleanup.get("state", "idle")
        active_post = post_state == "running" or self.post_status.get("state") in ACTIVE_POST_STATES
        include_rode = getattr(self, "include_rodecaster_in_completion", True)
        active_cleanup = include_rode and cleanup_state in {"checking", "copying", "verifying", "cleaning"}
        checks = [
            {
                "key": "recording-stopped",
                "label": "Recording stopped and files finalised",
                "passed": not self.recording and post_state in {"complete", "attention", "failed"},
            },
            {
                "key": "local-masters",
                "label": "SSD masters finalised",
                "passed": bool(verification.get("mastersVerified")),
                "count": verification.get("masterCount"),
            },
            {
                "key": "local-proxies",
                "label": "SSD proxies created and media-verified",
                "passed": bool(verification.get("proxiesVerified")),
                "count": verification.get("proxyCount"),
            },
        ]
        if include_rode:
            checks.append({
                "key": "rodecaster",
                "label": "RØDECaster archived and card cleanup checked",
                "passed": cleanup_state == "complete",
            })
        all_passed = bool(checks) and all(item["passed"] for item in checks)
        if self.recording:
            state = "recording"
            headline = "RECORDING IN PROGRESS"
            detail = "Post-record verification starts after STOP + DELIVER."
        elif active_post or active_cleanup:
            state = "running"
            headline = "POST-RECORD CHECKS RUNNING"
            detail = "Keep the studio and storage connected while files are copied and verified."
        elif all_passed:
            state = "complete"
            headline = "LOCAL RECORDING VERIFIED"
            detail = "Masters and playable proxies are finalised and delivered to LucidLink."
        elif include_rode and post_state == "complete" and cleanup_state == "attention":
            state = "attention"
            headline = "VIDEO DELIVERY PASSED — RØDECASTER NEEDS ATTENTION"
            detail = cleanup.get("error") or "The RØDECaster archive or cleanup check did not complete."
        elif post_state in {"attention", "failed", "cancelled"} or (include_rode and cleanup_state == "attention"):
            state = "attention"
            headline = "POST-RECORD CHECKS NEED ATTENTION"
            detail = verification.get("error") or cleanup.get("error") or "One or more delivery checks did not pass."
        elif include_rode and post_state == "complete":
            state = "attention"
            headline = "VIDEO VERIFIED — RØDECASTER CHECK PENDING"
            detail = "Local masters and proxies passed; RØDECaster has not yet completed its archive check."
        else:
            state = "pending"
            headline = "NO COMPLETED POST-RECORD CHECK YET"
            detail = "Run STOP after recording to finalise masters and create local SSD proxies."
        return {
            "state": state,
            "headline": headline,
            "detail": detail,
            "allChecksPassed": all_passed,
            "completedAt": verification.get("completedAt"),
            "jobId": verification.get("jobId"),
            "proxyTiming": verification.get("proxyTiming"),
            "checks": checks,
            "rodecasterIncluded": include_rode,
            "rodecaster": cleanup,
        }

    @property
    def requested_names(self) -> list[str]:
        return [str(channel["name"]) for channel in self.channels if channel.get("enabled", True)]

    def _recovery_restart_attention(self) -> str | None:
        """A persisted non-terminal recovery manifest is never silently resumed."""
        state = self.storage_manager._state().get("recoverySession") or {}
        path = state.get("path")
        if not path:
            return None
        try:
            ledger = SessionLedger.load(Path(path))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return f"Recovery session state cannot be read after restart: {exc}"
        if ledger.manifest.get("state") not in {"editor_ready", "attention", "complete"}:
            return "Recovery session was interrupted by a bridge restart; inspect its manifest before another take."
        return None

    def _persist_recovery_session(self) -> None:
        if getattr(self, "recovery_ledger", None) is None:
            return
        self._persist_runtime_state("recoverySession", {
            "sessionId": self.recovery_ledger.session_id,
            "path": str(self.recovery_ledger.path),
            "state": self.recovery_ledger.manifest.get("state"),
            "updatedAt": utc_now(),
        })

    def _begin_recovery_session(self) -> None:
        if not getattr(self, "recovery_worker", None) or not self.recovery_worker.enabled:
            return
        self.recovery_ledger = SessionLedger(self.recovery_worker.staging_root / "SESSIONS", self.recovery_mapping)
        self._persist_recovery_session()

    def _observe_liveness_once(self) -> None:
        """Read recorder and source-file evidence only. It never controls a recorder."""
        ledger = getattr(self, "recovery_ledger", None)
        if ledger is None or not self.recording:
            return
        recovery_config = self.config.get("recovery") or {}
        now = time.time()
        warning_after = float(recovery_config.get("noGrowthWarningSeconds", 10))
        failure_after = float(recovery_config.get("noGrowthFailureSeconds", 30))
        suspected: list[str] = []
        for mapping in self.recovery_mapping:
            channel = mapping["justInChannel"]
            evidence: dict = {"observedAt": utc_now(), "channel": channel}
            try:
                evidence["recording"] = bool(self.api.recording_status(channel).get("rec"))
            except JustControlApiError as exc:
                evidence["recording"] = None
                evidence["recordingError"] = str(exc)
            try:
                paths = self.api.recording_paths(channel).get("paths") or []
            except JustControlApiError as exc:
                paths = []
                evidence["pathsError"] = str(exc)
            normalized = []
            for item in paths:
                raw = item.get("path") if isinstance(item, dict) else item
                if raw:
                    normalized.append(str(raw))
            evidence["paths"] = normalized
            previous = (ledger.manifest.get("liveness") or {}).get(channel) or {}
            observed_files = []
            for raw in normalized:
                path = Path(raw)
                if path.is_file():
                    stat = path.stat()
                    observed_files.append({"path": str(path), "sizeBytes": stat.st_size, "modifiedAt": stat.st_mtime})
            evidence["files"] = observed_files
            previous_sizes = {item.get("path"): item.get("sizeBytes") for item in previous.get("files") or []}
            grew = any(item["sizeBytes"] > (previous_sizes.get(item["path"]) or 0) for item in observed_files)
            last_growth = now if grew else float(previous.get("lastGrowthAt") or now)
            evidence["lastGrowthAt"] = last_growth
            idle_seconds = max(0.0, now - last_growth)
            evidence["noGrowthSeconds"] = round(idle_seconds, 1)
            evidence["warning"] = bool(observed_files and idle_seconds >= warning_after)
            evidence["suspectedFailed"] = bool(
                evidence.get("recording") is False
                or (observed_files and idle_seconds >= failure_after and evidence.get("recording") is not True)
                or (evidence.get("recording") is False and not normalized)
            )
            ledger.manifest.setdefault("liveness", {})[channel] = evidence
            if evidence["suspectedFailed"]:
                suspected.append(channel)
        if suspected:
            ledger.transition("recording_suspect")
            ledger.emit("ndi.failure_suspected", {"channels": suspected})
        else:
            ledger.persist()

    def _liveness_loop(self) -> None:
        interval = max(1.0, float((self.config.get("recovery") or {}).get("livenessPollSeconds", 10)))
        while not self.liveness_stop.wait(interval):
            try:
                self._observe_liveness_once()
            except Exception as exc:
                if getattr(self, "recovery_ledger", None) is not None:
                    self.recovery_ledger.manifest.setdefault("livenessErrors", []).append({"at": utc_now(), "error": str(exc)})
                    self.recovery_ledger.persist()

    def _start_liveness_monitor(self) -> None:
        if getattr(self, "recovery_ledger", None) is None:
            return
        self.liveness_stop.clear()
        self._observe_liveness_once()
        self.liveness_thread = threading.Thread(target=self._liveness_loop, name="v4-recovery-liveness", daemon=True)
        self.liveness_thread.start()

    def _stop_liveness_monitor(self) -> None:
        if not hasattr(self, "liveness_stop"):
            return
        self.liveness_stop.set()
        if self.liveness_thread is not None:
            self.liveness_thread.join(timeout=2)
        self.liveness_thread = None

    def _post_readiness(self) -> dict:
        source_files_readable = False
        if self.source_root.is_dir():
            try:
                with os.scandir(self.source_root) as entries:
                    next(entries, None)
                source_files_readable = True
            except OSError:
                source_files_readable = False
        checks = {
            "sourceRoot": self.source_root.is_dir(),
            "sourceFilesReadable": source_files_readable,
            "backupVolume": self.backup_root is None or self.backup_root.parent.is_dir(),
            "proxyWorkingParent": self.proxy_working_root.parent.is_dir(),
            "ffmpeg": Path(self.ffmpeg_path).is_file(),
            "ffprobe": Path(self.ffprobe_path).is_file(),
        }
        if not self.local_only:
            checks["deliveryVolume"] = self.delivery_root.parent.is_dir()
            checks["proxyDeliveryVolume"] = self.proxy_delivery_root.parent.is_dir()
        return {"ok": all(checks.values()), "checks": checks}

    def _channel_details(self, name: str, all_channels: set[str], active_channels: set[str]) -> dict:
        detail = {"name": name, "configured": name in all_channels, "active": name in active_channels}
        if name not in all_channels:
            detail["error"] = "Channel is not configured in Just In Mac Lite NDI"
            return detail
        try:
            detail["canRecord"] = bool(self.api.can_record(name).get("rec"))
        except JustControlApiError as exc:
            detail["error"] = str(exc)
            return detail
        warnings = []
        try:
            detail["recording"] = bool(self.api.recording_status(name).get("rec"))
        except JustControlApiError as exc:
            detail["recording"] = None
            warnings.append(f"Recording status unavailable: {exc}")
        try:
            detail["filename"] = self.api.current_filename(name).get("value", "")
        except JustControlApiError as exc:
            detail["filename"] = ""
            warnings.append(f"Current filename unavailable: {exc}")
        if warnings:
            detail["warnings"] = warnings
        return detail

    def _obs_status(self) -> dict:
        try:
            data = obs_request(self.obs_host, self.obs_port, self.obs_timeout, "GetRecordStatus", self.obs_password)
            return {"reachable": True, "recording": data.get("outputActive"), "outputPath": data.get("outputPath")}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    def status(self) -> dict:
        storage_report = self.storage_manager.report()
        payload = {
            "ok": False,
            "service": "fx30-just-control-v4-local",
            "recorderMode": self.recorder_mode,
            "localOnly": self.local_only,
            "api": {"baseUrl": getattr(self.api, "base_url", "unavailable")},
            "sourceRoot": str(self.source_root),
            "sourceMounted": self.source_root.exists(),
            "backupRoot": str(self.backup_root) if self.backup_root else None,
            "backupMounted": self.backup_root.parent.exists() if self.backup_root else False,
            "deliveryRoot": str(self.delivery_root),
            "deliveryMounted": self.delivery_root.parent.exists(),
            "proxyWorkingRoot": str(self.proxy_working_root),
            "proxyDeliveryRoot": str(self.proxy_delivery_root),
            "delivery": dict(getattr(self, "config", {}).get("delivery") or {
                "provider": "local-only" if self.local_only else "configured delivery",
                "justInMasters": str(self.delivery_root),
                "justInProxies": str(self.proxy_delivery_root),
            }),
            "postRecord": dict(self.post_status),
            "postRecordReadiness": self._post_readiness(),
            "requestedChannels": self.requested_names,
            "recording": self.recording,
            "lastResult": self.last_result,
            "storageReport": storage_report,
            "rodecasterCleanup": (self.storage_manager._state().get("rodecasterCleanup") or {"state": "idle"}),
            "postRecordSummary": self._post_record_summary(),
            "failedStopRecovery": self.failed_stop_recovery_status(),
            "noCameraRecord": self._no_camera_action_status(),
            "recovery": {
                "enabled": bool(getattr(self, "recovery_worker", None) and self.recovery_worker.enabled),
                "localTestOnly": bool(getattr(self, "recovery_worker", None) and self.recovery_worker.local_test_only),
                "session": dict(self.recovery_ledger.manifest) if getattr(self, "recovery_ledger", None) else None,
                "restartAttention": getattr(self, "recovery_restart_attention", None),
                "zowiePrerequisite": (getattr(self, "config", {}).get("recovery") or {}).get("zowiePrerequisite") or {},
            },
        }
        if self.recorder_mode == "obs":
            obs = self._obs_status()
            payload["obs"] = obs
            payload["apiReachable"] = True
            payload["activeChannels"] = []
            payload["allChannels"] = []
            payload["channels"] = []
            payload["ok"] = bool(obs.get("reachable")) and bool(payload["postRecordReadiness"].get("ok"))
            return payload
        try:
            all_channels = set(self.api.all_channels())
            active_channels = set(self.api.active_channels())
            payload["apiReachable"] = True
            payload["allChannels"] = sorted(all_channels)
            payload["activeChannels"] = sorted(active_channels)
            payload["missingRequestedChannels"] = [name for name in self.requested_names if name not in all_channels]
            payload["channels"] = [self._channel_details(name, all_channels, active_channels) for name in self.requested_names]
            payload["ok"] = not payload["missingRequestedChannels"] and all(
                item.get("canRecord") for item in payload["channels"]
            )
        except JustControlApiError as exc:
            payload["apiReachable"] = False
            payload["error"] = str(exc)
        return payload

    def prepare(self) -> dict:
        if getattr(self, "recovery_restart_attention", None):
            raise RuntimeError(self.recovery_restart_attention)
        current = self.status()
        if self.recorder_mode == "obs":
            if not (current.get("obs") or {}).get("reachable"):
                raise RuntimeError("OBS WebSocket is unavailable")
            return {"preparedRecorder": "obs"}
        storage = current.get("storageReport") or {}
        if not storage.get("ok"):
            raise RuntimeError(
                "Required storage is unavailable: " + ", ".join(storage.get("hardFailures") or ["unknown storage"])
            )
        readiness = current.get("postRecordReadiness", {})
        if self.auto_post_record and not readiness.get("ok"):
            failed = [name for name, ready in readiness.get("checks", {}).items() if not ready]
            raise RuntimeError(f"Post-record workflow is not ready: {', '.join(failed)}")
        missing = current.get("missingRequestedChannels", [])
        if missing and self.require_all:
            raise RuntimeError(f"Missing requested Just In Mac Lite NDI channels: {', '.join(missing)}")
        prepared = []
        for channel in self.channels:
            name = str(channel["name"])
            if name not in current.get("allChannels", []):
                continue
            if name not in current.get("activeChannels", []):
                self.api.start_channel(name)
            capture = channel.get("capturePreset")
            if capture:
                self.api.load_capture_preset(name, capture, channel.get("capturePresetId"))
            destination = channel.get("destinationPreset")
            if destination:
                self.api.load_destination_preset(name, destination, channel.get("destinationPresetId"))
            prepared.append(name)
        return {"preparedChannels": prepared}

    def storage_report(self, *, refresh_sony: bool = False) -> dict:
        return self.storage_manager.report(refresh_sony=refresh_sony)

    def confirm_rodecaster_capacity(self, remaining_minutes: float, source: str) -> dict:
        return {"ok": True, "action": "confirm-rodecaster-capacity", "capacity": self.storage_manager.confirm_rodecaster_capacity(remaining_minutes, source)}

    def override_rodecaster_capacity(self, phrase: str, reason: str) -> dict:
        return {"ok": True, "action": "override-rodecaster-capacity", "capacity": self.storage_manager.override_rodecaster_capacity(phrase, reason)}

    def recover_rodecaster_capacity(self) -> dict:
        return {"ok": True, "action": "recover-rodecaster-capacity", "capacity": self.storage_manager.recover_rodecaster_capacity()}

    def start_rodecaster_cleanup(self) -> dict:
        if self.recording:
            raise RuntimeError("RØDECaster SSD archive is unavailable while recording")
        if self.post_status.get("state") in ACTIVE_POST_STATES:
            raise RuntimeError("RØDECaster SSD archive is unavailable while local post-record work is active")
        if not self.storage_cleanup_lock.acquire(blocking=False):
            raise RuntimeError("RØDECaster storage cleanup is already running")
        self.storage_manager.state_file.parent.mkdir(parents=True, exist_ok=True)

        def worker() -> None:
            try:
                subprocess.run(
                    [str(item) for item in self.rodecaster_cleanup_command],
                    cwd="/Users/podcaststudio/Developer/StudioOS-Build-V4/fx30_bridge",
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=3600,
                )
            except Exception as exc:
                try:
                    state = self.storage_manager._state()
                    state["rodecasterCleanup"] = {
                        "state": "attention",
                        "updatedAt": datetime.now().isoformat(timespec="seconds"),
                        "error": str(exc),
                    }
                    temporary = self.storage_manager.state_file.with_name(f".{self.storage_manager.state_file.name}.partial")
                    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    temporary.replace(self.storage_manager.state_file)
                except OSError:
                    pass
            finally:
                self.storage_cleanup_lock.release()

        threading.Thread(target=worker, name="rodecaster-storage-clean", daemon=True).start()
        return {"ok": True, "action": "rodecaster-clean", "state": "checking"}

    def open_delivered_files(self, target: str = "all") -> dict:
        paths = {
            "masters": self.delivery_root,
            "proxies": self.proxy_delivery_root,
            "rodecaster": Path("/Volumes/Extreme SSD/TEST-RECS/AUDIO/RODECASTER"),
        }
        selected = list(paths.items()) if target == "all" else [(target, paths.get(target))]
        if not selected or selected[0][1] is None:
            raise ValueError("Unknown delivered-files target")
        opened = []
        missing = []
        for name, path in selected:
            if path and path.is_dir():
                subprocess.run(["/usr/bin/open", str(path)], check=False, timeout=10)
                opened.append({"target": name, "path": str(path)})
            else:
                missing.append({"target": name, "path": str(path) if path else None})
        if not opened:
            raise RuntimeError("No verified delivery folders are currently available")
        return {"ok": True, "action": "open-delivered-files", "opened": opened, "missing": missing}

    def _recording_started(self, name: str, attempts: int = 10, delay: float = 0.25) -> bool:
        for attempt in range(attempts):
            try:
                if self.api.recording_status(name).get("rec"):
                    return True
            except JustControlApiError:
                pass
            try:
                if self.api.recording_paths(name).get("paths"):
                    return True
            except JustControlApiError:
                pass
            if attempt + 1 < attempts:
                time.sleep(delay)
        return False

    def _recording_stopped(self, name: str, attempts: int = 10, delay: float = 0.25) -> Optional[bool]:
        """Return True/False when the live API provides evidence, otherwise None."""
        for attempt in range(attempts):
            evidence = []
            try:
                evidence.append(not bool(self.api.recording_status(name).get("rec")))
            except JustControlApiError:
                pass
            try:
                evidence.append(not bool(self.api.recording_paths(name).get("paths")))
            except JustControlApiError:
                pass
            if evidence and all(evidence):
                return True
            if attempt + 1 < attempts:
                time.sleep(delay)
        return False if evidence else None

    def start_recording(self, proposed_filename: str = "") -> dict:
        with self.lock:
            if self.recording:
                raise RuntimeError("A v4 local recording is already active")
            if self.post_status.get("state") in ACTIVE_POST_STATES:
                raise RuntimeError(
                    f"Previous take is still in post-record processing ({self.post_status.get('state')}). "
                    "Wait for /v4/status postRecord.state=complete before starting another recording."
                )
            self.prepare()
            if self.recorder_mode == "obs":
                obs_request(self.obs_host, self.obs_port, self.obs_timeout, "StartRecord", self.obs_password)
                if not obs_recording_status(self.obs_host, self.obs_port, self.obs_timeout, self.obs_password):
                    raise RuntimeError("OBS accepted StartRecord but recording could not be verified")
                self.recording = True
                self.session_started_at = time.time()
                self.session_files = []
                result = {"ok": True, "action": "start", "recorder": "obs", "startedAt": datetime.now().isoformat(timespec="seconds")}
                self.last_result = result
                return result
            self._begin_recovery_session()
            started: list[str] = []
            attempted: list[str] = []
            try:
                for name in self.requested_names:
                    attempted.append(name)
                    try:
                        self.api.start_recording(name, proposed_filename)
                    except JustControlApiError:
                        if not self._recording_started(name):
                            raise
                    else:
                        if not self._recording_started(name):
                            raise RuntimeError(
                                f"Just In Mac Lite NDI accepted the start request for {name}, "
                                "but no active recording could be verified"
                            )
                    started.append(name)
            except Exception:
                for name in attempted:
                    try:
                        self.api.stop_recording(name)
                    except Exception:
                        pass
                # A partial/failed start can still have consumed unknown device time.
                self.storage_manager.mark_rodecaster_take_attention(
                    "Recording start did not complete and take duration is unknown; reconfirm capacity"
                )
                if getattr(self, "recovery_ledger", None) is not None:
                    self.recovery_ledger.transition("attention", startFailureAt=utc_now())
                    self.recovery_ledger.emit("recording.start_failed", {"attemptedChannels": attempted})
                    self._persist_recovery_session()
                raise
            self.recording = True
            self.session_started_at = time.time()
            self.session_files = []
            if getattr(self, "recovery_ledger", None) is not None:
                self.recovery_ledger.transition("recording", verifiedStartAt=utc_now(), startedChannels=started)
                self.recovery_ledger.emit("recording.start_verified", {"channels": started})
                self._persist_recovery_session()
                self._start_liveness_monitor()
            # An explicit amber override is bounded to one successful start.
            self.storage_manager.consume_rodecaster_override()
            result = {"ok": True, "action": "start", "channels": started, "startedAt": datetime.now().isoformat(timespec="seconds")}
            self.last_result = result
            return result

    def stop_recording(self) -> dict:
        with self.lock:
            session_started_at = self.session_started_at
            was_recording = self.recording or session_started_at is not None
            if self.recorder_mode == "obs":
                if was_recording:
                    obs_request(self.obs_host, self.obs_port, self.obs_timeout, "StopRecord", self.obs_password)
                if obs_recording_status(self.obs_host, self.obs_port, self.obs_timeout, self.obs_password):
                    raise RuntimeError("OBS stop was accepted but recording is still active")
                stopped_at = time.time()
                self.recording = False
                self.session_started_at = None
                self.session_files = []
                result = {"ok": True, "action": "stop", "recorder": "obs", "alreadyStopped": not was_recording, "stoppedAt": datetime.now().isoformat(timespec="seconds")}
                if self.auto_post_record and was_recording:
                    result["postRecord"] = self._queue_post_record(session_started_at, stopped_at)
                self.last_result = result
                return result
            self._stop_liveness_monitor()
            stopped = []
            errors = {}
            session_files = list(getattr(self, "session_files", []))
            for name in self.requested_names:
                request_error = None
                try:
                    for item in self.api.recording_paths(name).get("paths") or []:
                        raw_path = item.get("path") if isinstance(item, dict) else item
                        if raw_path:
                            session_files.append(Path(str(raw_path)))
                except JustControlApiError:
                    pass
                try:
                    self.api.stop_recording(name)
                except JustControlApiError as exc:
                    request_error = exc
                stop_state = self._recording_stopped(name)
                if stop_state is True:
                    stopped.append(name)
                elif request_error is not None:
                    errors[name] = str(request_error)
                elif stop_state is None:
                    errors[name] = "stop request returned, but Just In did not provide terminal status"
                else:
                    errors[name] = "stop request returned but the channel still reports recording"
            if errors:
                self.storage_manager.mark_rodecaster_take_attention(
                    "Not every recorder stop was verified; take duration is unknown; reconfirm capacity"
                )
                if getattr(self, "recovery_ledger", None) is not None:
                    self.recovery_ledger.transition("stop_unknown", stopErrors=errors)
                    self.recovery_ledger.emit("recording.stop_unknown", {"errors": errors})
                    self._persist_recovery_session()
                failure = {
                    "action": "stop",
                    "failedAt": utc_now(),
                    "previousSession": {"recording": self.recording, "sessionStartedAt": session_started_at},
                    "stoppedChannels": stopped,
                    "channelErrors": errors,
                }
                self._persist_runtime_state("failedStop", failure)
                self.last_result = {"ok": False, **failure}
                raise RuntimeError(
                    "Failed to stop every requested channel: "
                    + "; ".join(f"{name}: {message}" for name, message in errors.items())
                )
            stopped_at = time.time()
            capacity = None
            if was_recording and session_started_at is not None:
                capacity = self.storage_manager.mark_rodecaster_take_complete(session_started_at, stopped_at)
            self.recording = False
            self.session_started_at = None
            self.session_files = []
            if getattr(self, "recovery_ledger", None) is not None:
                self.recovery_ledger.transition("settling", verifiedStopAt=utc_now(), stoppedChannels=stopped)
                self.recovery_ledger.emit("recording.stop_terminal", {"channels": stopped})
                self._persist_recovery_session()
            result = {
                "ok": True,
                "action": "stop",
                "channels": stopped,
                "alreadyStopped": not was_recording,
                "stoppedAt": datetime.now().isoformat(timespec="seconds"),
            }
            if capacity is not None:
                result["rodecasterCapacity"] = capacity
            if self.auto_post_record and stopped and was_recording:
                result["postRecord"] = self._queue_post_record(
                    session_started_at, stopped_at, session_files=session_files
                )
            self.last_result = result
            return result

    def _queue_post_record(
        self,
        session_started_at: float | None,
        stopped_at: float | None = None,
        *,
        session_files: list[Path] | None = None,
    ) -> dict:
        not_before = (session_started_at or (time.time() - 86400)) - 10
        files = list(dict.fromkeys(session_files or []))
        if not files:
            files = [item for item in movie_files(self.source_root, self.extensions) if item.stat().st_mtime >= not_before]
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        job = {
            "id": job_id,
            "files": files,
            "notBefore": not_before,
            "sessionStartedAt": session_started_at,
            "sessionStoppedAt": stopped_at,
            "recoveryPath": str(self.recovery_ledger.path) if getattr(self, "recovery_ledger", None) else None,
        }
        self.post_cancel_event.clear()
        self.post_status = {
            "state": "queued",
            "jobId": job_id,
            "filesFound": len(files),
            "queuedAt": datetime.now().isoformat(timespec="seconds"),
        }
        self.post_queue.put(job)
        self._persist_runtime_state("postRecordVerification", {
            "state": "running",
            "jobId": job_id,
            "startedAt": datetime.now().isoformat(timespec="seconds"),
            "mastersVerified": False,
            "proxiesVerified": False,
        })
        return dict(self.post_status)

    def _post_worker(self) -> None:
        while True:
            job = self.post_queue.get()
            try:
                self._process_post_job(job)
            except OperationCancelled as exc:
                self.post_status = {
                    "state": "cancelled",
                    "jobId": job["id"],
                    "error": str(exc),
                    "cancelledAt": datetime.now().isoformat(timespec="seconds"),
                }
                self.last_result = {"ok": False, "action": "post-record-cancel", **self.post_status}
                self._persist_runtime_state("postRecordVerification", {
                    "state": "cancelled",
                    "jobId": job["id"],
                    "error": str(exc),
                    "updatedAt": datetime.now().isoformat(timespec="seconds"),
                })
                notify_studio(
                    f"post-record-cancelled-{job['id']}",
                    "Post-record cancelled",
                    "Masters and proxies are not fully verified.",
                    enabled=getattr(self, "mac_notifications", True),
                )
                print(f"CANCELLED post-record job {job['id']}", flush=True)
            except Exception as exc:
                self.post_status = {
                    "state": "failed",
                    "jobId": job["id"],
                    "error": str(exc),
                    "failedAt": datetime.now().isoformat(timespec="seconds"),
                }
                print(f"ERROR post-record job {job['id']}: {exc}", flush=True)
                self._persist_runtime_state("postRecordVerification", {
                    "state": "failed",
                    "jobId": job["id"],
                    "error": str(exc),
                    "updatedAt": datetime.now().isoformat(timespec="seconds"),
                })
                notify_studio(
                    f"post-record-failed-{job['id']}",
                    "Post-record failed",
                    str(exc),
                    enabled=getattr(self, "mac_notifications", True),
                )
            finally:
                self.post_queue.task_done()

    def _post_progress(self, state: str, job_id: str, **initial) -> callable:
        self.post_status = {"state": state, "jobId": job_id, "cancellable": True, **initial}

        def update(progress: dict) -> None:
            if self.post_cancel_event.is_set():
                self.post_status = {
                    **self.post_status,
                    "state": "cancelling",
                    "previousState": state,
                    "cancellable": False,
                }
                return
            self.post_status = {
                "state": state,
                "jobId": job_id,
                "cancellable": True,
                **initial,
                **progress,
            }

        return update

    def cancel_post_record(self) -> dict:
        with self.lock:
            state = self.post_status.get("state")
            if state not in ACTIVE_POST_STATES:
                raise RuntimeError("There is no active post-record task to cancel")
            self.post_cancel_event.set()
            self.post_status = {
                **self.post_status,
                "state": "cancelling",
                "previousState": self.post_status.get("previousState", state),
                "cancellable": False,
                "cancelRequestedAt": datetime.now().isoformat(timespec="seconds"),
            }
            return {"ok": True, "action": "cancel-post-record", "postRecord": dict(self.post_status)}

    def acknowledge_post_record_attention(self) -> dict:
        """Dismiss a terminal historical receipt without touching any media."""
        with self.lock:
            if self.recording:
                raise RuntimeError("Cannot acknowledge post-record attention while recording")
            if self.post_status.get("state") in ACTIVE_POST_STATES:
                raise RuntimeError("Cannot acknowledge post-record attention while processing is active")
            persisted = self.storage_manager._state()
            verification = persisted.get("postRecordVerification") or {}
            previous_state = verification.get("state", "idle")
            if previous_state not in {"attention", "failed", "cancelled"}:
                raise RuntimeError("There is no terminal post-record attention to acknowledge")
            acknowledged_at = datetime.now().isoformat(timespec="seconds")
            self._persist_runtime_state("postRecordVerification", {
                "state": "acknowledged",
                "acknowledgedAt": acknowledged_at,
                "previousState": previous_state,
                "previousJobId": verification.get("jobId"),
            })
            self.post_status = {"state": "idle"}
            self.last_result = {
                "ok": True,
                "action": "acknowledge-post-record-attention",
                "acknowledgedAt": acknowledged_at,
                "previousState": previous_state,
            }
            return dict(self.last_result)

    def _failed_recovery_mapping(self, ledger: SessionLedger, expected_duration: float) -> tuple[dict, dict] | None:
        """Return one explicitly evidenced failed mapping, never infer one from a green STOP."""
        for mapping in ledger.manifest.get("mapping") or []:
            channel = mapping["justInChannel"]
            evidence = (ledger.manifest.get("liveness") or {}).get(channel) or {}
            if not evidence:
                return mapping, {
                    "channel": channel,
                    "expectedDurationSeconds": expected_duration,
                    "observedDurationSeconds": 0,
                    "liveness": {"error": "No liveness evidence was persisted for this mapped channel"},
                    "probe": None,
                }
            files = evidence.get("files") or []
            probe = None
            if files:
                probe = probe_media(Path(files[-1]["path"]), self.ffprobe_path)
            failure = (
                bool(evidence.get("suspectedFailed"))
                or not bool(files)
                or (bool(files) and not bool((probe or {}).get("ok")))
            )
            if not failure:
                continue
            return mapping, {
                "channel": channel,
                "expectedDurationSeconds": expected_duration,
                "observedDurationSeconds": float((probe or {}).get("durationSeconds") or 0),
                "liveness": evidence,
                "probe": probe,
            }
        return None

    def _run_recovery_after_post(self, job: dict) -> dict | None:
        recovery_path = job.get("recoveryPath")
        if not recovery_path or not getattr(self, "recovery_worker", None) or not self.recovery_worker.enabled:
            return None
        ledger = SessionLedger.load(Path(recovery_path))
        started_at = float(job.get("sessionStartedAt") or 0)
        stopped_at = float(job.get("sessionStoppedAt") or 0)
        expected_duration = max(0.0, stopped_at - started_at) if started_at and stopped_at else 0.0
        failed = self._failed_recovery_mapping(ledger, expected_duration)
        if failed is None:
            ledger.transition("complete", recovery={"state": "not-required"})
            ledger.emit("ndi.validation_passed", {"recovery": "not-required"})
            self.recovery_ledger = ledger
            self._persist_recovery_session()
            return {"state": "not-required"}
        mapping, failed_ndi = failed
        ledger.transition("recovery_required", failedNdi=failed_ndi)
        ledger.emit("ndi.validation_failed", {"channel": failed_ndi["channel"]})
        try:
            result = self.recovery_worker.recover(ledger, mapping, failed_ndi)
        except RecoveryAttention as exc:
            ledger.transition("attention", recovery={"state": "attention", "error": str(exc)})
            ledger.emit("recovery.attention", {"error": str(exc)})
            self.recovery_ledger = ledger
            self._persist_recovery_session()
            return {"state": "attention", "error": str(exc)}
        self.recovery_ledger = ledger
        self._persist_recovery_session()
        return result

    def retry_failed_post_record(self, requested_files: list[str] | None = None) -> dict:
        """Retry only the last verified take. This never sends recorder controls."""
        with self.lock:
            if self.recording:
                raise RuntimeError("Post-record retry is unavailable while recording")
            if self.post_status.get("state") in ACTIVE_POST_STATES:
                raise RuntimeError("Post-record work is already active")
            persisted = self.storage_manager._state()
            failed = persisted.get("postRecordVerification") or {}
            if failed.get("state") != "failed":
                raise RuntimeError("There is no failed post-record job to retry")
            take = (persisted.get("rodecasterCapacity") or {}).get("lastVerifiedTake") or {}
            try:
                started_at = datetime.fromisoformat(str(take["startedAt"])).timestamp()
                stopped_at = datetime.fromisoformat(str(take["stoppedAt"])).timestamp()
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("The failed take has no verified start/stop window") from exc

            candidates = []
            raw_candidates = [Path(item) for item in requested_files] if requested_files else movie_files(self.source_root, self.extensions)
            source_root = self.source_root.resolve()
            for item in raw_candidates:
                try:
                    item.resolve().relative_to(source_root)
                    stat = item.stat()
                except (OSError, ValueError):
                    continue
                if item.suffix.lower() not in self.extensions:
                    continue
                if started_at - 10 <= stat.st_mtime <= stopped_at + 10:
                    candidates.append(item)
            selected = []
            missing = []
            for channel in self.requested_names:
                matches = [item for item in candidates if channel.casefold() in item.name.casefold()]
                if not matches:
                    missing.append(channel)
                    continue
                selected.append(max(matches, key=lambda item: item.stat().st_mtime))
            if missing:
                raise RuntimeError(
                    "Post-record retry is blocked because the failed take is missing: " + ", ".join(missing)
                )

            original_id = str(failed.get("jobId") or datetime.now().strftime("%Y%m%d-%H%M%S"))
            job_id = f"{original_id}-retry"
            job = {
                "id": job_id,
                "files": selected,
                "notBefore": started_at - 10,
                "sessionStartedAt": started_at,
                "sessionStoppedAt": stopped_at,
                "recoveryPath": None,
            }
            self.post_cancel_event.clear()
            self.post_status = {
                "state": "queued",
                "jobId": job_id,
                "filesFound": len(selected),
                "retriedFrom": original_id,
                "queuedAt": datetime.now().isoformat(timespec="seconds"),
            }
            self.post_queue.put(job)
            self._persist_runtime_state("postRecordVerification", {
                "state": "running",
                "jobId": job_id,
                "retriedFrom": original_id,
                "startedAt": datetime.now().isoformat(timespec="seconds"),
                "mastersVerified": False,
                "proxiesVerified": False,
            })
            return {"ok": True, "action": "retry-post-record", **self.post_status}

    def _process_post_job(self, job: dict) -> dict:
        files = list(job["files"])
        progress = self._post_progress("waiting_for_files", job["id"], filesFound=len(files))
        if files:
            try:
                files = stable_paths(
                    files,
                    stable_seconds=self.stable_seconds,
                    settle_timeout_seconds=self.settle_timeout_seconds,
                    cancel_event=self.post_cancel_event,
                )
            except PermissionError as exc:
                raise RuntimeError(
                    "V4 cannot read the recording files. Give the V4 Python service Full Disk Access in macOS Privacy & Security, then retry this post-record job."
                ) from exc
        else:
            # Just In can make the final .mov visible only after its API has
            # already reported stopped. Wait for that late-appearing final
            # file instead of treating the first empty snapshot as a take
            # with no media.
            files = stable_files(
                self.source_root,
                self.extensions,
                not_before=job.get("notBefore"),
                stable_seconds=self.stable_seconds,
                settle_timeout_seconds=self.settle_timeout_seconds,
            )
        if not files:
            raise RuntimeError(f"No stable recording files found for post-record job {job['id']}")

        result = {"jobId": job["id"], "sourceRoot": str(self.source_root)}
        if self.backup_root:
            progress = self._post_progress("backing_up", job["id"], files=len(files))
            result["backup"] = copy_verified(
                files,
                self.source_root,
                self.backup_root,
                cancel_event=self.post_cancel_event,
                progress=progress,
            )

        progress = self._post_progress("copying_masters", job["id"], files=len(files))
        if self.local_only:
            result["masters"] = [
                {
                    "source": str(item),
                    "destination": str(item),
                    "bytes": item.stat().st_size,
                    "skipped": True,
                    "verified": True,
                    "verification": "stable-local-source",
                }
                for item in files
            ]
            progress({"completedFiles": len(files), "totalFiles": len(files)})
        else:
            result["masters"] = copy_verified(
                files,
                self.source_root,
                self.delivery_root,
                cancel_event=self.post_cancel_event,
                progress=progress,
            )

        video_files = [item for item in files if item.suffix.lower() in self.proxy_extensions]
        proxy_started_at = datetime.now().isoformat(timespec="seconds")
        proxy_started_monotonic = time.monotonic()
        progress = self._post_progress(
            "creating_proxies",
            job["id"],
            files=len(video_files),
            proxyStartedAt=proxy_started_at,
        )
        result["proxiesCreated"] = create_proxies(
            video_files,
            self.source_root,
            self.proxy_working_root,
            ffmpeg_path=self.ffmpeg_path,
            ffprobe_path=self.ffprobe_path,
            height=self.proxy_height,
            video_bitrate=self.proxy_video_bitrate,
            audio_bitrate=self.proxy_audio_bitrate,
            cancel_event=self.post_cancel_event,
            progress=progress,
        )
        result["proxyTiming"] = {
            "startedAt": proxy_started_at,
            "completedAt": datetime.now().isoformat(timespec="seconds"),
            "elapsedSeconds": round(time.monotonic() - proxy_started_monotonic, 1),
            "sourceCount": len(video_files),
        }
        proxy_files = [Path(receipt["proxy"]) for receipt in result["proxiesCreated"]]
        progress = self._post_progress("copying_proxies", job["id"], files=len(proxy_files))
        if self.local_only:
            result["proxiesDelivered"] = [
                {
                    "source": item.get("source"),
                    "destination": item.get("proxy"),
                    "bytes": item.get("bytes"),
                    "skipped": item.get("skipped", False),
                    "verified": item.get("verified", False),
                    "verification": "ffprobe-local",
                }
                for item in result["proxiesCreated"]
            ]
            progress({"completedFiles": len(proxy_files), "totalFiles": len(proxy_files)})
        else:
            result["proxiesDelivered"] = copy_verified(
                proxy_files,
                self.proxy_working_root,
                self.proxy_delivery_root,
                cancel_event=self.post_cancel_event,
                progress=progress,
            )
        result["recovery"] = self._run_recovery_after_post(job)
        result["ok"] = True
        result["completedAt"] = datetime.now().isoformat(timespec="seconds")
        masters_verified = bool(result.get("masters")) and all(item.get("verified") for item in result["masters"])
        proxies_verified = bool(result.get("proxiesDelivered")) and all(item.get("verified") for item in result["proxiesDelivered"])
        recovery_attention = bool((result.get("recovery") or {}).get("state") == "attention")
        terminal_state = "complete" if masters_verified and proxies_verified and not recovery_attention else "attention"
        self._persist_runtime_state("postRecordVerification", {
            "state": terminal_state,
            "jobId": job["id"],
            "completedAt": result["completedAt"],
            "mastersVerified": masters_verified,
            "proxiesVerified": proxies_verified,
            "masterCount": len(result.get("masters") or []),
            "proxyCount": len(result.get("proxiesDelivered") or []),
            "proxyTiming": result.get("proxyTiming"),
            "masterPaths": [item.get("destination") for item in result.get("masters") or []],
            "proxyPaths": [item.get("destination") for item in result.get("proxiesDelivered") or []],
        })
        self.post_status = {**result, "state": terminal_state}
        self.last_result = {**result, "ok": terminal_state == "complete", "action": "post-record", "state": terminal_state}
        if terminal_state == "attention":
            notify_studio(
                f"post-record-attention-{job['id']}",
                "Post-record needs attention",
                "Master or proxy verification did not pass.",
                enabled=getattr(self, "mac_notifications", True),
            )
        elif getattr(self, "notify_post_success", False):
            notify_studio(
                f"post-record-complete-{job['id']}",
                "Post-record complete",
                f"{len(result.get('masters') or [])} masters finalised and {len(result.get('proxiesDelivered') or [])} playable proxies verified on Extreme SSD.",
                enabled=getattr(self, "mac_notifications", True),
            )
        print(json.dumps(self.last_result, sort_keys=True), flush=True)
        return result

    def ingest(self) -> dict:
        if not self.source_root.is_dir():
            raise RuntimeError(f"Source recording directory is not mounted: {self.source_root}")
        not_before = (self.session_started_at or (time.time() - 86400)) - 10
        files = stable_files(
            self.source_root,
            self.extensions,
            not_before=not_before,
            stable_seconds=self.stable_seconds,
            settle_timeout_seconds=self.settle_timeout_seconds,
        )
        if not files:
            raise RuntimeError(f"No stable Just In Mac Lite NDI movie files found under {self.source_root}")
        job = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-manual"), "files": files}
        processed = self._process_post_job(job)
        result = {"ok": True, "action": "ingest", **processed}
        self.last_result = result
        return result


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._file(Path(__file__).with_name("dashboard.html"), "text/html; charset=utf-8")
            return
        if path == "/dashboard.js":
            self._file(Path(__file__).with_name("dashboard.js"), "text/javascript; charset=utf-8")
            return
        if path == "/v4/status":
            self._json(200, self.bridge.status())
            return
        if path == "/v4/storage":
            self._json(200, self.bridge.storage_report())
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        print(f"{datetime.now().isoformat(timespec='seconds')} POST {self.path}", flush=True)
        try:
            body = self._body()
            if self.path == "/v4/prepare":
                result = self.bridge.prepare()
            elif self.path == "/v4/capacity/confirm":
                result = self.bridge.confirm_rodecaster_capacity(float(body.get("remainingMinutes")), str(body.get("source", "")))
            elif self.path == "/v4/capacity/override":
                result = self.bridge.override_rodecaster_capacity(str(body.get("phrase", "")), str(body.get("reason", "")))
            elif self.path == "/v4/capacity/recover":
                result = self.bridge.recover_rodecaster_capacity()
            elif self.path == "/v4/storage/refresh":
                result = self.bridge.storage_report(refresh_sony=True)
            elif self.path == "/v4/storage/rodecaster-archive":
                result = self.bridge.start_rodecaster_cleanup()
            elif self.path == "/v4/files/open":
                result = self.bridge.open_delivered_files(str(body.get("target", "all")))
            elif self.path == "/v4/record/start":
                result = self.bridge.start_recording(str(body.get("proposedFilename", "")))
            elif self.path == "/v4/record/stop":
                result = self.bridge.stop_recording()
            elif self.path == "/v4/record/no-camera/start":
                result = self.bridge.start_no_camera_recording()
            elif self.path == "/v4/record/no-camera/stop":
                result = self.bridge.stop_no_camera_recording()
            elif self.path == "/v4/record/recover-failed-stop":
                result = self.bridge.recover_failed_stop(
                    body.get("rodecasterConfirmedOff") is True,
                    str(body.get("rodecasterConfirmationSource", "")),
                )
            elif self.path == "/v4/post-record/cancel":
                result = self.bridge.cancel_post_record()
            elif self.path == "/v4/post-record/acknowledge":
                result = self.bridge.acknowledge_post_record_attention()
            elif self.path == "/v4/post-record/retry":
                files = body.get("files")
                if files is not None and not isinstance(files, list):
                    raise ValueError("Post-record retry files must be a list")
                result = self.bridge.retry_failed_post_record(files)
            elif self.path in ("/v4/ingest", "/v4/copy"):
                result = self.bridge.ingest()
            else:
                self._json(404, {"ok": False, "error": "Not found"})
                return
            print(json.dumps(result, sort_keys=True), flush=True)
            self._json(200, result)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            notify_request_failure(self.path, exc, enabled=getattr(self.bridge, "mac_notifications", True))
            print(f"ERROR {self.path}: {exc}", flush=True)
            self._json(409, {"ok": False, "error": str(exc)})
        except JustControlApiError as exc:
            notify_request_failure(self.path, exc, enabled=getattr(self.bridge, "mac_notifications", True))
            print(f"ERROR {self.path}: {exc}; status={exc.status}; body={exc.body}", flush=True)
            self._json(502, {"ok": False, "error": str(exc), "status": exc.status, "body": exc.body})


def main() -> None:
    parser = argparse.ArgumentParser(description="FX30 Just In Mac Lite NDI v4 local-only bridge")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--port", type=int, help="Override the configured bridge port")
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args()
    config = load_config(Path(args.config))
    bridge_config = config.get("bridge", {})
    port = args.port if args.port is not None else int(bridge_config.get("port", 3060))
    server = ThreadingHTTPServer((bridge_config.get("host", "127.0.0.1"), port), Handler)
    Handler.bridge = Bridge(config)
    dashboard_url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"Just In Mac Lite NDI v4 local-only listening on {dashboard_url}", flush=True)
    if args.open_dashboard:
        threading.Timer(0.4, webbrowser.open, args=(dashboard_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
