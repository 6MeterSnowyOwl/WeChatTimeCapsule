from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_windows_name(value: str, fallback: str = "未命名会话", max_length: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    value = value[:max_length].rstrip(" .") or fallback
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = "_" + value
    return value


def escape_markdown(value: str) -> str:
    """Escape user text without turning it into HTML or a screenshot."""
    value = value.replace("\\", "\\\\")
    value = re.sub(r"([`*_{}\[\]<>])", r"\\\1", value)
    lines = []
    for line in value.splitlines() or [""]:
        line = re.sub(r"^(\s*)(#{1,6}|>|[-+]\s|\d+[.)]\s)", r"\1\\\2", line)
        lines.append(line)
    return "\n".join(lines)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wechat_exporter")
    logger.setLevel(logging.INFO)
    for existing in tuple(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
