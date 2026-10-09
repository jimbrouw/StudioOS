import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zowie_main_stream_status as status


class ZowieMainStreamStatusTests(unittest.TestCase):
    def test_check_marks_only_hd_main_stream_for_manual_attention(self):
        config = {
            "zowieResolutionGuard": {
                "devices": [
                    {"cameraId": "fx30-a", "host": "192.168.0.101"},
                    {"cameraId": "fx30-b", "host": "192.168.0.102"},
                ]
            }
        }

        with tempfile.TemporaryDirectory() as directory, \
                patch("zowie_main_stream_status._configured_password", return_value="secret"), \
                patch("zowie_main_stream_status.verify_resolution", side_effect=[
                    {"after": (3840, 2160)},
                    status.ZowieGuardError("192.168.0.102 Main Stream is 1920x1080, expected 3840x2160"),
                ]):
            results = status.check_devices(config, Path(directory))
            one = status.read_status("fx30-a", Path(directory))
            two = status.read_status("fx30-b", Path(directory))

        self.assertEqual([result["state"] for result in results], ["ready", "needs-4k"])
        self.assertEqual(one["detail"], "4K READY")
        self.assertEqual(two["detail"], "OPEN 102")

    def test_fix_mode_retries_an_unreachable_box_until_it_answers(self):
        config = {"zowieResolutionGuard": {"devices": [{"cameraId": "fx30-b", "host": "192.168.0.102"}]}}
        attempts = []

        def slow_box(*args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise status.ZowieGuardError("Cannot reach 192.168.0.102: timed out")
            return {"changed": False}

        with tempfile.TemporaryDirectory() as directory, \
                patch("zowie_main_stream_status._configured_password", return_value="secret"), \
                patch("zowie_main_stream_status.ensure_resolution", side_effect=slow_box):
            results = status.check_devices(
                config, Path(directory), fix=True, retry_seconds=10, retry_interval=0
            )

        self.assertEqual(results[0]["state"], "ready")
        self.assertEqual(len(attempts), 3)

    def test_check_without_fix_does_not_retry_an_unreachable_box(self):
        config = {"zowieResolutionGuard": {"devices": [{"cameraId": "fx30-b", "host": "192.168.0.102"}]}}

        with tempfile.TemporaryDirectory() as directory, \
                patch("zowie_main_stream_status._configured_password", return_value="secret"), \
                patch("zowie_main_stream_status.verify_resolution",
                      side_effect=status.ZowieGuardError("Cannot reach 192.168.0.102: timed out")) as verify:
            results = status.check_devices(config, Path(directory))

        self.assertEqual(results[0]["state"], "unavailable")
        self.assertEqual(verify.call_count, 1)

    def test_unknown_device_status_prompts_for_a_check(self):
        with tempfile.TemporaryDirectory() as directory:
            current = status.read_status("fx30-c", Path(directory))

        self.assertEqual(current, {"state": "unknown", "detail": "Press ZOWIE 4K"})


if __name__ == "__main__":
    unittest.main()