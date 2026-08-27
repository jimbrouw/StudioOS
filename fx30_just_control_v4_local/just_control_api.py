#!/usr/bin/env python3
"""Small, dependency-free client for the Just In Mac Lite NDI REST API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class JustControlApiError(RuntimeError):
    """The local Just In Mac Lite NDI API could not complete a request."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class JustControlClient:
    host: str = "127.0.0.1"
    port: int = 8080
    timeout: float = 5.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise JustControlApiError(
                f"Just In Mac Lite NDI returned HTTP {exc.code} for {path}",
                status=exc.code,
                body=detail,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JustControlApiError(f"Could not reach Just In Mac Lite NDI at {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise JustControlApiError(f"Just In Mac Lite NDI returned invalid JSON for {path}") from exc

    def all_channels(self) -> list[str]:
        return list(self._request("GET", "/ingest/allChannels").get("channel-names", []))

    def active_channels(self) -> list[str]:
        return list(self._request("GET", "/ingest/activeChannels").get("channel-names", []))

    def can_record(self, channel: str) -> dict:
        return self._request("POST", "/ingest/requestCanRecord", {"channel": channel})

    def recording_status(self, channel: str) -> dict:
        return self._request("POST", "/ingest/requestRecordingStatus", {"channel": channel})

    def current_filename(self, channel: str) -> dict:
        return self._request("POST", "/ingest/requestCurrentFilename", {"channel": channel})

    def recording_paths(self, channel: str) -> dict:
        return self._request("POST", "/ingest/recordingPaths", {"channel": channel})

    def recording_configuration(self, channel: str) -> dict:
        return self._request("POST", "/ingest/recordingConfiguration", {"channel": channel})

    def capture_presets(self, channel: str) -> dict:
        return self._request("POST", "/ingest/requestCapturePresets", {"channel": channel})

    def destination_presets(self, channel: str) -> dict:
        return self._request("POST", "/ingest/requestDestinationPresets", {"channel": channel})

    def start_channel(self, channel: str) -> dict:
        return self._request("POST", "/ingest/startChannel", {"channel": channel})

    def stop_channel(self, channel: str) -> dict:
        return self._request("POST", "/ingest/stopChannel", {"channel": channel})

    def load_capture_preset(self, channel: str, name: str, preset_id: int | None = None) -> dict:
        payload = {"channel": channel, "capture-preset-name": name}
        if preset_id is not None:
            payload["capture-preset-id"] = preset_id
        return self._request("POST", "/ingest/requestLoadCapturePreset", payload)

    def load_destination_preset(self, channel: str, name: str, preset_id: int | None = None) -> dict:
        payload = {"channel": channel, "destination-preset-name": name}
        if preset_id is not None:
            payload["destination-preset-id"] = preset_id
        return self._request("POST", "/ingest/requestLoadDestinationPreset", payload)

    def start_recording(self, channel: str, proposed_filename: str = "") -> dict:
        return self._request(
            "POST",
            "/ingest/startRecordingWithFilename",
            {
                "channel": channel,
                "proposed-filename": proposed_filename,
                "metadata": {
                    "toa-just-in-engine-alternative-start-timecode-frames": 0,
                    "tal-ingest-engine-override-naming-preset": 0,
                    "toa-just-in-engine-alternative-start-timecode-active": 0,
                },
            },
        )

    def stop_recording(self, channel: str) -> dict:
        return self._request(
            "POST",
            "/ingest/stopRecording",
            {
                "channel": channel,
                "metadata": {
                    "toa-just-in-engine-alternative-stop-timecode-active": 0,
                    "toa-just-in-engine-alternative-stop-timecode-frames": 0,
                },
            },
        )
