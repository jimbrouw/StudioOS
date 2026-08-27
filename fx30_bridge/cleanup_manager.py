#!/usr/bin/env python3
import os
import json
import time
import re
import subprocess
from pathlib import Path
from obs_verify import get_mp4_duration

MANIFEST_FILE = Path(__file__).resolve().parent / "session_manifests.json"

def load_manifests():
    if not MANIFEST_FILE.exists():
        return []
    try:
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading manifests: {e}")
        return []

def save_manifests(manifests):
    try:
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifests, f, indent=2)
    except Exception as e:
        print(f"Error saving manifests: {e}")

def get_session_time_from_filename(filename):
    # Matches YYYY-MM-DD HH-MM-SS.MP4 or YYYY-MM-DD HH-MM-SS-N.MP4
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}-\d{2}-\d{2})", filename)
    if match:
        date_str = match.group(1)
        time_str = match.group(2).replace("-", ":")
        return f"{date_str} {time_str}"
    return None

def infer_original_name(proxy_name):
    # If C1955S03.MP4 -> C1955.MP4
    # Replaces S\d+ with empty string case-insensitively
    name, ext = os.path.splitext(proxy_name)
    clean_name = re.sub(r"[sS]\d+", "", name)
    return f"{clean_name}{ext}"

def scan_and_update_manifests(config):
    manifests = load_manifests()
    manifests_by_time = {m["sessionTime"]: m for m in manifests}

    lucid_config = config.get("lucidLink") or {}
    lucid_enabled = lucid_config.get("enabled", False)
    delivery_root = Path(lucid_config.get("deliveryRoot") or "")
    
    obs_config = config.get("obs") or {}
    obs_folder = Path(obs_config.get("masterFolder", "/Volumes/Extreme SSD/TEST-RECS/OBS-MASTERS"))

    # 1. Scan LucidLink folders for delivered proxies
    if lucid_enabled and delivery_root.exists() and delivery_root.is_dir():
        for camera in config.get("cameras", []):
            if not camera.get("enabled", True):
                continue
            camera_id = camera["id"]
            
            # Map camera folder name
            camera_folders = lucid_config.get("cameraFolders") or {}
            folder_name = camera_folders.get(camera_id)
            if not folder_name:
                folder_name = str(camera_id).replace("-", "_").upper()
            
            camera_delivery = delivery_root / folder_name
            if not camera_delivery.exists() or not camera_delivery.is_dir():
                continue

            for item in camera_delivery.iterdir():
                if item.is_file() and item.suffix.lower() in {".mp4", ".mxf"}:
                    if item.name.startswith("._"):
                        continue
                    
                    session_time = get_session_time_from_filename(item.name)
                    if not session_time:
                        continue
                    
                    stat = item.stat()
                    mtime = stat.st_mtime
                    size = stat.st_size
                    
                    # Check if session exists in manifest
                    # We look for a session within 120 seconds of this file's mtime
                    matched_session = None
                    for s_time, m in manifests_by_time.items():
                        if abs(m.get("mtime", 0) - mtime) <= 120:
                            matched_session = m
                            break
                    
                    if not matched_session:
                        # Create a new session manifest
                        matched_session = {
                            "sessionTime": session_time,
                            "mtime": mtime,
                            "cameras": {},
                            "obsMasters": []
                        }
                        manifests_by_time[session_time] = matched_session
                        manifests.append(matched_session)
                    
                    # Update camera details in the session
                    if camera_id not in matched_session["cameras"]:
                        # Look for local proxy file in ingestFolder to find the original Sony name
                        sony_proxy_name = None
                        ingest_folder = Path(camera.get("ingestFolder", ""))
                        if ingest_folder.exists() and ingest_folder.is_dir():
                            # Find local files with matching size
                            for f in ingest_folder.iterdir():
                                if f.is_file() and f.stat().st_size == size:
                                    # Check mtime similarity within 120 seconds
                                    if abs(f.stat().st_mtime - mtime) <= 120:
                                        sony_proxy_name = f.name
                                        break
                        
                        if not sony_proxy_name:
                            # fallback: try to find any proxy file with mtime proximity
                            candidates = []
                            if ingest_folder.exists() and ingest_folder.is_dir():
                                for f in ingest_folder.iterdir():
                                    if f.is_file() and abs(f.stat().st_mtime - mtime) <= 600:
                                        candidates.append(f)
                            if candidates:
                                candidates.sort(key=lambda f: abs(f.stat().st_mtime - mtime))
                                sony_proxy_name = candidates[0].name
                        
                        if not sony_proxy_name:
                            sony_proxy_name = f"UNKNOWN_{item.name}"

                        sony_orig_name = infer_original_name(sony_proxy_name)
                        
                        matched_session["cameras"][camera_id] = {
                            "sonyOriginalName": sony_orig_name,
                            "sonyProxyName": sony_proxy_name,
                            "lucidLinkPath": str(item),
                            "lucidLinkSize": size,
                            "lucidLinkMtime": mtime,
                            "localLandingPath": str(ingest_folder / sony_proxy_name) if camera.get("ingestFolder") else "",
                            "localLandingSize": size,
                            "emailSent": False,
                            "deleted": False
                        }

    # 2. Update OBS master recording associations
    for session in manifests:
        session_mtime = session.get("mtime", 0)
        if not session_mtime:
            continue
        
        # Check if OBS files match this session's timestamp
        session_obs_files = []
        if obs_folder.exists() and obs_folder.is_dir():
            for item in obs_folder.iterdir():
                if item.is_file() and item.suffix.lower() in {".mp4", ".mkv", ".mov", ".mxf"}:
                    if item.name.startswith("._"):
                        continue
                    stat = item.stat()
                    if abs(stat.st_mtime - session_mtime) <= 300: # within 5 minutes
                        session_obs_files.append({
                            "name": item.name,
                            "path": str(item),
                            "sizeBytes": stat.st_size,
                            "modifiedAt": stat.st_mtime
                        })
        
        # Update session OBS files list
        # Check if they are growing and fetch duration
        updated_obs = []
        for f in session_obs_files:
            path = Path(f["path"])
            # check growth by sleeping briefly or checking modification time vs now
            growing = False
            if time.time() - f["modifiedAt"] < 10:
                # File was modified less than 10s ago, check growth
                size1 = path.stat().st_size
                time.sleep(0.5)
                size2 = path.stat().st_size
                growing = size2 > size1
            
            duration = None
            if path.suffix.lower() == ".mp4":
                duration = get_mp4_duration(path)
            
            updated_obs.append({
                "name": f["name"],
                "path": f["path"],
                "sizeBytes": f["sizeBytes"],
                "modifiedAt": int(f["modifiedAt"]),
                "growing": growing,
                "durationSeconds": duration
            })
        session["obsMasters"] = updated_obs

    # 3. A camera-card proxy is eligible only when the exact proxy exists in
    # both configured destinations: the local SSD landing folder and LucidLink.
    # OBS masters and an age-based retention window do not prove that this
    # individual proxy completed its two configured copies.
    camera_config_by_id = {
        camera["id"]: camera
        for camera in config.get("cameras", [])
        if camera.get("enabled", True)
    }
    for session in manifests:
        for camera_id, cam_data in session["cameras"].items():
            if cam_data.get("deleted", False):
                cam_data["cleanupEligibility"] = "Deleted from camera card"
                continue
            
            # Check delete failed state from manifest
            if cam_data.get("deleteFailed", False):
                cam_data["cleanupEligibility"] = "Delete failed"
                continue

            camera = camera_config_by_id.get(camera_id, {})
            proxy_name = cam_data.get("sonyProxyName", "")
            local_path = Path(cam_data.get("localLandingPath", ""))
            if not local_path and camera.get("ingestFolder") and proxy_name:
                local_path = Path(camera["ingestFolder"]) / proxy_name
            lucid_path = Path(cam_data.get("lucidLinkPath", ""))
            local_size = local_path.stat().st_size if local_path.is_file() else 0
            lucid_size = lucid_path.stat().st_size if lucid_path.is_file() else 0
            cam_data["localLandingPath"] = str(local_path) if local_path else ""
            cam_data["localLandingSize"] = local_size
            cam_data["lucidLinkSize"] = lucid_size
            cam_data["copyVerification"] = {
                "ssd": bool(local_size),
                "lucidLink": bool(lucid_size),
                "sameSize": bool(local_size and local_size == lucid_size),
            }

            if not local_size:
                cam_data["cleanupEligibility"] = "Waiting for SSD copy"
                continue
            if not lucid_size:
                cam_data["cleanupEligibility"] = "Waiting for LucidLink copy"
                continue
            if local_size != lucid_size:
                cam_data["cleanupEligibility"] = "Copy size mismatch"
                continue

            cam_data["cleanupEligibility"] = "Ready for manual delete"

    # Save manifests
    # Sort manifests by mtime descending
    manifests.sort(key=lambda m: m.get("mtime", 0), reverse=True)
    save_manifests(manifests)
    return manifests

