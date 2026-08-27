import os
import tempfile
import threading
import time
import unittest
import stat
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from v4_bridge import ACTIVE_POST_STATES, Bridge


class ImmediateThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class Completed:
    def __init__(self, returncode=0, stdout="RECORD SENT: NO CAMERA", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def bridge_for(root: Path) -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.recording = False
    bridge.post_status = {"state": "idle"}
    bridge.no_camera_action_lock = threading.Lock()
    bridge.no_camera_action = {"state": "idle"}
    bridge.no_camera_arm_receipt_dir = root / "receipts"
    bridge.no_camera_arm_receipt_dir.mkdir()
    bridge.no_camera_session_path = root / "session"
    bridge.no_camera_record_command = root / "record.command"
    bridge.no_camera_stop_command = root / "stop.command"
    for command in (bridge.no_camera_record_command, bridge.no_camera_stop_command):
        command.write_text("#!/bin/bash\nexit 0\n")
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return bridge


def write_fresh_arm_receipt(bridge: Bridge) -> Path:
    receipt = bridge.no_camera_arm_receipt_dir / "20260818-arm-no-camera.txt"
    receipt.write_text("result=success mode_restored_on_exit=true\n")
    return receipt


class NoCameraRecordTests(unittest.TestCase):
    def run_immediately(self, bridge: Bridge, action: str):
        with patch("v4_bridge.threading.Thread", ImmediateThread), patch(
            "v4_bridge.subprocess.run", return_value=Completed()
        ) as run:
            result = bridge.start_no_camera_recording() if action == "start" else bridge.stop_no_camera_recording()
        return result, run

    def test_start_requires_fresh_successful_arm_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "ARM: NO CAMERA"):
                bridge.start_no_camera_recording()

    def test_start_blocks_stale_or_failed_arm_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            receipt = write_fresh_arm_receipt(bridge)
            receipt.write_text("result=failed\n")
            with self.assertRaisesRegex(RuntimeError, "did not pass"):
                bridge.start_no_camera_recording()
            write_fresh_arm_receipt(bridge)
            old = time.time() - 901
            os.utime(receipt, (old, old))
            with self.assertRaisesRegex(RuntimeError, "older than 15 minutes"):
                bridge.start_no_camera_recording()

    def test_start_blocks_recording_post_work_or_existing_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            write_fresh_arm_receipt(bridge)
            bridge.recording = True
            with self.assertRaisesRegex(RuntimeError, "already reports"):
                bridge.start_no_camera_recording()
            bridge.recording = False
            bridge.post_status = {"state": next(iter(ACTIVE_POST_STATES))}
            with self.assertRaisesRegex(RuntimeError, "post-record"):
                bridge.start_no_camera_recording()
            bridge.post_status = {"state": "idle"}
            bridge.no_camera_session_path.touch()
            with self.assertRaisesRegex(RuntimeError, "needs STOP"):
                bridge.start_no_camera_recording()

    def test_start_runs_only_the_reviewed_coordinator(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            receipt = write_fresh_arm_receipt(bridge)
            result, run = self.run_immediately(bridge, "start")
            self.assertTrue(result["accepted"])
            self.assertEqual(result["armReceipt"], str(receipt))
            run.assert_called_once_with(
                [str(bridge.no_camera_record_command)],
                cwd="/Users/jim/Developer/build",
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            self.assertEqual(bridge.no_camera_action["state"], "sent")
            self.assertEqual(bridge.no_camera_action["action"], "start")

    def test_duplicate_start_is_blocked_while_guard_is_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            write_fresh_arm_receipt(bridge)
            bridge.no_camera_action = {"state": "starting"}
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                bridge.start_no_camera_recording()

    def test_stop_requires_v4_recording_and_session_marker_then_runs_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "does not report"):
                bridge.stop_no_camera_recording()
            bridge.recording = True
            with self.assertRaisesRegex(RuntimeError, "marker is missing"):
                bridge.stop_no_camera_recording()
            bridge.no_camera_session_path.touch()
            result, run = self.run_immediately(bridge, "stop")
            self.assertTrue(result["accepted"])
            run.assert_called_once_with(
                [str(bridge.no_camera_stop_command)],
                cwd="/Users/jim/Developer/build",
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            self.assertEqual(bridge.no_camera_action["state"], "stop-sent")

    def test_stop_can_use_explicit_no_camera_mode_and_recording_lock_if_receipt_write_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = bridge_for(root)
            bridge.recording = True
            mode_file = root / "mode"
            mode_file.write_text("ndi-test\n")
            recording_lock = root / "recording.lock"
            recording_lock.touch()
            bridge.storage_manager = SimpleNamespace(mode_file=mode_file)
            bridge.recording_lock_path = recording_lock
            result, run = self.run_immediately(bridge, "stop")
            self.assertTrue(result["accepted"])
            run.assert_called_once()
            self.assertEqual(bridge.no_camera_action["state"], "stop-sent")

    def test_failed_coordinator_keeps_attention_and_never_retries_itself(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            write_fresh_arm_receipt(bridge)
            with patch("v4_bridge.threading.Thread", ImmediateThread), patch(
                "v4_bridge.subprocess.run", return_value=Completed(1, "", "STOP BLOCKED")
            ) as run:
                bridge.start_no_camera_recording()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(bridge.no_camera_action["state"], "attention")
            self.assertIn("STOP BLOCKED", bridge.no_camera_action["detail"])
