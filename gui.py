from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from wechat_exporter.gui.__main__ import run

    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    raise SystemExit(run(root))
