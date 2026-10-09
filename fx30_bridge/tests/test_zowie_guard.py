import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zowie_guard


class ZowieGuardTests(unittest.TestCase):
    def test_4k_write_caps_controlled_stream_gop(self):
        encoders = [
            {"width": 1920, "height": 1080, "framerate": 30, "gop": 60},
            {"width": 640, "height": 360, "framerate": 15, "gop": 30},
            {"width": 3840, "height": 2160, "framerate": 30, "gop": 60},
        ]
        verified = [
            {"width": 3840, "height": 2160},
            {"width": 640, "height": 360},
            {"width": 3840, "height": 2160},
        ]

        with patch("zowie_guard._login", return_value="session"), \
                patch("zowie_guard._read_encoders", side_effect=[encoders, verified]), \
                patch("zowie_guard._write_encoders") as write_encoders:
            result = zowie_guard.ensure_resolution("192.168.0.101", "admin", "secret")

        submitted = write_encoders.call_args.args[2]
        self.assertEqual((submitted[0]["width"], submitted[0]["height"]), (3840, 2160))
        self.assertEqual(submitted[0]["gop"], 30)
        self.assertEqual(submitted[1]["gop"], 15)
        self.assertEqual(submitted[2]["gop"], 60)
        self.assertTrue(result["changed"])

    def test_4k_verification_never_writes_a_setting(self):
        encoders = [{"width": 3840, "height": 2160}]

        with patch("zowie_guard._login", return_value="session"), \
                patch("zowie_guard._read_encoders", return_value=encoders), \
                patch("zowie_guard._write_encoders") as write_encoders:
            result = zowie_guard.verify_resolution("192.168.0.101", "admin", "secret")

        self.assertEqual(result["after"], (3840, 2160))
        self.assertFalse(result["changed"])
        write_encoders.assert_not_called()

    @patch("zowie_guard.subprocess.run")
    def test_configured_password_reads_keychain_without_printing_secret(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "secret-value\n"

        password = zowie_guard._configured_password({
            "passwordEnv": "FX30_ZOWIE_B_PASSWORD",
            "allowFactoryDefaultPassword": False,
        })

        self.assertEqual(password, "secret-value")
        run.assert_called_once()

    @patch("zowie_guard.time.sleep")
    @patch("zowie_guard._configured_password", return_value="secret")
    @patch("zowie_guard.ensure_resolution")
    def test_config_requires_consecutive_stable_4k_reads(
        self, ensure_resolution, _configured_password, sleep
    ):
        ensure_resolution.return_value = {
            "host": "192.168.0.101",
            "before": (3840, 2160),
            "after": (3840, 2160),
            "changed": False,
        }
        config = {
            "zowieResolutionGuard": {
                "enabled": True,
                "stableReadCount": 3,
                "stableReadIntervalSeconds": 2,
                "devices": [{"name": "Zowie A", "host": "192.168.0.101"}],
            }
        }

        results = zowie_guard.guard_configured_devices(config)

        self.assertEqual(ensure_resolution.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(results[0]["stableReadCount"], 3)

    @patch("zowie_guard.guard_configured_devices")
    def test_config_cli_runs_all_configured_devices(self, guard):
        guard.return_value = [{
            "name": "Zowie A",
            "before": (1920, 1080),
            "after": (3840, 2160),
            "changed": True,
        }]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"zowieResolutionGuard": {"enabled": True}}))
            with patch("zowie_guard.os.sys.argv", [
                "zowie_guard.py", "--config", str(config_path)
            ]):
                result = zowie_guard.main()

        self.assertEqual(result, 0)
        guard.assert_called_once()

    def test_encoder_write_sends_only_main_stream_and_checks_rsp(self):
        encoders = [{"width": 3840}, {"width": 640}]
        with patch("zowie_guard._post", return_value={"status": "00000", "rsp": "succeed"}) as post:
            zowie_guard._write_encoders("h", "uuid", encoders, 5)
        self.assertEqual(post.call_args.args[2]["venc"], [{"width": 3840}])

        rejected = {"status": "00000", "rsp": "invalid keyinterval, must be in range: 20~300\n"}
        with patch("zowie_guard._post", return_value=rejected):
            with self.assertRaises(zowie_guard.ZowieGuardError):
                zowie_guard._write_encoders("h", "uuid", encoders, 5)


if __name__ == "__main__":
    unittest.main()
