from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .archive import (
    ArchiveTarget,
    ArchivedMessage,
    AssetCounts,
    IncrementalResult,
    RESOURCE_PATTERN,
    _find_previous_asset,
    _render_snapshot,
    _validate_records,
    clone_immutable_asset,
    collect_incremental,
    load_snapshot_index,
    media_kind_for_type,
    media_relative_path,
    write_message_store_atomic,
)
from .avatars import AvatarExporter
from .integrity import generate_current_manifest, sha256_file
from .media import ImageExporter, VideoExporter
from .migrations import backup_mutable_state, ensure_current_schema
from .models import AvatarSnapshot
from .utils import write_json_atomic


CAPSULE_SCHEMA_VERSION = 1
CURRENT_JOURNAL = "current_capsule_journal.json"
CURRENT_TRANSACTION = ".current_transaction"


@dataclass(frozen=True)
class CapsuleResult:
    path: Path
    saved_at: str
    save_mode: str
    message_count: int
    new_message_count: int
    image_count: int
    new_image_count: int
    video_count: int
    new_video_count: int
    asset_counts: AssetCounts
    coverage_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    avatar_history_count: int = 0


@dataclass(frozen=True)
class AvatarHistoryEntry:
    sha256: str
    first_observed_at: str
    path: str
    current: bool


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".part_{uuid.uuid4().hex}")
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        if temporary.stat().st_size <= 0:
            raise RuntimeError("refusing to install an empty capsule file")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_relative(relative: str) -> Path:
    path = Path(relative.replace("/", os.sep))
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("unsafe current capsule transaction path")
    return path


def _restore_current_transaction(target_root: Path, transaction: Path, journal: dict) -> None:
    previous = transaction / "previous"
    for raw in journal.get("managed_files", []):
        relative = _safe_relative(str(raw))
        destination = target_root / relative
        backup = previous / relative
        if backup.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            restore = destination.with_name(f".restore_{uuid.uuid4().hex}")
            shutil.copy2(backup, restore)
            restore.replace(destination)
        else:
            destination.unlink(missing_ok=True)


def recover_current_capsule_transaction(target_root: Path) -> bool:
    journal_path = target_root / CURRENT_JOURNAL
    transaction = target_root / CURRENT_TRANSACTION
    if not journal_path.is_file():
        return False
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("phase") != "committed":
        _restore_current_transaction(target_root, transaction, journal)
    if transaction.is_dir():
        shutil.rmtree(transaction)
    journal_path.unlink(missing_ok=True)
    return True


def _commit_current_capsule(target_root: Path, staging: Path, now: datetime) -> None:
    recover_current_capsule_transaction(target_root)
    transaction = target_root / CURRENT_TRANSACTION
    transaction.mkdir(parents=True)
    previous = transaction / "previous"
    staged_files = [
        path for path in sorted(staging.rglob("*")) if path.is_file()
    ]
    managed = [path.relative_to(staging).as_posix() for path in staged_files]
    managed.append("integrity/current.json")
    for relative_text in managed:
        relative = _safe_relative(relative_text)
        source = target_root / relative
        if source.is_file():
            backup = previous / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
    journal_path = target_root / CURRENT_JOURNAL
    journal = {
        "schema_version": 1,
        "operation_id": uuid.uuid4().hex,
        "created_at": now.isoformat(),
        "phase": "prepared",
        "managed_files": managed,
    }
    write_json_atomic(journal_path, journal)
    try:
        # Assets are installed first and are a complete superset needed by the
        # new Markdown. The readable chat is replaced only after validation.
        for source in staged_files:
            relative = source.relative_to(staging)
            if relative in {Path("chat.md"), Path("capsule_meta.json")}:
                continue
            _copy_atomic(source, target_root / relative)
        _copy_atomic(staging / "capsule_meta.json", target_root / "capsule_meta.json")
        _copy_atomic(staging / "chat.md", target_root / "chat.md")
        generate_current_manifest(target_root, created_at=now)
        journal["phase"] = "committed"
        write_json_atomic(journal_path, journal)
    except Exception:
        _restore_current_transaction(target_root, transaction, journal)
        raise
    finally:
        if journal.get("phase") == "committed":
            shutil.rmtree(transaction, ignore_errors=True)
            journal_path.unlink(missing_ok=True)


