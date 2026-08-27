#!/usr/bin/python3
import json
import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path.home() / "Library/Application Support/StudioOS/Config/fx30-bridge.json"
sys.path.insert(0, str(PACKAGE_DIR))

import bridge


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    bridge.watch_v4_completion_email(config)


if __name__ == "__main__":
    main()