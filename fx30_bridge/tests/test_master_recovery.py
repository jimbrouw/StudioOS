import unittest
from unittest.mock import patch

import bridge


class MasterRecoveryTests(unittest.TestCase):
    def setUp(self):
        with bridge.STATE_LOCK:
            bridge.STATE["ingest_running"] = True
            bridge.STATE["last_master_camera_id"] = None

    @patch("bridge.notify_mac")
    @patch("bridge.log_event")
    @patch("bridge.restart_warm_controllers_after_ingest", return_value=[])
    @patch("bridge.stop_warm_controllers_for_ingest")
    @patch("bridge.verify_local_media", return_value={"ok": True, "width": 3840, "height": 2160})
    @patch("bridge.extract_downloaded_file", return_value="/tmp/CAMERA_B.MP4")
    @patch("bridge.run_camera_command_streaming", return_value={"id": "fx30-b", "ok": True})
    def test_targeted_recovery_runs_only_selected_camera(
        self,
        run_camera,
        _extract,
        _verify,
        _stop_warm,
        _restart_warm,
        _log,
        _notify,
    ):
        config = {
            "ingestTimeoutSeconds": 1800,
            "cameras": [
                {"id": "fx30-a", "enabled": True},
                {"id": "fx30-b", "enabled": True},
                {"id": "fx30-c", "enabled": True},
            ],
        }

        bridge.run_master_recovery_job(config, "fx30-b")

        run_camera.assert_called_once_with(config["cameras"][1], "masterIngest", 1800)
        with bridge.STATE_LOCK:
            self.assertEqual(bridge.STATE["last_master_camera_id"], "fx30-b")
            self.assertTrue(bridge.STATE["last_master_ok"])


if __name__ == "__main__":
    unittest.main()
