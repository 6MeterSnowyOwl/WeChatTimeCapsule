from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .utils import write_json_atomic
from .deletion import load_deleted_snapshot_ids


MANIFEST_SCHEMA_VERSION = 1
CURRENT_CAPSULE_MANIFEST_SCHEMA_VERSION = 1
RESOURCE_PATTERN = re.compile(
    r'(?:src="(?P<html>[^"]+)"|!\[[^\]]*\]\((?P<markdown>[^)]+)\))'
)
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    path: str = ""
    detail: str = ""


@dataclass(frozen=True)
class IntegrityResult:
    snapshot_id: str
    checked_files: int
    issues: tuple[IntegrityIssue, ...]
    manifest_present: bool

    @property
    def ok(self) -> bool:
        return not self.issues

    def count(self, *codes: str) -> int:
        return sum(issue.code in codes for issue in self.issues)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_file_facts(snapshot_dir: Path) -> dict[str, dict[str, int | str]]:
    facts: dict[str, dict[str, int | str]] = {}
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(snapshot_dir).as_posix()
            facts[relative] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return facts


def manifest_path(target_root: Path, snapshot_id: str) -> Path:
    return target_root / "integrity" / f"{snapshot_id}.json"


def generate_manifest(
    target_root: Path,
    snapshot_id: str,
    *,
    created_at: datetime | None = None,
) -> Path:
    snapshot_dir = target_root / "snapshots" / snapshot_id
    if not snapshot_dir.is_dir():
        raise FileNotFoundError(f"snapshot directory not found: {snapshot_id}")
    before = snapshot_file_facts(snapshot_dir)
    destination = manifest_path(target_root, snapshot_id)
    write_json_atomic(
        destination,
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": (created_at or datetime.now().astimezone()).isoformat(),
            "files": before,
        },
    )
    if snapshot_file_facts(snapshot_dir) != before:
        raise RuntimeError("snapshot changed while generating integrity manifest")
    return destination


def generate_missing_manifests(target_root: Path) -> list[Path]:
    index = _load_index(target_root)
    before = {
        entry["id"]: snapshot_file_facts(target_root / "snapshots" / entry["id"])
        for entry in index
        if (target_root / "snapshots" / entry["id"]).is_dir()
    }
    created: list[Path] = []
    for entry in index:
        snapshot_id = str(entry.get("id") or "")
        if not snapshot_id or manifest_path(target_root, snapshot_id).exists():
            continue
        created.append(generate_manifest(target_root, snapshot_id))
    after = {
        snapshot_id: snapshot_file_facts(target_root / "snapshots" / snapshot_id)
        for snapshot_id in before
    }
    if before != after:
        raise RuntimeError("historical snapshot changed while establishing manifests")
    return created


def _load_index(target_root: Path) -> list[dict]:
    path = target_root / "snapshot_index.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshots = raw.get("snapshots")
    if not isinstance(snapshots, list):
        raise ValueError("snapshot index is invalid")
    return snapshots


def _safe_snapshot_path(snapshot_dir: Path, reference: str) -> Path | None:
    if not reference or URL_SCHEME.match(reference) or Path(reference).is_absolute():
        return None
    candidate = (snapshot_dir / Path(reference.replace("/", os.sep))).resolve()
    try:
        candidate.relative_to(snapshot_dir.resolve())
    except ValueError:
        return None
    return candidate