def delete_camera_file(config, camera_id, filename):
    """
    Spawns sony-rec to delete the camera content containing the named Sony
    original file. Proxy backup verification is performed by the caller.
    """
    cameras = {c["id"]: c for c in config.get("cameras", []) if c.get("enabled", True)}
    if camera_id not in cameras:
        return {"ok": False, "error": f"Camera {camera_id} is not enabled or configured."}
    
    # Run the compiled helper in RemoteControl mode
    # Command: ./sony-rec --camera <id> --action delete-file --filename <filename>
    # Note: we need to run it in the bridge directory
    helper_path = Path(__file__).resolve().parent / "sony-rec"
    if not helper_path.exists():
        return {"ok": False, "error": "sony-rec helper executable not found."}
    camera_config = cameras[camera_id]
    if not camera_config.get("mac"):
        return {"ok": False, "error": "Camera has no configured MAC; refusing deletion without exact identity."}
    
    cmd = [
        str(helper_path),
        "--camera", camera_id,
        "--mac", camera_config["mac"],
        "--action", "delete-file",
        "--filename", filename
    ]
    
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = f"{helper_path.parent}:{helper_path.parent / 'CrAdapter'}"
    env["SONY_REC_NONINTERACTIVE"] = "1"
    if camera_config.get("password"):
        env["SONY_REC_PASSWORD"] = camera_config["password"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=90,
            env=env
        )
        ok = proc.returncode == 0
        output = proc.stdout + "\n" + proc.stderr
        return {
            "ok": ok,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "message": output.strip()
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
