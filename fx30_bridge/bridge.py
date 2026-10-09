#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import os
import pty
import queue
import re
import shutil
import smtplib
import ssl
import subprocess
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError


STATE = {
    "recording": False,
    "recording_started_at": None,
    "last_action": None,
    "record_action_running": False,
    "record_action": None,
    "last_record_action_started": None,
    "last_record_action_finished": None,
    "last_stop_time": None,
    "auto_ingest_scheduled_at": None,
    "auto_ingest_stop_time": None,
    "last_results": [],
    "ingest_running": False,
    "last_ingest_started": None,
    "last_ingest_finished": None,
    "last_ingest_results": [],
    "last_ingest_ok": None,
    "last_ingest_file": None,
    "last_ingest_message": None,
    "last_delivery_results": [],
    "last_master_file": None,
    "last_master_camera_id": None,
    "last_master_message": None,
    "last_master_ok": None,
    "last_email_result": None,
    "last_email_ok": None,
    "last_mac_notification": None,
    "ingest_progress": None,
    # Last read-only Sony SDK card report.  This deliberately remains cached
    # while recording/copying so a status refresh cannot disrupt the camera.
    "card_status": {},
}
STATE_LOCK = Lock()
CONFIG_LOCK = Lock()
CAMERA_CONTROLLERS = {}
CAMERA_CONTROLLERS_LOCK = Lock()
WARM_BATCH_LOCK = Lock()


def format_recording_time(seconds):
    """Return the same HH:MM:SS-style counter used by the dashboard."""
    if not isinstance(seconds, (int, float)):
        return "00:00:00"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def existing_storage_path(folder):
    """Find the nearest existing path so disk_usage still works before a subfolder exists."""
    if not folder:
        return None
    path = Path(folder).expanduser()
    # Never fall back from an unmounted /Volumes/<name> path to /Volumes or /
    # because that would report the Mac's root disk instead of the recording SSD.
    if len(path.parts) >= 3 and path.parts[1] == "Volumes":
        volume_root = Path("/Volumes") / path.parts[2]
        if not volume_root.exists():
            return None
    while path != path.parent and not path.exists():
        path = path.parent
    return path if path.exists() else None


def storage_snapshot(config):
    """Return free space for the configured local recording volumes.

    Values labelled GB use decimal gigabytes (1 GB = 1,000,000,000 bytes).
    The binary GiB value is also returned to remove ambiguity.
    """
    cameras = []
    volumes = {}
    for camera in enabled_cameras(config):
        requested_path = camera.get("ingestFolder") or ""
        volume_path = existing_storage_path(requested_path)
        item = {
            "id": camera.get("id"),
            "requestedPath": requested_path,
            "volumePath": str(volume_path) if volume_path else None,
            "ok": False,
            "error": None,
        }
        if volume_path:
            try:
                usage = shutil.disk_usage(volume_path)
                free_gb = usage.free / 1_000_000_000
                free_gib = usage.free / (1024 ** 3)
                item.update({
                    "ok": True,
                    "totalBytes": usage.total,
                    "usedBytes": usage.used,
                    "freeBytes": usage.free,
                    "totalGB": round(usage.total / 1_000_000_000, 1),
                    "usedGB": round(usage.used / 1_000_000_000, 1),
                    "freeGB": round(free_gb, 1),
                    "freeGiB": round(free_gib, 1),
                    "freeGBText": f"{free_gb:.1f} GB free",
                })
                volumes[str(volume_path)] = item
            except OSError as exc:
                item["error"] = str(exc)
        else:
            item["error"] = "Recording volume is not mounted"
        cameras.append(item)

    first_ok = next((item for item in cameras if item.get("ok")), None)
    return {
        "ok": bool(first_ok),
        "freeBytes": first_ok.get("freeBytes") if first_ok else None,
        "freeGB": first_ok.get("freeGB") if first_ok else None,
        "freeGiB": first_ok.get("freeGiB") if first_ok else None,
        "freeGBText": first_ok.get("freeGBText") if first_ok else "Storage unavailable",
        "volumePath": first_ok.get("volumePath") if first_ok else None,
        "cameras": cameras,
        "volumes": list(volumes.values()),
    }


V4_STATUS_URL = "http://127.0.0.1:3060/v4/status"
V4_RODECASTER_ARCHIVE_URL = "http://127.0.0.1:3060/v4/storage/rodecaster-archive"
V4_EMAIL_STATE_PATH = Path.home() / "Library/Application Support/StudioOS/State/v4-email-state.json"