def verify_snapshot_offline(target_root: Path, snapshot_id: str) -> IntegrityResult:
    issues: list[IntegrityIssue] = []
    checked_files = 0
    snapshot_dir = target_root / "snapshots" / snapshot_id
    try:
        index = _load_index(target_root)
    except Exception as exc:
        return IntegrityResult(
            snapshot_id, 0,
            (IntegrityIssue("INVALID_SNAPSHOT_INDEX", detail=type(exc).__name__),),
            False,
        )
    entries = {str(item.get("id") or ""): item for item in index}
    try:
        deleted_snapshot_ids = load_deleted_snapshot_ids(target_root)
    except Exception as exc:
        deleted_snapshot_ids = frozenset()
        issues.append(
            IntegrityIssue("INVALID_DELETION_HISTORY", detail=type(exc).__name__)
        )
    entry = entries.get(snapshot_id)
    if entry is None:
        issues.append(IntegrityIssue("INDEX_MISSING_ENTRY"))
    elif entry.get("status") != "COMPLETE":
        issues.append(IntegrityIssue("INDEX_NOT_COMPLETE"))
    if not snapshot_dir.is_dir():
        issues.append(IntegrityIssue("MISSING_SNAPSHOT_DIRECTORY"))
        return IntegrityResult(snapshot_id, checked_files, tuple(issues), False)

    chat = snapshot_dir / "chat.md"
    metadata_path = snapshot_dir / "export_meta.json"
    for required in (chat, metadata_path):
        if not required.is_file():
            issues.append(
                IntegrityIssue("MISSING_REQUIRED_FILE", required.name)
            )
        elif required.stat().st_size <= 0:
            issues.append(IntegrityIssue("EMPTY_REQUIRED_FILE", required.name))
        else:
            checked_files += 1

    metadata = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("status") != "COMPLETE":
                issues.append(IntegrityIssue("METADATA_NOT_COMPLETE", "export_meta.json"))
            if metadata.get("snapshot_id") != snapshot_id:
                issues.append(IntegrityIssue("SNAPSHOT_ID_MISMATCH", "export_meta.json"))
            if int(metadata.get("message_count", -1)) < 0:
                issues.append(IntegrityIssue("INVALID_MESSAGE_COUNT", "export_meta.json"))
            first = metadata.get("first_message_time")
            last = metadata.get("last_message_time")
            if first and last and datetime.fromisoformat(first) > datetime.fromisoformat(last):
                issues.append(IntegrityIssue("INVALID_MESSAGE_TIME_RANGE", "export_meta.json"))
            parent = metadata.get("parent_snapshot_id")
            if parent and parent not in entries and parent not in deleted_snapshot_ids:
                issues.append(IntegrityIssue("INVALID_PARENT_SNAPSHOT", "export_meta.json"))
            if entry and int(entry.get("message_count", -1)) != int(metadata.get("message_count", -2)):
                issues.append(IntegrityIssue("INDEX_METADATA_COUNT_MISMATCH"))
        except Exception as exc:
            issues.append(
                IntegrityIssue("INVALID_SNAPSHOT_METADATA", "export_meta.json", type(exc).__name__)
            )

    if chat.is_file():
        try:
            text = chat.read_text(encoding="utf-8")
            for match in RESOURCE_PATTERN.finditer(text):
                reference = match.group("html") or match.group("markdown")
                resource = _safe_snapshot_path(snapshot_dir, reference)
                if resource is None:
                    issues.append(IntegrityIssue("PATH_ESCAPE", reference))
                    continue
                if not resource.is_file():
                    issues.append(IntegrityIssue("MISSING_ASSET", reference))
                elif resource.stat().st_size <= 0:
                    issues.append(IntegrityIssue("EMPTY_ASSET", reference))
                else:
                    checked_files += 1
        except UnicodeError as exc:
            issues.append(IntegrityIssue("INVALID_MARKDOWN_ENCODING", "chat.md", type(exc).__name__))

    manifest_file = manifest_path(target_root, snapshot_id)
    manifest_present = manifest_file.is_file()
    if manifest_present:
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if int(manifest.get("schema_version", -1)) != MANIFEST_SCHEMA_VERSION:
                issues.append(IntegrityIssue("UNSUPPORTED_MANIFEST_SCHEMA", manifest_file.name))
            if manifest.get("snapshot_id") != snapshot_id:
                issues.append(IntegrityIssue("MANIFEST_SNAPSHOT_ID_MISMATCH", manifest_file.name))
            files = manifest.get("files")
            if not isinstance(files, dict):
                raise ValueError("manifest files must be an object")
            for relative, expected in files.items():
                current = _safe_snapshot_path(snapshot_dir, relative)
                if current is None:
                    issues.append(IntegrityIssue("MANIFEST_PATH_ESCAPE", relative))
                    continue
                if not current.is_file():
                    code = "MISSING_ASSET" if relative.startswith("assets/") else "MISSING_FILE"
                    issues.append(IntegrityIssue(code, relative))
                    continue
                checked_files += 1
                size_matches = current.stat().st_size == int(expected.get("size", -1))
                hash_matches = sha256_file(current) == expected.get("sha256")
                if not size_matches or not hash_matches:
                    issues.append(IntegrityIssue("HASH_MISMATCH", relative))
        except Exception as exc:
            issues.append(
                IntegrityIssue("INVALID_MANIFEST", manifest_file.name, type(exc).__name__)
            )

    return IntegrityResult(snapshot_id, checked_files, tuple(issues), manifest_present)


def verify_all_snapshots_offline(target_root: Path) -> list[IntegrityResult]:
    return [
        verify_snapshot_offline(target_root, str(entry.get("id") or ""))
        for entry in _load_index(target_root)
    ]


