import json
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storage_manager import GB, StorageManager


def config_for(root: Path) -> dict:
    return {
        "sourceRoot": str(root / "ssd" / "recordings"),
        "proxyWorkingRoot": str(root / "mac" / "proxies"),
        "deliveryRoot": str(root / "lucidlink" / "masters"),
        "storagePolicy": {
            "targetHours": 3,
            "safetyMarginPercent": 15,
            "extremeSsdBaselineGBPerHour": 10,
            "proxyBaselineGBPerHour": 1,
            "rodecasterStaleHours": 24,
            "stateFile": str(root / "state.json"),
            "modeFile": str(root / "mode"),
        },
    }


class StorageManagerTests(unittest.TestCase):
    def test_required_bytes_include_target_and_safety_margin(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = StorageManager(config_for(Path(temp)))
            self.assertEqual(manager._required(10 * GB), round(10 * GB * 3 * 1.15))

    def test_missing_required_disk_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.joinpath("mode").write_text("ndi-test")
            with patch("storage_manager.existing_storage_path", return_value=None):
                report = StorageManager(config_for(root)).report()
            self.assertFalse(report["ok"])
            self.assertEqual(report["status"], "unavailable")
            self.assertIn("extreme-ssd", report["hardFailures"])

    def test_low_capacity_is_warning_not_hard_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("ssd", "mac", "lucidlink"):
                root.joinpath(name).mkdir()
            root.joinpath("mode").write_text("ndi-test")
            StorageManager(config_for(root)).confirm_rodecaster_capacity(300, "RØDECaster device screen")
            usage = type("Usage", (), {"total": 100 * GB, "used": 95 * GB, "free": 5 * GB})()
            with patch("storage_manager.shutil.disk_usage", return_value=usage):
                report = StorageManager(config_for(root)).report()
            self.assertTrue(report["ok"])
            self.assertEqual(report["status"], "warning")

    def test_legacy_rodecaster_cache_is_not_treated_as_capacity_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("ssd", "mac", "lucidlink"):
                root.joinpath(name).mkdir()
            root.joinpath("mode").write_text("ndi-test")
            root.joinpath("state.json").write_text(json.dumps({
                "rodecaster": {
                    "checkedAt": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                    "freeBytes": 20 * GB,
                    "totalBytes": 32 * GB,
                    "bytesPerHour": 2 * GB,
                }
            }))
            manager = StorageManager(config_for(root))
            capacity = next(item for item in manager.report()["surfaces"] if item["key"] == "rodecaster-capacity")
            self.assertEqual(capacity["state"], "unavailable")
            self.assertFalse(capacity["gateOpen"])

    def test_sony_cards_are_bypassed_in_ndi_test(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            self.assertTrue(all(item["status"] == "bypassed" for item in manager._sony()))

    def test_fx30_test_requires_only_cameras_enabled_by_bridge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.joinpath("mode").write_text("fx30-test")
            payload = {
                "cameraStatus": [{"id": "fx30-a", "warmConnection": True}],
                "cardStatus": {
                    "fx30-a": {"slots": [{"remainingMinutes": 240}]},
                },
            }
            with patch("storage_manager.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                rows = StorageManager(config_for(root))._sony()
            self.assertEqual([item["key"] for item in rows], ["fx30-a"])
            self.assertEqual(rows[0]["status"], "ready")

    def test_multiple_paths_on_same_ssd_produce_one_surface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("ssd", "mac", "lucidlink"):
                root.joinpath(name).mkdir()
            root.joinpath("mode").write_text("ndi-test")
            report = StorageManager(config_for(root)).report()
            self.assertEqual(sum(item["key"] == "extreme-ssd" for item in report["surfaces"]), 1)

    def test_rodecaster_capacity_is_required_even_when_legacy_preflight_flag_is_false(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            config = config_for(root)
            config["storagePolicy"]["includeRodecasterInPreflight"] = False
            manager = StorageManager(config)

            report = manager.report()

            capacity = next(item for item in report["surfaces"] if item["key"] == "rodecaster-capacity")
            self.assertFalse(report["ok"])
            self.assertEqual(capacity["state"], "unavailable")
            self.assertFalse(capacity["gateOpen"])

    def test_local_only_preflight_uses_one_combined_ssd_surface(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("ssd", "recordings").mkdir(parents=True)
            root.joinpath("mode").write_text("ndi-test")
            config = config_for(root)
            config["postRecord"] = {"localOnly": True}
            manager = StorageManager(config)

            report = manager.report()

            required = [item for item in report["surfaces"] if item.get("required")]
            self.assertEqual([item["key"] for item in required], ["extreme-ssd", "rodecaster-capacity"])
            self.assertEqual(required[0]["bytesPerHour"], 11 * GB)

    def test_zero_or_below_threshold_confirmation_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            capacity = manager.confirm_rodecaster_capacity(0, "RØDECaster device screen")
            self.assertEqual(capacity["state"], "below-threshold")
            self.assertFalse(capacity["gateOpen"])

    def test_confirmation_carries_across_verified_takes_and_charges_conservatively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            capacity = manager.confirm_rodecaster_capacity(300, "RØDECaster device screen")
            self.assertEqual(capacity["state"], "confirmed")
            self.assertTrue(capacity["gateOpen"])
            capacity = manager.mark_rodecaster_take_complete(100.0, 161.0)
            capacity = manager.rodecaster_capacity()
            self.assertEqual(capacity["state"], "confirmed")
            self.assertTrue(capacity["gateOpen"])
            self.assertEqual(capacity["remainingMinutes"], 298.0)
            self.assertEqual(capacity["lastVerifiedTake"]["chargedMinutes"], 2)

    def test_verified_take_that_drops_below_threshold_blocks_the_next_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            manager.confirm_rodecaster_capacity(208, "RØDECaster device screen")

            capacity = manager.mark_rodecaster_take_complete(100.0, 161.0)

            self.assertEqual(capacity["state"], "below-threshold")
            self.assertEqual(capacity["remainingMinutes"], 206.0)
            self.assertFalse(capacity["gateOpen"])

    def test_expired_and_single_use_override_never_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            capacity = manager.override_rodecaster_capacity("OVERRIDE RØDECASTER CAPACITY", "screen inaccessible during recovery")
            self.assertEqual(capacity["state"], "override")
            self.assertEqual(capacity["status"], "warning")
            self.assertTrue(capacity["gateOpen"])
            manager.consume_rodecaster_override()
            self.assertFalse(manager.rodecaster_capacity()["gateOpen"])

    def test_expired_confirmation_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            manager.confirm_rodecaster_capacity(300, "RØDECaster device screen")
            state = manager._state()
            state["rodecasterCapacity"]["confirmationDate"] = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
            manager._write_state(state)
            capacity = manager.rodecaster_capacity()
            self.assertEqual(capacity["state"], "stale")
            self.assertFalse(capacity["gateOpen"])

    def test_bridge_restart_requires_new_capacity_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            config = config_for(root)
            first = StorageManager(config, capacity_session_id="first-bridge")
            first.confirm_rodecaster_capacity(300, "RØDECaster device screen")

            capacity = StorageManager(config, capacity_session_id="restarted-bridge").rodecaster_capacity()

            self.assertEqual(capacity["state"], "stale")
            self.assertFalse(capacity["gateOpen"])

    def test_malformed_or_unknown_take_evidence_requires_attention_and_reconfirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            manager.confirm_rodecaster_capacity(300, "RØDECaster device screen")

            capacity = manager.mark_rodecaster_take_attention("stop verification failed")

            self.assertEqual(capacity["state"], "attention")
            self.assertFalse(capacity["gateOpen"])

    def test_malformed_persisted_evidence_cannot_open_the_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("mode").write_text("ndi-test")
            manager = StorageManager(config_for(root))
            manager._write_state({"rodecasterCapacity": {
                "state": "confirmed", "remainingMinutes": "lots", "source": "", "confirmedAt": "not-a-time"
            }})

            capacity = manager.rodecaster_capacity()

            self.assertEqual(capacity["state"], "attention")
            self.assertEqual(capacity["status"], "warning")
            self.assertFalse(capacity["gateOpen"])


if __name__ == "__main__":
    unittest.main()
