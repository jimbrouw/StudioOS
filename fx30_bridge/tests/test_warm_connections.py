import unittest
from threading import Event, Thread
from unittest.mock import MagicMock, patch

import bridge
import cleanup_manager


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

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_warm_enabled_cameras_starts_all_cameras_concurrently(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        cameras = [{"id": camera_id} for camera_id in ("fx30-a", "fx30-b", "fx30-c")]
        enabled_cameras.return_value = cameras
        entered = []
        all_entered = Event()
        release = Event()

        def controller_for(camera):
            controller = MagicMock()

            def start(_timeout_seconds):
                entered.append(camera["id"])
                if len(entered) == len(cameras):
                    all_entered.set()
                release.wait(1)
                return True

            controller.start.side_effect = start
            return controller

        warm_controller_for.side_effect = controller_for
        result_holder = {}
        worker = Thread(
            target=lambda: result_holder.setdefault(
                "results", bridge.warm_enabled_cameras({}, probe_status=False)
            )
        )
        worker.start()
        try:
            self.assertTrue(all_entered.wait(1))
        finally:
            release.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertCountEqual(entered, ["fx30-a", "fx30-b", "fx30-c"])
        self.assertEqual(
            [result["id"] for result in result_holder["results"]],
            ["fx30-a", "fx30-b", "fx30-c"],
        )

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_warm_enabled_cameras_keeps_configured_result_order(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        cameras = [{"id": camera_id} for camera_id in ("fx30-a", "fx30-b", "fx30-c")]
        enabled_cameras.return_value = cameras
        camera_c_finished = Event()
        camera_b_finished = Event()
        completed = []

        def controller_for(camera):
            controller = MagicMock()

            def start(_timeout_seconds):
                if camera["id"] == "fx30-a":
                    camera_b_finished.wait(1)
                elif camera["id"] == "fx30-b":
                    camera_c_finished.wait(1)
                completed.append(camera["id"])
                if camera["id"] == "fx30-c":
                    camera_c_finished.set()
                elif camera["id"] == "fx30-b":
                    camera_b_finished.set()
                return True

            controller.start.side_effect = start
            return controller

        warm_controller_for.side_effect = controller_for

        results = bridge.warm_enabled_cameras({}, probe_status=False)

        self.assertEqual(completed, ["fx30-c", "fx30-b", "fx30-a"])
        self.assertEqual(
            [result["id"] for result in results],
            ["fx30-a", "fx30-b", "fx30-c"],
        )

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_warm_enabled_cameras_isolates_worker_failure(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        cameras = [{"id": camera_id} for camera_id in ("fx30-a", "fx30-b", "fx30-c")]
        enabled_cameras.return_value = cameras
        controllers = {}

        def controller_for(camera):
            controller = MagicMock()
            if camera["id"] == "fx30-b":
                controller.start.side_effect = RuntimeError("camera unavailable")
            else:
                controller.start.return_value = True
            controllers[camera["id"]] = controller
            return controller

        warm_controller_for.side_effect = controller_for

        results = bridge.warm_enabled_cameras({}, probe_status=False)

        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertIn("camera unavailable", results[1]["error"])
        self.assertTrue(results[2]["ok"])
        for controller in controllers.values():
            controller.start.assert_called_once_with(90)

    @patch("bridge.warm_controller_for")
    @patch("bridge.enabled_cameras")
    def test_warm_enabled_cameras_does_not_submit_disabled_camera(
        self,
        enabled_cameras,
        warm_controller_for,
    ):
        enabled_cameras.return_value = [
            {"id": "fx30-a", "warmConnection": False},
            {"id": "fx30-b", "warmConnection": True},
        ]
        controller = MagicMock()
        controller.start.return_value = True
        warm_controller_for.return_value = controller

        results = bridge.warm_enabled_cameras({}, probe_status=False)

        self.assertFalse(results[0]["ok"])
        self.assertFalse(results[0]["warmConnection"])
        self.assertTrue(results[1]["ok"])
        warm_controller_for.assert_called_once_with({"id": "fx30-b", "warmConnection": True})
        controller.start.assert_called_once_with(90)

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

    @patch("bridge.json_response")
    def test_warm_endpoint_refuses_while_another_warm_batch_is_running(self, json_response):
        handler = object.__new__(bridge.BridgeHandler)
        handler.config = {}
        bridge.WARM_BATCH_LOCK.acquire()
        try:
            handler.handle_warm_connections()
        finally:
            bridge.WARM_BATCH_LOCK.release()

        self.assertEqual(json_response.call_args.args[1], 409)
        self.assertEqual(json_response.call_args.args[2]["error"], "Warm reconnect already in progress")

    @patch("bridge.json_response")
    @patch("bridge.ensure_warm_controllers")
    def test_warm_endpoint_refuses_while_record_action_or_ingest_is_running(
        self,
        ensure_warm_controllers,
        json_response,
    ):
        handler = object.__new__(bridge.BridgeHandler)
        handler.config = {}
        for state_key in ("record_action_running", "ingest_running"):
            with self.subTest(state_key=state_key):
                with bridge.STATE_LOCK:
                    bridge.STATE[state_key] = True
                handler.handle_warm_connections()
                with bridge.STATE_LOCK:
                    bridge.STATE[state_key] = False

        ensure_warm_controllers.assert_not_called()
        self.assertEqual(json_response.call_count, 2)
        for call in json_response.call_args_list:
            self.assertEqual(call.args[1], 409)

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

    @patch("bridge.WarmCameraController._assert_identity", return_value=True)
    @patch("bridge.subprocess.Popen")
    @patch("bridge.os.close")
    @patch("bridge.pty.openpty", return_value=(10, 11))
    def test_overlapping_starts_launch_one_helper(
        self,
        _openpty,
        _close,
        popen,
        _assert_identity,
    ):
        controller = bridge.WarmCameraController({
            "id": "fx30-a",
            "mac": "78:F5:05:44:97:0E",
            "statusCommand": ["/usr/bin/true"],
        })
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        entered = Event()
        release = Event()

        def wait_for_ready(_prefix, _timeout_seconds):
            entered.set()
            release.wait(1)
            return 'SONY_REC_READY {"mac": "78:F5:05:44:97:0E"}'

        controller._wait_for_prefix = MagicMock(side_effect=wait_for_ready)
        controller._read_stdout = MagicMock()
        first = Thread(target=controller.start, args=(12,))
        second = Thread(target=controller.start, args=(12,))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertEqual(popen.call_count, 1)
        finally:
            release.set()
        first.join(1)
        second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(popen.call_count, 1)

    @patch("bridge.os.close")
    @patch("bridge.os.write")
    def test_warm_stop_terminates_and_reaps_unresponsive_helper(self, _write, _close):
        controller = bridge.WarmCameraController({"id": "fx30-a"})
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [
            bridge.subprocess.TimeoutExpired("sony-rec", 1),
            None,
        ]
        controller.proc = proc
        controller.terminal_fd = 10

        controller.stop()

        proc.terminate.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertIsNone(controller.proc)
        self.assertIsNone(controller.terminal_fd)

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

    @patch("bridge.warm_controller_for")
    def test_ftp_test_uses_identity_verified_warm_connection(self, warm_controller_for):
        controller = MagicMock()
        controller.run.return_value = {"id": "fx30-a", "ok": True}
        warm_controller_for.return_value = controller

        result = bridge.run_camera_command(
            {"id": "fx30-a", "mac": "78:F5:05:44:97:0E", "warmConnection": True},
            "ftp-test",
            12,
        )

        self.assertTrue(result["ok"])
        controller.run.assert_called_once_with("ftp-test", 12)

    @patch("bridge.subprocess.run")
    def test_format_command_includes_configured_mac(self, subprocess_run):
        subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = {
            "cameras": [{
                "id": "fx30-a",
                "enabled": True,
                "mac": "78:F5:05:44:97:0E",
            }],
        }

        result = bridge.format_camera_card(config, "fx30-a", 1)

        self.assertTrue(result["ok"])
        command = subprocess_run.call_args.args[0]
        self.assertEqual(command[command.index("--mac") + 1], "78:F5:05:44:97:0E")

    @patch("cleanup_manager.subprocess.run")
    def test_cleanup_delete_command_includes_configured_mac(self, subprocess_run):
        subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = {
            "cameras": [{
                "id": "fx30-a",
                "enabled": True,
                "mac": "78:F5:05:44:97:0E",
            }],
        }

        result = cleanup_manager.delete_camera_file(config, "fx30-a", "C0001.MP4")

        self.assertTrue(result["ok"])
        command = subprocess_run.call_args.args[0]
        self.assertEqual(command[command.index("--mac") + 1], "78:F5:05:44:97:0E")

    @patch("bridge.Thread")
    @patch("bridge.run_camera_command", return_value={"ok": True, "stdout": 'SONY_REC_CARD_STATUS {"ok":true,"slots":[]}'})
    @patch("bridge.warm_controller_for")
    @patch("bridge.time.sleep")
    def test_card_status_restarts_warm_connection_in_background(
        self,
        _sleep,
        warm_controller_for,
        _run_camera_command,
        thread,
    ):
        controller = MagicMock()
        controller.proc.poll.return_value = None
        warm_controller_for.return_value = controller
        bridge.CAMERA_CONTROLLERS["fx30-a"] = controller
        config = {
            "cameras": [{"id": "fx30-a", "enabled": True}],
            "commandTimeoutSeconds": 12,
        }

        result = bridge.refresh_camera_card_status(config, "fx30-a")

        self.assertTrue(result["ok"])
        controller.stop.assert_called_once()
        thread.assert_called_once_with(target=controller.start, args=(12,), daemon=True)
        thread.return_value.start.assert_called_once()
        bridge.CAMERA_CONTROLLERS.pop("fx30-a", None)

    @patch("bridge.update_ingest_progress")
    @patch("bridge.enabled_cameras")
    @patch("bridge.warm_enabled_cameras")
    def test_post_ingest_restart_delegates_to_parallel_warm_helper(
        self,
        warm_enabled_cameras,
        enabled_cameras,
        update_ingest_progress,
    ):
        config = {"cameras": [{"id": "fx30-a"}, {"id": "fx30-b", "warmConnection": False}]}
        enabled_cameras.return_value = config["cameras"]
        warm_enabled_cameras.return_value = [
            {"id": "fx30-a", "ok": True, "warmConnection": True},
            {"id": "fx30-b", "ok": False, "warmConnection": False},
        ]

        restarted = bridge.restart_warm_controllers_after_ingest(config)

        warm_enabled_cameras.assert_called_once_with(config, probe_status=False)
        self.assertEqual(restarted, [{"id": "fx30-a", "ok": True}])
        update_ingest_progress.assert_called_once_with(warmConnectionsRestarted=restarted)

    @patch("bridge.open_dashboard")
    @patch("bridge.subprocess.Popen")
    @patch("bridge.enabled_cameras")
    @patch("bridge.warm_enabled_cameras")
    def test_background_initialization_never_opens_operator_apps(
        self,
        warm_enabled_cameras,
        enabled_cameras,
        popen,
        open_dashboard,
    ):
        config = {"cameras": [{"id": "fx30-a"}]}
        enabled_cameras.return_value = config["cameras"]
        warm_enabled_cameras.return_value = [{"id": "fx30-a", "ok": True, "warmConnection": True}]

        bridge.initialize_cameras(config, open_operator_apps=False)

        warm_enabled_cameras.assert_called_once_with(config, probe_status=False)
        popen.assert_not_called()
        open_dashboard.assert_not_called()

    @patch("bridge.open_dashboard")
    @patch("bridge.subprocess.Popen")
    @patch("bridge.enabled_cameras")
    @patch("bridge.warm_enabled_cameras")
    def test_foreground_initialization_opens_operator_apps_only_when_ready(
        self,
        warm_enabled_cameras,
        enabled_cameras,
        popen,
        open_dashboard,
    ):
        config = {"cameras": [{"id": "fx30-a"}]}
        enabled_cameras.return_value = config["cameras"]
        warm_enabled_cameras.return_value = [{"id": "fx30-a", "ok": False, "warmConnection": False}]

        bridge.initialize_cameras(config, open_operator_apps=True)

        popen.assert_not_called()
        open_dashboard.assert_not_called()

        warm_enabled_cameras.return_value = [{"id": "fx30-a", "ok": True, "warmConnection": True}]
        bridge.initialize_cameras(config, open_operator_apps=True)

        popen.assert_called_once()
        open_dashboard.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
