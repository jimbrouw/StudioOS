import json
import tempfile
import threading
import unittest
from pathlib import Path

from just_control_api import JustControlApiError
from v4_bridge import ACTIVE_POST_STATES, Bridge


class FakeStorage:
    def __init__(self, root: Path, mode: str = "strict"):
        self.state_file = root / "state.json"
        self.mode_file = root / "mode"
        self.mode_file.write_text(mode)
        self.fx30_status_url = "http://127.0.0.1:3030/record/status"
        self.state = {
            "failedStop": {
                "action": "stop",
                "failedAt": "2026-08-17T17:58:05+00:00",
                "channelErrors": {"Channel1": "connection refused"},
            }
        }

    def _state(self):
        return json.loads(json.dumps(self.state))

    def _write_state(self, state):
        self.state = json.loads(json.dumps(state))
        self.state_file.write_text(json.dumps(self.state, indent=2))


class FakeApi:
    def __init__(self, states=None, unknown=None):
        self.states = states or {"Channel1": False, "Channel2": False, "Channel3": False}
        self.unknown = set(unknown or [])
        self.status_calls = 0
        self.stop_calls = 0

    def all_channels(self):
        return list(self.states)

    def recording_status(self, name):
        self.status_calls += 1
        if name in self.unknown:
            raise JustControlApiError("Just In status unavailable")
        return {"rec": self.states[name]}

    def stop_recording(self, _name):
        self.stop_calls += 1
        raise AssertionError("Recovery must never send Just In STOP")


def bridge_for(root: Path, *, mode="strict", api=None) -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.api = api or FakeApi()
    bridge.channels = [{"name": name} for name in ("Channel1", "Channel2", "Channel3")]
    bridge.config = {"failedStopRecovery": {}}
    bridge.lock = threading.RLock()
    bridge.recording = True
    bridge.session_started_at = 1_786_000_000.0
    bridge.last_result = {"ok": False, "action": "stop"}
    bridge.post_status = {"state": "idle"}
    bridge.storage_manager = FakeStorage(root, mode)
    bridge.recording_lock_path = root / "strict_studio_recording.lock"
    bridge.stop_marker_path = root / "strict_studio_stop.sent"
    bridge.recording_lock_path.touch()
    bridge.stop_marker_path.mkdir()
    bridge._failed_stop_recovery_obs = lambda: {"state": "stopped", "recording": False}
    bridge._failed_stop_recovery_fx30 = lambda: {"state": "stopped", "recording": False, "recordActionRunning": False}
    return bridge


