#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import socket
from urllib.request import urlopen
from pathlib import Path

from zowie_guard import ZowieGuardError, guard_configured_devices


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def enabled_cameras(config):
    return [
        camera
        for camera in config.get("cameras", [])
        if camera.get("enabled", True)
    ]


def load_keychain_secret(environment_name):
    if not environment_name or os.environ.get(environment_name):
        return
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            environment_name,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        os.environ[environment_name] = result.stdout.strip()


def run_status_check(camera, timeout_seconds):
    command = camera.get("statusCommand")
    if not command:
        return {
            "ok": False,
            "message": "No statusCommand configured.",
        }

    env = os.environ.copy()
    env["SONY_REC_NONINTERACTIVE"] = "1"
    if camera.get("password"):
        env["SONY_REC_PASSWORD"] = camera["password"]

    try:
        proc = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "message": f"Timed out after {timeout_seconds} seconds.",
        }

    output = "\n".join(
        part.strip()
        for part in (proc.stdout, proc.stderr)
        if part.strip()
    )
    return {
        "ok": proc.returncode == 0,
        "message": output or f"Command exited {proc.returncode}.",
    }


def trust_camera(camera, timeout_seconds):
    camera_id = camera.get("id", "camera")
    print(f"\nChecking {camera_id} trust/connection...")
    result = run_status_check(camera, timeout_seconds)
    if result["ok"]:
        print(f"{camera_id}: trusted and reachable.")
        return True

    print(f"{camera_id}: not ready yet.")
    print(result["message"])

    while True:
        print("\nOn the FX30:")
        print("1. Network > Transfer/Remote > PC Remote Function > Pairing")
        print("2. Leave the camera on 'Perform pairing on the connected device'.")
        print("3. Approve on the camera if it asks.")
        print("\nThen press Enter here to try again.")
        answer = input("Press Enter to retry, 's' to skip, or 'q' to quit: ").strip().lower()
        if answer == "q":
            return False
        if answer == "s":
            print(f"Skipping {camera_id}. The bridge can still start, but this camera may fail.")
            return True

        result = run_status_check(camera, timeout_seconds)
        if result["ok"]:
            print(f"{camera_id}: pairing/trust check passed.")
            return True

        print(f"{camera_id}: still not ready.")
        print(result["message"])
        if "SONY_REC_PASSWORD is required" in result["message"]:
            print("\nThis means Access Authentication is enabled on the camera.")
            print("For a no-password closed setup, turn Access Authentication off in the camera network settings if your firmware allows it.")
            print("If the camera does not allow that, Sony requires a password and the bridge cannot bypass it.")


def bridge_is_running(config):
    host = config.get("dashboardHost") or "127.0.0.1"
    port = int(config.get("port", 3030))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def bridge_camera_is_ready(config):
    host = config.get("dashboardHost") or "127.0.0.1"
    port = int(config.get("port", 3030))
    try:
        with urlopen(f"http://{host}:{port}/record/status", timeout=2) as response:
            payload = json.load(response)
    except Exception:
        return False

    camera_status = payload.get("cameraStatus") or []
    return bool(camera_status) and all(
        camera.get("warmConnection") is True for camera in camera_status
    )


def open_obs():
    obs_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return subprocess.Popen(
        [
            "/usr/bin/open",
            "--env",
            f"PATH={obs_path}",
            "-b",
            "com.obsproject.obs-studio",
        ]
    )


def verify_zowie_encoders(config):
    guard = config.get("zowieResolutionGuard") or {}
    if not guard.get("enabled", False):
        return True

    for device in guard.get("devices", []):
        if device.get("enabled", True):
            load_keychain_secret(device.get("passwordEnv"))

    print("\nChecking Zowie encoder resolution before OBS...")
    try:
        results = guard_configured_devices(config)
    except ZowieGuardError as exc:
        print(f"ZOWIE 4K CHECK FAILED: {exc}")
        print("OBS was not opened, preventing an accidental 1080p recording.")
        return False

    for result in results:
        before_width, before_height = result["before"]
        after_width, after_height = result["after"]
        if result["changed"]:
            print(
                f"{result['name']}: corrected {before_width}x{before_height} "
                f"to {after_width}x{after_height}."
            )
        else:
            print(f"{result['name']}: verified {after_width}x{after_height}.")
    return True


def main():
    base_dir = Path(__file__).resolve().parent
    default_config = base_dir / "config.json"
    if not default_config.exists():
        default_config = base_dir / "config.example.json"

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument("--skip-trust-check", action="store_true")
    parser.add_argument("--skip-zowie-check", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)

    if bridge_is_running(config):
        dashboard = f"http://{config.get('dashboardHost', '127.0.0.1')}:{config.get('port', 3030)}/"
        if bridge_camera_is_ready(config):
            if not args.skip_zowie_check and not verify_zowie_encoders(config):
                return 1
            print("FX30 bridge and camera are ready. Opening OBS and the control panel.")
            open_obs()
            subprocess.Popen(["/usr/bin/open", dashboard])
            return 0
        print("FX30 bridge is running, but the camera warm connection is not ready.")
        print("OBS and the control panel were not opened. Check the camera status button.")
        return 1

    cameras = enabled_cameras(config)
    email_config = config.get("email") or {}
    if email_config.get("enabled") and email_config.get("provider", "").lower() == "resend":
        load_keychain_secret(email_config.get("apiKeyEnv", "FX30_RESEND_API_KEY"))

    print("FX30 studio launcher")
    print(f"Config: {config_path}")

    if not cameras:
        print("No enabled cameras in config. Starting bridge anyway.")
    elif not args.skip_trust_check:
        timeout_seconds = config.get("commandTimeoutSeconds", 90)
        for camera in cameras:
            if not trust_camera(camera, timeout_seconds):
                print("Stopped before launching bridge.")
                return 1

    if not args.skip_zowie_check and not verify_zowie_encoders(config):
        return 1

    print("\nStarting FX30 bridge.")
    print("Leave this window open while Companion is in use.")
    os.chdir(base_dir)
    os.execv(
        "/usr/bin/python3",
        ["/usr/bin/python3", "bridge.py", "--config", str(config_path)],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