def _snapshot_observed_at(snapshot: Path) -> str:
    metadata = snapshot / "export_meta.json"
    if metadata.is_file():
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
            value = str(raw.get("created_at") or raw.get("exported_at") or "")
            if value:
                return datetime.fromisoformat(value).astimezone().isoformat()
        except (OSError, ValueError, TypeError):
            pass
    try:
        return datetime.strptime(snapshot.name[:15], "%Y%m%d_%H%M%S").astimezone().isoformat()
    except ValueError:
        return datetime.fromtimestamp(snapshot.stat().st_mtime).astimezone().isoformat()


def _earlier(left: str, right: str) -> str:
    try:
        return min(datetime.fromisoformat(left), datetime.fromisoformat(right)).isoformat()
    except ValueError:
        return min(left, right)


def _history_filename(observed_at: str, digest: str, suffix: str) -> str:
    try:
        stamp = datetime.fromisoformat(observed_at).strftime("%Y%m%d_%H%M%S")
    except ValueError:
        stamp = "unknown_time"
    extension = suffix.casefold() if suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".img"
    return f"{stamp}_{digest[:8]}{extension}"


def _build_avatar_history(
    target: ArchiveTarget,
    staging: Path,
    captured: dict[str, AvatarSnapshot],
    now: datetime,
    counts: AssetCounts,
) -> tuple[dict[str, AvatarSnapshot], list[AvatarHistoryEntry]]:
    sources: dict[str, tuple[str, Path]] = {}

    existing_meta = target.root / "assets" / "avatars" / "avatar_history.json"
    if existing_meta.is_file():
        try:
            raw = json.loads(existing_meta.read_text(encoding="utf-8"))
            for item in raw.get("avatars", []):
                digest = str(item.get("sha256") or "")
                observed = str(item.get("first_observed_at") or "")
                source = target.root / _safe_relative(str(item.get("path") or ""))
                if len(digest) == 64 and observed and source.is_file() and sha256_file(source) == digest:
                    sources[digest] = (observed, source)
        except (OSError, ValueError, TypeError):
            pass

    snapshots = target.snapshots_dir
    if snapshots.is_dir():
        for snapshot in sorted(path for path in snapshots.iterdir() if path.is_dir() and not path.name.startswith(".")):
            observed = _snapshot_observed_at(snapshot)
            avatar_dir = snapshot / "assets" / "avatars"
            for source in sorted(avatar_dir.glob("contact.*")) if avatar_dir.is_dir() else []:
                if not source.is_file():
                    continue
                digest = sha256_file(source)
                if digest in sources:
                    sources[digest] = (_earlier(sources[digest][0], observed), sources[digest][1])
                else:
                    sources[digest] = (observed, source)

    current_digest = ""
    contact = captured.get("contact")
    if contact and contact.relative_path:
        current_source = staging / _safe_relative(contact.relative_path)
        current_digest = sha256_file(current_source)
        observed = now.isoformat()
        if current_digest in sources:
            sources[current_digest] = (_earlier(sources[current_digest][0], observed), current_source)
        else:
            sources[current_digest] = (observed, current_source)
    elif sources:
        current_digest, (_, current_source) = max(
            sources.items(), key=lambda item: datetime.fromisoformat(item[1][0])
        )
        destination = staging / "assets" / "avatars" / ("contact" + current_source.suffix.casefold())
        if not destination.exists():
            clone_immutable_asset(current_source, destination, counts)
        captured["contact"] = AvatarSnapshot("legacy_latest_known", destination.relative_to(staging).as_posix())

    entries: list[AvatarHistoryEntry] = []
    history_dir = staging / "assets" / "avatars" / "history" / "contact"
    for digest, (observed, source) in sorted(sources.items(), key=lambda item: item[1][0]):
        filename = _history_filename(observed, digest, source.suffix)
        destination = history_dir / filename
        if not destination.exists():
            clone_immutable_asset(source, destination, counts)
        entries.append(
            AvatarHistoryEntry(
                digest,
                observed,
                destination.relative_to(staging).as_posix(),
                digest == current_digest,
            )
        )
    write_json_atomic(
        staging / "assets" / "avatars" / "avatar_history.json",
        {
            "schema_version": 1,
            "semantics": "first_observed_at; not the actual avatar change time",
            "avatars": [entry.__dict__ for entry in entries],
        },
    )
    return captured, entries