def rodecaster_archive_readiness():
    """Read the V4-owned RØDECaster archive state without triggering it.

    The archive changes the RØDECaster USB mode, so it is deliberately not a
    STOP side effect. It is available only after V4 has finished a successful
    post-record job and OBS is no longer running.
    """
    try:
        with urlopen(Request(V4_STATUS_URL, method="GET"), timeout=2) as response:
            if response.status != 200:
                raise RuntimeError(f"V4 returned HTTP {response.status}")
            status = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "ready": False,
            "state": "unavailable",
            "message": f"V4 archive status is unavailable: {exc}",
        }

    cleanup = status.get("rodecasterCleanup") or {"state": "idle"}
    cleanup_state = cleanup.get("state", "idle")
    if status.get("recording"):
        return {
            "ready": False,
            "state": "blocked-recording",
            "message": "RØDECaster archive is unavailable while V4 reports recording.",
            "cleanup": cleanup,
        }

    summary = status.get("postRecordSummary") or {}
    if summary.get("state") != "complete" or not summary.get("allChecksPassed"):
        return {
            "ready": False,
            "state": "waiting-post-record",
            "message": "Wait for V4 post-record checks to complete successfully before archiving RØDECaster audio.",
            "cleanup": cleanup,
            "postRecord": summary,
        }

    if cleanup_state in {"checking", "copying", "verifying", "cleaning"}:
        return {
            "ready": False,
            "state": cleanup_state,
            "message": "RØDECaster archive is already running. Check the archive status below.",
            "cleanup": cleanup,
            "postRecord": summary,
        }

    obs_check = subprocess.run(
        ["/usr/bin/pgrep", "-q", "-x", "obs"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if obs_check.returncode == 0:
        return {
            "ready": False,
            "state": "blocked-obs",
            "message": "Quit OBS before archiving: RØDECaster transfer changes its USB mode.",
            "cleanup": cleanup,
            "postRecord": summary,
        }

    return {
        "ready": True,
        "state": cleanup_state,
        "message": "Ready to archive RØDECaster audio. Source recordings stay on the device.",
        "cleanup": cleanup,
        "postRecord": summary,
    }


def enabled_cameras(config):
    return [
        camera
        for camera in config.get("cameras", [])
        if camera.get("enabled", True)
    ]


def parse_card_status_output(output):
    """Extract the single structured line emitted by sony-rec --action card-status."""
    for line in reversed((output or "").splitlines()):
        if line.startswith("SONY_REC_CARD_STATUS "):
            try:
                return json.loads(line[len("SONY_REC_CARD_STATUS "):])
            except json.JSONDecodeError:
                return {"ok": False, "error": "Camera returned unreadable card status"}
    return {"ok": False, "error": "Camera did not return card status"}


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def json_response(handler, status, payload):
    data = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass


def html_response(handler, status, html):
    data = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# Secure Remote Access and Auditing Helpers
AUDIT_LOG_FILE = Path(__file__).resolve().parent / "audit.log"

def is_tailscale_ip(ip):
    if ip.startswith("fd7a:115c:a1e0:"):
        return True
    try:
        parts = ip.split('.')
        if len(parts) == 4 and parts[0] == '100':
            second = int(parts[1])
            return 64 <= second <= 127
    except Exception:
        pass
    return False

def is_allowed_ip(ip):
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("169.254."):
        return True
    if ip.startswith("172."):
        try:
            parts = ip.split('.')
            if len(parts) == 4:
                second = int(parts[1])
                return 16 <= second <= 31
        except Exception:
            pass
    if ip.startswith("fe80:") or ip.startswith("fc") or ip.startswith("fd"):
        return True
    return is_tailscale_ip(ip)

def get_visitor_type(ip):
    if ip in ("127.0.0.1", "::1"):
        return "Localhost"
    if is_tailscale_ip(ip):
        return "Remote (Tailscale)"
    if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("169.254."):
        return "Local Network"
    if ip.startswith("172."):
        try:
            parts = ip.split('.')
            if len(parts) == 4 and 16 <= int(parts[1]) <= 31:
                return "Local Network"
        except Exception:
            pass
    if ip.startswith("fe80:") or ip.startswith("fc") or ip.startswith("fd"):
        return "Local Network"
    return "Unknown"

def log_audit(ip, action, path, details=""):
    try:
        import datetime
        timestamp = datetime.datetime.now().isoformat()
        visitor_type = get_visitor_type(ip)
        log_entry = {
            "timestamp": timestamp,
            "ip": ip,
            "visitor_type": visitor_type,
            "action": action,
            "path": path,
            "details": details
        }
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        print(f"Error writing audit log: {e}")



DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FX30 Studio</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f5f7;
      --panel: rgba(255, 255, 255, .78);
      --panel-2: #f2f2f7;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --line: rgba(60, 60, 67, .18);
      --green: #248a3d;
      --red: #d70015;
      --amber: #b25000;
      --blue: #0066cc;
      --control: #ffffff;
      --control-hover: #f7f7f8;
      --shadow: 0 1px 2px rgba(0, 0, 0, .04);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.35;
      -webkit-font-smoothing: antialiased;
    }
    main {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 40px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: 30px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .updated {
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }
    .headerActions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }
    .dashboardLink {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      padding: 6px 12px;
      border: 1px solid rgba(0, 102, 204, .2);
      border-radius: 7px;
      background: rgba(0, 102, 204, .1);
      color: var(--blue);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
    }
    .dashboardLink:hover { filter: brightness(1.08); }
    .grid {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(280px, 1fr);
      gap: 18px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 20px;
      box-shadow: var(--shadow);
    }
    .statusPanel {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: minmax(240px, 1fr) minmax(300px, 1.2fr);
      gap: 20px;
      align-items: stretch;
    }
    .recordState {
      display: flex;
      flex-direction: column;
      justify-content: center;
      min-height: 190px;
      border-radius: 10px;
      background: var(--panel-2);
      padding: 20px;
      border: 1px solid var(--line);
    }
    .stateLabel {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 9px;
    }
    .recordLight {
      width: 12px;
      height: 12px;
      flex: 0 0 12px;
      border-radius: 50%;
      background: rgba(215, 0, 21, .2);
      box-shadow: 0 0 0 1px rgba(215, 0, 21, .25);
    }
    .recordLight.active {
      background: var(--red);
      box-shadow: 0 0 12px rgba(255, 77, 77, .8);
      animation: recordPulse 1s steps(2, end) infinite;
    }
    @keyframes recordPulse {
      50% { opacity: .25; }
    }
    @media (prefers-reduced-motion: reduce) {
      .recordLight.active { animation: none; }
    }
    .stateValue {
      font-size: clamp(42px, 6vw, 58px);
      line-height: 1;
      font-weight: 750;
      letter-spacing: 0;
    }
    .stateValue.recording { color: var(--red); }
    .stateValue.ready { color: var(--green); }
    .stateValue.busy { color: var(--amber); }
    .recordTimer {
      min-height: 30px;
      margin-top: 7px;
      color: var(--muted);
      font-size: 22px;
      font-variant-numeric: tabular-nums;
      font-weight: 700;
    }
    .recordTimer.active { color: var(--red); }
    .controls {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-top: 16px;
    }
    button {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 7px;
      min-height: 46px;
      padding: 8px 10px;
      color: var(--text);
      background: var(--control);
      font: inherit;
      font-size: 13px;
      font-weight: 600;
      line-height: 1.2;
      text-align: center;
      white-space: normal;
      overflow-wrap: anywhere;
      cursor: pointer;
    }
    button:hover { background: var(--control-hover); }
    button:focus-visible {
      outline: 3px solid rgba(0, 102, 204, .22);
      outline-offset: 2px;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: .48;
    }
    .start { background: var(--green); border-color: var(--green); color: white; }
    .stop { background: var(--red); border-color: var(--red); color: white; }
    .copy { background: var(--blue); border-color: var(--blue); color: white; }
    .start:hover, .stop:hover, .copy:hover { filter: brightness(.94); }
    .emergencyMaster {
      font-size: 13px;
      letter-spacing: .01em;
      padding-inline: 8px;
    }
    .emergencyMaster span {
      display: block;
      white-space: nowrap;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(130px, 1fr));
      gap: 10px;
    }
    .metric {
      min-height: 76px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      padding: 12px 14px;
    }
    .metric b {
      display: block;
      font-size: 17px;
      margin-top: 5px;
      overflow-wrap: anywhere;
    }
    .metric span, .sectionLabel {
      color: var(--muted);
      font-size: 12px;
    }
    .progressBox {
      grid-column: 1 / -1;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      padding: 12px 14px;
    }
    .progressHeader {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .progressTrack {
      height: 8px;
      border-radius: 999px;
      background: rgba(60, 60, 67, .12);
      border: 1px solid var(--line);
      overflow: hidden;
    }
    .progressFill {
      height: 100%;
      width: 0%;
      background: var(--blue);
      transition: width .25s ease;
    }
    .progressMeta {
      display: grid;
      grid-template-columns: repeat(3, minmax(80px, 1fr));
      gap: 10px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    h2 {
      margin: 0 0 14px;
      font-size: 17px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .row {
      display: grid;
      grid-template-columns: minmax(90px, .5fr) minmax(120px, 1fr) minmax(70px, .4fr);
      gap: 12px;
      padding: 11px 0;
      border-top: 1px solid var(--line);
      align-items: center;
    }
    .row:first-of-type { border-top: 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: rgba(60, 60, 67, .1);
      color: var(--muted);
      width: fit-content;
    }
    .pill.ok { color: var(--green); background: rgba(36, 138, 61, .13); }
    .pill.bad { color: var(--red); background: rgba(215, 0, 21, .12); }
    .pill.busy { color: var(--amber); background: rgba(178, 80, 0, .12); }
        .studioDialogBackdrop {
            position: fixed;
            inset: 0;
            z-index: 1000;
            display: grid;
            place-items: center;
            padding: 20px;
            background: rgba(20, 24, 28, .54);
            backdrop-filter: blur(3px);
        }
        .studioDialogBackdrop[hidden] { display: none; }
        .studioDialog {
            width: min(500px, 100%);
            border: 1px solid var(--line);
            border-radius: 10px;
            background: var(--panel);
            box-shadow: 0 20px 60px rgba(0, 0, 0, .25);
            padding: 22px;
        }
        .studioDialog h2 { margin-bottom: 8px; }
        .studioDialogMessage { margin: 0; color: var(--muted); white-space: pre-line; }
        .studioDialogInput {
            width: 100%;
            min-height: 40px;
            margin-top: 16px;
            border: 1px solid var(--line);
            border-radius: 7px;
            padding: 8px 10px;
            background: var(--control);
            color: var(--text);
            font: inherit;
        }
        .studioDialogActions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            margin-top: 20px;
        }
        .studioDialogActions button { min-width: 96px; }
        .studioDialogConfirm.destructive { border-color: var(--red); background: var(--red); color: white; }
    .muted { color: var(--muted); }
    .files {
      display: grid;
      gap: 10px;
    }
    .file {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding: 12px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
    }
    .fileName {
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .emailFields { display: grid; gap: 10px; }
    .emailPanel { grid-column: 1 / -1; }
    .emailPanel .emailFields,
    .emailPanel .emailActions,
    .emailPanel .savedMessage { max-width: 560px; }
    .emailField {
      display: grid;
      grid-template-columns: 1fr 44px;
      gap: 8px;
    }
    input[type="email"] {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      background: var(--control);
      color: var(--text);
      font: inherit;
    }
    .emailActions {
      display: flex;
      gap: 10px;
      margin-top: 12px;
    }
    .emailActions button, .removeEmail {
      min-height: 36px;
      padding: 8px 14px;
    }
    .removeEmail { font-size: 22px; }
    .saveEmail { background: var(--green); border-color: var(--green); color: white; }
    .savedMessage { margin-top: 10px; color: var(--green); min-height: 20px; }
     .error {
      display: none;
      margin: 0 0 16px;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid rgba(215, 0, 21, .25);
      background: rgba(215, 0, 21, .08);
      color: var(--red);
    }
    .cleanupRow {
      display: grid;
      grid-template-columns: 160px 220px 1fr auto;
      gap: 16px;
      padding: 12px 6px;
      border-top: 1px solid var(--line);
      align-items: center;
    }
    .cleanupRow:first-of-type { border-top: 0; }
    .cleanupRow:hover { background: rgba(60, 60, 67, .035); }
    .cleanupHeaderRow {
      display: grid;
      grid-template-columns: 160px 220px 1fr auto;
      gap: 16px;
      padding: 8px 6px;
      font-weight: 700;
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .cleanupCameras {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .cleanupCameraRow {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 4px 0;
      border-bottom: 1px dashed rgba(60, 60, 67, .12);
    }
    .cleanupCameraRow:last-child {
      border-bottom: 0;
    }
    #remoteBanner {
      background: rgba(0, 102, 204, .08) !important;
      border-color: rgba(0, 102, 204, .25) !important;
      border-radius: 10px !important;
    }
    #remoteBanner strong { color: var(--blue) !important; }
    #remoteBanner label {
      background: rgba(255, 255, 255, .75) !important;
      border-color: rgba(0, 102, 204, .25) !important;
    }
    #refreshCards, #refreshCleanup {
      min-height: 32px !important;
      padding: 6px 12px !important;
      background: rgba(0, 102, 204, .1) !important;
      border-color: rgba(0, 102, 204, .2) !important;
      color: var(--blue);
    }
    @media (max-width: 920px) {
      .statusPanel { grid-template-columns: 1fr; }
    }
    @media (max-width: 760px) {
      main { width: min(100vw - 20px, 1180px); padding-top: 18px; }
      header, .statusPanel { grid-template-columns: 1fr; display: grid; }
      .grid { grid-template-columns: 1fr; }
      .controls { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; gap: 5px; }
      .cleanupRow, .cleanupHeaderRow { grid-template-columns: 1fr; gap: 10px; }
      .updated { text-align: left; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>FX30 Studio</h1>
      <div class="headerActions">
        <button type="button" class="dashboardLink" id="archiveRodecaster" disabled>ARCHIVE RØDE</button>
        <div class="updated" id="rodecasterArchiveStatus">Checking RØDECaster archive…</div>
        <div class="updated" id="updated">Waiting for bridge...</div>
      </div>
    </header>

    <div class="error" id="error"></div>

    <div id="remoteBanner" style="display:none; background:#1b2a47; border: 1px solid #3d68ff; border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; align-items: center; justify-content: space-between; gap: 16px;">
      <div style="display:flex; align-items:center; gap:12px;">
        <span style="font-size: 20px;">⚠️</span>
        <div>
          <strong style="color:#67b7ff;">Remote Support Session (Tailscale)</strong>
          <div style="font-size:12px; color:var(--muted); margin-top:2px;" id="remoteBannerIp">Connecting from...</div>
        </div>
      </div>
      <div style="display:flex; align-items:center;">
        <label style="display:inline-flex; align-items:center; gap:8px; cursor:pointer; font-weight:700; background:#2a3a5c; padding:6px 12px; border-radius:6px; border:1px solid #3d68ff;">
          <input type="checkbox" id="remoteControlEnable" style="width:16px; height:16px; cursor:pointer;">
          Enable Controls
        </label>
      </div>
    </div>

    <section class="panel statusPanel">
      <div>
        <div class="recordState">
          <div class="stateLabel"><span class="recordLight" id="recordLight"></span>Camera state</div>
          <div class="stateValue ready" id="stateValue">READY</div>
          <div class="recordTimer" id="recordTimer">00:00:00</div>
          <div class="controls">
            <button class="start" data-action="start">REC START</button>
            <button class="stop" data-action="stop">REC STOP</button>
            <button data-action="ftp-test">TEST FTP</button>
            <button class="copy" data-action="ingest">COPY CAMERA TO SSD</button>
            <button class="copy" data-action="deliver">COPY SSD TO LUCIDLINK</button>
            <button class="stop emergencyMaster" data-action="recover-master" data-camera="fx30-a"><span>EMERGENCY</span><span>CAMERA 1</span><span>4K MASTER</span></button>
            <button class="stop emergencyMaster" data-action="recover-master" data-camera="fx30-b"><span>EMERGENCY</span><span>CAMERA 2</span><span>4K MASTER</span></button>
            <button class="stop emergencyMaster" data-action="recover-master" data-camera="fx30-c"><span>EMERGENCY</span><span>CAMERA 3</span><span>4K MASTER</span></button>
          </div>
        </div>
      </div>
      <div class="metrics">
        <div class="metric"><span>Last action</span><b id="lastAction">-</b></div>
        <div class="metric"><span>Action time</span><b id="elapsed">-</b></div>
        <div class="metric"><span>Warm link</span><b id="warm">-</b></div>
        <div class="metric"><span>Ingest</span><b id="ingest">-</b></div>
        <div class="metric"><span>Email</span><b id="email">-</b></div>
        <div class="metric"><span>Mac alert</span><b id="macAlert">-</b></div>
        <div class="progressBox">
          <div class="progressHeader">
            <div>
              <div class="sectionLabel">Copy progress</div>
              <strong id="copyFile">No copy running</strong>
            </div>
            <strong id="copyPercent">-</strong>
          </div>
          <div class="progressTrack"><div class="progressFill" id="copyFill"></div></div>
          <div class="progressMeta">
            <div id="copySize">Size -</div>
            <div id="copyCopied">Copied -</div>
            <div id="copyEta">ETA -</div>
          </div>
        </div>
      </div>
    </section>

    <div class="grid" style="margin-top:16px">
      <section class="panel">
        <h2>Cameras</h2>
        <div id="cameras"></div>
      </section>

      <section class="panel">
        <h2>Recent Files</h2>
        <div class="files" id="files"></div>
      </section>

      <section class="panel emailPanel">
        <h2>Email recipients</h2>
        <div class="emailFields" id="emailFields"></div>
        <div class="emailActions">
          <button type="button" id="addEmail">Add recipient</button>
          <button type="button" class="saveEmail" id="saveEmails">Save emails</button>
        </div>
        <div class="savedMessage" id="emailSaved"></div>
      </section>
    </div>

    <section class="panel" style="margin-top: 16px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; gap:12px;">
        <div>
          <h2 style="margin-bottom:4px;">Camera-card capacity &amp; last-resort format</h2>
          <p class="muted" style="margin:0; font-size:14px;">Sony reports remaining recording time for the active format. It does not expose trustworthy free GB. Format is blocked until tracked backups are verified on SSD and LucidLink, then still requires manual high-res confirmation.</p>
        </div>
                <button type="button" id="refreshCards" style="min-height:38px; padding:6px 14px; background:#244c78;">CHECK CARDS / FORMAT</button>
      </div>
            <div id="cardStatus"><div class="muted" style="padding:8px 0;">Press CHECK CARDS / FORMAT to show capacity and available format controls for each camera. Status pauses while recording or copying so the probe cannot interrupt the camera.</div></div>
    </section>

    <section class="panel" style="margin-top: 16px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
        <h2>Safe Camera-Card Cleanup & Backups</h2>
        <button type="button" id="refreshCleanup" style="min-height:38px; padding:6px 14px; background:#244c78;">Scan & Refresh</button>
      </div>
      <p style="color:var(--muted); font-size:14px; margin-top:-6px; margin-bottom:16px;">
        A camera-card clip is eligible only when its same non-empty proxy is verified in both the configured SSD landing folder and LucidLink folder. Delete uses the paired Sony original name stored in the manifest, after confirmation; SSD and LucidLink copies are never removed.
      </p>
      <div class="cleanupHeaderRow">
        <div>Session Time</div>
        <div>OBS Master status</div>
        <div>Camera backups & status</div>
      </div>
      <div id="cleanupList">
        <div class="muted" style="padding:12px 6px;">No cleanup manifest loaded. Press "Scan & Refresh" to scan files.</div>
      </div>
    </section>
  </main>

    <div class="studioDialogBackdrop" id="studioDialogBackdrop" hidden>
        <section class="studioDialog" role="dialog" aria-modal="true" aria-labelledby="studioDialogTitle" aria-describedby="studioDialogMessage">
            <h2 id="studioDialogTitle">Confirm action</h2>
            <p class="studioDialogMessage" id="studioDialogMessage"></p>
            <input class="studioDialogInput" id="studioDialogInput" type="text" autocomplete="off" spellcheck="false" hidden>
            <div class="studioDialogActions">
                <button type="button" id="studioDialogCancel">Cancel</button>
                <button type="button" class="studioDialogConfirm" id="studioDialogConfirm">Continue</button>
            </div>
        </section>
    </div>

    <script>
    window.onerror = function(msg, url, line, col, error) {
            console.error("JavaScript Error:", msg, url, line, col, error);
            const errorNode = document.getElementById("error");
            if (errorNode) {
                errorNode.style.display = "block";
                errorNode.textContent = `Page error: ${msg} (line ${line})`;
            }
      return false;
    };
    const statusUrl = "/record/status";
    const buttons = Array.from(document.querySelectorAll("button[data-action]"));
    const $ = (id) => document.getElementById(id);
    let recipientsLoaded = false;
    let lastVisitorType = "Unknown";
    let actionRunning = false;
    let rodecasterArchiveReady = false;
        let studioDialogResolve = null;

        function closeStudioDialog(value) {
            $("studioDialogBackdrop").hidden = true;
            document.body.style.overflow = "";
            if (studioDialogResolve) {
                const resolve = studioDialogResolve;
                studioDialogResolve = null;
                resolve(value);
            }
        }

        function showStudioDialog({ title, message, confirmLabel = "Continue", destructive = false, expectedText = null, cancel = true }) {
            const input = $("studioDialogInput");
            const confirmButton = $("studioDialogConfirm");
            $("studioDialogTitle").textContent = title;
            $("studioDialogMessage").textContent = message;
            $("studioDialogCancel").hidden = !cancel;
            confirmButton.textContent = confirmLabel;
            confirmButton.classList.toggle("destructive", destructive);
            input.hidden = expectedText === null;
            input.value = "";
            input.placeholder = expectedText === null ? "" : expectedText;
            confirmButton.disabled = expectedText !== null;
            input.oninput = () => {
                confirmButton.disabled = input.value !== expectedText;
            };
            $("studioDialogBackdrop").hidden = false;
            document.body.style.overflow = "hidden";
            window.setTimeout(() => (expectedText === null ? confirmButton : input).focus(), 0);
            return new Promise((resolve) => {
                studioDialogResolve = resolve;
            });
        }

        function studioConfirm(message, options = {}) {
            return showStudioDialog({ title: "Confirm action", ...options, message });
        }

        function studioPrompt(message, expectedText) {
            return showStudioDialog({ title: "Safety confirmation", message, confirmLabel: "Confirm", destructive: true, expectedText });
        }

        function studioAlert(message, title = "StudioOS") {
            return showStudioDialog({ title, message, confirmLabel: "OK", cancel: false });
        }

        $("studioDialogCancel").addEventListener("click", () => closeStudioDialog(false));
        $("studioDialogConfirm").addEventListener("click", () => {
            const input = $("studioDialogInput");
            closeStudioDialog(input.hidden ? true : input.value);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !$("studioDialogBackdrop").hidden) closeStudioDialog(false);
        });

    function refreshDisabledStates() {
      const isRemote = lastVisitorType === "Remote (Tailscale)";
      const remoteControlEnable = $("remoteControlEnable");
      const controlsLocked = isRemote && (!remoteControlEnable || !remoteControlEnable.checked);
      
      buttons.forEach((button) => {
        const cameraUnavailable = button.dataset.camera && button.dataset.cameraEnabled === "false";
        button.disabled = controlsLocked || actionRunning || cameraUnavailable;
      });
      
      const addEmail = $("addEmail");
      if (addEmail) addEmail.disabled = controlsLocked;
      const saveEmails = $("saveEmails");
      if (saveEmails) saveEmails.disabled = controlsLocked;
      const refreshCleanup = $("refreshCleanup");
      if (refreshCleanup) refreshCleanup.disabled = controlsLocked || actionRunning;
      const refreshCards = $("refreshCards");
      if (refreshCards) refreshCards.disabled = controlsLocked || actionRunning;
      const archiveRodecaster = $("archiveRodecaster");
      if (archiveRodecaster) archiveRodecaster.disabled = controlsLocked || actionRunning || !rodecasterArchiveReady;
      
      document.querySelectorAll(".cleanupCameraRow button").forEach(btn => {
        btn.disabled = controlsLocked;
      });
    }

    const remoteControlEnable = $("remoteControlEnable");
    if (remoteControlEnable) {
            remoteControlEnable.addEventListener("change", async () => {
        if (remoteControlEnable.checked) {
                    if (!await studioConfirm("You are about to enable control of the FX30 Studio from a remote connection.\\n\\nDo you want to proceed and assume control?", { title: "Remote control warning", confirmLabel: "Enable control", destructive: true })) {
            remoteControlEnable.checked = false;
          }
        }
        refreshDisabledStates();
      });
    }

    function addEmailField(value = "") {
      const row = document.createElement("div");
      row.className = "emailField";
      const input = document.createElement("input");
      input.type = "email";
      input.placeholder = "name@example.com";
      input.value = value;
      input.setAttribute("aria-label", "Email recipient");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "removeEmail";
      remove.title = "Remove recipient";
      remove.textContent = "x";
      remove.addEventListener("click", () => {
        row.remove();
        const emailFields = $("emailFields");
        if (emailFields && !emailFields.children.length) addEmailField();
      });
      row.append(input, remove);
      const emailFields = $("emailFields");
      if (emailFields) emailFields.append(row);
    }

    async function saveEmails() {
      const emailFields = $("emailFields");
      const recipients = emailFields ? Array.from(emailFields.querySelectorAll("input"))
        .map((input) => input.value.trim())
        .filter(Boolean) : [];
      const emailSaved = $("emailSaved");
      if (emailSaved) emailSaved.textContent = "";
      try {
        const response = await fetch("/settings/email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ recipients }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
        if (emailSaved) {
          emailSaved.textContent = `Saved ${result.recipients.length} recipient${result.recipients.length === 1 ? "" : "s"}.`;
        }
      } catch (error) {
        setError(`Could not save email recipients: ${error.message}`);
      }
    }

    function seconds(value) {
      return typeof value === "number" ? `${value.toFixed(3)}s` : "-";
    }

    function recordingTime(startedAt) {
      if (typeof startedAt !== "number") return "00:00:00";
      const total = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const seconds = total % 60;
      return [hours, minutes, seconds]
        .map((value) => String(value).padStart(2, "0"))
        .join(":");
    }

    function prettyAction(action) {
      if (!action) return "-";
      return String(action).replace("record/", "").replace("_", " ");
    }

    function fileSize(bytes) {
      if (!bytes) return "-";
      const units = ["B", "KB", "MB", "GB"];
      let value = bytes;
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value = value / 1024;
        unit += 1;
      }
      return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
    }

    function emailStatus(state) {
      const result = state.last_email_result;
      if (!result) return "WAITING";
      if (result.skipped) return "SKIPPED";
      if (state.last_email_ok === true || result.ok === true) return "SENT";
      if (state.last_email_ok === false || result.ok === false) return "FAILED";
      return "CHECK";
    }

    function macAlertStatus(state) {
      const result = state.last_mac_notification;
      if (!result) return "WAITING";
      if (result.skipped) return "SKIPPED";
      if (result.ok) return "SENT";
      return "FAILED";
    }

    function duration(seconds) {
      if (typeof seconds !== "number") return "-";
      if (seconds < 60) return `${Math.round(seconds)}s`;
      const minutes = Math.floor(seconds / 60);
      const rest = Math.round(seconds % 60);
      return `${minutes}m ${rest}s`;
    }

    function setError(message) {
      const node = $("error");
      node.style.display = message ? "block" : "none";
      node.textContent = message || "";
    }

    async function postAction(action, cameraId = null) {
      if (action === "stop") {
                if (!await studioConfirm("Are you sure you want to stop all camera recordings?", { title: "Stop all cameras", confirmLabel: "Stop recordings", destructive: true })) {
          return;
        }
      } else if (action === "ingest") {
                if (!await studioConfirm("Copy the newest camera proxy to the local staging folder? Nothing will be sent to LucidLink or email.", { title: "Copy camera proxy", confirmLabel: "Start copy" })) {
          return;
        }
      } else if (action === "deliver") {
                if (!await studioConfirm("Copy the verified local proxy to LucidLink?", { title: "Send to LucidLink", confirmLabel: "Start copy" })) {
          return;
        }
      } else if (action === "recover-master") {
        const cameraLabel = cameraId ? cameraId.toUpperCase() : "all enabled cameras";
                if (!await studioConfirm(`Pull the newest 4K master from ${cameraLabel} to Documents/CAMERA BACK UP INGEST?\\n\\nThis can take much longer than the proxy copy.`, { title: "Emergency 4K recovery", confirmLabel: "Start recovery", destructive: true })) {
          return;
        }
      } else if (action === "start") {
                if (!await studioConfirm("Are you sure you want to start all camera recordings?", { title: "Start all cameras", confirmLabel: "Start recordings" })) {
          return;
        }
      }

      actionRunning = true;
      refreshDisabledStates();
      try {
        const cameraQuery = cameraId ? `?camera=${encodeURIComponent(cameraId)}` : "";
        const response = await fetch(`/record/${action}${cameraQuery}`, { method: "POST" });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        setError(`Command failed: ${error.message}`);
      } finally {
        actionRunning = false;
        refreshDisabledStates();
      }
    }

    async function archiveRodecaster() {
      if (!rodecasterArchiveReady) return;
            if (!await studioConfirm("Archive all available RØDECaster takes now? V4 post-record checks must be complete and OBS must stay closed. The source recordings remain on the RØDECaster.", { title: "Archive RØDECaster", confirmLabel: "Start archive" })) {
        return;
      }
      actionRunning = true;
      refreshDisabledStates();
      setError("");
      try {
        const response = await fetch("/record/rodecaster-archive", { method: "POST" });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        setError(`RØDECaster archive was not started: ${error.message}`);
      } finally {
        actionRunning = false;
        refreshDisabledStates();
      }
    }

    function render(data) {
      lastVisitorType = data.visitorType || "Unknown";
      const isRemote = lastVisitorType === "Remote (Tailscale)";
      const remoteBanner = $("remoteBanner");
      if (remoteBanner) {
        if (isRemote) {
          remoteBanner.style.display = "flex";
          $("remoteBannerIp").textContent = `Connecting from ${data.visitorIp || "Unknown"} (Tailscale)`;
        } else {
          remoteBanner.style.display = "none";
        }
      }

      const state = data.state || {};
      if (!recipientsLoaded) {
        const recipients = Array.isArray(data.emailRecipients) ? data.emailRecipients : [];
        (recipients.length ? recipients : [""]).forEach(addEmailField);
        recipientsLoaded = true;
      }
      const last = Array.isArray(state.last_results) ? state.last_results[0] : null;
      const recording = Boolean(state.recording);
      const busy = Boolean(state.record_action_running);
      const stateValue = $("stateValue");

      stateValue.textContent = busy ? "BUSY" : recording ? "REC" : "READY";
      stateValue.className = `stateValue ${busy ? "busy" : recording ? "recording" : "ready"}`;
      $("recordLight").classList.toggle("active", recording);
      $("recordTimer").textContent = recordingTime(state.recording_started_at);
      $("recordTimer").classList.toggle("active", recording);
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      $("lastAction").textContent = prettyAction(state.last_action);
      $("elapsed").textContent = seconds(last && last.elapsedSeconds);
      const cameraStatus = Array.isArray(data.cameraStatus) ? data.cameraStatus : [];
      const enabledCameraIds = new Set(cameraStatus.map((camera) => camera.id));
      document.querySelectorAll("button[data-camera]").forEach((button) => {
        const enabled = enabledCameraIds.has(button.dataset.camera);
        button.dataset.cameraEnabled = enabled ? "true" : "false";
        button.title = enabled ? `Pull newest 4K master from ${button.dataset.camera.toUpperCase()}` : `${button.dataset.camera.toUpperCase()} is not enabled`;
      });
      const warmReady = cameraStatus.length > 0 && cameraStatus.every((camera) => camera.warmConnection);
      $("warm").textContent = warmReady ? "READY" : "CHECK";
      $("ingest").textContent = state.ingest_running
        ? `${state.ingestRunningForSeconds || 0}s`
        : state.autoIngestStartsInSeconds !== null && state.autoIngestStartsInSeconds !== undefined
          ? `AUTO COPY ${state.autoIngestStartsInSeconds}s`
        : (state.last_ingest_message || "Idle");
      $("email").textContent = emailStatus(state);
      $("macAlert").textContent = macAlertStatus(state);
      const progress = state.ingest_progress || {};
      const percent = typeof progress.percent === "number" ? progress.percent : null;
      $("copyFile").textContent = progress.fileName || progress.message || "No copy running";
      $("copyPercent").textContent = percent === null ? "-" : `${percent.toFixed(percent % 1 ? 1 : 0)}%`;
      $("copyFill").style.width = `${percent === null ? 0 : Math.max(0, Math.min(100, percent))}%`;
      $("copySize").textContent = `Size ${fileSize(progress.sizeBytes)}`;
      $("copyCopied").textContent = `Copied ${fileSize(progress.downloadedBytes)}`;
      $("copyEta").textContent = progress.status === "copying"
        ? `ETA ${duration(progress.etaSeconds)}`
        : progress.status
          ? progress.status
          : "ETA -";

      const rodecasterArchive = data.rodecasterArchive || {};
      rodecasterArchiveReady = rodecasterArchive.ready === true;
      const archiveButton = $("archiveRodecaster");
      if (archiveButton) archiveButton.textContent = rodecasterArchiveReady ? "ARCHIVE RØDE" : "RØDE ARCHIVE HELD";
      const archiveStatus = $("rodecasterArchiveStatus");
      if (archiveStatus) archiveStatus.textContent = rodecasterArchive.message || "RØDECaster archive status unavailable.";

      const cameraRows = (data.ingest || []).map((camera) => {
        const live = cameraStatus.find((item) => item.id === camera.id);
        const result = Array.isArray(state.last_results)
          ? state.last_results.find((item) => item.id === camera.id)
          : null;
        const ok = camera.folderError ? false : (result ? Boolean(result.ok) : true);
        const resultState = result && result.recordingState ? result.recordingState : (recording ? "recording" : "not_recording");
        const folderLine = camera.folderError
          ? `Folder access blocked: ${camera.folderError}`
          : `${camera.folder || "-"}${camera.deliveryFolder ? " -> " + camera.deliveryFolder : ""}`;
        return `<div class="row">
          <div><strong>${camera.id}</strong></div>
          <div class="muted">${resultState} / warm ${live && live.warmConnection ? "ready" : "check"}<br>${folderLine}</div>
          <div><span class="pill ${ok ? "ok" : "bad"}">${ok ? "OK" : "FAIL"}</span></div>
        </div>`;
      }).join("");
      $("cameras").innerHTML = cameraRows || `<div class="muted">No enabled cameras.</div>`;

      const files = [];
      (data.ingest || []).forEach((camera) => {
        (camera.stableFiles || []).forEach((file) => files.push({ camera: camera.id, ...file }));
      });
      $("files").innerHTML = files.slice(0, 8).map((file) => `
        <div class="file">
          <div>
            <div class="fileName">${file.name}</div>
            <div class="muted">${file.camera}</div>
          </div>
          <div class="muted">${fileSize(file.sizeBytes)}</div>
        </div>
      `).join("") || `<div class="muted">No stable files yet.</div>`;

      const cardStatus = data.cardStatus || {};
      const cardRows = Object.values(cardStatus).map((report) => {
        const cameraId = report.cameraId || "camera";
        if (!report.ok) {
          return `<div class="row"><div><strong>${cameraId}</strong></div><div class="muted">${report.error || report.message || "Card status unavailable"}</div><div><span class="pill bad">CHECK</span></div></div>`;
        }
        const slotText = (report.slots || []).map((slot) => {
          const minutes = Number(slot.remainingMinutes);
          const time = Number.isFinite(minutes) && minutes >= 0 ? duration(minutes * 60) : "Unavailable";
          const format = slot.fullFormatAvailable
            ? `<button type="button" class="stop" style="min-height:30px; height:30px; padding:2px 8px; font-size:12px; margin-left:8px;" onclick="formatCard('${cameraId}', ${slot.slot})">FORMAT SLOT ${slot.slot}</button>`
            : `<span class="muted">Format unavailable</span>`;
          return `<div style="margin-top:5px;"><strong>Slot ${slot.slot}</strong>: ${time} record time remaining ${format}</div>`;
        }).join("") || "No slot data";
        const freshness = report.stale ? "HELD" : "LIVE";
        return `<div class="row"><div><strong>${cameraId}</strong><br><span class="pill ${report.stale ? "busy" : "ok"}">${freshness}</span></div><div class="muted">${slotText}</div><div><span class="pill ${report.recording ? "busy" : "ok"}">${report.recording ? "REC" : "READY"}</span></div></div>`;
      }).join("");
    $("cardStatus").innerHTML = cardRows || `<div class="muted" style="padding:8px 0;">Press CHECK CARDS / FORMAT to show capacity and available format controls for each camera. Status pauses while recording or copying so the probe cannot interrupt the camera.</div>`;

      // Render cleanup manifests
      const manifests = Array.isArray(data.cleanupManifests) ? data.cleanupManifests : [];
      $("cleanupList").innerHTML = manifests.map((m) => {
        const obsCount = Array.isArray(m.obsMasters) ? m.obsMasters.length : 0;
        let obsStatusText = "Missing";
        let obsPillClass = "bad";
        if (obsCount > 0) {
          const growing = m.obsMasters.some(o => o.growing);
          if (growing) {
            obsStatusText = `${obsCount} file(s) (growing)`;
            obsPillClass = "busy";
          } else {
            const first = m.obsMasters[0];
            const dur = first.durationSeconds ? `${Math.round(first.durationSeconds)}s` : "no dur";
            obsStatusText = `${obsCount} file(s) (${dur})`;
            obsPillClass = "ok";
          }
        }

        const cameraRows = Object.entries(m.cameras).map(([camId, cam]) => {
          const elig = cam.cleanupEligibility || "Unknown";
          let eligClass = "muted";
          if (elig === "Ready for manual delete") eligClass = "pill busy";
          else if (elig === "Deleted from camera card") eligClass = "pill ok";
          else if (elig === "Delete failed") eligClass = "pill bad";
          else eligClass = "pill";

          let buttonHtml = "";
          if (elig === "Ready for manual delete" || elig === "Delete failed") {
            const deleteKey = `${m.sessionTime}|${camId}|${cam.sonyProxyName}`;
            const deleteArmed = pendingCleanupDelete
              && pendingCleanupDelete.key === deleteKey
              && pendingCleanupDelete.expiresAt > Date.now();
            const deleteLabel = deleteArmed ? "Press Again to Delete" : "Delete Backup";
            const deleteBackground = deleteArmed ? "background:#f59e0b;" : "";
            buttonHtml = `<button type="button" class="stop" style="min-height:30px; height:30px; padding:2px 8px; font-size:12px; margin-left:8px; ${deleteBackground}" onclick="confirmDeleteFile('${m.sessionTime}', '${camId}', '${cam.sonyProxyName}', this)">${deleteLabel}</button>`;
          }

          return `<div class="cleanupCameraRow">
            <div>
              <strong>${camId}</strong>: <span class="muted">${cam.sonyProxyName} (${fileSize(cam.lucidLinkSize)})</span>
            </div>
            <div>
              <span class="${eligClass}">${elig}</span>
              ${buttonHtml}
            </div>
          </div>`;
        }).join("");

        return `<div class="cleanupRow">
          <div><strong>${m.sessionTime}</strong></div>
          <div><span class="pill ${obsPillClass}">${obsStatusText}</span></div>
          <div class="cleanupCameras">${cameraRows || '<div class="muted">No camera files</div>'}</div>
        </div>`;
      }).join("") || `<div class="muted" style="padding:12px 6px;">No cleanup manifest loaded. Press "Scan & Refresh" to scan files.</div>`;
      refreshDisabledStates();
    }

    async function refresh() {
      try {
        const response = await fetch(statusUrl, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        setError("");
        render(data);
      } catch (error) {
        setError(`Bridge status unavailable: ${error.message}`);
      }
    }

    let pendingCleanupDelete = null;
    const cleanupDeleteConfirmWindowMs = 6000;

    async function confirmDeleteFile(sessionTime, cameraId, filename, button) {
      const isRemote = lastVisitorType === "Remote (Tailscale)";
      const remoteControlEnable = $("remoteControlEnable");
      const controlsLocked = isRemote && (!remoteControlEnable || !remoteControlEnable.checked);
      if (controlsLocked) {
                await studioAlert("Controls are locked for remote connections.", "Controls locked");
        return;
      }

      const deleteKey = `${sessionTime}|${cameraId}|${filename}`;
      const now = Date.now();
      const isSecondPress = pendingCleanupDelete
        && pendingCleanupDelete.key === deleteKey
        && pendingCleanupDelete.expiresAt > now;

      if (!isSecondPress) {
        pendingCleanupDelete = {
          key: deleteKey,
          expiresAt: now + cleanupDeleteConfirmWindowMs
        };
        if (button) {
          button.textContent = "Press Again to Delete";
          button.style.background = "#f59e0b";
          button.title = "Press this same button again within 6 seconds to permanently delete the camera-card backup.";
        }
        window.setTimeout(() => {
          if (pendingCleanupDelete && pendingCleanupDelete.key === deleteKey && pendingCleanupDelete.expiresAt <= Date.now()) {
            pendingCleanupDelete = null;
            if (button && button.isConnected) {
              button.textContent = "Delete Backup";
              button.style.background = "";
              button.title = "";
            }
          }
        }, cleanupDeleteConfirmWindowMs + 100);
        return;
      }

      pendingCleanupDelete = null;
      if (button) {
        button.disabled = true;
        button.textContent = "Deleting...";
      }
      setError("");
      try {
        const response = await fetch("/record/cleanup-delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sessionTime, cameraId, filename })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
          throw new Error(result.error || result.message || `HTTP ${response.status}`);
        }
        await refresh();
      } catch (error) {
        setError(`Delete command failed: ${error.message}`);
        if (button && button.isConnected) {
          button.disabled = false;
          button.textContent = "Delete Backup";
          button.style.background = "";
        }
      }
    }

    const refreshCleanupBtn = $("refreshCleanup");
    if (refreshCleanupBtn) {
      refreshCleanupBtn.addEventListener("click", async () => {
        refreshCleanupBtn.disabled = true;
        setError("");
        try {
          const response = await fetch("/record/cleanup-scan", { method: "POST" });
          const result = await response.json();
          if (!response.ok || !result.ok) {
            throw new Error(result.error || `HTTP ${response.status}`);
          }
          await refresh();
        } catch (error) {
          setError(`Scan failed: ${error.message}`);
        } finally {
          refreshDisabledStates();
        }
      });
    }

    async function refreshCardStatus() {
      const button = $("refreshCards");
      if (button) button.disabled = true;
      try {
        const response = await fetch("/record/card-status", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const result = await response.json();
        if (!response.ok && !result.cards) throw new Error(result.error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        setError(`Card-status refresh failed: ${error.message}`);
      } finally {
        refreshDisabledStates();
      }
    }

    async function formatCard(cameraId, slot) {
      const isRemote = lastVisitorType === "Remote (Tailscale)";
      const remoteControlEnable = $("remoteControlEnable");
      if (isRemote && (!remoteControlEnable || !remoteControlEnable.checked)) {
                await studioAlert("Controls are locked for remote connections.", "Controls locked");
        return;
      }
            const acknowledgment = await studioPrompt(`LAST RESORT: this permanently erases every file on ${cameraId} Slot ${slot}.\\n\\nType FORMAT SLOT ${slot} exactly:`, `FORMAT SLOT ${slot}`);
      if (acknowledgment !== `FORMAT SLOT ${slot}`) return;
            const highResAcknowledgment = await studioPrompt("This system verifies proxy copies only. Confirm the higher-resolution footage is either safely copied or you accept that it will be unrecoverable.\\n\\nType I HAVE MANUALLY VERIFIED THE HIGH-RES FOOTAGE exactly:", "I HAVE MANUALLY VERIFIED THE HIGH-RES FOOTAGE");
      if (highResAcknowledgment !== "I HAVE MANUALLY VERIFIED THE HIGH-RES FOOTAGE") return;
      actionRunning = true;
      refreshDisabledStates();
      try {
                const requestBody = { cameraId, slot, acknowledgment, highResAcknowledgment };
                const sendFormatRequest = () => fetch("/record/format-card", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(requestBody),
                    });
                let response = await sendFormatRequest();
                let result = await response.json();
                if (response.status === 409 && result.requiresBackupOverride) {
                    const overridePhrase = `ERASE ${cameraId.toUpperCase()} SLOT ${slot} WITHOUT VERIFIED BACKUP`;
                    const backupOverrideAcknowledgment = await studioPrompt(
                        `${result.error}\n\nLAST CHANCE: type ${overridePhrase} exactly to format anyway:`,
                        overridePhrase,
                    );
                    if (backupOverrideAcknowledgment !== overridePhrase) return;
                    requestBody.backupOverrideAcknowledgment = backupOverrideAcknowledgment;
                    response = await sendFormatRequest();
                    result = await response.json();
                }
        if (!response.ok || !result.ok) throw new Error(result.error || result.stderr || "Camera rejected format");
        await studioAlert(`Slot ${slot} format command completed.`, "Card format complete");
        await refreshCardStatus();
      } catch (error) {
                const message = `Card format failed: ${error.message}`;
                setError(message);
                await studioAlert(message, "Card format blocked");
      } finally {
        actionRunning = false;
        refreshDisabledStates();
      }
    }

    window.confirmDeleteFile = confirmDeleteFile; // Expose to global scope for onclick handler
    window.formatCard = formatCard;

    buttons.forEach((button) => {
      button.addEventListener("click", () => postAction(button.dataset.action, button.dataset.camera || null));
    });
    const archiveRodecasterButton = $("archiveRodecaster");
    if (archiveRodecasterButton) archiveRodecasterButton.addEventListener("click", archiveRodecaster);

    const addEmailBtn = $("addEmail");
    if (addEmailBtn) {
      addEmailBtn.addEventListener("click", () => addEmailField());
    }

    const saveEmailsBtn = $("saveEmails");
    if (saveEmailsBtn) {
      saveEmailsBtn.addEventListener("click", saveEmails);
    }
    const refreshCardsBtn = $("refreshCards");
    if (refreshCardsBtn) refreshCardsBtn.addEventListener("click", refreshCardStatus);

    refresh();
    setInterval(refresh, 750);
  </script>
</body>
</html>
"""


def log_event(label, payload):
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}")
    print(json.dumps(payload, indent=2))


def extract_downloaded_file(results):
    for result in results:
        stdout = result.get("stdout", "")
        match = re.search(r"File:\s+(.+)", stdout)
        if match:
            return match.group(1).strip()
    return None


def extract_downloaded_files(results):
    files = []
    for result in results:
        stdout = result.get("stdout", "")
        match = re.search(r"File:\s+(.+)", stdout)
        if match:
            files.append({
                "cameraId": result.get("id"),
                "source": match.group(1).strip(),
            })
    return files


def camera_delivery_folder(config, camera_id):
    lucid = config.get("lucidLink") or {}
    root = lucid.get("deliveryRoot")
    if not root:
        return None
    camera_folders = lucid.get("cameraFolders") or {}
    folder_name = camera_folders.get(camera_id)
    if not folder_name:
        folder_name = str(camera_id or "unknown").replace("-", "_").upper()
    return Path(root) / folder_name


def delivery_label(config):
    return str((config.get("lucidLink") or {}).get("label") or "LucidLink")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_media(path):
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        return {"ok": False, "path": str(source), "error": "Local media file is missing or empty"}
    ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(source)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "path": str(source), "error": f"Media verification failed: {exc}"}
    if probe.returncode != 0:
        return {"ok": False, "path": str(source), "error": (probe.stderr or probe.stdout or "ffprobe failed").strip()}
    try:
        probe_data = json.loads(probe.stdout)
        duration = float((probe_data.get("format") or {}).get("duration") or 0)
    except (ValueError, json.JSONDecodeError):
        duration = 0
    if duration <= 0:
        return {"ok": False, "path": str(source), "error": "Media duration is missing or zero"}
    video_streams = [item for item in probe_data.get("streams", []) if item.get("codec_type") == "video"]
    video = video_streams[0] if video_streams else {}
    return {
        "ok": True,
        "path": str(source),
        "sizeBytes": source.stat().st_size,
        "durationSeconds": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "sha256": sha256_file(source),
    }


def timestamped_delivery_path(destination_folder, source):
    timestamp = time.strftime("%Y-%m-%d %H-%M-%S")
    destination = destination_folder / f"{timestamp}{source.suffix}"
    if not destination.exists():
        return destination

    for index in range(2, 1000):
        candidate = destination_folder / f"{timestamp}-{index}{source.suffix}"
        if not candidate.exists():
            return candidate

    raise FileExistsError(f"Could not find an unused timestamped filename for {source}")


def copy_to_lucidlink(config, downloaded_files):
    lucid = config.get("lucidLink") or {}
    label = delivery_label(config)
    if not lucid.get("enabled", False):
        return []

    delivery_root = Path(lucid.get("deliveryRoot") or "")
    volume_root = None
    if (
        delivery_root.is_absolute()
        and len(delivery_root.parts) >= 3
        and delivery_root.parts[1] == "Volumes"
    ):
        candidates = [delivery_root, *delivery_root.parents]
        volume_root = next(
            (
                candidate
                for candidate in candidates
                if candidate != Path("/Volumes") and os.path.ismount(candidate)
            ),
            None,
        )
    if delivery_root.parts[1:2] == ("Volumes",) and volume_root is None:
        expected_mount = Path("/", *delivery_root.parts[1:3])
        return [{
            "ok": False,
            "cameraId": item.get("cameraId"),
            "source": item.get("source"),
            "destination": str(delivery_root),
            "error": (
                f"{label} is not mounted below {expected_mount}. "
                f"Connect {label}, then press the delivery button again."
            ),
        } for item in downloaded_files]

    results = []
    for item in downloaded_files:
        source = Path(item["source"])
        destination_folder = Path(item["destinationFolder"]) if item.get("destinationFolder") else camera_delivery_folder(config, item.get("cameraId"))
        if not destination_folder:
            results.append({
                "ok": False,
                "cameraId": item.get("cameraId"),
                "source": str(source),
                "error": f"{label} delivery root is not configured",
            })
            continue
        relative_path = item.get("relativePath")
        if relative_path:
            destination = destination_folder / relative_path
        elif item.get("preserveName"):
            destination = destination_folder / source.name
        else:
            destination = timestamped_delivery_path(destination_folder, source)
        try:
            if not source.exists():
                raise FileNotFoundError(f"Source file does not exist: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.stat().st_size == source.stat().st_size:
                results.append({"ok": True, "skipped": True, "cameraId": item.get("cameraId"), "source": str(source), "destination": str(destination), "originalName": source.name, "sizeBytes": destination.stat().st_size})
                continue
            shutil.copy2(source, destination)
            source_size = source.stat().st_size
            destination_size = destination.stat().st_size
            if source_size != destination_size:
                raise IOError(
                    f"Copied size mismatch: source={source_size}, destination={destination_size}"
                )
            results.append({
                "ok": True,
                "cameraId": item.get("cameraId"),
                "source": str(source),
                "destination": str(destination),
                "originalName": source.name,
                "sizeBytes": destination_size,
            })
        except Exception as exc:
            results.append({
                "ok": False,
                "cameraId": item.get("cameraId"),
                "source": str(source),
                "destination": str(destination),
                "error": str(exc),
            })
    return results


def obs_delivery_items(config):
    obs = config.get("obs") or {}
    if not obs.get("copyStableFiles", False):
        return []
    extensions = {".mp4", ".mov", ".mxf", ".wav"}
    stable_seconds = int(config.get("stableFileSeconds", 30))
    folder = Path(obs.get("masterFolder") or "")
    delivery_root = Path((config.get("lucidLink") or {}).get("deliveryRoot") or "")
    delivery_folder = delivery_root / (obs.get("deliveryFolder") or "OBS-MASTERS")
    items = [
        {"source": item["path"], "destinationFolder": str(delivery_folder), "preserveName": True, "cameraId": "obs"}
        for item in list_stable_files(folder, extensions, stable_seconds)
    ]

    if not obs.get("copyAudioStems", True):
        return items

    audio_folder_name = obs.get("audioStemsFolder") or "AUDIO-STEMS"
    audio_source = folder / audio_folder_name
    audio_delivery = delivery_root / (
        obs.get("audioStemsDeliveryFolder") or audio_folder_name
    )
    cutoff = time.time() - stable_seconds
    if not audio_source.is_dir():
        return items

    for source in sorted(audio_source.rglob("*")):
        if not source.is_file() or source.name.startswith("._"):
            continue
        try:
            if source.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        items.append({
            "source": str(source),
            "destinationFolder": str(audio_delivery),
            "relativePath": str(source.relative_to(audio_source)),
            "preserveName": True,
            "cameraId": "obs-audio",
        })
    return items


def send_ingest_email(config, delivery_results):
    email_config = config.get("email") or {}
    if not email_config.get("enabled", False):
        return {"ok": True, "skipped": True, "reason": "Email is disabled"}

    delivered = [result for result in delivery_results if result.get("ok")]
    if not delivered:
        return {"ok": True, "skipped": True, "reason": "No delivered files to email"}

    provider = email_config.get("provider", "smtp").lower()
    if provider == "resend":
        return send_resend_email(email_config, delivered)
    return send_smtp_email(email_config, delivered)


def email_subject_and_body(email_config, delivered):
    studioos_delivery = any(result.get("workflow") == "studioos" for result in delivered)
    subject_prefix = email_config.get(
        "studioSubjectPrefix" if studioos_delivery else "subjectPrefix",
        "StudioOS recording ready" if studioos_delivery else "FX30 files ready",
    )
    file_names = ", ".join(Path(result["destination"]).name for result in delivered)
    subject = f"{subject_prefix}: {file_names}"
    heading = "StudioOS recording is ready" if studioos_delivery else "FX30 files are ready"
    body_lines = [f"{heading} in LucidLink:", ""]
    for result in delivered:
        body_lines.append(result["destination"])
    folder = Path(delivered[0]["destination"]).parent
    studio_url = email_config.get("studioUrl", "http://127.0.0.1:3030").rstrip("/")
    camera_id = delivered[0].get("cameraId", "")
    folder_url = (
        email_config.get("lucidLinkWebUrl", "https://app.lucidlink.com/")
        if studioos_delivery
        else f"{studio_url}/open/fx30-folder?camera={camera_id}"
    )
    escaped_folder = html.escape(str(folder))
    escaped_url = html.escape(folder_url, quote=True)
    escaped_files = html.escape(file_names)
    escaped_heading = html.escape(heading)
    button_label = "Open in LucidLink" if studioos_delivery else "Open FX30 folder"
    location_note = (
        '<p style="color:#344054;font-size:14px">Location: studio / Studio Recordings</p>'
        if studioos_delivery
        else ""
    )
    html_body = f"""\
<!doctype html>
<html>
  <body style="font-family:Arial,sans-serif;color:#17202a;line-height:1.5">
    <h2 style="margin:0 0 12px">{escaped_heading}</h2>
    <p>{escaped_files}</p>
    <p style="margin:24px 0">
      <a href="{escaped_url}" style="background:#2463a2;color:#fff;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:bold">{button_label}</a>
    </p>
        {location_note}
    <p style="color:#667085;font-size:13px">{escaped_folder}</p>
  </body>
</html>"""
    return subject, "\n".join(body_lines), html_body, file_names


def v4_completion_email_items(status):
    summary = status.get("postRecordSummary") or {}
    result = status.get("lastResult") or {}
    job_id = summary.get("jobId")
    if (
        status.get("localOnly") is not False
        or (status.get("delivery") or {}).get("provider") != "LucidLink"
        or summary.get("state") != "complete"
        or not summary.get("allChecksPassed")
        or not job_id
        or result.get("jobId") != job_id
        or result.get("state") != "complete"
    ):
        return None
    receipts = [*(result.get("masters") or []), *(result.get("proxiesDelivered") or [])]
    if not receipts or not all(item.get("verified") and item.get("destination") for item in receipts):
        return None
    destinations = [Path(item["destination"]) for item in receipts]
    if not all(path.is_absolute() and path.parts[1:2] == ("Volumes",) for path in destinations):
        return None
    return job_id, [
        {"ok": True, "workflow": "studioos", "destination": str(path)}
        for path in destinations
    ]


def v4_error_email_alert(status):
    """Return one stable alert for an actionable Just In video/audio fault."""
    problems = []
    category = None
    job_id = None

    if status.get("apiReachable") is False:
        category = "just-in-unreachable"
        problems.append(status.get("error") or "Just In API is unreachable")

    missing_channels = status.get("missingRequestedChannels") or []
    if missing_channels:
        category = category or "just-in-channel-error"
        problems.append(f"Missing channels: {', '.join(str(item) for item in missing_channels)}")

    for channel in status.get("channels") or []:
        if channel.get("error"):
            category = category or "just-in-channel-error"
            problems.append(f"{channel.get('name') or 'Just In channel'}: {channel['error']}")

    failed_stop = status.get("failedStopRecovery") or {}
    if failed_stop.get("state") in {"attention", "failed", "stop_unknown"}:
        category = category or "just-in-stop-error"
        channel_errors = failed_stop.get("channelErrors") or failed_stop.get("errors") or {}
        if channel_errors:
            problems.extend(f"{name}: {message}" for name, message in sorted(channel_errors.items()))
        else:
            problems.append(failed_stop.get("error") or "Just In did not confirm that every channel stopped")

    post_record = status.get("postRecord") or {}
    post_state = post_record.get("state")
    if post_state in {"attention", "failed", "cancelled"}:
        category = category or "just-in-media-error"
        job_id = post_record.get("jobId")
        problems.append(post_record.get("error") or "Just In master or proxy verification did not pass")

    summary = status.get("postRecordSummary") or {}
    if summary.get("state") == "attention" and not summary.get("allChecksPassed"):
        category = category or "just-in-media-error"
        job_id = job_id or summary.get("jobId")
        problems.append(summary.get("detail") or "Just In video or audio checks need attention")

    problems = list(dict.fromkeys(str(problem).strip() for problem in problems if str(problem).strip()))
    if not problems:
        return None

    fingerprint_source = json.dumps(
        {"category": category, "jobId": job_id, "problems": problems},
        sort_keys=True,
    ).encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
    subject = "StudioOS alert: Just In video/audio error"
    body = "StudioOS detected a Just In video or audio problem:\n\n" + "\n".join(
        f"- {problem}" for problem in problems
    )
    if job_id:
        body += f"\n\nPost-record job: {job_id}"
    body += "\nDashboard: http://127.0.0.1:3030"
    return {
        "fingerprint": fingerprint,
        "category": category,
        "jobId": job_id,
        "subject": subject,
        "body": body,
        "problems": problems,
    }


def send_v4_error_email(config, alert):
    email_config = config.get("email") or {}
    if not email_config.get("enabled", False):
        return {"ok": True, "skipped": True, "reason": "Email is disabled"}
    if email_config.get("provider", "smtp").lower() != "resend":
        return {"ok": False, "error": "Just In error alerts require the Resend email provider"}
    escaped_body = html.escape(alert["body"]).replace("\n", "<br>")
    html_body = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#17202a;line-height:1.5">'
        '<h2 style="color:#b42318">Just In video/audio error</h2>'
        f'<p>{escaped_body}</p></body></html>'
    )
    return send_resend_message(
        email_config,
        alert["subject"],
        alert["body"],
        html_body,
    )


def write_v4_email_state(state):
    V4_EMAIL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = V4_EMAIL_STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, V4_EMAIL_STATE_PATH)


def watch_v4_completion_email(config, poll_seconds=15):
    attempted_at = {}
    while True:
        try:
            with urlopen(Request(V4_STATUS_URL, method="GET"), timeout=5) as response:
                status = json.loads(response.read().decode("utf-8"))
            persisted = {}
            if V4_EMAIL_STATE_PATH.exists():
                persisted = json.loads(V4_EMAIL_STATE_PATH.read_text(encoding="utf-8"))

            alert = v4_error_email_alert(status)
            if alert:
                fingerprint = alert["fingerprint"]
                last_attempt = attempted_at.get(fingerprint, 0)
                if persisted.get("lastAlertFingerprint") != fingerprint and time.time() - last_attempt >= 300:
                    attempted_at[fingerprint] = time.time()
                    email_result = send_v4_error_email(config, alert)
                    with STATE_LOCK:
                        STATE["last_email_result"] = email_result
                        STATE["last_email_ok"] = bool(email_result.get("ok"))
                    if email_result.get("ok") and not email_result.get("skipped"):
                        persisted.update({
                            "lastAlertFingerprint": fingerprint,
                            "lastAlertCategory": alert["category"],
                            "lastAlertJobId": alert.get("jobId"),
                            "lastAlertSentAt": time.time(),
                        })
                        write_v4_email_state(persisted)
            elif persisted.get("lastAlertFingerprint"):
                for key in ("lastAlertFingerprint", "lastAlertCategory", "lastAlertJobId", "lastAlertSentAt"):
                    persisted.pop(key, None)
                write_v4_email_state(persisted)

            completion = v4_completion_email_items(status)
            if completion:
                job_id, delivered = completion
                last_attempt = attempted_at.get(job_id, 0)
                if persisted.get("lastEmailedJobId") != job_id and time.time() - last_attempt >= 300:
                    attempted_at[job_id] = time.time()
                    email_result = send_ingest_email(config, delivered)
                    with STATE_LOCK:
                        STATE["last_email_result"] = email_result
                        STATE["last_email_ok"] = bool(email_result.get("ok"))
                    if email_result.get("ok") and not email_result.get("skipped"):
                        persisted.update({"lastEmailedJobId": job_id, "sentAt": time.time()})
                        write_v4_email_state(persisted)
        except Exception as exc:
            print(f"V4 completion email watcher: {exc}", flush=True)
        time.sleep(poll_seconds)


def resend_api_key(email_config):
    api_key_name = email_config.get("apiKeyEnv", "FX30_RESEND_API_KEY")
    api_key = os.environ.get(api_key_name)
    if api_key:
        return api_key
    keychain_result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", api_key_name, "-w"],
        capture_output=True,
        text=True,
    )
    return keychain_result.stdout.strip() if keychain_result.returncode == 0 else None


def send_resend_message(email_config, subject, body, html_body, files=None):
    sender = email_config.get("from")
    recipients = email_config.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    api_key = resend_api_key(email_config)

    missing = [
        name
        for name, value in {
            "from": sender,
            "to": recipients,
            "apiKey": api_key,
        }.items()
        if not value
    ]
    if missing:
        return {"ok": False, "error": f"Missing Resend email settings: {', '.join(missing)}"}

    data = json.dumps({
        "from": sender,
        "to": recipients,
        "subject": subject,
        "text": body,
        "html": html_body,
    }).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "FX30-Studio/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8")
        return {
            "ok": True,
            "provider": "resend",
            "to": recipients,
            "files": files or "",
            "response": response_body,
        }
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "provider": "resend",
            "error": f"Resend HTTP {exc.code}: {response_body}",
        }
    except Exception as exc:
        return {"ok": False, "provider": "resend", "error": str(exc)}


def send_resend_email(email_config, delivered):
    subject, body, html_body, file_names = email_subject_and_body(email_config, delivered)
    return send_resend_message(email_config, subject, body, html_body, file_names)


def send_smtp_email(email_config, delivered):
    host = email_config.get("smtpHost")
    port = int(email_config.get("smtpPort", 587))
    sender = email_config.get("from")
    recipients = email_config.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    username = os.environ.get(email_config.get("usernameEnv", "FX30_SMTP_USERNAME"))
    password = os.environ.get(email_config.get("passwordEnv", "FX30_SMTP_PASSWORD"))

    missing = [
        name
        for name, value in {
            "smtpHost": host,
            "from": sender,
            "to": recipients,
            "username": username,
            "password": password,
        }.items()
        if not value
    ]
    if missing:
        return {"ok": False, "error": f"Missing email settings: {', '.join(missing)}"}

    subject, body, html_body, file_names = email_subject_and_body(email_config, delivered)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(message)
        return {"ok": True, "to": recipients, "files": file_names}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def update_ingest_progress(**updates):
    with STATE_LOCK:
        progress = STATE.get("ingest_progress") or {}
        progress.update(updates)
        started_at = progress.get("startedAt")
        if started_at:
            progress["elapsedSeconds"] = round(time.time() - started_at, 1)
        size_bytes = progress.get("sizeBytes")
        downloaded_bytes = progress.get("downloadedBytes")
        if size_bytes and downloaded_bytes is not None:
            percent = min(100, max(0, (downloaded_bytes / size_bytes) * 100))
            progress["percent"] = round(percent, 1)
            elapsed = progress.get("elapsedSeconds") or 0
            if downloaded_bytes > 0 and percent < 100 and elapsed > 0:
                total_estimate = elapsed / (percent / 100)
                progress["etaSeconds"] = round(max(0, total_estimate - elapsed), 1)
        STATE["ingest_progress"] = progress


def parse_ingest_line(line, camera):
    match = re.search(
        r"Downloading latest contents-transfer file:\s+(.+?)\s+\((\d+)\s+bytes\)\s+to\s+(.+)$",
        line,
    )
    if match:
        file_name = match.group(1).strip()
        size_bytes = int(match.group(2))
        folder = match.group(3).strip()
        path = str(Path(folder) / file_name)
        update_ingest_progress(
            camera=camera["id"],
            fileName=file_name,
            filePath=path,
            sizeBytes=size_bytes,
            downloadedBytes=0,
            percent=0,
            status="copying",
            message=f"Copying {file_name}",
        )
        return

    match = re.search(r"Download progress\s+(.+?):\s+(\d+)%", line)
    if match:
        update_ingest_progress(
            fileName=match.group(1).strip(),
            percent=int(match.group(2)),
            status="copying",
        )
        return

    match = re.search(r"Get Contents Data InProgress per=(\d+)%", line)
    if match:
        update_ingest_progress(percent=int(match.group(1)), status="copying")
        return

    match = re.search(r"(?:File:|File =|\[COMPLETE\].* File:)\s*(.+)$", line)
    if match:
        file_path = match.group(1).strip()
        path = Path(file_path)
        size = path.stat().st_size if path.exists() else None
        updates = {
            "filePath": file_path,
            "fileName": path.name,
            "downloadedBytes": size,
            "status": "complete",
            "percent": 100,
            "message": f"Copied {path.name}",
        }
        if size is not None:
            updates["sizeBytes"] = size
        update_ingest_progress(**updates)


def poll_ingest_file_size():
    progress = STATE.get("ingest_progress") or {}
    file_path = progress.get("filePath")
    if not file_path:
        return None
    path = Path(file_path)
    if path.exists():
        size = path.stat().st_size
        update_ingest_progress(downloadedBytes=size)
        return size
    return None


def notify_mac(title, message, enabled=True):
    if not enabled:
        with STATE_LOCK:
            STATE["last_mac_notification"] = {
                "ok": True,
                "skipped": True,
                "reason": "macNotifications is disabled",
            }
        return
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            timeout=5,
            capture_output=True,
            text=True,
        )
        result = {
            "ok": proc.returncode == 0,
            "returnCode": proc.returncode,
            "title": title,
            "message": message,
        }
        if proc.stdout.strip():
            result["stdout"] = proc.stdout.strip()
        if proc.stderr.strip():
            result["stderr"] = proc.stderr.strip()
        if proc.returncode != 0:
            print(f"macOS notification failed: {result}")
    except Exception as exc:
        result = {
            "ok": False,
            "title": title,
            "message": message,
            "error": str(exc),
        }
        print(f"macOS notification failed: {result}")
    with STATE_LOCK:
        STATE["last_mac_notification"] = result


def sony_command_environment(command):
    env = os.environ.copy()
    executable = Path(command[0])
    if executable.name == "sony-rec":
        runtime = executable.parent if executable.is_absolute() else Path(__file__).resolve().parent
        env["DYLD_LIBRARY_PATH"] = f"{runtime}:{runtime / 'CrAdapter'}{':' + env['DYLD_LIBRARY_PATH'] if env.get('DYLD_LIBRARY_PATH') else ''}"
    env["SONY_REC_NONINTERACTIVE"] = "1"
    return env


def notify_studio_event(event_key, title, message, enabled=True):
    if not enabled:
        return notify_mac(title, message, enabled=False)
    helper = Path("/Users/podcaststudio/Developer/StudioOS-Build-V4/studio_notification.command")
    try:
        proc = subprocess.run(
            ["/bin/bash", str(helper), event_key, title, str(message)[:500]],
            timeout=8,
            capture_output=True,
            text=True,
        )
        status = proc.stdout.strip() or "failed"
        result = {
            "ok": proc.returncode == 0,
            "sent": status == "sent",
            "status": status,
            "eventKey": event_key,
            **({"error": proc.stderr.strip()} if proc.stderr.strip() else {}),
        }
    except Exception as exc:
        result = {"ok": False, "sent": False, "eventKey": event_key, "error": str(exc)}
        print(f"macOS notification failed: {result}")
    with STATE_LOCK:
        STATE["last_mac_notification"] = result
    return result


def dashboard_url(config):
    host = config.get("dashboardHost") or config.get("host", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{config['port']}/"


def open_dashboard(config):
    if not config.get("openDashboard", False):
        return
    url = dashboard_url(config)
    try:
        subprocess.Popen(
            ["/usr/bin/open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Opened FX30 dashboard: {url}")
    except Exception as exc:
        print(f"Could not open dashboard automatically: {exc}")


def normalize_mac(value):
    return "".join(ch.upper() for ch in (value or "") if ch.isalnum())


class WarmCameraController:
    def __init__(self, camera):
        self.camera = camera
        self.proc = None
        self.terminal_fd = None
        self.lock = Lock()
        self.lines = queue.Queue()
        self.started_at = None
        self.identity_error = None

    def start(self, timeout_seconds):
        with self.lock:
            return self._start_locked(timeout_seconds)

    def _start_locked(self, timeout_seconds):
        if self.proc and self.proc.poll() is None:
            return True

        for attempt in range(2):
            command = self._server_command()
            if not command:
                return False

            env = sony_command_environment(command)
            if self.camera.get("password"):
                env["SONY_REC_PASSWORD"] = self.camera["password"]

            self.terminal_fd, child_fd = pty.openpty()
            self.proc = subprocess.Popen(
                command,
                cwd=Path(command[0]).resolve().parent,
                stdin=child_fd,
                stdout=child_fd,
                stderr=child_fd,
                close_fds=True,
                env=env,
            )
            os.close(child_fd)
            self.started_at = time.time()
            self.identity_error = None
            reader_fd = os.dup(self.terminal_fd)
            Thread(target=self._read_stdout, args=(reader_fd,), daemon=True).start()
            ready_line = self._wait_for_prefix("SONY_REC_READY ", timeout_seconds)
            ready = ready_line is not None
            if ready:
                ready = self._assert_identity(ready_line)
            if ready:
                return True
            self._stop_locked()
            if attempt == 0:
                print(f"Warm camera connection {self.camera['id']} failed on first attempt; retrying once before giving up.")
                continue
            return False
        return False

    def _assert_identity(self, ready_line):
        """Fail closed if the body sony-rec actually connected to isn't the
        one configured for this slot. Never let a MAC mismatch reach ARM."""
        configured_mac = self.camera.get("mac")
        if not configured_mac:
            self.identity_error = f"Camera {self.camera['id']} has no configured MAC; refusing to trust this connection"
            return False
        try:
            payload = json.loads(ready_line.removeprefix("SONY_REC_READY "))
        except Exception as exc:
            self.identity_error = f"Could not parse SONY_REC_READY: {exc}"
            return False
        connected_mac = payload.get("mac")
        if normalize_mac(connected_mac) != normalize_mac(configured_mac):
            self.identity_error = (
                f"Identity mismatch for {self.camera['id']}: configured MAC {configured_mac} "
                f"but sony-rec connected to {connected_mac!r}"
            )
            print(f"REFUSING camera connection: {self.identity_error}")
            return False
        return True

    def run(self, action, timeout_seconds):
        started_at = time.time()
        with self.lock:
            if not self._start_locked(timeout_seconds):
                return {
                    "id": self.camera["id"],
                    "ok": False,
                    "error": self.identity_error or "Warm camera connection did not become ready",
                }

            self._drain_lines()
            try:
                os.write(self.terminal_fd, (action + "\n").encode())
            except Exception as exc:
                self._stop_locked()
                return {
                    "id": self.camera["id"],
                    "ok": False,
                    "error": f"Warm camera connection write failed: {exc}",
                }

            line = self._wait_for_prefix("SONY_REC_RESULT ", timeout_seconds)
            if line is None:
                # A running helper process is not proof that the Sony link is
                # still usable. Drop it after a timed-out command so status
                # cannot advertise a stale warm connection as camera-ready.
                self._stop_locked()
                return {
                    "id": self.camera["id"],
                    "ok": False,
                    "error": f"Warm camera command timed out after {timeout_seconds} seconds",
                }

            try:
                payload = json.loads(line.removeprefix("SONY_REC_RESULT "))
            except Exception as exc:
                return {
                    "id": self.camera["id"],
                    "ok": False,
                    "error": f"Could not parse warm camera response: {exc}",
                    "stdout": line,
                }

            return {
                "id": self.camera["id"],
                "ok": bool(payload.get("ok")),
                "returnCode": 0 if payload.get("ok") else 1,
                "elapsedSeconds": round(time.time() - started_at, 3),
                "recordingState": payload.get("recordingState"),
                "stdout": json.dumps(payload),
                "warmConnection": True,
            }

    def stop(self):
        with self.lock:
            self._stop_locked()

    def _stop_locked(self):
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.poll() is None and self.terminal_fd is not None:
                os.write(self.terminal_fd, b"quit\n")
        except Exception:
            pass
        if self.terminal_fd is not None:
            try:
                os.close(self.terminal_fd)
            except OSError:
                pass
            self.terminal_fd = None
        if proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)

    def _server_command(self):
        command = self.camera.get("statusCommand") or self.camera.get("startCommand")
        if not command:
            return None
        executable = command[0]
        result = [executable, "--camera", self.camera["id"], "--server"]
        if self.camera.get("mac"):
            result += ["--mac", self.camera["mac"]]
        if self.camera.get("ip"):
            result += ["--ip", self.camera["ip"]]
        return result

    def _read_stdout(self, terminal_fd):
        try:
            with os.fdopen(terminal_fd, "r", buffering=1, errors="replace") as stream:
                for line in stream:
                    self.lines.put(line.rstrip("\r\n"))
        except Exception as exc:
            self.lines.put(f"SONY_REC_READER_ERROR {exc}")

    def _wait_for_prefix(self, prefix, timeout_seconds):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=0.25)
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    return None
                continue
            if line.startswith(prefix):
                return line
        return None

    def _drain_lines(self):
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                return


def warm_controller_for(camera):
    with CAMERA_CONTROLLERS_LOCK:
        controller = CAMERA_CONTROLLERS.get(camera["id"])
        if controller is None:
            controller = WarmCameraController(camera)
            CAMERA_CONTROLLERS[camera["id"]] = controller
        return controller


def existing_warm_controller(camera_id):
    with CAMERA_CONTROLLERS_LOCK:
        return CAMERA_CONTROLLERS.get(camera_id)


def warm_enabled_cameras(config, probe_status=True, block=True):
    """Warm enabled cameras concurrently and return ordered, fail-closed results."""
    if not WARM_BATCH_LOCK.acquire(blocking=block):
        return None
    try:
        cameras = list(enabled_cameras(config))
        results = [None] * len(cameras)
        eligible = []
        timeout_seconds = config.get("commandTimeoutSeconds", 90)
        for index, camera in enumerate(cameras):
            if not camera.get("warmConnection", True):
                results[index] = {
                    "id": camera["id"],
                    "ok": False,
                    "warmConnection": False,
                    "error": "Warm camera connection is disabled in config",
                }
                continue
            if not camera.get("persistentWarmConnection", True):
                if any(result is not None for result in results[:index]):
                    time.sleep(config.get("warmConnectDelaySeconds", 0))
                result = run_camera_command(camera, "status", timeout_seconds)
                result["warmConnection"] = bool(result.get("ok"))
                results[index] = result
                continue
            eligible.append((index, camera, warm_controller_for(camera)))

        def warm_camera(camera, controller):
            started_at = time.time()
            try:
                if probe_status:
                    result = dict(controller.run("status", timeout_seconds))
                else:
                    ready = controller.start(timeout_seconds)
                    result = {
                        "id": camera["id"],
                        "ok": ready,
                        "error": None if ready else controller.identity_error or "Warm camera connection did not become ready",
                    }
            except Exception as exc:
                result = {
                    "id": camera["id"],
                    "ok": False,
                    "error": f"Warm camera connection failed: {exc}",
                }
            result["id"] = camera["id"]
            result["warmConnection"] = bool(result.get("ok"))
            if not probe_status:
                result.setdefault("elapsedSeconds", round(time.time() - started_at, 3))
            return result

        parallel_warm_connect = config.get("parallelWarmConnect", True)
        if eligible and not probe_status and not parallel_warm_connect:
            for position, (index, camera, controller) in enumerate(eligible):
                if position:
                    time.sleep(config.get("warmConnectDelaySeconds", 0))
                results[index] = warm_camera(camera, controller)
        elif eligible:
            with ThreadPoolExecutor(max_workers=len(eligible), thread_name_prefix="fx30-warm") as executor:
                futures = [
                    (index, executor.submit(warm_camera, camera, controller))
                    for index, camera, controller in eligible
                ]
                for index, future in futures:
                    results[index] = future.result()
        return results
    finally:
        WARM_BATCH_LOCK.release()


def stop_warm_controllers(config, reason="switching camera mode"):
    stopped = []
    for camera in enabled_cameras(config):
        controller = existing_warm_controller(camera["id"])
        if controller and controller.proc and controller.proc.poll() is None:
            controller.stop()
            stopped.append(camera["id"])
    if stopped:
        update_ingest_progress(
            status="switching",
            message=reason,
            warmConnectionsStopped=stopped,
        )
    return stopped


def stop_warm_controllers_for_ingest(config):
    stopped = stop_warm_controllers(config, "Switching camera to copy mode")
    if stopped:
        time.sleep(config.get("modeSwitchDelaySeconds", 0.5))
    return stopped


def restart_warm_controllers_after_ingest(config):
    results = warm_enabled_cameras(config, probe_status=False)
    warm_camera_ids = {
        camera["id"]
        for camera in enabled_cameras(config)
        if camera.get("warmConnection", True)
    }
    restarted = [
        {"id": result["id"], "ok": result.get("ok", False)}
        for result in results
        if result["id"] in warm_camera_ids
    ]
    if restarted:
        update_ingest_progress(warmConnectionsRestarted=restarted)
    return restarted


def ensure_warm_controllers(config, block=True):
    """Open and read-only probe every enabled Sony camera connection."""
    return warm_enabled_cameras(config, probe_status=True, block=block)


def initialize_cameras(config, open_operator_apps):
    results = warm_enabled_cameras(config, probe_status=False)
    required_camera_ids = {
        camera["id"]
        for camera in enabled_cameras(config)
        if camera.get("warmConnection", True)
    }
    required_results = [
        result for result in results if result["id"] in required_camera_ids
    ]
    for result in results:
        print(f"Warm camera connection {result['id']}: {'ready' if result.get('ok') else 'not ready; will fall back'}")
    all_ready = bool(required_results) and all(result.get("ok") for result in required_results)
    if all_ready and open_operator_apps:
        obs_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        subprocess.Popen(
            [
                "/usr/bin/open",
                "--env",
                f"PATH={obs_path}",
                "-b",
                "com.obsproject.obs-studio",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        open_dashboard(config)
    elif all_ready:
        print("FX30 background warm initialization completed; OBS and browser launch suppressed.")
    else:
        failed_ids = ", ".join(
            result["id"] for result in required_results if not result.get("ok")
        )
        print(f"FX30 dashboard not opened because warm connections remain unready: {failed_ids}.")


def run_camera_command(camera, action, timeout_seconds=4):
    if (
        action in {"start", "stop", "status", "ftp-test"}
        and camera.get("warmConnection", True)
        and camera.get("persistentWarmConnection", True)
    ):
        result = warm_controller_for(camera).run(action, timeout_seconds)
        return result

    key = f"{action}Command"
    command = camera.get(key)
    if not command:
        return {
            "id": camera["id"],
            "ok": False,
            "warning": f"No {key} configured",
        }

    started_at = time.time()
    try:
        env = sony_command_environment(command)
        if camera.get("password"):
            env["SONY_REC_PASSWORD"] = camera["password"]
        proc = subprocess.run(
            command,
            cwd=Path(command[0]).resolve().parent,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "id": camera["id"],
            "ok": False,
            "error": f"Camera command timed out after {timeout_seconds} seconds",
            "likelyCause": "The FX30 is waiting for pairing/authentication, is not reachable by the Sony SDK, or another app is already connected.",
            "command": command,
        }
    except Exception as exc:
        return {
            "id": camera["id"],
            "ok": False,
            "error": str(exc),
            "command": command,
        }

    result = {
        "id": camera["id"],
        "ok": proc.returncode == 0,
        "returnCode": proc.returncode,
        "elapsedSeconds": round(time.time() - started_at, 3),
    }

    if proc.stdout.strip():
        result["stdout"] = proc.stdout.strip()
    if proc.stderr.strip():
        result["stderr"] = proc.stderr.strip()
    return result


def refresh_camera_card_status(config, camera_id, restart_warm=True):
    """Read remaining record time without allowing a probe to interrupt work."""
    camera = next((item for item in enabled_cameras(config) if item["id"] == camera_id), None)
    if not camera:
        return {"ok": False, "error": "Camera is not enabled"}
    with STATE_LOCK:
        busy = bool(STATE["recording"] or STATE["ingest_running"] or STATE["record_action_running"])
        cached = (STATE.get("card_status") or {}).get(camera_id)
    if busy:
        return {
            **(cached or {"ok": False}),
            "stale": True,
            "message": "Card status is held while recording, copying, or changing camera state.",
        }

    controller = existing_warm_controller(camera_id)
    was_warm = bool(controller and controller.proc and controller.proc.poll() is None)
    if was_warm:
        controller.stop()
        time.sleep(config.get("modeSwitchDelaySeconds", 0.5))
    result = run_camera_command(camera, "cardStatus", timeout_seconds=35)
    status = parse_card_status_output(result.get("stdout", ""))
    status.update({"cameraId": camera_id, "checkedAt": time.time(), "stale": False})
    if not result.get("ok"):
        status["ok"] = False
        status["error"] = result.get("error") or result.get("warning") or status.get("error") or "Camera status request failed"
    if was_warm and restart_warm:
        Thread(
            target=warm_controller_for(camera).start,
            args=(config.get("commandTimeoutSeconds", 90),),
            daemon=True,
        ).start()
    with STATE_LOCK:
        STATE.setdefault("card_status", {})[camera_id] = status
    return status


def camera_card_backup_verified(config, camera_id):
    """Require every tracked camera backup to have verified SSD + LucidLink copies."""
    import cleanup_manager
    matching = []
    for session in cleanup_manager.load_manifests():
        camera = (session.get("cameras") or {}).get(camera_id)
        if camera:
            matching.append(camera)
    if not matching:
        return False, "No verified camera-card backups are recorded. Run Scan & Refresh first."
    unsafe = [
        item for item in matching
        if not item.get("deleted") and not (item.get("copyVerification") or {}).get("sameSize")
    ]
    if unsafe:
        return False, f"{len(unsafe)} tracked backup(s) are not verified in both SSD and LucidLink."
    return True, None


def camera_card_format_backup_authorized(config, camera_id, slot, body):
    verified, reason = camera_card_backup_verified(config, camera_id)
    if verified:
        return True, None
    expected = f"ERASE {camera_id.upper()} SLOT {slot} WITHOUT VERIFIED BACKUP"
    if body.get("backupOverrideAcknowledgment") == expected:
        return True, None
    return False, {
        "ok": False,
        "error": reason,
        "requiresBackupOverride": True,
    }


def format_camera_card(config, camera_id, slot):
    camera = next((item for item in enabled_cameras(config) if item["id"] == camera_id), None)
    if not camera:
        return {"ok": False, "error": "Camera is not enabled"}
    if not camera.get("mac"):
        return {"ok": False, "error": "Camera has no configured MAC; refusing to format without exact identity"}
    helper = Path(__file__).resolve().parent / "sony-rec"
    command = [
        str(helper), "--camera", camera_id, "--mac", camera["mac"],
        "--action", "format-card", "--slot", str(slot), "--confirm-format",
    ]
    env = sony_command_environment(command)
    if camera.get("password"):
        env["SONY_REC_PASSWORD"] = camera["password"]
    try:
        proc = subprocess.run(command, cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=15 * 60, env=env)
        return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Card format did not finish within 15 minutes"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_camera_command_streaming(camera, action, timeout_seconds=4):
    key = f"{action}Command"
    command = camera.get(key)
    if not command:
        return {
            "id": camera["id"],
            "ok": False,
            "warning": f"No {key} configured",
        }

    started_at = time.time()
    env = os.environ.copy()
    env["SONY_REC_NONINTERACTIVE"] = "1"
    if camera.get("password"):
        env["SONY_REC_PASSWORD"] = camera["password"]

    lines = queue.Queue()
    output = []
    try:
        proc = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as exc:
        return {
            "id": camera["id"],
            "ok": False,
            "error": str(exc),
            "command": command,
        }

    def read_output():
        try:
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))
        finally:
            lines.put(None)

    Thread(target=read_output, daemon=True).start()
    deadline = time.time() + timeout_seconds
    last_downloaded_bytes = None
    reader_done = False
    while time.time() < deadline:
        try:
            line = lines.get(timeout=0.25)
        except queue.Empty:
            downloaded_bytes = poll_ingest_file_size()
            if (
                action in {"ingest", "masterIngest"}
                and downloaded_bytes is not None
                and downloaded_bytes != last_downloaded_bytes
            ):
                last_downloaded_bytes = downloaded_bytes
                deadline = time.time() + timeout_seconds
            if proc.poll() is not None and reader_done:
                break
            continue

        if line is None:
            reader_done = True
            if proc.poll() is not None:
                break
            continue

        output.append(line)
        if action in {"ingest", "masterIngest"}:
            parse_ingest_line(line, camera)
            downloaded_bytes = poll_ingest_file_size()
            if (
                downloaded_bytes is not None
                and downloaded_bytes != last_downloaded_bytes
            ):
                last_downloaded_bytes = downloaded_bytes
                deadline = time.time() + timeout_seconds

        if proc.poll() is not None and reader_done:
            break

    if proc.poll() is None:
        proc.kill()
        timeout_description = (
            f"Camera download made no progress for {timeout_seconds} seconds"
            if action in {"ingest", "masterIngest"}
            else f"Camera command timed out after {timeout_seconds} seconds"
        )
        return {
            "id": camera["id"],
            "ok": False,
            "error": timeout_description,
            "stdout": "\n".join(output),
            "command": command,
        }

    return {
        "id": camera["id"],
        "ok": proc.returncode == 0,
        "returnCode": proc.returncode,
        "elapsedSeconds": round(time.time() - started_at, 3),
        "stdout": "\n".join(output),
    }


def run_rodecaster_ingest(config):
    rodecaster = config.get("rodecaster") or {}
    if not rodecaster.get("enabled", False):
        return {
            "ok": True,
            "skipped": True,
            "message": "RØDECaster ingest is disabled",
        }

    command = rodecaster.get("ingestCommand") or [
        "./transfer_and_ingest_rodecaster.command"
    ]
    timeout_seconds = int(rodecaster.get("timeoutSeconds", 1800))
    started_at = time.time()
    update_ingest_progress(
        status="rodecaster",
        message="Transferring newest RØDECaster recording",
    )
    try:
        proc = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"RØDECaster ingest timed out after {timeout_seconds} seconds",
            "command": command,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "command": command,
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    receipt = None
    json_start = stdout.find("{")
    if json_start >= 0:
        try:
            receipt = json.loads(stdout[json_start:])
        except json.JSONDecodeError:
            receipt = None

    result = {
        "ok": proc.returncode == 0,
        "returnCode": proc.returncode,
        "elapsedSeconds": round(time.time() - started_at, 3),
        "command": command,
        "stdout": stdout,
    }
    if stderr:
        result["stderr"] = stderr
    if receipt:
        result["receipt"] = receipt
        result["take"] = receipt.get("take")
        result["stagingDestination"] = receipt.get("stagingDestination")
        result["lucidDestination"] = receipt.get("lucidDestination")
    if proc.returncode != 0:
        result["error"] = stderr or stdout or "RØDECaster ingest failed"
    return result


def run_ingest_job(config):
    timeout_seconds = config.get("ingestTimeoutSeconds", 1800)
    payload = None
    restarted_warm = []
    try:
        stopped_warm = stop_warm_controllers_for_ingest(config)
        if stopped_warm:
            log_event("record/ingest released warm connection", {
                "ok": True,
                "stoppedWarmConnections": stopped_warm,
            })
        update_ingest_progress(
            status="connecting",
            message="Connecting in copy mode",
        )
        results = [
            run_camera_command_streaming(camera, "ingest", timeout_seconds)
            for camera in enabled_cameras(config)
        ]
        ok = all(result.get("ok") for result in results)
        warnings = [
            result.get("warning")
            for result in results
            if result.get("warning")
        ]
        payload = {
            "ok": ok,
            "action": "record/ingest",
            "cameraResults": results,
            "warnings": warnings,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "action": "record/ingest",
            "error": str(exc),
            "cameraResults": [],
            "warnings": [],
        }
    finally:
        results = payload.get("cameraResults", []) if payload else []
        ok = bool(payload and payload.get("ok"))
        downloaded_file = extract_downloaded_file(results)
        downloaded_files = extract_downloaded_files(results)
        delivery_config = config.get("lucidLink") or {}
        auto_delivery = bool(delivery_config.get("autoDeliverAfterIngest", True))
        local_verifications = [verify_local_media(item["source"]) for item in downloaded_files] if ok else []
        local_ok = bool(local_verifications) and all(item.get("ok") for item in local_verifications)
        payload["localVerifications"] = local_verifications
        delivery_items = downloaded_files + (obs_delivery_items(config) if ok else [])
        delivery_results = copy_to_lucidlink(config, delivery_items) if ok and local_ok and auto_delivery else []
        delivery_ok = all(result.get("ok") for result in delivery_results)
        rodecaster_result = run_rodecaster_ingest(config)
        rodecaster_ok = bool(rodecaster_result.get("ok"))
        payload["rodecasterResult"] = rodecaster_result
        payload["rodecasterOk"] = rodecaster_ok
        email_result = (
            send_ingest_email(config, delivery_results)
            if ok and local_ok and delivery_ok and auto_delivery
            else {
                "ok": True,
                "skipped": True,
                "reason": (
                    "Email skipped because camera ingest or local verification failed"
                    if not ok or not local_ok
                    else "Email skipped because manual LucidLink delivery is selected"
                ),
            }
        )
        email_ok = bool(email_result is None or email_result.get("ok"))
        final_ok = ok and local_ok and (delivery_ok if auto_delivery else True) and rodecaster_ok
        restarted_warm = restart_warm_controllers_after_ingest(config)
        if restarted_warm:
            payload["warmConnectionsRestarted"] = restarted_warm
        if delivery_results:
            payload["deliveryResults"] = delivery_results
            payload["deliveryOk"] = delivery_ok
        if email_result:
            payload["emailResult"] = email_result
            payload["emailOk"] = email_ok
        if not rodecaster_ok:
            rodecaster_error = (
                rodecaster_result.get("error")
                or "RØDECaster ingest needs attention"
            )
            message = f"Camera/OBS copy finished; {rodecaster_error}"
        elif ok and local_ok and downloaded_file:
            if not auto_delivery:
                message = f"Local SSD copy verified: {Path(downloaded_file).name}; ready for COPY SSD TO LUCIDLINK"
            elif delivery_results and delivery_ok:
                delivered_name = Path(delivery_results[0]["destination"]).name
                message = f"Delivered {delivered_name} to {delivery_label(config)}"
                if not email_ok:
                    message += "; email failed"
            elif delivery_results:
                delivery_error = delivery_results[0].get("error")
                message = delivery_error or (
                    f"Downloaded {Path(downloaded_file).name}; {delivery_label(config)} delivery failed"
                )
            else:
                message = f"Downloaded {Path(downloaded_file).name}"
        elif ok:
            take = rodecaster_result.get("take")
            message = (
                f"File sequence complete, including RØDECaster {take}"
                if take
                else "File sequence complete, including RØDECaster"
            )
        else:
            message = "Download failed"
        if final_ok:
            update_ingest_progress(
                status="complete",
                percent=100,
                message=message,
                finishedAt=time.time(),
            )
        else:
            update_ingest_progress(
                status="failed",
                message=message,
                finishedAt=time.time(),
            )
        with STATE_LOCK:
            STATE["ingest_running"] = False
            STATE["last_ingest_finished"] = time.time()
            STATE["last_ingest_results"] = results
            STATE["last_ingest_ok"] = final_ok
            STATE["last_ingest_file"] = downloaded_file
            STATE["last_ingest_message"] = message
            STATE["last_delivery_results"] = delivery_results
            STATE["last_email_result"] = email_result
            STATE["last_email_ok"] = email_ok if email_result is not None else None
            STATE["last_action"] = "ingest"
            STATE["last_results"] = results
        log_event("record/ingest finished", payload)
        notify_mac("FX30 ingest", message, config.get("macNotifications", True))


def run_delivery_job(config):
    with STATE_LOCK:
        source_path = STATE.get("last_ingest_file")
        local_ingest_ok = STATE.get("last_ingest_ok") is True
    verification = verify_local_media(source_path) if source_path and local_ingest_ok else {
        "ok": False,
        "path": str(source_path or ""),
        "error": "Run COPY CAMERA TO SSD successfully before LucidLink delivery",
    }
    cameras = enabled_cameras(config)
    camera_id = cameras[0].get("id") if len(cameras) == 1 else None
    delivery_results = []
    if verification.get("ok") and camera_id:
        delivery_results = copy_to_lucidlink(config, [{"cameraId": camera_id, "source": source_path}])
        for result in delivery_results:
            if not result.get("ok"):
                continue
            destination = result.get("destination")
            try:
                destination_hash = sha256_file(destination)
                result["sha256"] = destination_hash
                if destination_hash != verification.get("sha256"):
                    result["ok"] = False
                    result["error"] = "LucidLink copy hash does not match the verified SSD source"
            except Exception as exc:
                result["ok"] = False
                result["error"] = f"LucidLink hash verification failed: {exc}"
    delivery_ok = bool(delivery_results) and all(item.get("ok") for item in delivery_results)
    if delivery_ok:
        destination = delivery_results[0]["destination"]
        message = f"Verified LucidLink copy: {Path(destination).name}"
    else:
        message = verification.get("error") or (
            delivery_results[0].get("error") if delivery_results else "LucidLink delivery failed"
        )
    with STATE_LOCK:
        STATE["ingest_running"] = False
        STATE["last_ingest_finished"] = time.time()
        STATE["last_delivery_results"] = delivery_results
        STATE["last_ingest_message"] = message
        STATE["last_action"] = "deliver"
        STATE["ingest_progress"] = {
            "status": "complete" if delivery_ok else "failed",
            "message": message,
            "percent": 100 if delivery_ok else None,
            "finishedAt": time.time(),
        }
    log_event("record/deliver finished", {
        "ok": delivery_ok,
        "action": "record/deliver",
        "localVerification": verification,
        "deliveryResults": delivery_results,
        "message": message,
    })
    notify_mac("FX30 LucidLink delivery", message, config.get("macNotifications", True))


def run_master_recovery_job(config, camera_id=None):
    results = []
    verification = {"ok": False, "error": "Emergency master recovery did not run"}
    restarted_warm = []
    target_cameras = enabled_cameras(config)
    if camera_id:
        target_cameras = [camera for camera in target_cameras if camera.get("id") == camera_id]
    try:
        stop_warm_controllers_for_ingest(config)
        results = [
            run_camera_command_streaming(camera, "masterIngest", config.get("ingestTimeoutSeconds", 1800))
            for camera in target_cameras
        ]
        downloaded_file = extract_downloaded_file(results)
        verification = verify_local_media(downloaded_file) if downloaded_file else {
            "ok": False,
            "error": "Sony did not return a completed master file path",
        }
        if verification.get("ok") and (
            verification.get("width", 0) < 3840 or verification.get("height", 0) < 2160
        ):
            verification["ok"] = False
            verification["error"] = (
                f"Refusing recovered file because it is {verification.get('width')}x{verification.get('height')}, not a 4K master"
            )
        master_ok = bool(results) and all(item.get("ok") for item in results) and verification.get("ok") is True
        if master_ok:
            target_label = camera_id or "all enabled cameras"
            message = f"Emergency 4K master verified locally for {target_label}: {Path(downloaded_file).name}"
        else:
            message = verification.get("error") or "Emergency 4K master recovery failed"
    except Exception as exc:
        downloaded_file = None
        master_ok = False
        message = f"Emergency 4K master recovery failed: {exc}"
    finally:
        restarted_warm = restart_warm_controllers_after_ingest(config)
        with STATE_LOCK:
            STATE["ingest_running"] = False
            STATE["last_ingest_finished"] = time.time()
            STATE["last_master_file"] = downloaded_file
            STATE["last_master_camera_id"] = camera_id
            STATE["last_master_ok"] = master_ok
            STATE["last_master_message"] = message
            STATE["last_ingest_message"] = message
            STATE["last_action"] = "recover-master"
            STATE["ingest_progress"] = {
                "status": "complete" if master_ok else "failed",
                "message": message,
                "percent": 100 if master_ok else None,
                "finishedAt": time.time(),
            }
        log_event("record/recover-master finished", {
            "ok": master_ok,
            "action": "record/recover-master",
            "cameraId": camera_id,
            "cameraResults": results,
            "verification": verification,
            "warmConnectionsRestarted": restarted_warm,
            "message": message,
        })
        notify_mac("FX30 emergency master recovery", message, config.get("macNotifications", True))


def schedule_auto_ingest_after_stop(config, stopped_at):
    delay_seconds = int(config.get("autoIngestAfterStopSeconds", 0))
    if delay_seconds <= 0:
        return False

    scheduled_at = time.time() + delay_seconds
    with STATE_LOCK:
        STATE["auto_ingest_scheduled_at"] = scheduled_at
        STATE["auto_ingest_stop_time"] = stopped_at

    def delayed_ingest():
        time.sleep(delay_seconds)
        skip_reason = None
        with STATE_LOCK:
            if STATE.get("last_stop_time") != stopped_at:
                skip_reason = "A newer stop cycle replaced this schedule"
            elif STATE.get("recording") or STATE.get("record_action_running"):
                skip_reason = "Recording restarted before automatic ingest"
            elif STATE.get("ingest_running"):
                skip_reason = "An ingest is already running"
            elif (
                STATE.get("last_ingest_started")
                and STATE["last_ingest_started"] >= stopped_at
            ):
                skip_reason = "COPY FILES already ran after this stop"
            else:
                STATE["ingest_running"] = True
                STATE["last_ingest_started"] = time.time()
                STATE["last_ingest_finished"] = None
                STATE["last_action"] = "auto_ingest"
                STATE["last_email_result"] = None
                STATE["last_email_ok"] = None
                STATE["ingest_progress"] = {
                    "status": "starting",
                    "message": "Automatic COPY FILES after record stop",
                    "startedAt": STATE["last_ingest_started"],
                    "elapsedSeconds": 0,
                    "percent": None,
                    "sizeBytes": None,
                    "downloadedBytes": None,
                    "etaSeconds": None,
                }
            STATE["auto_ingest_scheduled_at"] = None
            STATE["auto_ingest_stop_time"] = None

        if skip_reason:
            log_event("record/auto-ingest skipped", {
                "ok": True,
                "skipped": True,
                "reason": skip_reason,
                "stoppedAt": stopped_at,
            })
            return

        log_event("record/auto-ingest started", {
            "ok": True,
            "delaySeconds": delay_seconds,
            "stoppedAt": stopped_at,
        })
        run_ingest_job(config)

    Thread(target=delayed_ingest, daemon=True).start()
    log_event("record/auto-ingest scheduled", {
        "ok": True,
        "delaySeconds": delay_seconds,
        "scheduledAt": scheduled_at,
        "stoppedAt": stopped_at,
    })
    return True


def run_record_action_job(config, action):
    timeout_seconds = config.get("commandTimeoutSeconds", 4)
    payload = None
    with STATE_LOCK:
        session_started_at = STATE.get("recording_started_at")
    try:
        results = [
            run_camera_command(camera, action, timeout_seconds)
            for camera in enabled_cameras(config)
        ]
        ok = all(result.get("ok") for result in results)
        warnings = [
            result.get("warning")
            for result in results
            if result.get("warning")
        ]
        payload = {
            "ok": ok,
            "action": f"record/{action}",
            "cameraResults": results,
            "warnings": warnings,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "action": f"record/{action}",
            "error": str(exc),
            "cameraResults": [],
            "warnings": [],
        }
    finally:
        results = payload.get("cameraResults", []) if payload else []
        ok = bool(payload and payload.get("ok"))
        with STATE_LOCK:
            if action == "start":
                STATE["recording"] = ok
                STATE["recording_started_at"] = time.time() if ok else None
            if action == "stop":
                STATE["recording"] = False
                STATE["recording_started_at"] = None
                STATE["last_stop_time"] = time.time()
            STATE["record_action_running"] = False
            STATE["record_action"] = action
            STATE["last_record_action_finished"] = time.time()
            STATE["last_action"] = action
            STATE["last_results"] = results
        # Keep the Remote-mode controller alive between takes by default. Copy
        # and recovery jobs explicitly release it when they need Contents
        # Transfer mode, so dropping it after every STOP only adds a cold Sony
        # reconnect to the next RECORD.
        if action == "stop" and ok and config.get("releaseWarmAfterStop", False):
            stopped = stop_warm_controllers(config, "Ready for copy mode")
            if stopped:
                payload["warmConnectionsStoppedAfterStop"] = stopped
        if action == "stop" and ok:
            native_ftp_started = start_native_ftp_ingest(
                config,
                session_started_at or time.time(),
            )
            if native_ftp_started:
                payload["nativeFtpWatchStarted"] = True
            elif schedule_auto_ingest_after_stop(
                config,
                STATE["last_stop_time"],
            ):
                payload["autoIngestScheduledSeconds"] = int(
                    config.get("autoIngestAfterStopSeconds", 0)
                )
        if action in {"start", "stop"} and not ok:
            failed_ids = [result.get("id", "unknown camera") for result in results if not result.get("ok")]
            verb = "Start" if action == "start" else "Stop"
            notify_studio_event(
                f"fx30-{action}-failed",
                f"FX30 {action.upper()} command failed",
                f"{verb} failed for: {', '.join(failed_ids) or 'unknown camera'}",
                config.get("macNotifications", True),
            )
        log_event(f"record/{action} finished", payload)


def stable_files_snapshot(folder, extensions, stable_seconds):
    path = Path(folder)
    try:
        if not path.exists():
            return [], None

        now = time.time()
        files = []
        for item in path.iterdir():
            if not item.is_file():
                continue
            if item.name.startswith("._"):
                continue
            if extensions and item.suffix.lower() not in extensions:
                continue
            stat = item.stat()
            if now - stat.st_mtime < stable_seconds:
                continue
            files.append({
                "name": item.name,
                "path": str(item),
                "sizeBytes": stat.st_size,
                "modifiedAt": int(stat.st_mtime),
            })
        return sorted(files, key=lambda f: f["modifiedAt"], reverse=True), None
    except OSError as exc:
        return [], str(exc)


def list_stable_files(folder, extensions, stable_seconds):
    files, _ = stable_files_snapshot(folder, extensions, stable_seconds)
    return files


def wait_for_native_ftp_files(config, session_started_at):
    ftp_config = config.get("nativeFtp") or {}
    timeout_seconds = int(ftp_config.get("arrivalTimeoutSeconds", 1800))
    poll_seconds = max(1, int(ftp_config.get("pollSeconds", 5)))
    stable_seconds = int(config.get("stableFileSeconds", 30))
    extensions = {
        ext.lower()
        for ext in config.get("proxyExtensions", [".mp4", ".mxf"])
    }
    cameras = enabled_cameras(config)
    deadline = time.time() + timeout_seconds
    found = {}

    while time.time() < deadline:
        for camera in cameras:
            candidates = list_stable_files(
                camera.get("ingestFolder", ""),
                extensions,
                stable_seconds,
            )
            candidate = next(
                (
                    item for item in candidates
                    if item["modifiedAt"] >= int(session_started_at) - 2
                ),
                None,
            )
            if candidate:
                found[camera["id"]] = candidate

        update_ingest_progress(
            status="waiting_for_ftp",
            camerasReady=len(found),
            camerasExpected=len(cameras),
            message=f"Native FTP files ready: {len(found)}/{len(cameras)} cameras",
        )
        if len(found) == len(cameras):
            break
        time.sleep(poll_seconds)

    return [{
        "id": camera["id"],
        "ok": camera["id"] in found,
        "recordingState": (
            "ftp_file_ready" if camera["id"] in found else "ftp_file_timeout"
        ),
        **(
            {
                "file": found[camera["id"]]["path"],
                "sizeBytes": found[camera["id"]]["sizeBytes"],
            }
            if camera["id"] in found
            else {
                "error": (
                    f"No stable FTP movie appeared in "
                    f"{camera.get('ingestFolder')} within {timeout_seconds} seconds"
                )
            }
        ),
    } for camera in cameras]


def run_native_ftp_ingest_job(config, session_started_at):
    results = wait_for_native_ftp_files(config, session_started_at)
    arrived = [
        {"cameraId": result["id"], "source": result["file"]}
        for result in results
        if result.get("ok") and result.get("file")
    ]
    delivery_results = copy_to_lucidlink(config, arrived)
    arrival_ok = bool(results) and all(result.get("ok") for result in results)
    delivery_ok = bool(delivery_results) and all(
        result.get("ok") for result in delivery_results
    )
    email_result = (
        send_ingest_email(config, delivery_results)
        if arrival_ok and delivery_ok
        else {
            "ok": True,
            "skipped": True,
            "reason": "Email skipped because FTP arrival or LucidLink delivery failed",
        }
    )
    final_ok = arrival_ok and delivery_ok
    message = (
        f"Native FTP delivered {len(delivery_results)} camera file(s) to LucidLink"
        if final_ok
        else "Native FTP ingest needs attention"
    )
    update_ingest_progress(
        status="complete" if final_ok else "failed",
        percent=100 if final_ok else None,
        message=message,
        finishedAt=time.time(),
    )
    with STATE_LOCK:
        STATE["ingest_running"] = False
        STATE["last_ingest_finished"] = time.time()
        STATE["last_ingest_results"] = results
        STATE["last_ingest_ok"] = final_ok
        STATE["last_ingest_file"] = arrived[0]["source"] if arrived else None
        STATE["last_ingest_message"] = message
        STATE["last_delivery_results"] = delivery_results
        STATE["last_email_result"] = email_result
        STATE["last_email_ok"] = bool(email_result.get("ok"))
        STATE["last_action"] = "ftp_ingest"
    log_event("native FTP ingest finished", {
        "ok": final_ok,
        "cameraResults": results,
        "deliveryResults": delivery_results,
        "emailResult": email_result,
    })
    notify_mac("FX30 native FTP", message, config.get("macNotifications", True))


def start_native_ftp_ingest(config, session_started_at):
    if not (config.get("nativeFtp") or {}).get("enabled", False):
        return False
    with STATE_LOCK:
        if STATE["ingest_running"]:
            return False
        STATE["ingest_running"] = True
        STATE["last_ingest_started"] = time.time()
        STATE["last_ingest_finished"] = None
        STATE["last_email_result"] = None
        STATE["last_email_ok"] = None
        STATE["ingest_progress"] = {
            "status": "waiting_for_ftp",
            "message": "Waiting for native FTP files from the cameras",
            "startedAt": STATE["last_ingest_started"],
            "elapsedSeconds": 0,
            "percent": None,
        }
    Thread(
        target=run_native_ftp_ingest_job,
        args=(config, session_started_at),
        daemon=True,
    ).start()
    return True


class BridgeHandler(BaseHTTPRequestHandler):
    config = None
    config_path = None

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        client_ip = self.client_address[0]
        path = urlparse(self.path).path

        # 1. Enforce IP Filtering Security Rules
        if not is_allowed_ip(client_ip):
            log_audit(client_ip, "ACCESS_DENIED", path, f"Access blocked to IP: {client_ip}")
            json_response(self, 403, {"ok": False, "error": "Forbidden: Access restricted to local and secure VPN interfaces."})
            return

        # 2. Audit State-Changing Actions
        state_changing = (
            "/record/start", "/record/stop", "/record/warm", "/record/ftp-test",
            "/record/ingest", "/record/deliver", "/record/recover-master", "/record/cleanup-scan",
            "/record/cleanup-delete", "/record/cleanup-delete-all", "/record/format-card",
            "/record/rodecaster-archive", "/settings/email", "/open/fx30-folder", "/open/studio-folder"
        )
        if path in state_changing:
            log_audit(client_ip, f"ACTION_{self.command}_{path.replace('/', '_').upper()}", path, f"HTTP method: {self.command}")

        if path == "/":
            html_response(self, 200, DASHBOARD_HTML)
            return
        if path == "/health":
            json_response(self, 200, {"ok": True})
            return
        if path == "/studio/ready":
            self.handle_studio_ready()
            return
        if path == "/record/start":
            self.handle_record_action("start")
            return
        if path == "/record/stop":
            self.handle_record_action("stop")
            return
        if path == "/record/warm" and self.command == "POST":
            self.handle_warm_connections()
            return
        if path == "/record/ftp-test":
            self.handle_record_action("ftp-test")
            return
        if path == "/record/ingest":
            self.handle_ingest()
            return
        if path == "/record/deliver":
            self.handle_delivery()
            return
        if path == "/record/recover-master":
            self.handle_master_recovery()
            return
        if path == "/record/status":
            self.handle_status()
            return
        if path == "/record/rodecaster-archive" and self.command == "POST":
            self.handle_rodecaster_archive()
            return
        if path == "/record/card-status" and self.command == "POST":
            self.handle_card_status()
            return
        if path == "/record/format-card" and self.command == "POST":
            self.handle_format_card()
            return
        if path == "/record/cleanup-scan" and self.command == "POST":
            self.handle_cleanup_scan()
            return
        if path == "/record/cleanup-report" and self.command == "GET":
            self.handle_cleanup_report()
            return
        if path == "/record/cleanup-delete" and self.command == "POST":
            self.handle_cleanup_delete()
            return
        if path == "/record/cleanup-delete-all" and self.command == "POST":
            self.handle_cleanup_delete_all()
            return
        if path == "/open/fx30-folder":
            self.handle_open_fx30_folder()
            return
        if path == "/open/studio-folder":
            self.handle_open_studio_folder()
            return
        if path == "/settings/email" and self.command == "POST":
            self.handle_email_settings()
            return
        json_response(self, 404, {"ok": False, "error": "Unknown endpoint"})

    def handle_rodecaster_archive(self):
        readiness = rodecaster_archive_readiness()
        if not readiness.get("ready"):
            json_response(self, 409, {"ok": False, "error": readiness.get("message", "RØDECaster archive is not ready."), "rodecasterArchive": readiness})
            return
        try:
            request = Request(
                V4_RODECASTER_ARCHIVE_URL,
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status < 200 or response.status >= 300 or not payload.get("ok"):
                    raise RuntimeError(payload.get("error") or f"V4 returned HTTP {response.status}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            json_response(self, 502, {"ok": False, "error": f"V4 rejected RØDECaster archive: {detail}"})
            return
        except Exception as exc:
            json_response(self, 502, {"ok": False, "error": f"RØDECaster archive could not be queued: {exc}"})
            return
        json_response(self, 200, {"ok": True, "message": "RØDECaster archive queued. Watch this status until V4 reports verified completion.", "rodecasterArchive": payload})

    def handle_open_fx30_folder(self):
        query = parse_qs(urlparse(self.path).query)
        requested_camera = (query.get("camera") or [None])[0]
        camera_ids = {
            camera["id"] for camera in enabled_cameras(self.config)
        }
        if requested_camera not in camera_ids:
            json_response(self, 400, {
                "ok": False,
                "error": "Unknown camera folder",
            })
            return
        folder = camera_delivery_folder(self.config, requested_camera)
        if not folder or not folder.exists():
            json_response(self, 404, {
                "ok": False,
                "error": "The LucidLink folder is not available.",
            })
            return
        subprocess.Popen(
            ["/usr/bin/open", str(folder)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        html_response(
            self,
            200,
            """<!doctype html><html><body style="font-family:-apple-system,sans-serif;padding:32px">
            <h1>FX30 folder opened</h1><p>You can close this page and use Finder.</p>
            </body></html>""",
        )

    def handle_open_studio_folder(self):
        try:
            with urlopen(Request(V4_STATUS_URL, method="GET"), timeout=5) as response:
                status = json.loads(response.read().decode("utf-8"))
            roots = [Path(status[key]) for key in ("deliveryRoot", "proxyDeliveryRoot")]
            common_root = Path(os.path.commonpath([str(path) for path in roots]))
            if (
                status.get("localOnly") is not False
                or (status.get("delivery") or {}).get("provider") != "LucidLink"
                or common_root.parts[1:2] != ("Volumes",)
                or not common_root.is_dir()
            ):
                raise RuntimeError("The verified LucidLink delivery folder is not available.")
            subprocess.Popen(
                ["/usr/bin/open", str(common_root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            html_response(
                self,
                200,
                """<!doctype html><html><body style="font-family:-apple-system,sans-serif;padding:32px">
                <h1>LucidLink delivery opened</h1><p>You can close this page and use Finder.</p>
                </body></html>""",
            )
        except Exception as exc:
            json_response(self, 503, {"ok": False, "error": str(exc)})

    def handle_studio_ready(self):
        lucid = self.config.get("lucidLink") or {}
        root = Path(lucid.get("deliveryRoot") or "")
        mounted = False
        if root.is_absolute() and len(root.parts) >= 3 and root.parts[1] == "Volumes":
            mounted = any(
                candidate != Path("/Volumes") and os.path.ismount(candidate)
                for candidate in [root, *root.parents]
            ) and root.exists()
        app_name = lucid.get("appName", "LucidLink")
        launched = False
        if lucid.get("openAppOnReady", True):
            proc = subprocess.run(["/usr/bin/open", "-a", app_name], capture_output=True, text=True)
            launched = proc.returncode == 0
        json_response(self, 200 if mounted else 503, {
            "ok": mounted,
            "lucidLinkMounted": mounted,
            "lucidLinkPath": str(root),
            "appOpened": launched,
            "message": "LucidLink is ready" if mounted else "LucidLink opened; connect the filespace, then press Ready again."
        })

    def handle_email_settings(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            request_data = json.loads(self.rfile.read(content_length) or b"{}")
            recipients = request_data.get("recipients")
            if not isinstance(recipients, list):
                raise ValueError("Recipients must be a list.")
            recipients = list(dict.fromkeys(
                str(value).strip()
                for value in recipients
                if str(value).strip()
            ))
            if not recipients:
                raise ValueError("Add at least one email address.")
            invalid = [
                value for value in recipients
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value)
            ]
            if invalid:
                raise ValueError(f"Check this email address: {invalid[0]}")

            with CONFIG_LOCK:
                self.config.setdefault("email", {})["to"] = recipients
                temporary_path = self.config_path.with_suffix(
                    self.config_path.suffix + ".tmp"
                )
                temporary_path.write_text(
                    json.dumps(self.config, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, self.config_path)
            json_response(self, 200, {
                "ok": True,
                "recipients": recipients,
            })
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def handle_record_action(self, action):
        with STATE_LOCK:
            if STATE["record_action_running"]:
                running_for = None
                if STATE["last_record_action_started"]:
                    running_for = round(time.time() - STATE["last_record_action_started"], 1)
                payload = {
                    "ok": True,
                    "action": f"record/{action}",
                    "started": False,
                    "message": f"Record {STATE['record_action']} already running",
                    "runningForSeconds": running_for,
                    "state": dict(STATE),
                }
                log_event(f"record/{action} already running", payload)
                json_response(self, 200, payload)
                return

            STATE["record_action_running"] = True
            STATE["record_action"] = action
            STATE["last_record_action_started"] = time.time()
            STATE["last_record_action_finished"] = None
            STATE["last_action"] = action

        thread = Thread(target=run_record_action_job, args=(self.config, action), daemon=True)
        thread.start()

        payload = {
            "ok": True,
            "action": f"record/{action}",
            "started": True,
            "message": f"Record {action} started in background. Check /record/status or the bridge terminal for results.",
        }
        log_event(f"record/{action} started", payload)
        json_response(self, 200, payload)

    def handle_warm_connections(self):
        with STATE_LOCK:
            blocked_by = []
            if STATE["recording"]:
                blocked_by.append("FX30 recording is active")
            if STATE["record_action_running"]:
                blocked_by.append("an FX30 record action is running")
            if STATE["ingest_running"]:
                blocked_by.append("FX30 ingest is running")
        if blocked_by:
            json_response(self, 409, {
                "ok": False,
                "action": "record/warm",
                "error": "Warm reconnect refused: " + "; ".join(blocked_by),
            })
            return

        results = ensure_warm_controllers(self.config, block=False)
        if results is None:
            json_response(self, 409, {
                "ok": False,
                "action": "record/warm",
                "error": "Warm reconnect already in progress",
            })
            return
        ok = bool(results) and all(result.get("ok") for result in results)
        payload = {
            "ok": ok,
            "action": "record/warm",
            "cameraResults": results,
            "message": "Enabled FX30 warm connections are ready" if ok else "One or more FX30 warm connections are not ready",
        }
        log_event("record/warm finished", payload)
        json_response(self, 200 if ok else 503, payload)

    def handle_ingest(self):
        with STATE_LOCK:
            if STATE["ingest_running"]:
                running_for = None
                if STATE["last_ingest_started"]:
                    running_for = round(time.time() - STATE["last_ingest_started"], 1)
                payload = {
                    "ok": True,
                    "action": "record/ingest",
                    "started": False,
                    "message": "Ingest already running",
                    "runningForSeconds": running_for,
                    "state": dict(STATE),
                }
                log_event("record/ingest already running", payload)
                json_response(self, 200, payload)
                return

            STATE["ingest_running"] = True
            STATE["last_ingest_started"] = time.time()
            STATE["last_ingest_finished"] = None
            STATE["last_action"] = "ingest"
            STATE["last_email_result"] = None
            STATE["last_email_ok"] = None
            STATE["ingest_progress"] = {
                "status": "starting",
                "message": "Preparing copy",
                "startedAt": STATE["last_ingest_started"],
                "elapsedSeconds": 0,
                "percent": None,
                "sizeBytes": None,
                "downloadedBytes": None,
                "etaSeconds": None,
            }

        thread = Thread(target=run_ingest_job, args=(self.config,), daemon=True)
        thread.start()

        payload = {
            "ok": True,
            "action": "record/ingest",
            "started": True,
            "message": "Ingest started in background. Check /record/status or the bridge terminal for results.",
        }
        log_event("record/ingest started", payload)
        json_response(self, 200, payload)

    def handle_delivery(self):
        with STATE_LOCK:
            if STATE["ingest_running"]:
                json_response(self, 409, {"ok": False, "error": "A copy is already running"})
                return
            if STATE.get("recording") or STATE.get("record_action_running"):
                json_response(self, 409, {"ok": False, "error": "LucidLink delivery is blocked while camera recording state is changing"})
                return
            STATE["ingest_running"] = True
            STATE["last_ingest_started"] = time.time()
            STATE["last_action"] = "deliver"
            STATE["ingest_progress"] = {
                "status": "starting",
                "message": "Verifying SSD file before LucidLink copy",
                "startedAt": STATE["last_ingest_started"],
                "percent": None,
            }
        Thread(target=run_delivery_job, args=(self.config,), daemon=True).start()
        payload = {
            "ok": True,
            "action": "record/deliver",
            "started": True,
            "message": "LucidLink delivery started in background. Check /record/status for verification.",
        }
        log_event("record/deliver started", payload)
        json_response(self, 200, payload)

    def handle_master_recovery(self):
        query = parse_qs(urlparse(self.path).query)
        requested_camera = (query.get("camera") or [None])[0]
        enabled_ids = {camera.get("id") for camera in enabled_cameras(self.config)}
        if requested_camera and requested_camera not in enabled_ids:
            json_response(self, 400, {
                "ok": False,
                "error": f"{requested_camera} is not enabled",
            })
            return
        with STATE_LOCK:
            if STATE["ingest_running"]:
                json_response(self, 409, {"ok": False, "error": "A copy is already running"})
                return
            if STATE.get("recording") or STATE.get("record_action_running"):
                json_response(self, 409, {"ok": False, "error": "Emergency recovery is blocked until recording is confirmed stopped"})
                return
            STATE["ingest_running"] = True
            STATE["last_ingest_started"] = time.time()
            STATE["last_action"] = "recover-master"
            STATE["ingest_progress"] = {
                "status": "starting",
                "message": f"Emergency recovery: locating newest 4K master for {requested_camera or 'all enabled cameras'}",
                "startedAt": STATE["last_ingest_started"],
                "percent": None,
            }
        Thread(target=run_master_recovery_job, args=(self.config, requested_camera), daemon=True).start()
        payload = {
            "ok": True,
            "action": "record/recover-master",
            "cameraId": requested_camera,
            "started": True,
            "message": "Emergency 4K master recovery started. This is manual and local-only.",
        }
        log_event("record/recover-master started", payload)
        json_response(self, 200, payload)

    def handle_status(self):
        with STATE_LOCK:
            state = dict(STATE)

        now = time.time()
        if state.get("recording") and state.get("recording_started_at"):
            state["recordingElapsedSeconds"] = round(
                max(0, now - state["recording_started_at"]), 1
            )
            state["recordingElapsed"] = format_recording_time(
                state["recordingElapsedSeconds"]
            )
        else:
            state["recordingElapsedSeconds"] = 0
            state["recordingElapsed"] = "00:00:00"
        storage = storage_snapshot(self.config)

        ingest = []
        camera_status = []
        extensions = {
            ext.lower()
            for ext in self.config.get("proxyExtensions", [])
        }
        stable_seconds = self.config.get("stableFileSeconds", 30)
        for camera in enabled_cameras(self.config):
            controller = existing_warm_controller(camera["id"])
            warm_running = bool(
                controller
                and controller.proc
                and controller.proc.poll() is None
            )
            camera_status.append({
                "id": camera["id"],
                "enabled": True,
                "warmConnection": warm_running,
                "warmStartedAt": controller.started_at if controller else None,
            })
            delivery_folder = camera_delivery_folder(self.config, camera["id"])
            stable_files, folder_error = stable_files_snapshot(
                camera.get("ingestFolder", ""),
                extensions,
                stable_seconds,
            )
            ingest.append({
                "id": camera["id"],
                "folder": camera.get("ingestFolder"),
                "deliveryFolder": str(delivery_folder) if delivery_folder else None,
                "masterFolder": camera.get("masterIngestFolder"),
                "stableFiles": stable_files[:5],
                "folderError": folder_error,
            })

        if state["last_stop_time"]:
            elapsed = time.time() - state["last_stop_time"]
            state["postStopDebounceComplete"] = (
                elapsed >= self.config.get("postStopDebounceSeconds", 10)
            )
            state["secondsSinceStop"] = round(elapsed, 1)
        if state.get("auto_ingest_scheduled_at"):
            state["autoIngestStartsInSeconds"] = max(
                0,
                int(round(state["auto_ingest_scheduled_at"] - now)),
            )
        else:
            state["autoIngestStartsInSeconds"] = None
        if state["ingest_running"] and state["last_ingest_started"]:
            state["ingestRunningForSeconds"] = round(time.time() - state["last_ingest_started"], 1)
        if state["record_action_running"] and state["last_record_action_started"]:
            state["recordActionRunningForSeconds"] = round(time.time() - state["last_record_action_started"], 1)

        # Load cached manifests
        import cleanup_manager
        manifests = cleanup_manager.load_manifests()
        rodecaster_archive = rodecaster_archive_readiness()

        client_ip = self.client_address[0]
        json_response(self, 200, {
            "ok": True,
            "state": state,
            "cameraStatus": camera_status,
            "ingest": ingest,
            "storage": storage,
            "emailRecipients": (self.config.get("email") or {}).get("to", []),
            "cleanupManifests": manifests,
            "cardStatus": state.get("card_status") or {},
            "rodecasterArchive": rodecaster_archive,
            "visitorType": get_visitor_type(client_ip),
            "visitorIp": client_ip,
        })

    def handle_card_status(self):
        requested_camera = None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            requested_camera = (json.loads(self.rfile.read(content_length) or b"{}") or {}).get("cameraId")
        except (ValueError, json.JSONDecodeError):
            pass
        camera_ids = [camera["id"] for camera in enabled_cameras(self.config)]
        if requested_camera:
            camera_ids = [requested_camera] if requested_camera in camera_ids else []
        if not camera_ids:
            json_response(self, 400, {"ok": False, "error": "Unknown camera"})
            return
        reports = {camera_id: refresh_camera_card_status(self.config, camera_id) for camera_id in camera_ids}
        json_response(self, 200, {"ok": all(item.get("ok") for item in reports.values()), "cards": reports})

    def handle_format_card(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(content_length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            json_response(self, 400, {"ok": False, "error": "Invalid JSON"})
            return
        camera_id = body.get("cameraId")
        slot = body.get("slot")
        expected_acknowledgment = f"FORMAT SLOT {slot}"
        if slot not in {1, 2} or body.get("acknowledgment") != expected_acknowledgment:
            json_response(self, 400, {"ok": False, "error": f"Type {expected_acknowledgment} exactly to continue."})
            return
        if body.get("highResAcknowledgment") != "I HAVE MANUALLY VERIFIED THE HIGH-RES FOOTAGE":
            json_response(self, 400, {"ok": False, "error": "Manual high-resolution-footage verification is required."})
            return
        with STATE_LOCK:
            if STATE["recording"] or STATE["ingest_running"] or STATE["record_action_running"]:
                json_response(self, 409, {"ok": False, "error": "Format is unavailable while recording, copying, or changing camera state."})
                return
        authorized, failure = camera_card_format_backup_authorized(self.config, camera_id, slot, body)
        if not authorized:
            json_response(self, 409, failure)
            return
        report = refresh_camera_card_status(self.config, camera_id, restart_warm=False)
        slot_report = next((item for item in report.get("slots", []) if item.get("slot") == slot), None)
        if not report.get("ok") or not slot_report or not slot_report.get("fullFormatAvailable"):
            json_response(self, 409, {"ok": False, "error": "Sony SDK has not confirmed that full format is available for this slot."})
            return
        controller = existing_warm_controller(camera_id)
        if controller and controller.proc and controller.proc.poll() is None:
            controller.stop()
        time.sleep(self.config.get("modeSwitchDelaySeconds", 0.5))
        result = format_camera_card(self.config, camera_id, slot)
        with STATE_LOCK:
            STATE.setdefault("card_status", {}).pop(camera_id, None)
        json_response(self, 200 if result.get("ok") else 502, result)

    def handle_cleanup_report(self):
        try:
            import cleanup_manager
            manifests = cleanup_manager.load_manifests()
            json_response(self, 200, {
                "ok": True,
                "manifests": manifests
            })
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def handle_cleanup_scan(self):
        try:
            import cleanup_manager
            manifests = cleanup_manager.scan_and_update_manifests(self.config)
            json_response(self, 200, {
                "ok": True,
                "manifests": manifests
            })
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def handle_cleanup_delete(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            request_data = json.loads(self.rfile.read(content_length) or b"{}")
            session_time = request_data.get("sessionTime")
            camera_id = request_data.get("cameraId")
            filename = request_data.get("filename")

            if not session_time or not camera_id or not filename:
                json_response(self, 400, {
                    "ok": False,
                    "error": "Missing sessionTime, cameraId, or filename."
                })
                return

            import cleanup_manager
            manifests = cleanup_manager.load_manifests()
            matched_session = next((m for m in manifests if m["sessionTime"] == session_time), None)
            if not matched_session or camera_id not in matched_session["cameras"]:
                json_response(self, 404, {"ok": False, "error": "Session or camera not found in manifest."})
                return
            
            cam_data = matched_session["cameras"][camera_id]
            manifest_proxy_name = cam_data.get("sonyProxyName", "")
            camera_filename = cam_data.get("sonyOriginalName", "")
            if filename != manifest_proxy_name:
                json_response(self, 400, {
                    "ok": False,
                    "error": "Requested proxy does not match the cleanup manifest. Scan and refresh before retrying."
                })
                return
            if not camera_filename:
                json_response(self, 400, {
                    "ok": False,
                    "error": "Manifest has no paired Sony original filename. Scan and refresh before retrying."
                })
                return
            eligibility = cam_data.get("cleanupEligibility")
            
            if eligibility not in {"Ready for manual delete", "Delete failed"}:
                json_response(self, 400, {
                    "ok": False,
                    "error": f"File is not eligible for deletion. Current status: {eligibility}"
                })
                return

            print(f"Triggering SDK deletion for {camera_id} camera content {camera_filename} (verified proxy {manifest_proxy_name})...")
            result = cleanup_manager.delete_camera_file(self.config, camera_id, camera_filename)
            
            if result.get("ok"):
                cam_data["deleted"] = True
                cam_data["cleanupEligibility"] = "Deleted from camera card"
                if "deleteFailed" in cam_data:
                    del cam_data["deleteFailed"]
            else:
                cam_data["deleteFailed"] = True
                cam_data["cleanupEligibility"] = "Delete failed"
                cam_data["deleteError"] = result.get("error") or result.get("message") or "Unknown SDK error"
            
            cleanup_manager.save_manifests(manifests)
            
            json_response(self, 200, {
                "ok": result.get("ok"),
                "message": result.get("message") or result.get("error") or "Deletion complete",
                "cameraFilename": camera_filename,
                "verifiedProxyFilename": manifest_proxy_name,
                "manifests": manifests
            })
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def handle_cleanup_delete_all(self):
        """Scan + delete ALL eligible camera-card backups. No JSON body needed."""
        try:
            import cleanup_manager
            manifests = cleanup_manager.scan_and_update_manifests(self.config)

            deleted = []
            failed = []
            skipped = []

            for session in manifests:
                for camera_id, cam_data in session.get("cameras", {}).items():
                    elig = cam_data.get("cleanupEligibility", "")
                    proxy_filename = cam_data.get("sonyProxyName", "")
                    camera_filename = cam_data.get("sonyOriginalName", "")

                    if elig not in {"Ready for manual delete", "Delete failed"}:
                        skipped.append({"cameraId": camera_id, "filename": proxy_filename, "reason": elig})
                        continue
                    if not camera_filename:
                        skipped.append({"cameraId": camera_id, "filename": proxy_filename, "reason": "Missing Sony original filename"})
                        continue

                    print(f"[cleanup-delete-all] Deleting {camera_id} camera content {camera_filename} (verified proxy {proxy_filename})...")
                    result = cleanup_manager.delete_camera_file(self.config, camera_id, camera_filename)

                    if result.get("ok"):
                        cam_data["deleted"] = True
                        cam_data["cleanupEligibility"] = "Deleted from camera card"
                        if "deleteFailed" in cam_data:
                            del cam_data["deleteFailed"]
                        deleted.append({"cameraId": camera_id, "filename": camera_filename, "verifiedProxyFilename": proxy_filename})
                    else:
                        cam_data["deleteFailed"] = True
                        cam_data["cleanupEligibility"] = "Delete failed"
                        cam_data["deleteError"] = result.get("error") or result.get("message") or "Unknown SDK error"
                        failed.append({"cameraId": camera_id, "filename": camera_filename, "verifiedProxyFilename": proxy_filename, "error": cam_data["deleteError"]})

            cleanup_manager.save_manifests(manifests)

            ok = len(failed) == 0
            message = f"Deleted {len(deleted)} file(s)"
            if failed:
                message += f", {len(failed)} failed"
            if skipped:
                message += f", {len(skipped)} not eligible"

            json_response(self, 200, {
                "ok": ok,
                "message": message,
                "deleted": deleted,
                "failed": failed,
                "skipped": skipped,
                "manifests": manifests,
            })
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.json")),
        help="Path to bridge config JSON",
    )
    parser.add_argument(
        "--background-service",
        action="store_true",
        help="Serve the bridge without opening OBS or a browser window.",
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)

    BridgeHandler.config = config
    BridgeHandler.config_path = config_path

    try:
        server = ThreadingHTTPServer((config["host"], config["port"]), BridgeHandler)
    except OSError:
        with CAMERA_CONTROLLERS_LOCK:
            controllers = list(CAMERA_CONTROLLERS.values())
        for controller in controllers:
            controller.stop()
        raise

    Thread(target=watch_v4_completion_email, args=(config,), daemon=True).start()
    Thread(
        target=initialize_cameras,
        args=(config, not args.background_service),
        daemon=True,
    ).start()
    print(f"FX30 bridge listening on http://{config['host']}:{config['port']}")
    try:
        server.serve_forever()
    finally:
        with CAMERA_CONTROLLERS_LOCK:
            controllers = list(CAMERA_CONTROLLERS.values())
        for controller in controllers:
            controller.stop()


if __name__ == "__main__":
    main()
