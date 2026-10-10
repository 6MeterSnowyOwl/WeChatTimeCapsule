from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


ALLOWED_STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_REQUIRED", "PENDING_POST_UPLOAD"}
FORBIDDEN_DIRECTORY_NAMES = {
    ".git", ".pytest_cache", ".venv", ".venv-release-py312", "__pycache__",
    "build", "diagnostics", "dist", "exports", "logs", "outputs", "snapshots",
    "state", "state_backups", "work",
}
FORBIDDEN_EXTENSIONS = {".db", ".db-shm", ".db-wal", ".pdb", ".sqlite", ".sqlite3"}
DEFAULT_PRIVATE_PATTERNS = ("wx" + "id_",)
CLEAN_MACHINE_FIELDS = {
    "windows_version", "architecture", "python_installed", "app_started", "exit_code",
    "dll_error", "package_version", "package_sha256",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_bytes(payload: bytes, privacy_patterns: Iterable[str] = DEFAULT_PRIVATE_PATTERNS) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for pattern in privacy_patterns:
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            needle = pattern.encode(encoding)
            if needle and needle in payload:
                hits.append({"pattern": pattern, "encoding": encoding})
                break
    return hits


def scan_tree(
    root: Path,
    *,
    privacy_patterns: Iterable[str] = DEFAULT_PRIVATE_PATTERNS,
    check_forbidden: bool = True,
) -> list[dict[str, str]]:
    root = root.resolve()
    patterns = tuple(privacy_patterns)
    hits: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        for pattern in patterns:
            if pattern in relative:
                hits.append({"kind": "path", "path": relative, "pattern": pattern, "encoding": "text"})
        if check_forbidden:
            parts = {part.casefold() for part in path.relative_to(root).parts}
            if path.is_dir() and path.name.casefold() in FORBIDDEN_DIRECTORY_NAMES:
                hits.append({"kind": "forbidden", "path": relative, "pattern": path.name, "encoding": "path"})
            if path.is_file() and path.suffix.casefold() in FORBIDDEN_EXTENSIONS:
                hits.append({"kind": "forbidden", "path": relative, "pattern": path.suffix, "encoding": "path"})
        if path.is_file():
            for found in scan_bytes(path.read_bytes(), patterns):
                hits.append({"kind": "content", "path": relative, **found})
    return hits


def validate_clean_machine_result(payload: dict[str, object]) -> dict[str, object]:
    unexpected = sorted(set(payload) - CLEAN_MACHINE_FIELDS)
    missing = sorted(CLEAN_MACHINE_FIELDS - set(payload))
    if unexpected:
        raise ValueError(f"unexpected fields: {', '.join(unexpected)}")
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if not isinstance(payload["python_installed"], bool) or not isinstance(payload["app_started"], bool):
        raise ValueError("boolean result fields are invalid")
    if not isinstance(payload["dll_error"], bool):
        raise ValueError("dll_error must be boolean")
    if not isinstance(payload["exit_code"], int):
        raise ValueError("exit_code must be an integer")
    digest = str(payload["package_sha256"])
    if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
        raise ValueError("package_sha256 is invalid")
    return payload


def summarize_connections(
    records: Iterable[dict[str, object]], *, app_pids: set[int], weixin_pids: set[int]
) -> dict[str, object]:
    result: dict[str, object] = {
        "app": {"process": "微信时间胶囊.exe", "pids": sorted(app_pids), "outbound_count": 0, "records": []},
        "weixin": {"process": "Weixin.exe", "pids": sorted(weixin_pids), "outbound_count": 0, "records": []},
        "unattributed_count": 0,
    }
    for record in records:
        pid = int(record.get("pid", -1))
        sanitized = {
            "pid": pid,
            "protocol": str(record.get("protocol", "")),
            "remote": str(record.get("remote", "")),
        }
        if pid in app_pids:
            bucket = result["app"]
        elif pid in weixin_pids:
            bucket = result["weixin"]
        else:
            result["unattributed_count"] = int(result["unattributed_count"]) + 1
            continue
        assert isinstance(bucket, dict)
        bucket["outbound_count"] = int(bucket["outbound_count"]) + 1
        bucket["records"].append(sanitized)
    return result


def evaluate_public_release(
    gates: dict[str, str], hard_gates: Iterable[str]
) -> tuple[bool, list[str]]:
    invalid = {name: status for name, status in gates.items() if status not in ALLOWED_STATUSES}
    if invalid:
        raise ValueError(f"invalid gate status: {invalid}")
    blockers = [name for name in hard_gates if gates.get(name) in {"FAIL", "BLOCKED"}]
    missing = [name for name in hard_gates if name not in gates]
    blockers.extend(missing)
    return not blockers, blockers


def validate_release_evidence(
    evidence: dict[str, object], expected_source_hashes: dict[str, str]
) -> list[str]:
    issues: list[str] = []
    if evidence.get("apache_license_matches") is not True:
        issues.append("Apache-2.0 LICENSE mismatch")
    if evidence.get("third_party_license_file_count") != 66:
        issues.append("third-party license tree is incomplete")
    actual_sources = evidence.get("source_hashes")
    if not isinstance(actual_sources, dict) or any(
        actual_sources.get(name) != digest for name, digest in expected_source_hashes.items()
    ):
        issues.append("corresponding-source hash mismatch")
    for key, label in (
        ("binary_privacy_hits", "binary privacy hits"),
        ("source_privacy_hits", "source privacy hits"),
    ):
        count = evidence.get(key)
        if count != 0:
            issues.append(f"{label}: {count}")
    tested = evidence.get("tested_wechat_versions")
    tested = set(tested) if isinstance(tested, list) else set()
    if not {"4.1.13.63", "4.1.13.65"}.issubset(tested):
        issues.append("tested WeChat version metadata is incomplete")
    return issues


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
