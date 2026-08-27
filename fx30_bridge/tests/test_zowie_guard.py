import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zowie_guard


class ZowieGuardTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
