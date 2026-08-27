import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rodecaster_ingest as ingest


def make_take(root: Path, name: str, payload: bytes = b"audio", duration: float = 60.0) -> Path:
    take = root / name
    take.mkdir(parents=True)
    take.joinpath("Meta.xml").write_text(
        f"<?xml version='1.0'?><cereal><value0><duration>{duration}</duration></value0></cereal>",
        encoding="utf-8",
    )
    take.joinpath("Stereo Mix.wav").write_bytes(payload)
    return take


def args_for(root: Path, **overrides) -> argparse.Namespace:
    values = {
        "source_root": root / "card" / "RODECaster",
        "mount": None,
        "take": None,
        "staging_root": root / "ssd",
        "lucid_root": root / "shade",
        "stable_seconds": 0,
        "wait_seconds": 0,
        "dry_run": False,
        "staging_only": False,
        "all_takes": True,
        "delete_verified": True,
        "state_file": root / "state.json",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RodecasterCleanupTests(unittest.TestCase):
    def test_all_valid_takes_are_verified_twice_then_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            make_take(source, "Take 1", b"one")
            make_take(source, "Take 2", b"two")

            result = ingest.run(args_for(root))

            self.assertTrue(result["ok"])
            self.assertEqual(result["deleted"], 2)
            self.assertFalse((source / "Take 1").exists())
            self.assertEqual((root / "ssd" / "Take 1" / "Stereo Mix.wav").read_bytes(), b"one")
            self.assertEqual((root / "shade" / "Take 2" / "Stereo Mix.wav").read_bytes(), b"two")
            receipt = json.loads((root / "shade" / "_receipts" / "Take_1-archive.json").read_text())
            self.assertEqual(receipt["cleanupState"], "deleted")

    def test_destination_mismatch_retains_source_and_continues_other_takes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            make_take(source, "Bad Take", b"source")
            make_take(source, "Good Take", b"good")
            bad_destination = root / "ssd" / "Bad Take"
            make_take(root / "ssd", "Bad Take", b"different")

            result = ingest.run(args_for(root))

            self.assertFalse(result["ok"])
            self.assertTrue((source / "Bad Take").exists())
            self.assertFalse((source / "Good Take").exists())
            self.assertEqual(len(result["failures"]), 1)
            self.assertTrue(bad_destination.exists())

    def test_dry_run_never_deletes_or_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            make_take(source, "Take 1")

            result = ingest.run(args_for(root, dry_run=True, delete_verified=False))

            self.assertTrue(result["ok"])
            self.assertTrue((source / "Take 1").exists())
            self.assertFalse((root / "ssd").exists())

    def test_invalid_folder_is_ignored_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            make_take(source, "Take 1")
            unrelated = source / "Exports"
            unrelated.mkdir(parents=True)
            unrelated.joinpath("notes.txt").write_text("keep")

            result = ingest.run(args_for(root))

            self.assertTrue(result["ok"])
            self.assertTrue(unrelated.exists())

    def test_source_is_retained_if_delete_operation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            take = make_take(source, "Take 1")

            with patch("rodecaster_ingest.shutil.rmtree", side_effect=OSError("card locked")):
                result = ingest.run(args_for(root))

            self.assertFalse(result["ok"])
            self.assertTrue(take.exists())
            self.assertEqual(result["failures"][0]["cleanupState"], "retained_attention")

    def test_delete_flag_rejects_partial_or_dry_run_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card" / "RODECaster"
            make_take(source, "Take 1")
            with self.assertRaises(ingest.IngestError):
                ingest.run(args_for(root, staging_only=True))


if __name__ == "__main__":
    unittest.main()
