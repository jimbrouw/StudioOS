import unittest
from unittest.mock import MagicMock, patch

import bridge


class WarmConnectionTests(unittest.TestCase):
    def setUp(self):
        with bridge.STATE_LOCK:
            self.previous_state = dict(bridge.STATE)
            bridge.STATE["recording"] = False
            bridge.STATE["record_action_running"] = False
            bridge.STATE["ingest_running"] = False

    def tearDown(self):
        with bridge.STATE_LOCK:
            bridge.STATE.clear()
            bridge.STATE.update(self.previous_state)

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_ensure_warm_controllers_starts_each_enabled_camera(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        cameras = [
            {"id": "fx30-a", "warmConnection": True},
            {"id": "fx30-b", "warmConnection": True},
        ]
        enabled_cameras.return_value = cameras
        controllers = {}

        def controller_for(camera):
            controller = MagicMock()
            controller.run.return_value = {
                "id": camera["id"],
                "ok": True,
                "recordingState": "not_recording",
            }
            controllers[camera["id"]] = controller
            return controller

        warm_controller_for.side_effect = controller_for

        results = bridge.ensure_warm_controllers({"commandTimeoutSeconds": 12})

        self.assertEqual(
            results,
            [
                {"id": "fx30-a", "ok": True, "recordingState": "not_recording", "warmConnection": True},
                {"id": "fx30-b", "ok": True, "recordingState": "not_recording", "warmConnection": True},
            ],
        )
        for controller in controllers.values():
            controller.run.assert_called_once_with("status", 12)

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_ensure_warm_controllers_reports_disabled_warm_link(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        enabled_cameras.return_value = [
            {"id": "fx30-a", "warmConnection": False},
        ]

        results = bridge.ensure_warm_controllers({})

        self.assertFalse(results[0]["ok"])
        warm_controller_for.assert_not_called()

    @patch("bridge.json_response")
    @patch("bridge.ensure_warm_controllers")
    def test_warm_endpoint_refuses_while_recording(
        self,
        ensure_warm_controllers,
        json_response,
    ):
        with bridge.STATE_LOCK:
            bridge.STATE["recording"] = True
        handler = object.__new__(bridge.BridgeHandler)
        handler.config = {}

        handler.handle_warm_connections()

        ensure_warm_controllers.assert_not_called()
        self.assertEqual(json_response.call_args.args[1], 409)

    @patch("bridge.json_response")
    @patch("bridge.ensure_warm_controllers")
    def test_warm_endpoint_reports_ready(
        self,
        ensure_warm_controllers,
        json_response,
    ):
        ensure_warm_controllers.return_value = [
            {"id": "fx30-a", "ok": True, "warmConnection": True},
        ]
        handler = object.__new__(bridge.BridgeHandler)
        handler.config = {}

        handler.handle_warm_connections()

        self.assertEqual(json_response.call_args.args[1], 200)
        self.assertTrue(json_response.call_args.args[2]["ok"])

    @patch("bridge.schedule_auto_ingest_after_stop", return_value=False)
    @patch("bridge.start_native_ftp_ingest", return_value=False)
    @patch("bridge.stop_warm_controllers")
    @patch("bridge.run_camera_command", return_value={"id": "fx30-a", "ok": True})
    @patch("bridge.log_event")
    def test_successful_stop_keeps_warm_connection_by_default(
        self,
        _log_event,
        _run_camera_command,
        stop_warm_controllers,
        _start_native_ftp_ingest,
        _schedule_auto_ingest,
    ):
        config = {"cameras": [{"id": "fx30-a", "enabled": True}]}

        bridge.run_record_action_job(config, "stop")

        stop_warm_controllers.assert_not_called()

    @patch("bridge.WarmCameraController._assert_identity", side_effect=[False, True])
    @patch("bridge.subprocess.Popen")
    @patch("bridge.os.close")
    @patch("bridge.pty.openpty")
    def test_warm_start_retries_once_before_failing(
        self,
        openpty,
        _close,
        popen,
        assert_identity,
    ):
        controller = bridge.WarmCameraController({
            "id": "fx30-a",
            "mac": "78:F5:05:44:97:0E",
            "statusCommand": ["/usr/bin/true", "--camera", "fx30-a", "--server"],
        })
        controller._wait_for_prefix = MagicMock(side_effect=["SONY_REC_READY {\"mac\": \"78:F5:05:44:97:0E\"}", "SONY_REC_READY {\"mac\": \"78:F5:05:44:97:0E\"}"])
        controller.stop = MagicMock()
        controller._read_stdout = MagicMock()

        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        openpty.return_value = (10, 11)

        result = controller.start(12)

        self.assertTrue(result)
        self.assertEqual(controller._wait_for_prefix.call_count, 2)
        self.assertEqual(popen.call_count, 2)

    @patch("bridge.warm_controller_for")
    def test_failed_warm_action_does_not_start_competing_one_shot_command(
        self,
        warm_controller_for,
    ):
        controller = MagicMock()
        controller.run.return_value = {
            "id": "fx30-a",
            "ok": False,
            "error": "Warm camera connection did not become ready",
        }
        warm_controller_for.return_value = controller

        with patch("bridge.subprocess.run") as subprocess_run:
            result = bridge.run_camera_command(
                {
                    "id": "fx30-a",
                    "warmConnection": True,
                    "startCommand": ["./sony-rec", "--action", "start"],
                },
                "start",
            )

        self.assertFalse(result["ok"])
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
