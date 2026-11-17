"""Container / launchd health check: exit 0 when the tools work and the data dir is writable."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from clipwatcher import config


def main() -> int:
    data = Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work"))
    try:
        data.mkdir(parents=True, exist_ok=True)
        (data / ".health").write_text("ok")
        subprocess.run([config.ffmpeg_bin(), "-version"], capture_output=True, check=True, timeout=10)
        subprocess.run(["streamlink", "--version"], capture_output=True, check=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"unhealthy: {e!r}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
