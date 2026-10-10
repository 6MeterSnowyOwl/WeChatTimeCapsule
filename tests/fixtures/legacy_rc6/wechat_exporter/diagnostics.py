from __future__ import annotations

import json
import locale
import os
import platform
import re
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6 import __version__ as pyside_version

from . import PRODUCT_NAME, PRODUCT_SLUG, __version__
from .archive import SNAPSHOT_SCHEMA_VERSION
from .account import upstream_version
from .migrations import CURRENT_MESSAGE_SCHEMA_VERSION, CURRENT_MUTABLE_SCHEMA_VERSION


@dataclass(frozen=True)
class DiagnosticsResult:
    path: Path
    files: tuple[str, ...]
    sensitive_hits: int


WXID_PREFIX = "wx" + "id_"
WXID_PATTERN = re.compile(rf"\b{WXID_PREFIX}[A-Za-z0-9_]+\b", re.IGNORECASE)
USER_PATH_PATTERN = re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+", re.IGNORECASE)
SECRET_WORD_PATTERN = re.compile(
    r"\b[A-Za-z0-9_]*(?:DATABASE_KEY|MEDIA_KEY|API_KEY|ACCESS_TOKEN|TOKEN|SECRET)[A-Za-z0-9_]*\b",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<name>(?:db|database|media|api)?[_ -]?(?:key|token|secret|password))"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)


def sanitize_text(text: str, sensitive_values: tuple[str, ...] = ()) -> str:
    result = str(text)
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    replacements = [profile, *sensitive_values]
    for value in sorted({item for item in replacements if item}, key=len, reverse=True):
        pattern = re.escape(value)
        if value.replace("_", "").isalnum():
            pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
        result = re.sub(pattern, "<REDACTED>", result, flags=re.IGNORECASE)
    result = USER_PATH_PATTERN.sub(r"%USERPROFILE%\\<REDACTED>", result)
    result = WXID_PATTERN.sub("<REDACTED_WXID>", result)
    result = SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("name") + match.group("sep") + "<REDACTED_SECRET>",
        result,
    )
    result = SECRET_WORD_PATTERN.sub("<REDACTED_SECRET>", result)
    return result


def _display_scale_percent() -> int | None:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        return int(ctypes.windll.shcore.GetScaleFactorForDevice(0))
    except Exception:
        return None


def _filesystem_type(path: Path) -> str:
    if platform.system() != "Windows":
        return "unknown"
    try:
        import ctypes

        fs_name = ctypes.create_unicode_buffer(64)
        root = str(path.resolve().anchor)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, None, None, None, fs_name, len(fs_name)
        )
        return fs_name.value if ok else "unknown"
    except Exception:
        return "unknown"


def _hardlink_supported(app_data_dir: Path) -> bool:
    app_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=app_data_dir) as temporary:
            root = Path(temporary)
            source = root / "source.tmp"
            linked = root / "linked.tmp"
            source.write_bytes(b"hardlink-probe")
            os.link(source, linked)
            return linked.read_bytes() == b"hardlink-probe"
    except OSError:
        return False


def _recent_log(path: Path, limit: int = 200_000) -> str:
    if not path.is_file():
        return "No GUI log is available.\n"
    with path.open("rb") as handle:
        size = path.stat().st_size
        if size > limit:
            handle.seek(-limit, os.SEEK_END)
        return handle.read().decode("utf-8", "replace")


def summarize_recent_log(text: str) -> str:
    """Keep chronology/severity only; discard all free-form log message text."""
    result = []
    pattern = re.compile(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:,\d+)?)\s+"
        r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\b"
    )
    for line in text.splitlines()[-1000:]:
        match = pattern.match(line)
        if match:
            result.append(f"{match.group('timestamp')} {match.group('level')} details_redacted")
        elif line.strip():
            result.append("unstructured_log_line_redacted")
    return "\n".join(result) + ("\n" if result else "")


def generate_diagnostics(service) -> DiagnosticsResult:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    destination_dir = service.settings.app_data_dir / "diagnostics"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"diagnostics_{timestamp}.zip"
    counter = 1
    while destination.exists():
        destination = destination_dir / f"diagnostics_{timestamp}_{counter:02d}.zip"
        counter += 1

    targets = service._targets()
    sensitive_values: list[str] = []
    for target in targets:
        sensitive_values.extend((target.display_name, target.username, str(target.root)))
    for account in service.account_options():
        sensitive_values.extend((account.stable_id, account.account_locator))
    status = service.detect_wechat()
    if status.account_name:
        sensitive_values.append(status.account_name)

    issue_counts: Counter[str] = Counter()
    capsule_count = 0
    passed = 0
    failed = 0
    for target in targets:
        if not (target.root / "chat.md").is_file() or not (target.root / "capsule_meta.json").is_file():
            continue
        result = service.verify_capsule(target.root)
        capsule_count += 1
        if result.ok and result.manifest_present:
            passed += 1
        else:
            failed += 1
        issue_counts.update(issue.code for issue in result.issues)
        if result.ok and not result.manifest_present:
            issue_counts["NO_MANIFEST"] += 1

    documents = {
        "diagnostics/app.json": {
            "product_name": PRODUCT_NAME,
            "product_id": PRODUCT_SLUG,
            "product_version": __version__,
            "python_version": platform.python_version(),
            "pyside6_version": pyside_version,
            "wechatauto_replica_version": upstream_version(),
            "mutable_schema_version": CURRENT_MUTABLE_SCHEMA_VERSION,
            "message_schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "current_capsule_schema_version": 1,
        },
        "diagnostics/environment.json": {
            "operating_system": platform.system(),
            "windows_release": platform.release(),
            "windows_version": platform.version(),
            "architecture": platform.machine(),
            "display_scale_percent": _display_scale_percent(),
            "locale": locale.getlocale()[0] or "unknown",
            "filesystem_type": _filesystem_type(service.settings.app_data_dir),
            "hardlink_supported": _hardlink_supported(service.settings.app_data_dir),
        },
        "diagnostics/compatibility.json": {
            "wechat_detected": status.runtime_capability.value
            != "WECHAT_NOT_RUNNING",
            "wechat_version": status.version or "unknown",
            "verification": status.version_verification.value.lower(),
            "runtime_capability": status.runtime_capability.value.lower(),
            "probe_error_code": status.probe_error_code.value.lower(),
            "account_count": status.account_count,
            "operations_allowed": status.operations_allowed,
        },
        "diagnostics/integrity_summary.json": {
            "target_count": len(targets),
            "current_capsule_count": capsule_count,
            "pass_count": passed,
            "fail_count": failed,
            "error_code_counts": dict(sorted(issue_counts.items())),
        },
    }
    safe_values = tuple(item for item in sensitive_values if item)
    serialized: dict[str, str] = {
        name: sanitize_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", safe_values
        )
        for name, payload in documents.items()
    }
    serialized["diagnostics/recent_logs.txt"] = summarize_recent_log(
        _recent_log(service.settings.app_data_dir / "logs" / "gui.log")
    )

    forbidden = tuple(
        item for item in safe_values
        if len(item) >= 3 and item not in {"Windows", "unknown"}
    )
    hits = 0
    for content in serialized.values():
        for value in forbidden:
            pattern = re.escape(value)
            if value.replace("_", "").isalnum():
                pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
            hits += len(re.findall(pattern, content, flags=re.IGNORECASE))
    if hits:
        raise RuntimeError("诊断报告隐私自检失败，未生成文件。")

    temporary = destination.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(serialized.items()):
                archive.writestr(name, content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return DiagnosticsResult(destination, tuple(sorted(serialized)), hits)