def load_avatar_history(target_root: Path) -> list[AvatarHistoryEntry]:
    path = target_root / "assets" / "avatars" / "avatar_history.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for item in raw.get("avatars", []):
        relative = str(item.get("path") or "")
        source = target_root / _safe_relative(relative)
        digest = str(item.get("sha256") or "")
        if source.is_file() and len(digest) == 64 and sha256_file(source) == digest:
            result.append(AvatarHistoryEntry(
                digest,
                str(item.get("first_observed_at") or ""),
                relative,
                bool(item.get("current")),
            ))
    return sorted(result, key=lambda item: item.first_observed_at, reverse=True)


def _ensure_durable_assets(target: ArchiveTarget, records: list[ArchivedMessage], staging: Path) -> None:
    for record in records:
        relative = media_relative_path(record)
        if relative is None:
            continue
        source = staging / relative
        if not source.is_file():
            raise RuntimeError(f"staged media asset is missing: {record.media_asset}")
        destination = target.state_dir / "assets" / relative.relative_to("assets")
        if destination.is_file():
            if sha256_file(destination) != sha256_file(source):
                raise RuntimeError("durable media asset hash conflict")
            continue
        _copy_atomic(source, destination)


def _write_current_archive_state(
    target: ArchiveTarget,
    records: list[ArchivedMessage],
    saved_at: str,
    mode: str,
) -> None:
    previous = {}
    path = target.root / "archive_state.json"
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = {}
    last = records[-1] if records else None
    write_json_atomic(path, {
        "schema_version": int(previous.get("schema_version", 2)),
        "last_snapshot_id": previous.get("last_snapshot_id"),
        "last_sort_seq": last.message.sort_seq if last else 0,
        "last_message_identity": last.identity if last else None,
        "last_sort_key": [
            last.message.sort_seq if last else 0,
            last.message.local_id if last else None,
        ],
        "message_count": len(records),
        "current_capsule_saved_at": saved_at,
        "current_capsule_save_mode": mode,
    })


def _validate_staging(staging: Path, message_count: int) -> None:
    chat = staging / "chat.md"
    metadata_path = staging / "capsule_meta.json"
    if not chat.is_file() or not metadata_path.is_file():
        raise RuntimeError("current capsule staging is incomplete")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if int(metadata.get("message_count", -1)) != message_count:
        raise RuntimeError("current capsule message count mismatch")
    text = chat.read_text(encoding="utf-8")
    for match in RESOURCE_PATTERN.finditer(text):
        reference = match.group("html") or match.group("markdown")
        if not reference or Path(reference).is_absolute() or re.match(r"^[A-Za-z]:", reference):
            raise RuntimeError("current capsule contains an unsafe resource path")
        candidate = (staging / Path(reference.replace("/", os.sep))).resolve()
        try:
            candidate.relative_to(staging.resolve())
        except ValueError as exc:
            raise RuntimeError("current capsule resource escapes staging") from exc
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise RuntimeError("current capsule contains a missing resource")