class FailedStopRecoveryTests(unittest.TestCase):
    def recover(self, bridge):
        return bridge.recover_failed_stop(True, "physical RØDECaster REC light and timer checked off")

    def test_visibility_requires_stale_v4_all_stopped_and_no_active_post_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            status = bridge.failed_stop_recovery_status()
            self.assertTrue(status["eligible"])
            self.assertTrue(status["staleV4Session"])
            self.assertFalse(status["postRecordActive"])
            self.assertTrue(all(item["state"] == "stopped" for item in status["justIn"].values()))

    def test_blocks_if_any_just_in_channel_is_still_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary), api=FakeApi({"Channel1": False, "Channel2": True, "Channel3": False}))
            with self.assertRaisesRegex(RuntimeError, "Just In channel"):
                self.recover(bridge)
            self.assertTrue(bridge.recording)
            self.assertTrue(bridge.recording_lock_path.exists())

    def test_blocks_if_just_in_status_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary), api=FakeApi(unknown={"Channel2"}))
            with self.assertRaisesRegex(RuntimeError, "Just In channel"):
                self.recover(bridge)
            self.assertTrue(bridge.stop_marker_path.exists())

    def test_blocks_if_obs_is_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            bridge._failed_stop_recovery_obs = lambda: {"state": "recording", "recording": True}
            with self.assertRaisesRegex(RuntimeError, "OBS recording inactive"):
                self.recover(bridge)
            self.assertTrue(bridge.recording_lock_path.exists())

    def test_blocks_if_obs_status_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            bridge._failed_stop_recovery_obs = lambda: {"state": "unknown", "error": "OBS unavailable"}
            with self.assertRaisesRegex(RuntimeError, "OBS recording inactive"):
                self.recover(bridge)
            self.assertTrue(bridge.stop_marker_path.exists())

    def test_blocks_if_fx30_is_recording_or_unknown_in_camera_mode(self):
        for fx30 in ({"state": "recording", "recording": True}, {"state": "unknown", "error": "offline"}):
            with self.subTest(fx30=fx30), tempfile.TemporaryDirectory() as temporary:
                bridge = bridge_for(Path(temporary), mode="strict")
                bridge._failed_stop_recovery_fx30 = lambda fx30=fx30: fx30
                with self.assertRaisesRegex(RuntimeError, "FX30 recording inactive"):
                    self.recover(bridge)
                self.assertTrue(bridge.recording_lock_path.exists())

    def test_no_camera_mode_does_not_query_fx30_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary), mode="ndi-test")
            bridge._failed_stop_recovery_fx30 = lambda: (_ for _ in ()).throw(AssertionError("FX30 must be bypassed"))
            result = self.recover(bridge)
            self.assertTrue(result["ok"])
            self.assertEqual(result["receipt"]["observedStoppedStates"]["fx30"]["state"], "not-required")

    def test_blocks_without_physical_rodecaster_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "RØDECaster REC light"):
                bridge.recover_failed_stop(False, "")
            self.assertTrue(bridge.recording)
            self.assertTrue(bridge.stop_marker_path.exists())

    def test_blocks_while_post_record_work_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            bridge.post_status = {"state": next(iter(ACTIVE_POST_STATES))}
            with self.assertRaisesRegex(RuntimeError, "post-record work"):
                self.recover(bridge)
            self.assertTrue(bridge.recording_lock_path.exists())

    def test_success_retires_markers_only_after_complete_read_only_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            result = self.recover(bridge)
            self.assertTrue(result["ok"])
            self.assertFalse(result["alreadyRecovered"])
            self.assertFalse(bridge.recording)
            self.assertIsNone(bridge.session_started_at)
            self.assertFalse(bridge.recording_lock_path.exists())
            self.assertFalse(bridge.stop_marker_path.exists())
            self.assertEqual(bridge.api.stop_calls, 0)
            receipt = bridge.storage_manager.state["failedStopRecovery"]
            self.assertEqual(receipt["result"], "recovered")
            self.assertEqual(receipt["originalStopFailure"]["channelErrors"]["Channel1"], "connection refused")
            self.assertTrue(receipt["rodecasterConfirmation"]["confirmedOff"])
            self.assertEqual(bridge.storage_manager.state["failedStop"]["action"], "stop")

    def test_marker_retirement_failure_restores_any_marker_already_retired(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            bridge.stop_marker_path.joinpath("unexpected-content").write_text("do not remove")
            with self.assertRaisesRegex(RuntimeError, "markers could not be retired"):
                self.recover(bridge)
            self.assertTrue(bridge.recording)
            self.assertTrue(bridge.recording_lock_path.exists())
            self.assertTrue(bridge.stop_marker_path.exists())
            self.assertEqual(bridge.storage_manager.state["failedStopRecovery"]["result"], "marker-retirement-failed")

    def test_repeated_recovery_is_idempotent_and_does_not_repeat_observation_or_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = bridge_for(Path(temporary))
            self.recover(bridge)
            status_calls = bridge.api.status_calls
            result = self.recover(bridge)
            self.assertTrue(result["alreadyRecovered"])
            self.assertEqual(bridge.api.status_calls, status_calls)
            self.assertEqual(bridge.api.stop_calls, 0)
            self.assertEqual(bridge.storage_manager.state["failedStopRecovery"]["result"], "recovered")