def current_capsule_file_facts(target_root: Path) -> dict[str, dict[str, int | str]]:
    facts: dict[str, dict[str, int | str]] = {}
    for relative in (Path("chat.md"), Path("capsule_meta.json")):
        path = target_root / relative
        if path.is_file():
            facts[relative.as_posix()] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    assets = target_root / "assets"
    if assets.is_dir():
        for path in sorted(assets.rglob("*")):
            if path.is_file():
                relative = path.relative_to(target_root).as_posix()
                facts[relative] = {
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
    metadata_file = target_root / 'capsule_meta.json'
    if metadata_file.is_file():
        try:
            metadata = json.loads(metadata_file.read_text(encoding='utf8'))
        except (ValueError, UnicodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get('product_history_version') == 1:
            for path in sorted((target_root / 'state').rglob('*')):
                if path.is_file():
                    facts[path.relative_to(target_root).as_posix()] = {
                        'size': path.stat().st_size, 'sha256': sha256_file(path)}
            target = target_root / 'target.json'
            if target.is_file():
                facts['target.json'] = {'size': target.stat().st_size, 'sha256': sha256_file(target)}
    return facts


def generate_current_manifest(
    target_root: Path,
    *,
    created_at: datetime | None = None,
) -> Path:
    facts = current_capsule_file_facts(target_root)
    if "chat.md" not in facts or "capsule_meta.json" not in facts:
        raise RuntimeError("current capsule is missing required files")
    destination = target_root / "integrity" / "current.json"
    write_json_atomic(
        destination,
        {
            "schema_version": CURRENT_CAPSULE_MANIFEST_SCHEMA_VERSION,
            "capsule": "current",
            "created_at": (created_at or datetime.now().astimezone()).isoformat(),
            "files": facts,
        },
    )
    if current_capsule_file_facts(target_root) != facts:
        raise RuntimeError("current capsule changed while generating integrity manifest")
    return destination


def verify_current_capsule(target_root: Path) -> IntegrityResult:
    issues: list[IntegrityIssue] = []
    checked = 0
    chat = target_root / "chat.md"
    metadata_path = target_root / "capsule_meta.json"
    for required in (chat, metadata_path):
        if not required.is_file():
            issues.append(IntegrityIssue("MISSING_REQUIRED_FILE", required.name))
        elif required.stat().st_size <= 0:
            issues.append(IntegrityIssue("EMPTY_REQUIRED_FILE", required.name))
        else:
            checked += 1

    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if int(metadata.get("capsule_schema_version", -1)) != 1:
                issues.append(IntegrityIssue("UNSUPPORTED_CAPSULE_SCHEMA", metadata_path.name))
            if metadata.get("integrity_status") != "complete":
                issues.append(IntegrityIssue("METADATA_NOT_COMPLETE", metadata_path.name))
            if int(metadata.get("message_count", -1)) < 0:
                issues.append(IntegrityIssue("INVALID_MESSAGE_COUNT", metadata_path.name))
            first = metadata.get("first_message_time")
            last = metadata.get("last_message_time")
            if first and last and datetime.fromisoformat(first) > datetime.fromisoformat(last):
                issues.append(IntegrityIssue("INVALID_MESSAGE_TIME_RANGE", metadata_path.name))
        except Exception as exc:
            issues.append(IntegrityIssue("INVALID_CAPSULE_METADATA", metadata_path.name, type(exc).__name__))

    if chat.is_file():
        try:
            text = chat.read_text(encoding="utf-8")
            for match in RESOURCE_PATTERN.finditer(text):
                reference = match.group("html") or match.group("markdown")
                resource = _safe_snapshot_path(target_root, reference)
                if resource is None:
                    issues.append(IntegrityIssue("PATH_ESCAPE", reference))
                elif not resource.is_file():
                    issues.append(IntegrityIssue("MISSING_ASSET", reference))
                elif resource.stat().st_size <= 0:
                    issues.append(IntegrityIssue("EMPTY_ASSET", reference))
                else:
                    checked += 1
        except UnicodeError as exc:
            issues.append(IntegrityIssue("INVALID_MARKDOWN_ENCODING", "chat.md", type(exc).__name__))

    manifest_file = target_root / "integrity" / "current.json"
    manifest_present = manifest_file.is_file()
    if manifest_present:
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if int(manifest.get("schema_version", -1)) != CURRENT_CAPSULE_MANIFEST_SCHEMA_VERSION:
                issues.append(IntegrityIssue("UNSUPPORTED_MANIFEST_SCHEMA", manifest_file.name))
            if manifest.get("capsule") != "current":
                issues.append(IntegrityIssue("MANIFEST_CAPSULE_MISMATCH", manifest_file.name))
            expected = manifest.get("files")
            if not isinstance(expected, dict):
                raise ValueError("manifest files must be an object")
            current = current_capsule_file_facts(target_root)
            for relative in sorted(set(expected) | set(current)):
                if relative not in expected:
                    issues.append(IntegrityIssue("UNTRACKED_FILE", relative))
                    continue
                if relative not in current:
                    code = "MISSING_ASSET" if relative.startswith("assets/") else "MISSING_FILE"
                    issues.append(IntegrityIssue(code, relative))
                    continue
                checked += 1
                if current[relative] != expected[relative]:
                    issues.append(IntegrityIssue("HASH_MISMATCH", relative))
        except Exception as exc:
            issues.append(IntegrityIssue("INVALID_MANIFEST", manifest_file.name, type(exc).__name__))
    return IntegrityResult("current", checked, tuple(issues), manifest_present)
