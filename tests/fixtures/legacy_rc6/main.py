from __future__ import annotations

import sys
from pathlib import Path

from wechat_exporter.app import run


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _configure_console()
    raise SystemExit(run(Path(__file__).resolve().parent))

