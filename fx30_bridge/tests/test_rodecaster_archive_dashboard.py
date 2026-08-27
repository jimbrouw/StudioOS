import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def completed_v4_status(**overrides):
    status = {
        "recording": False,
        "postRecordSummary": {"state": "complete", "allChecksPassed": True},
        "rodecasterCleanup": {"state": "idle"},
    }
    status.update(overrides)
    return status


class RodecasterArchiveDashboardTests(unittest.TestCase):
    @patch("bridge.subprocess.run")
    @patch("bridge.urlopen")
    def test_archive_becomes_ready_only_after_successful_post_record_and_obs_exit(self, urlopen, run):
        urlopen.return_value = FakeResponse(completed_v4_status())
        run.return_value = SimpleNamespace(returncode=1)

        result = bridge.rodecaster_archive_readiness()

        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "idle")
        run.assert_called_once_with(
            ["/usr/bin/pgrep", "-q", "-x", "obs"],
            stdout=bridge.subprocess.DEVNULL,
            stderr=bridge.subprocess.DEVNULL,
            check=False,
        )

    @patch("bridge.subprocess.run")
    @patch("bridge.urlopen")
    def test_archive_waits_for_post_record_without_probing_or_controlling_obs(self, urlopen, run):
        urlopen.return_value = FakeResponse(completed_v4_status(
            postRecordSummary={"state": "running", "allChecksPassed": False},
        ))

        result = bridge.rodecaster_archive_readiness()

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "waiting-post-record")
        run.assert_not_called()

    @patch("bridge.subprocess.run")
    @patch("bridge.urlopen")
    def test_archive_is_held_while_obs_is_running(self, urlopen, run):
        urlopen.return_value = FakeResponse(completed_v4_status())
        run.return_value = SimpleNamespace(returncode=0)

        result = bridge.rodecaster_archive_readiness()

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "blocked-obs")

    @patch("bridge.json_response")
    @patch("bridge.urlopen")
    @patch("bridge.rodecaster_archive_readiness")
    def test_archive_endpoint_queues_v4_only_after_the_guard_passes(self, readiness, urlopen, json_response):
        readiness.return_value = {"ready": True, "state": "idle"}
        urlopen.return_value = FakeResponse({"ok": True, "state": "checking"})
        handler = object.__new__(bridge.BridgeHandler)

        handler.handle_rodecaster_archive()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, bridge.V4_RODECASTER_ARCHIVE_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json_response.call_args.args[1], 200)
        self.assertTrue(json_response.call_args.args[2]["ok"])


if __name__ == "__main__":
    unittest.main()
