from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .utils import write_json_atomic


CURRENT_MUTABLE_SCHEMA_VERSION = 2
CURRENT_MESSAGE_SCHEMA_VERSION = 2
STATE_BACKUP_RETENTION = 5
MUTABLE_JSON_FILES = ("target.json", "archive_state.json", "snapshot_index.json")


class UnsupportedFutureSchema(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied: tuple[str, ...]
    backup_path: Path | None


def _read_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return dict(default)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return raw


def _load_documents(root: Path) -> dict[str, dict]:
    return {
        "target.json": _read_json(root / "target.json", {}),
        "archive_state.json": _read_json(root / "archive_state.json", {}),
        "snapshot_index.json": _read_json(
            root / "snapshot_index.json", {"snapshots": []}
        ),
        "state/schema.json": _read_json(root / "state" / "schema.json", {}),
    }


def detect_schema_version(root: Path) -> int:
    documents = _load_documents(root)
    versions = {
        int(document.get("schema_version", 0))
        for name, document in documents.items()
        if name != "state/schema.json" and document
    }
    if not versions:
        return 0
    if len(versions) != 1:
        raise ValueError(f"mutable metadata schema versions disagree: {sorted(versions)}")
    version = versions.pop()
    if version > CURRENT_MUTABLE_SCHEMA_VERSION:
        raise UnsupportedFutureSchema(
            "该备份由更新版本创建。当前程序无法安全读取。"
        )
    if version < 0:
        raise ValueError("schema_version must not be negative")
    return version


def _v0_to_v1(documents: dict[str, dict]) -> dict[str, dict]:
    result = {name: dict(document) for name, document in documents.items()}
    for name in MUTABLE_JSON_FILES:
        result[name]["schema_version"] = 1
    result["snapshot_index.json"].setdefault("snapshots", [])
    result["state/schema.json"] = {
        "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
        "format": "wechat-exporter-normalized-message-jsonl",
        "identity": "local_id",
        "sort_key": ["sort_seq", "create_time", "local_id"],
    }
    return result


def _v1_to_v2(documents: dict[str, dict]) -> dict[str, dict]:
    result = {name: dict(document) for name, document in documents.items()}
    for name in MUTABLE_JSON_FILES:
        result[name]["schema_version"] = 2
    state = result["archive_state.json"]
    identity = str(state.get("last_message_identity") or "")
    local_id = identity.removeprefix("local:") if identity else None
    state["last_sort_key"] = [int(state.get("last_sort_seq") or 0), local_id]
    result["state/schema.json"] = {
        "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
        "format": "wechat-exporter-normalized-message-jsonl",
        "identity": "local_id",
        "sort_key": ["sort_seq", "create_time", "local_id"],
    }
    return result


MIGRATIONS: dict[int, tuple[str, Callable[[dict[str, dict]], dict[str, dict]]]] = {
    0: ("v0_to_v1", _v0_to_v1),
    1: ("v1_to_v2", _v1_to_v2),
}


def _allocate_backup_path(root: Path, now: datetime) -> Path:
    parent = root / "state_backups"
    parent.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%Y%m%d_%H%M%S")
    candidate = parent / base
    suffix = 1
    while candidate.exists():
        candidate = parent / f"{base}_{suffix:02d}"
        suffix += 1
    return candidate


def backup_mutable_state(
    root: Path,
    *,
    reason: str,
    now: datetime | None = None,
) -> Path:
    now = now or datetime.now().astimezone()
    destination = _allocate_backup_path(root, now)
    destination.mkdir(parents=True)
    for relative in (
        Path("target.json"),
        Path("archive_state.json"),
        Path("snapshot_index.json"),
        Path("state/schema.json"),
        Path("state/messages.jsonl"),
    ):
        source = root / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    write_json_atomic(
        destination / "backup_meta.json",
        {"created_at": now.isoformat(), "reason": reason},
    )
    _enforce_backup_retention(root)
    return destination


def _enforce_backup_retention(root: Path) -> None:
    parent = root / "state_backups"
    if not parent.is_dir():
        return
    backups = sorted(path for path in parent.iterdir() if path.is_dir())
    for obsolete in backups[:-STATE_BACKUP_RETENTION]:
        resolved = obsolete.resolve()
        resolved.relative_to(parent.resolve())
        shutil.rmtree(resolved)


def _validate_documents(documents: dict[str, dict], expected_version: int) -> None:
    for name in MUTABLE_JSON_FILES:
        if int(documents[name].get("schema_version", -1)) != expected_version:
            raise ValueError(f"{name} did not migrate to schema {expected_version}")
    schema = documents["state/schema.json"]
    if int(schema.get("schema_version", -1)) != CURRENT_MESSAGE_SCHEMA_VERSION:
        raise ValueError("normalized message schema is unsupported")
    if not isinstance(documents["snapshot_index.json"].get("snapshots"), list):
        raise ValueError("snapshot index must contain a snapshots list")


def _migrate_message_store(source: Path, destination: Path) -> None:
    from .models import ChatMessage

    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8") as reader, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as writer:
        for number, line in enumerate(reader, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                message = ChatMessage.from_json_dict(raw)
            except Exception as exc:
                raise ValueError(
                    f"cannot migrate normalized message at line {number}"
                ) from exc
            migrated = {
                "identity": raw.get("identity"),
                **message.to_json_dict(),
                "media": raw.get("media"),
            }
            writer.write(json.dumps(migrated, ensure_ascii=False) + "\n")


def migrate_mutable_state(
    root: Path,
    *,
    now: datetime | None = None,
    write_func: Callable[[Path, dict], None] = write_json_atomic,
) -> MigrationResult:
    documents = _load_documents(root)
    start = detect_schema_version(root)
    source_message_version = int(
        documents["state/schema.json"].get("schema_version", 0)
    )
    needs_message_migration = source_message_version < CURRENT_MESSAGE_SCHEMA_VERSION
    if source_message_version > CURRENT_MESSAGE_SCHEMA_VERSION:
        raise UnsupportedFutureSchema(
            "该备份的消息状态由更新版本创建。当前程序不会修改它。"
        )
    if start == CURRENT_MUTABLE_SCHEMA_VERSION and not needs_message_migration:
        _validate_documents(documents, start)
        return MigrationResult(start, start, (), None)

    version = start
    applied: list[str] = []
    while version < CURRENT_MUTABLE_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RuntimeError(f"no migration registered from schema {version}")
        name, transform = migration
        documents = transform(documents)
        version += 1
        _validate_documents(documents, version)
        applied.append(name)

    if needs_message_migration:
        documents["state/schema.json"] = {
            "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
            "format": "wechat-exporter-normalized-message-jsonl",
            "identity": "local_id",
            "sort_key": ["sort_seq", "create_time", "local_id"],
            "content_model": "structured-v2",
        }
        applied.append(
            f"message_v{source_message_version}_to_v{CURRENT_MESSAGE_SCHEMA_VERSION}"
        )
    _validate_documents(documents, version)

    backup = backup_mutable_state(root, reason=f"schema_migration_{start}_to_{version}", now=now)
    original_paths = {
        relative: (root / relative).is_file()
        for relative in documents
    }
    message_relative = "state/messages.jsonl"
    migrate_messages = needs_message_migration and (root / message_relative).is_file()
    if migrate_messages:
        original_paths[message_relative] = True
    staging = root / ".migration_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for relative, payload in documents.items():
            write_func(staging / relative, payload)
        if migrate_messages:
            _migrate_message_store(
                root / message_relative, staging / message_relative
            )
        staged = _load_documents(staging)
        _validate_documents(staged, version)
        try:
            for relative in original_paths:
                source = staging / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
        except Exception:
            for relative, existed in original_paths.items():
                destination = root / relative
                saved = backup / relative
                if existed and saved.is_file():
                    restore = destination.with_suffix(destination.suffix + ".restore")
                    shutil.copy2(saved, restore)
                    restore.replace(destination)
                elif not existed and destination.exists():
                    destination.unlink()
            raise
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    shutil.rmtree(staging)
    return MigrationResult(start, version, tuple(applied), backup)


def ensure_current_schema(root: Path) -> None:
    version = detect_schema_version(root)
    if version != CURRENT_MUTABLE_SCHEMA_VERSION:
        raise RuntimeError(
            f"archive target schema {version} requires migration before use"
        )
    _validate_documents(_load_documents(root), version)