def create_current_capsule(
    db,
    target: ArchiveTarget,
    self_username: str,
    work_root: Path,
    logger,
    *,
    now: datetime | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
    collection: IncrementalResult | None = None,
    save_mode: str = "quick",
    reconciliation: dict | None = None,
) -> CapsuleResult:
    now = now or datetime.now().astimezone()
    ensure_current_schema(target.root)
    incremental = collection or collect_incremental(db, target, work_root, logger)
    if incremental.new_identities and target.message_store.is_file() and save_mode != "reconciliation":
        backup_mutable_state(target.root, reason="quick_backup_new_messages")
    records = list(incremental.records)
    index = load_snapshot_index(target)
    legacy_parent = str(index["snapshots"][-1].get("id") or "") if index["snapshots"] else None
    staging = target.root / (".current_staging_" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    counts = AssetCounts()
    new_image_count = 0
    new_video_count = 0
    try:
        exporters = {}
        media_specs = {
            "图片": ("images", ImageExporter),
            "视频": ("videos", VideoExporter),
        }
        updated: list[ArchivedMessage] = []
        for record in records:
            spec = media_specs.get(record.message.message_type)
            if spec is None:
                updated.append(record)
                continue
            kind, exporter_type = spec
            relative = media_relative_path(record)
            if relative is not None:
                source = _find_previous_asset(target, legacy_parent, relative)
                if source is None:
                    raise RuntimeError(f"archived media asset is missing: {record.media_asset}")
                clone_immutable_asset(source, staging / relative, counts)
                updated.append(replace(record, media_kind=kind))
                continue
            if record.identity not in incremental.new_identities:
                updated.append(record)
                continue
            if kind not in exporters:
                media_dir = staging / "assets" / kind
                media_dir.mkdir(parents=True, exist_ok=True)
                exporters[kind] = exporter_type(db, target.username, media_dir, logger)
            result = exporters[kind].export(record.message)
            asset_name = Path(result.relative_path).name if result.relative_path else ""
            if asset_name:
                counts.new += 1
                if kind == "images":
                    new_image_count += 1
                else:
                    new_video_count += 1
            updated.append(replace(
                record,
                media_status=result.status,
                media_asset=asset_name,
                media_kind=kind,
            ))
        records = updated
        _validate_records(records)

        avatar_dir = staging / "assets" / "avatars"
        avatar_exporter = AvatarExporter(db, avatar_dir, logger)
        avatars = {
            "self": avatar_exporter.export("self", self_username, "我的", input_func, print_func),
            "contact": avatar_exporter.export("contact", target.username, "对方", input_func, print_func),
        }
        avatars, history = _build_avatar_history(target, staging, avatars, now, counts)
        stats, image_count, video_count = _render_snapshot(
            staging / "chat.md", target, records, avatars, now
        )
        metadata = {
            "product_version": __version__,
            "capsule_schema_version": CAPSULE_SCHEMA_VERSION,
            "last_saved_at": now.isoformat(),
            "last_save_mode": save_mode,
            "message_count": len(records),
            "new_message_count": len(incremental.new_identities),
            "first_message_time": (
                datetime.fromtimestamp(stats.first_message_time).astimezone().isoformat()
                if stats.first_message_time else None
            ),
            "last_message_time": (
                datetime.fromtimestamp(stats.last_message_time).astimezone().isoformat()
                if stats.last_message_time else None
            ),
            "image_count": image_count,
            "video_count": video_count,
            "avatar_history_count": len(history),
            "avatars": {
                role: {
                    "status": avatar.status,
                    "path": avatar.relative_path,
                    "remote_available": avatar.remote_available,
                }
                for role, avatar in avatars.items()
            },
            "legacy_snapshot_count": len(index["snapshots"]),
            "integrity_status": "complete",
            "message_coverage": {
                "fully_supported": stats.fully_supported_counts,
                "partial": stats.partial_support_counts,
                "placeholder": stats.placeholder_counts,
                "placeholder_count": stats.unsupported_message_count,
            },
            "markdown_order_verified": stats.markdown_order_verified,
        }
        if reconciliation is not None:
            metadata["reconciliation"] = reconciliation
        write_json_atomic(staging / "capsule_meta.json", metadata)
        _validate_staging(staging, len(records))
        _ensure_durable_assets(target, records, staging)

        write_message_store_atomic(target.message_store, records)
        _write_current_archive_state(target, records, now.isoformat(), save_mode)
        _commit_current_capsule(target.root, staging, now)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return CapsuleResult(
        path=target.root,
        saved_at=now.isoformat(),
        save_mode=save_mode,
        message_count=len(records),
        new_message_count=len(incremental.new_identities),
        image_count=image_count,
        new_image_count=new_image_count,
        video_count=video_count,
        new_video_count=new_video_count,
        asset_counts=counts,
        coverage_summary={
            "fully_supported": stats.fully_supported_counts,
            "partial": stats.partial_support_counts,
            "placeholder": stats.placeholder_counts,
        },
        avatar_history_count=len(history),
    )
