from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .archive import ArchiveTarget, load_message_store
from .migrations import backup_mutable_state
from .utils import write_json_atomic


DELETION_SCHEMA_VERSION = 1
JOURNAL_NAME = "snapshot_deletion_journal.json"
TOMBSTONE_NAME = "snapshot_deletions.json"
SAFE_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class SnapshotDeletionResult:
    snapshot_id: str
    deleted_at: str
    remaining_snapshot_count: int
    preserved_media_count: int


def _read_json_object(path: Path, default: dict) -> dict:
    if not path.is_file():
        return dict(default)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain an object")
    return raw


def load_deleted_snapshot_ids(target_root: Path) -> frozenset[str]:
    raw = _read_json_object(
        target_root / TOMBSTONE_NAME,
        {"schema_version": DELETION_SCHEMA_VERSION, "deleted_snapshots": []},
    )
    if int(raw.get("schema_version", 0)) > DELETION_SCHEMA_VERSION:
        raise RuntimeError("snapshot deletion history was created by a newer version")
    rows = raw.get("deleted_snapshots", [])
    if not isinstance(rows, list):
        raise ValueError("deleted_snapshots must be a list")
    return frozenset(
        str(row.get("snapshot_id") or "")
        for row in rows
        if isinstance(row, dict) and str(row.get("snapshot_id") or "")
    )


def _index_document(target_root: Path) -> dict:
    raw = _read_json_object(target_root / "snapshot_index.json", {"snapshots": []})
    if not isinstance(raw.get("snapshots"), list):
        raise ValueError("snapshot index must contain a snapshots list")
    return raw


def _asset_source(
    target: ArchiveTarget,
    media_kind: str,
    asset_name: str,
    snapshot_ids: list[str],
    preferred_snapshot_id: str,
) -> Path | None:
    ordered = [preferred_snapshot_id]
    ordered.extend(reversed(snapshot_ids))
    seen: set[str] = set()
    for snapshot_id in ordered:
        if snapshot_id in seen:
            continue
        seen.add(snapshot_id)
        candidate = target.snapshots_dir / snapshot_id / "assets" / media_kind / asset_name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    legacy = target.root / "assets" / media_kind / asset_name
    return legacy if legacy.is_file() and legacy.stat().st_size > 0 else None


def ensure_durable_media(
    target: ArchiveTarget,
    *,
    preferred_snapshot_id: str,
) -> int:
    """Preserve every normalized media asset independently from snapshots."""
    if not target.message_store.is_file():
        return 0
    records = load_message_store(target.message_store)
    assets = sorted({
        (record.media_kind or ({"图片": "images", "视频": "videos"}.get(
            record.message.message_type, ""
        )), record.media_asset)
        for record in records
        if record.media_asset
    })
    index = _index_document(target.root)
    snapshot_ids = [str(row.get("id") or "") for row in index["snapshots"]]
    preserved = 0
    for media_kind, asset_name in assets:
        if media_kind not in {"images", "videos"}:
            raise RuntimeError("normalized media contains an unsupported media kind")
        if Path(asset_name).name != asset_name or asset_name in {".", ".."}:
            raise RuntimeError("normalized media contains an unsafe asset name")
        vault = target.state_dir / "assets" / media_kind
        destination = vault / asset_name
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        source = _asset_source(
            target, media_kind, asset_name, snapshot_ids, preferred_snapshot_id
        )
        if source is None:
            raise RuntimeError(
                "无法安全删除：内部历史状态依赖的已归档媒体没有可用副本。"
            )
        vault.mkdir(parents=True, exist_ok=True)
        temporary = vault / f".part_{uuid.uuid4().hex}"
        try:
            try:
                os.link(source, temporary)
            except OSError:
                shutil.copy2(source, temporary)
            if temporary.stat().st_size <= 0:
                raise RuntimeError("durable media copy is empty")
            temporary.replace(destination)
            preserved += 1
        finally:
            temporary.unlink(missing_ok=True)
    return preserved


def _write_tombstone(target_root: Path, snapshot_id: str, deleted_at: str) -> None:
    path = target_root / TOMBSTONE_NAME
    raw = _read_json_object(
        path,
        {"schema_version": DELETION_SCHEMA_VERSION, "deleted_snapshots": []},
    )
    if int(raw.get("schema_version", 0)) > DELETION_SCHEMA_VERSION:
        raise RuntimeError("snapshot deletion history was created by a newer version")
    rows = raw.get("deleted_snapshots", [])
    if not isinstance(rows, list):
        raise ValueError("deleted_snapshots must be a list")
    if not any(
        isinstance(row, dict) and row.get("snapshot_id") == snapshot_id
        for row in rows
    ):
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "deleted_at": deleted_at,
                "reason": "user_requested",
            }
        )
    raw["schema_version"] = DELETION_SCHEMA_VERSION
    raw["deleted_snapshots"] = rows
    write_json_atomic(path, raw)


def _update_archive_state(target_root: Path, latest_snapshot_id: str | None) -> None:
    path = target_root / "archive_state.json"
    raw = _read_json_object(path, {})
    raw["last_snapshot_id"] = latest_snapshot_id
    write_json_atomic(path, raw)


def _phase(hook: Callable[[str], None] | None, value: str) -> None:
    if hook:
        hook(value)


