import tempfile
import unittest
from pathlib import Path

from recovery import RecoveryAttention, RecoveryWorker, SessionLedger, mapping_from_config
from v4_bridge import Bridge


def config(root: Path) -> dict:
    return {
        "channels": [
            {"name": "Channel1", "enabled": True},
            {"name": "Channel2", "enabled": False},
            {"name": "Channel3", "enabled": False},
        ],
        "postRecord": {"ffprobePath": "/fixture/ffprobe"},
        "recovery": {
            "enabled": True,
            "localTestOnly": True,
            "stagingRoot": str(root / "staging"),
            "deliveryRoot": str(root / "delivery"),
            "fixtureCandidatesRoot": str(root / "candidates"),
            "durationDifferenceSeconds": 30,
            "mappings": [
                {"logicalCamera": 1, "ndiLabel": "NDI 1", "justInChannel": "Channel1", "fx30CameraId": "fx30-a", "enabled": True},
                {"logicalCamera": 2, "ndiLabel": "NDI 2", "justInChannel": "Channel2", "fx30CameraId": "fx30-b", "enabled": False},
                {"logicalCamera": 3, "ndiLabel": "NDI 3", "justInChannel": "Channel3", "fx30CameraId": "fx30-c", "enabled": False},
            ],
        },
    }


def probe(path: Path, _ffprobe: str) -> dict:
    duration = 60 if "short" in path.name else 100
    return {
        "ok": True,
        "path": str(path),
        "sizeBytes": path.stat().st_size,
        "durationSeconds": duration,
        "width": 3840,
        "height": 2160,
        "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
    }


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = config(self.root)
        self.mapping = mapping_from_config(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, name="camera-master.mp4"):
        folder = self.root / "candidates" / "fx30-a"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / name
        source.write_bytes(b"fixture-camera-master")
        return source

    def ledger(self):
        return SessionLedger(self.root / "sessions", self.mapping, session_id="fixture-session")

    def test_only_enabled_camera_is_in_active_mapping(self):
        self.assertEqual([item["fx30CameraId"] for item in self.mapping], ["fx30-a"])

    def test_recovery_preserves_source_and_delivers_verified_package(self):
        source = self.candidate()
        ledger = self.ledger()
        worker = RecoveryWorker(self.cfg, probe=probe)

        result = worker.recover(
            ledger,
            self.mapping[0],
            {"expectedDurationSeconds": 100, "observedDurationSeconds": 100},
        )

        self.assertEqual(source.read_bytes(), b"fixture-camera-master")
        self.assertFalse(result["selected"]["requiresReview"])
        self.assertTrue(Path(result["copy"]["destination"]).is_file())
        self.assertTrue(Path(result["delivery"]["destination"]).is_file())
        self.assertEqual(ledger.manifest["state"], "editor_ready")
        event_types = [line.split('"type": "')[1].split('"')[0] for line in ledger.events_path.read_text().splitlines()]
        self.assertIn("recovery.completed", event_types)

    def test_duration_difference_over_30_seconds_requires_review_and_does_not_deliver(self):
        self.candidate("camera-short-master.mp4")
        ledger = self.ledger()
        worker = RecoveryWorker(self.cfg, probe=probe)

        result = worker.recover(
            ledger,
            self.mapping[0],
            {"expectedDurationSeconds": 100, "observedDurationSeconds": 100},
        )

        self.assertTrue(result["selected"]["requiresReview"])
        self.assertEqual(result["delivery"], None)
        self.assertEqual(ledger.manifest["state"], "attention")

    def test_multiple_candidates_fail_closed_without_copying(self):
        self.candidate("one.mp4")
        self.candidate("two.mp4")
        ledger = self.ledger()
        worker = RecoveryWorker(self.cfg, probe=probe)

        with self.assertRaisesRegex(RecoveryAttention, "exactly one"):
            worker.recover(ledger, self.mapping[0], {"expectedDurationSeconds": 100})

        self.assertFalse((self.root / "staging" / "fixture-session").exists())

    def test_event_emission_is_idempotent(self):
        ledger = self.ledger()
        first = ledger.emit("recording.start_verified")
        duplicate = ledger.emit("recording.start_verified")
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(duplicate["duplicate"])

    def test_liveness_marks_stopped_channel_as_suspected_without_control_action(self):
        class Api:
            def recording_status(self, _channel):
                return {"rec": False}

            def recording_paths(self, _channel):
                return {"paths": []}

        bridge = Bridge.__new__(Bridge)
        bridge.config = self.cfg
        bridge.recording = True
        bridge.api = Api()
        bridge.recovery_mapping = self.mapping
        bridge.recovery_ledger = self.ledger()

        bridge._observe_liveness_once()

        evidence = bridge.recovery_ledger.manifest["liveness"]["Channel1"]
        self.assertTrue(evidence["suspectedFailed"])
        self.assertEqual(bridge.recovery_ledger.manifest["state"], "recording_suspect")


if __name__ == "__main__":
    unittest.main()
