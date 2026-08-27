import unittest
from unittest.mock import patch

import bridge


class RecordNotificationTests(unittest.TestCase):
    def setUp(self):
        with bridge.STATE_LOCK:
            bridge.STATE["recording"] = False
            bridge.STATE["recording_started_at"] = None
            bridge.STATE["record_action_running"] = True

    @patch("bridge.log_event")
    @patch("bridge.notify_studio_event")
    @patch("bridge.run_camera_command")
    def test_failed_camera_start_notifies_once(self, run_camera, notify, _log):
        run_camera.side_effect = [
            {"id": "fx30-a", "ok": True},
            {"id": "fx30-b", "ok": False, "error": "offline"},
        ]
        config = {
            "macNotifications": True,
            "cameras": [
                {"id": "fx30-a", "enabled": True},
                {"id": "fx30-b", "enabled": True},
            ],
        }

        bridge.run_record_action_job(config, "start")

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "fx30-start-failed")
        self.assertIn("fx30-b", notify.call_args.args[2])

    @patch("bridge.log_event")
    @patch("bridge.notify_studio_event")
    @patch("bridge.run_camera_command", return_value={"id": "fx30-a", "ok": True})
    def test_successful_camera_start_does_not_notify(self, _run_camera, notify, _log):
        config = {"macNotifications": True, "cameras": [{"id": "fx30-a", "enabled": True}]}

        bridge.run_record_action_job(config, "start")

        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