def _verify_deletion_state(target_root: Path, snapshot_id: str) -> None:
    index = _index_document(target_root)
    ids = [str(row.get("id") or "") for row in index["snapshots"]]
    if snapshot_id in ids:
        raise RuntimeError("snapshot deletion index verification failed")
    if any(not (target_root / "snapshots" / item).is_dir() for item in ids):
        raise RuntimeError("remaining snapshot directory verification failed")
    state = _read_json_object(target_root / "archive_state.json", {})
    expected_latest = ids[-1] if ids else None
    if state.get("last_snapshot_id") != expected_latest:
        raise RuntimeError("archive state deletion verification failed")
    if snapshot_id not in load_deleted_snapshot_ids(target_root):
        raise RuntimeError("snapshot deletion tombstone verification failed")


def _finish_deletion(
    target_root: Path,
    journal: dict,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> SnapshotDeletionResult:
    snapshot_id = str(journal.get("snapshot_id") or "")
    deleting_name = str(journal.get("deleting_name") or "")
    operation_id = str(journal.get("operation_id") or "")
    deleted_at = str(journal.get("deleted_at") or "")
    if not SAFE_SNAPSHOT_ID.fullmatch(snapshot_id):
        raise RuntimeError("invalid snapshot deletion journal")
    expected_name = f".deleting_{snapshot_id}_{operation_id}"
    if not operation_id or deleting_name != expected_name:
        raise RuntimeError("invalid snapshot deletion journal")

    snapshots_dir = target_root / "snapshots"
    source = snapshots_dir / snapshot_id
    staged = snapshots_dir / deleting_name
    if source.exists() and staged.exists():
        raise RuntimeError("ambiguous snapshot deletion state")
    if source.is_dir():
        source.replace(staged)
    elif source.exists():
        raise RuntimeError("snapshot path is not a directory")
    _phase(phase_hook, "snapshot_renamed")

    _write_tombstone(target_root, snapshot_id, deleted_at)
    _phase(phase_hook, "tombstone_written")

    index = _index_document(target_root)
    index["snapshots"] = [
        row for row in index["snapshots"] if str(row.get("id") or "") != snapshot_id
    ]
    write_json_atomic(target_root / "snapshot_index.json", index)
    _phase(phase_hook, "index_written")

    latest = index["snapshots"][-1]["id"] if index["snapshots"] else None
    _update_archive_state(target_root, latest)
    _phase(phase_hook, "state_written")

    manifest = target_root / "integrity" / f"{snapshot_id}.json"
    staged_manifest = target_root / "integrity" / f".deleting_{operation_id}.json"
    if manifest.exists() and staged_manifest.exists():
        raise RuntimeError("ambiguous integrity deletion state")
    if manifest.is_file():
        manifest.replace(staged_manifest)
    _phase(phase_hook, "manifest_staged")

    _verify_deletion_state(target_root, snapshot_id)
    _phase(phase_hook, "archive_verified")

    if staged.is_dir():
        shutil.rmtree(staged)
    staged_manifest.unlink(missing_ok=True)
    _phase(phase_hook, "payload_removed")

    (target_root / JOURNAL_NAME).unlink(missing_ok=True)
    return SnapshotDeletionResult(
        snapshot_id=snapshot_id,
        deleted_at=deleted_at,
        remaining_snapshot_count=len(index["snapshots"]),
        preserved_media_count=int(journal.get("preserved_media_count") or 0),
    )


def recover_pending_snapshot_deletion(target_root: Path) -> SnapshotDeletionResult | None:
    journal_path = target_root / JOURNAL_NAME
    if not journal_path.is_file():
        return None
    journal = _read_json_object(journal_path, {})
    if int(journal.get("schema_version", 0)) != DELETION_SCHEMA_VERSION:
        raise RuntimeError("unsupported snapshot deletion journal")
    return _finish_deletion(target_root, journal)


def delete_snapshot_transaction(
    target: ArchiveTarget,
    snapshot_id: str,
    *,
    now: datetime | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> SnapshotDeletionResult:
    if (target.root / JOURNAL_NAME).exists():
        recover_pending_snapshot_deletion(target.root)
    if not SAFE_SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("invalid snapshot id")
    index = _index_document(target.root)
    if snapshot_id not in {str(row.get("id") or "") for row in index["snapshots"]}:
        raise FileNotFoundError("要删除的时间胶囊不存在。")
    snapshot_dir = target.snapshots_dir / snapshot_id
    if not snapshot_dir.is_dir():
        raise FileNotFoundError("要删除的时间胶囊目录不存在。")
    # Fail closed before any rename if deletion metadata belongs to a newer format.
    load_deleted_snapshot_ids(target.root)

    preserved = ensure_durable_media(target, preferred_snapshot_id=snapshot_id)
    _phase(phase_hook, "media_ready")
    backup_mutable_state(target.root, reason="delete_snapshot")
    operation_id = uuid.uuid4().hex
    deleted_at = (now or datetime.now().astimezone()).isoformat()
    journal = {
        "schema_version": DELETION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "snapshot_id": snapshot_id,
        "deleting_name": f".deleting_{snapshot_id}_{operation_id}",
        "deleted_at": deleted_at,
        "reason": "user_requested",
        "preserved_media_count": preserved,
    }
    write_json_atomic(target.root / JOURNAL_NAME, journal)
    _phase(phase_hook, "journal_written")
    return _finish_deletion(target.root, journal, phase_hook=phase_hook)
