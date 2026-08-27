#!/usr/bin/env python3
"""Verify and enforce the ZowieBox main-stream encoder resolution."""

import argparse
import getpass
import json
import os
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SUCCESS_STATUS = "00000"


class ZowieGuardError(RuntimeError):
    pass


def _post(host, endpoint, payload, timeout_seconds, session_uuid=None):
    query_separator = "&" if "?" in endpoint else "?"
    url = f"http://{host}{endpoint}{query_separator}{urlencode({'t': int(time.time() * 1000)})}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
    }
    if session_uuid:
        headers["uuid"] = session_uuid

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise ZowieGuardError(f"HTTP {exc.code} from {host}") from exc
    except URLError as exc:
        raise ZowieGuardError(f"Cannot reach {host}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ZowieGuardError(f"Timed out connecting to {host}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ZowieGuardError(f"Invalid JSON response from {host}") from exc

    status = str(result.get("status", ""))
    if status != SUCCESS_STATUS:
        raise ZowieGuardError(f"Zowie API status {status or 'missing'} from {host}")
    return result


def _login(host, username, password, timeout_seconds):
    response = _post(
        host,
        "/system?option=setinfo",
        {
            "group": "account",
            "opt": "login_account",
            "data": {"username": username, "password": password},
        },
        timeout_seconds,
    )
    session_uuid = (response.get("data") or {}).get("uuid")
    if not session_uuid:
        raise ZowieGuardError(f"Login to {host} returned no session UUID")
    return session_uuid


def _read_encoders(host, session_uuid, timeout_seconds):
    response = _post(
        host,
        "/video?option=getinfo",
        {"group": "all"},
        timeout_seconds,
        session_uuid,
    )
    encoders = (response.get("all") or {}).get("venc")
    if not isinstance(encoders, list) or not encoders:
        raise ZowieGuardError(f"No encoder configuration returned by {host}")
    return encoders


def _write_encoders(host, session_uuid, encoders, timeout_seconds):
    _post(
        host,
        "/video?option=setinfo",
        {"group": "venc", "venc": encoders},
        timeout_seconds,
        session_uuid,
    )


def ensure_resolution(
    host,
    username,
    password,
    width=3840,
    height=2160,
    timeout_seconds=5,
):
    session_uuid = _login(host, username, password, timeout_seconds)
    encoders = _read_encoders(host, session_uuid, timeout_seconds)
    main_encoder = encoders[0]
    before = (int(main_encoder.get("width", 0)), int(main_encoder.get("height", 0)))

    changed = before != (width, height)
    if changed:
        main_encoder["width"] = width
        main_encoder["height"] = height
        _write_encoders(host, session_uuid, encoders, timeout_seconds)

    verified_encoders = _read_encoders(host, session_uuid, timeout_seconds)
    verified_main = verified_encoders[0]
    after = (int(verified_main.get("width", 0)), int(verified_main.get("height", 0)))
    if after != (width, height):
        raise ZowieGuardError(
            f"{host} read back {after[0]}x{after[1]}, expected {width}x{height}"
        )

    return {"host": host, "before": before, "after": after, "changed": changed}


def _configured_password(device):
    environment_name = device.get("passwordEnv")
    password = os.environ.get(environment_name, "") if environment_name else ""
    if password:
        return password

    if environment_name:
        keychain = subprocess.run(
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
        if keychain.returncode == 0 and keychain.stdout.strip():
            return keychain.stdout.strip()

    # This is deliberately opt-in and only supports the device's unchanged
    # factory credential. Use passwordEnv/Keychain after changing the account.
    if device.get("allowFactoryDefaultPassword"):
        return device.get("username", "admin")
    return ""


def guard_configured_devices(config):
    guard = config.get("zowieResolutionGuard") or {}
    if not guard.get("enabled", False):
        return []

    default_width = int(guard.get("requiredWidth", 3840))
    default_height = int(guard.get("requiredHeight", 2160))
    timeout_seconds = float(guard.get("timeoutSeconds", 5))
    retry_seconds = float(guard.get("retrySeconds", 30))
    retry_interval = max(0.25, float(guard.get("retryIntervalSeconds", 2)))
    results = []

    for device in guard.get("devices", []):
        if not device.get("enabled", True):
            continue
        host = device.get("host", "").strip()
        username = device.get("username", "admin")
        password = _configured_password(device)
        if not host:
            raise ZowieGuardError("An enabled Zowie device has no host")
        if not password:
            environment_name = device.get("passwordEnv", "ZOWIE_PASSWORD")
            raise ZowieGuardError(
                f"No password for {host}; set {environment_name} in the environment or Keychain"
            )

        deadline = time.monotonic() + retry_seconds
        last_error = None
        while True:
            try:
                result = ensure_resolution(
                    host,
                    username,
                    password,
                    int(device.get("requiredWidth", default_width)),
                    int(device.get("requiredHeight", default_height)),
                    timeout_seconds,
                )
                result["name"] = device.get("name") or host
                results.append(result)
                break
            except ZowieGuardError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise ZowieGuardError(
                        f"{device.get('name') or host} did not become 4K-ready: {last_error}"
                    ) from exc
                time.sleep(retry_interval)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Verify and enforce a ZowieBox main-stream resolution."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--host")
    target.add_argument("--config")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", default="FX30_ZOWIE_A_PASSWORD")
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()

    if args.config:
        with open(args.config, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
        try:
            results = guard_configured_devices(config)
        except (OSError, ValueError, ZowieGuardError) as exc:
            print(f"ZOWIE CHECK FAILED: {exc}", file=os.sys.stderr)
            return 1
        if not results:
            print("ZOWIE CHECK FAILED: resolution guard is disabled", file=os.sys.stderr)
            return 1
        for result in results:
            action = "corrected" if result["changed"] else "verified"
            print(
                f"ZOWIE 4K {action}: {result['name']} "
                f"{result['before'][0]}x{result['before'][1]} -> "
                f"{result['after'][0]}x{result['after'][1]}"
            )
        return 0

    password = os.environ.get(args.password_env) or getpass.getpass(
        f"Password for {args.username}@{args.host}: "
    )
    try:
        result = ensure_resolution(
            args.host,
            args.username,
            password,
            args.width,
            args.height,
            args.timeout,
        )
    except ZowieGuardError as exc:
        print(f"ZOWIE CHECK FAILED: {exc}", file=os.sys.stderr)
        return 1

    action = "corrected" if result["changed"] else "verified"
    print(f"ZOWIE 4K {action}: {args.host} is {args.width}x{args.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
