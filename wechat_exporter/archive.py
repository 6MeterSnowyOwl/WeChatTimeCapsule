from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from .avatars import AvatarExporter
from .markdown import MarkdownArchiveWriter, sender_resolver
from .media import ImageExporter, VideoExporter
from .messages import iter_oldest_first, spool_all_messages, verify_public_pagination_scope
from .migrations import (
    CURRENT_MESSAGE_SCHEMA_VERSION,
    CURRENT_MUTABLE_SCHEMA_VERSION,
    backup_mutable_state,
    ensure_current_schema,
)
from .models import AvatarSnapshot, ChatMessage, ContactChoice, ExportStats, MediaResult
from .utils import safe_windows_name, write_json_atomic

if TYPE_CHECKING:
    from .history_contracts import ArchiveScope, SourceLocator


STATE_SCHEMA_VERSION = CURRENT_MUTABLE_SCHEMA_VERSION
SNAPSHOT_SCHEMA_VERSION = 1
INCREMENTAL_PAGE_SIZE = 200
IMAGE_FILE_PATTERN = re.compile(
    r"^\d{8}_\d{6}_(?P<local_id>[^_]+)(?P<thumb>_thumb)?\.(?:jpe?g|png|gif|webp)$",
    re.IGNORECASE,
)
RESOURCE_PATTERN = re.compile(
    r'(?:src="(?P<html>[^"]+)"|!\[[^\]]*\]\((?P<markdown>[^)]+)\))'
)


@dataclass(frozen=True)
class ArchiveTarget:
    root: Path
    username: str
    display_name: str

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def message_store(self) -> Path:
        return self.state_dir / "messages.jsonl"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"


@dataclass(frozen=True)
class ArchivedMessage:
    message: ChatMessage
    media_status: str = ""
    media_asset: str = ""
    media_kind: str = ""
    # Memory-only historical core envelope; legacy serializers remain v2.
    canonical_identity: str | None = None
    scope: ArchiveScope | None = None
    binding_status: str = ""
    source_refs: tuple[SourceLocator, ...] = ()
    legacy_aliases: tuple[dict, ...] = ()
    technical_observations: tuple[dict, ...] = ()

    @property
    def identity(self) -> str:
        if self.canonical_identity is not None:
            return self.canonical_identity
        return "local:" + self.message.local_id

    def to_json_dict(self) -> dict:
        return {
            "identity": self.identity,
            **self.message.to_json_dict(),
            "media": {
                "status": self.media_status,
                "asset_name": self.media_asset,
                "kind": self.media_kind or media_kind_for_type(self.message.message_type),
            } if self.media_status or self.media_asset else None,
        }

    @classmethod
    def from_json_dict(cls, raw: dict) -> "ArchivedMessage":
        message_fields = {
            key: raw[key]
            for key in (
                "local_id", "message_type", "sender_id", "sender_username",
                "create_time", "content", "sort_seq",
            )
        }
        if "subtype" in raw:
            message_fields["subtype"] = raw["subtype"]
        if "payload" in raw:
            message_fields["payload"] = raw["payload"]
        message = ChatMessage.from_json_dict(message_fields)
        expected = "local:" + message.local_id
        if raw.get("identity") != expected:
            raise ValueError("message identity does not match local_id")
        media = raw.get("media") or {}
        media_asset = str(media.get("asset_name") or "")
        media_kind = str(media.get("kind") or media_kind_for_type(message.message_type))
        if media_asset and (Path(media_asset).name != media_asset or media_asset in {".", ".."}):
            raise ValueError("message media has an unsafe asset name")
        if media_kind and media_kind not in {"images", "videos"}:
            raise ValueError("message media has an unsupported kind")
        return cls(
            message=message,
            media_status=str(media.get("status") or ""),
            media_asset=media_asset,
            media_kind=media_kind,
        )


def media_kind_for_type(message_type: str) -> str:
    return {"图片": "images", "视频": "videos"}.get(message_type, "")


def media_relative_path(record: ArchivedMessage) -> Path | None:
    kind = record.media_kind or media_kind_for_type(record.message.message_type)
    if not kind or not record.media_asset:
        return None
    return Path("assets") / kind / record.media_asset


@dataclass
class AssetCounts:
    reused: int = 0
    hardlinked: int = 0
    copied: int = 0
    new: int = 0


@dataclass(frozen=True)
class IncrementalResult:
    records: tuple[ArchivedMessage, ...]
    new_identities: frozenset[str]
    duplicate_count: int
    mode: str
    source_rows_read: int


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    path: Path
    message_count: int
    new_message_count: int
    image_count: int
    new_image_count: int
    duplicate_count: int
    asset_counts: AssetCounts
    coverage_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    video_count: int = 0
    new_video_count: int = 0


def message_identity(message: ChatMessage) -> str:
    return "local:" + message.local_id


def _target_payload(target: ArchiveTarget) -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "username": target.username,
        "display_name": target.display_name,
    }


def save_target(target: ArchiveTarget) -> None:
    write_json_atomic(target.root / "target.json", _target_payload(target))


def _write_message_schema(target: ArchiveTarget) -> None:
    write_json_atomic(
        target.state_dir / "schema.json",
        {
            "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
            "format": "wechat-exporter-normalized-message-jsonl",
            "identity": "local_id",
            "sort_key": ["sort_seq", "create_time", "local_id"],
            "content_model": "structured-v2",
        },
    )


def _write_empty_archive_state(target: ArchiveTarget) -> None:
    write_json_atomic(
        target.root / "archive_state.json",
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_snapshot_id": None,
            "last_sort_seq": 0,
            "last_message_identity": None,
            "last_sort_key": [0, None],
            "message_count": 0,
        },
    )


def discover_targets(exports_root: Path) -> list[ArchiveTarget]:
    targets: list[ArchiveTarget] = []
    if not exports_root.is_dir():
        return targets
    for child in exports_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        target_path = child / "target.json"
        if target_path.is_file():
            try:
                raw = json.loads(target_path.read_text(encoding="utf-8"))
                username = str(raw.get("username") or "").strip()
                display_name = str(raw.get("display_name") or username).strip()
                if username:
                    targets.append(ArchiveTarget(child, username, display_name))
            except (OSError, ValueError, TypeError):
                continue
            continue
    return sorted(targets, key=lambda item: item.display_name.casefold())


def create_target(exports_root: Path, contact: ContactChoice) -> ArchiveTarget:
    for existing in discover_targets(exports_root):
        if existing.username == contact.username:
            updated = ArchiveTarget(existing.root, existing.username, contact.display_name)
            save_target(updated)
            return updated
    if exports_root.is_dir():
        for child in exports_root.iterdir():
            legacy_meta = child / "export_meta.json"
            legacy_chat = child / "chat.md"
            if not child.is_dir() or not legacy_meta.is_file() or not legacy_chat.is_file():
                continue
            try:
                raw = json.loads(legacy_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if str(raw.get("username") or "") == contact.username:
                target = ArchiveTarget(child, contact.username, contact.display_name)
                save_target(target)
                if not (child / "snapshot_index.json").exists():
                    write_json_atomic(
                        child / "snapshot_index.json",
                        {"schema_version": STATE_SCHEMA_VERSION, "snapshots": []},
                    )
                _write_message_schema(target)
                if not (child / "archive_state.json").exists():
                    _write_empty_archive_state(target)
                return target
    base = safe_windows_name(contact.display_name)
    root = exports_root / base
    suffix = 1
    while root.exists():
        root = exports_root / f"{base}_{suffix:02d}"
        suffix += 1
    root.mkdir(parents=True)
    target = ArchiveTarget(root, contact.username, contact.display_name)
    save_target(target)
    write_json_atomic(
        root / "snapshot_index.json",
        {"schema_version": STATE_SCHEMA_VERSION, "snapshots": []},
    )
    _write_message_schema(target)
    _write_empty_archive_state(target)
    return target


def load_snapshot_index(target: ArchiveTarget) -> dict:
    path = target.root / "snapshot_index.json"
    if not path.is_file():
        return {"schema_version": STATE_SCHEMA_VERSION, "snapshots": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshots = raw.get("snapshots")
    if not isinstance(snapshots, list):
        raise ValueError("snapshot_index.json snapshots must be a list")
    return {"schema_version": int(raw.get("schema_version", 0)), "snapshots": snapshots}


def latest_snapshot_entry(target: ArchiveTarget) -> dict | None:
    snapshots = load_snapshot_index(target)["snapshots"]
    return snapshots[-1] if snapshots else None


def load_message_store(path: Path) -> list[ArchivedMessage]:
    records: list[ArchivedMessage] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(ArchivedMessage.from_json_dict(json.loads(line)))
            except Exception as exc:
                raise ValueError(f"invalid normalized message at line {number}") from exc
    _validate_records(records)
    return records


def write_message_store_atomic(path: Path, records: Iterable[ArchivedMessage]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record.to_json_dict(), ensure_ascii=False) + "\n")
    temporary.replace(path)


def _validate_records(records: Iterable[ArchivedMessage]) -> None:
    identities: set[str] = set()
    previous = None
    for record in records:
        if record.identity in identities:
            raise ValueError(f"duplicate normalized identity: {record.identity}")
        identities.add(record.identity)
        if previous is not None and record.message.stable_key < previous:
            raise ValueError("normalized message store is not chronologically ordered")
        previous = record.message.stable_key


def _full_scan(
    db,
    username: str,
    work_root: Path,
    progress: Callable[[int], None] | None = None,
) -> tuple[list[ArchivedMessage], int]:
    with tempfile.TemporaryDirectory(prefix="state-recovery-", dir=work_root) as temporary:
        spool = spool_all_messages(
            db, username, Path(temporary), page_size=1000, progress=progress
        )
        records = [ArchivedMessage(message) for message in iter_oldest_first(spool)]
    return records, spool.duplicate_message_id_count


def _read_state(target: ArchiveTarget) -> tuple[list[ArchivedMessage], dict] | None:
    state_path = target.root / "archive_state.json"
    if not state_path.is_file() or not target.message_store.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        records = load_message_store(target.message_store)
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            return None
        if int(state.get("message_count", -1)) != len(records):
            return None
        if records:
            last = records[-1]
            if int(state.get("last_sort_seq", -1)) != last.message.sort_seq:
                return None
            if state.get("last_message_identity") != last.identity:
                return None
        return records, state
    except (OSError, ValueError, TypeError, KeyError):
        return None


def collect_incremental(
    db,
    target: ArchiveTarget,
    work_root: Path,
    logger: logging.Logger,
) -> IncrementalResult:
    loaded = _read_state(target)
    if loaded is None:
        logger.warning("归档状态缺失或不一致，执行目标会话全量安全恢复 username=%s", target.username)
        preserved: list[ArchivedMessage] = []
        if target.message_store.is_file():
            try:
                preserved = load_message_store(target.message_store)
            except (OSError, ValueError):
                preserved = []
        scanned, duplicates = _full_scan(db, target.username, work_root)
        by_identity = {record.identity: record for record in preserved}
        preserved_identities = frozenset(by_identity)
        for record in scanned:
            by_identity.setdefault(record.identity, record)
        records = sorted(by_identity.values(), key=lambda item: item.message.stable_key)
        _validate_records(records)
        new_identities = frozenset(set(by_identity) - set(preserved_identities))
        return IncrementalResult(
            tuple(records), new_identities,
            duplicates, "full_recovery", len(scanned),
        )

    existing, state = loaded
    by_identity = {record.identity: record for record in existing}
    old_identities = frozenset(by_identity)
    last_sort_seq = int(state.get("last_sort_seq") or 0)
    since_seq = max(0, last_sort_seq - 1)
    source_rows_read = 0
    duplicate_count = 0

    while True:
        raw_page = db.get_new_messages(
            target.username, since_seq=since_seq, limit=INCREMENTAL_PAGE_SIZE
        )
        if not raw_page:
            break
        source_rows_read += len(raw_page)
        page = sorted(
            (ChatMessage.from_upstream(item) for item in raw_page),
            key=lambda item: item.stable_key,
        )
        for message in page:
            identity = message_identity(message)
            if identity in by_identity:
                duplicate_count += 1
                continue
            by_identity[identity] = ArchivedMessage(message)
        maximum = max(message.sort_seq for message in page)
        if maximum <= since_seq:
            if len(raw_page) >= INCREMENTAL_PAGE_SIZE:
                raise RuntimeError("增量分页 cursor 未前进，已停止以防漏消息")
            break
        since_seq = maximum
        if len(raw_page) < INCREMENTAL_PAGE_SIZE:
            break

    records = sorted(by_identity.values(), key=lambda item: item.message.stable_key)
    _validate_records(records)
    new_identities = frozenset(set(by_identity) - set(old_identities))
    return IncrementalResult(
        tuple(records), new_identities, duplicate_count, "incremental", source_rows_read
    )


def _legacy_image_index(image_dir: Path) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    if not image_dir.is_dir():
        return result
    for path in image_dir.iterdir():
        if not path.is_file():
            continue
        match = IMAGE_FILE_PATTERN.match(path.name)
        if match:
            status = "thumbnail" if match.group("thumb") else "success"
            result[match.group("local_id")] = (status, path.name)
    return result


def attach_legacy_media(
    records: Iterable[ArchivedMessage], image_dir: Path
) -> list[ArchivedMessage]:
    index = _legacy_image_index(image_dir)
    return [
        replace(
            record,
            media_status=index.get(record.message.local_id, ("failed", ""))[0],
            media_asset=index.get(record.message.local_id, ("failed", ""))[1],
            media_kind="images",
        ) if record.message.message_type == "图片" else record
        for record in records
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clone_immutable_asset(source: Path, destination: Path, counts: AssetCounts) -> str:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite immutable asset: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        counts.hardlinked += 1
        mode = "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        counts.copied += 1
        mode = "copy"
    counts.reused += 1
    return mode


def allocate_snapshot_id(snapshots_dir: Path, now: datetime) -> str:
    base = now.strftime("%Y%m%d_%H%M%S")
    candidate = base
    suffix = 1
    while (snapshots_dir / candidate).exists() or (snapshots_dir / (".tmp_" + candidate)).exists():
        candidate = f"{base}_{suffix:02d}"
        suffix += 1
    return candidate


def snapshot_hashes(snapshot_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(snapshot_dir).as_posix()] = _sha256(path)
    return hashes


def _find_previous_asset(target: ArchiveTarget, parent_id: str | None, relative: Path) -> Path | None:
    if parent_id:
        candidate = target.snapshots_dir / parent_id / relative
        if candidate.is_file():
            return candidate
    durable = target.state_dir / "assets" / relative.relative_to("assets")
    if durable.is_file():
        return durable
    legacy = target.root / relative
    return legacy if legacy.is_file() else None


def _capture_avatars(
    db,
    target: ArchiveTarget,
    staging: Path,
    parent_id: str | None,
    self_username: str,
    counts: AssetCounts,
    logger: logging.Logger,
    input_func: Callable[[str], str],
    print_func: Callable[..., None],
) -> dict[str, AvatarSnapshot]:
    avatar_dir = staging / "assets" / "avatars"
    exporter = AvatarExporter(db, avatar_dir, logger)
    captured = {
        "self": exporter.export("self", self_username, "我的", input_func, print_func),
        "contact": exporter.export("contact", target.username, "对方", input_func, print_func),
    }
    for role, snapshot in list(captured.items()):
        if not snapshot.relative_path:
            continue
        current = staging / snapshot.relative_path
        previous_dir = Path("assets") / "avatars"
        previous = None
        if parent_id:
            for candidate in (target.snapshots_dir / parent_id / previous_dir).glob(
                ("me" if role == "self" else "contact") + ".*"
            ):
                if candidate.is_file() and _sha256(candidate) == _sha256(current):
                    previous = candidate
                    break
        if previous:
            current.unlink()
            clone_immutable_asset(previous, current, counts)
            captured[role] = AvatarSnapshot(
                status="reused_" + snapshot.status,
                relative_path=snapshot.relative_path,
                remote_available=snapshot.remote_available,
            )
        else:
            counts.new += 1
    return captured


def _render_snapshot(
    path: Path,
    target: ArchiveTarget,
    records: Iterable[ArchivedMessage],
    avatars: dict[str, AvatarSnapshot],
    created_at: datetime,
) -> tuple[ExportStats, int, int]:
    rows = list(records)
    first_time = rows[0].message.create_time if rows else None
    last_time = rows[-1].message.create_time if rows else None
    stats = ExportStats(
        message_count=len(rows), first_message_time=first_time, last_message_time=last_time
    )
    resolve = sender_resolver(
        ContactChoice.from_fields(username=target.username, nickname=target.display_name)
    )
    image_count = 0
    video_count = 0
    with MarkdownArchiveWriter(
        path, target.display_name, created_at, first_time, last_time, avatars
    ) as writer:
        previous = None
        for record in rows:
            message = record.message
            if previous is not None and message.stable_key < previous:
                stats.markdown_order_verified = False
            previous = message.stable_key
            stats.message_type_counts[message.message_type] = (
                stats.message_type_counts.get(message.message_type, 0) + 1
            )
            if message.message_type == "文本":
                stats.text_message_count += 1
            if message.message_type == "图片":
                stats.image_message_count += 1
                if record.media_asset:
                    image_count += 1
            if message.message_type == "视频" and record.media_asset:
                video_count += 1
            role = "self" if str(message.sender_id) == "2" else "contact"
            stats.sender_counts[role] += 1
            result = None
            if message.message_type in {"图片", "视频"}:
                relative = media_relative_path(record)
                result = MediaResult(
                    record.media_status or "failed",
                    relative.as_posix() if relative else None,
                )
            writer.write_message(message, resolve(message), result, stats)
    return stats, image_count, video_count


def validate_snapshot(
    snapshot_dir: Path,
    records: Iterable[ArchivedMessage],
    expected_snapshot_id: str,
) -> dict:
    chat = snapshot_dir / "chat.md"
    metadata_path = snapshot_dir / "export_meta.json"
    if not chat.is_file() or not metadata_path.is_file():
        raise RuntimeError("snapshot is missing chat.md or export_meta.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("snapshot_id") != expected_snapshot_id:
        raise RuntimeError("snapshot metadata id mismatch")
    rows = list(records)
    _validate_records(rows)
    if int(metadata.get("message_count", -1)) != len(rows):
        raise RuntimeError("snapshot message count mismatch")
    text = chat.read_text(encoding="utf-8")
    missing: list[str] = []
    absolute: list[str] = []
    references = 0
    for match in RESOURCE_PATTERN.finditer(text):
        reference = match.group("html") or match.group("markdown")
        references += 1
        if Path(reference).is_absolute() or re.match(r"^[A-Za-z]:", reference):
            absolute.append(reference)
            continue
        candidate = (snapshot_dir / Path(reference.replace("/", os.sep))).resolve()
        try:
            candidate.relative_to(snapshot_dir.resolve())
        except ValueError:
            absolute.append(reference)
            continue
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            missing.append(reference)
    if missing or absolute:
        raise RuntimeError("snapshot contains invalid or non-local resource references")
    if metadata.get("markdown_order_verified") is not True:
        raise RuntimeError("snapshot message order validation failed")
    return {
        "message_count": len(rows),
        "resource_reference_count": references,
        "missing_resource_count": len(missing),
        "duplicate_message_count": 0,
        "time_order_verified": True,
    }


def _write_archive_state(
    target: ArchiveTarget,
    records: list[ArchivedMessage],
    snapshot_id: str,
) -> None:
    last = records[-1] if records else None
    write_json_atomic(
        target.root / "archive_state.json",
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_snapshot_id": snapshot_id,
            "last_sort_seq": last.message.sort_seq if last else 0,
            "last_message_identity": last.identity if last else None,
            "last_sort_key": [
                last.message.sort_seq if last else 0,
                last.message.local_id if last else None,
            ],
            "message_count": len(records),
        },
    )


def migrate_legacy_export(
    db,
    target: ArchiveTarget,
    work_root: Path,
    logger: logging.Logger,
) -> bool:
    if (target.root / "target.json").is_file() and latest_snapshot_entry(target):
        return False
    legacy_chat = target.root / "chat.md"
    legacy_meta_path = target.root / "export_meta.json"
    if not legacy_chat.is_file() or not legacy_meta_path.is_file():
        save_target(target)
        if not (target.root / "snapshot_index.json").exists():
            write_json_atomic(
                target.root / "snapshot_index.json",
                {"schema_version": STATE_SCHEMA_VERSION, "snapshots": []},
            )
            _write_message_schema(target)
        return False

    original_hashes = {
        "chat.md": _sha256(legacy_chat),
        "export_meta.json": _sha256(legacy_meta_path),
    }
    legacy_meta = json.loads(legacy_meta_path.read_text(encoding="utf-8"))
    created_at = datetime.fromisoformat(legacy_meta["exported_at"])
    target.snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = allocate_snapshot_id(target.snapshots_dir, created_at)
    staging = target.snapshots_dir / (".tmp_" + snapshot_id)
    staging.mkdir(parents=True)
    counts = AssetCounts()
    try:
        shutil.copy2(legacy_chat, staging / "chat.md")
        for relative_dir in (Path("assets/images"), Path("assets/avatars")):
            source_dir = target.root / relative_dir
            if not source_dir.is_dir():
                continue
            for source in source_dir.iterdir():
                if source.is_file():
                    clone_immutable_asset(source, staging / relative_dir / source.name, counts)

        records, duplicates = _full_scan(db, target.username, work_root)
        records = attach_legacy_media(records, target.root / "assets" / "images")
        if len(records) != int(legacy_meta.get("message_count", -1)):
            raise RuntimeError("legacy message count no longer matches a safe full scan")
        metadata = {
            **legacy_meta,
            "status": "COMPLETE",
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": created_at.isoformat(),
            "parent_snapshot_id": None,
            "new_message_count": len(records),
            "new_image_count": int(legacy_meta.get("image_count", 0)),
            "reused_asset_count": counts.reused,
            "hardlinked_asset_count": counts.hardlinked,
            "copied_asset_count": counts.copied,
            "new_asset_count": 0,
            "duplicate_message_id_count": 0,
            "source_duplicate_message_id_count": duplicates,
            "input_mode": "legacy_import_full_scan",
        }
        write_json_atomic(staging / "export_meta.json", metadata)
        validate_snapshot(staging, records, snapshot_id)
        final = target.snapshots_dir / snapshot_id
        staging.replace(final)
        save_target(target)
        write_json_atomic(
            target.root / "snapshot_index.json",
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "snapshots": [{
                    "id": snapshot_id,
                    "created_at": created_at.isoformat(),
                    "message_count": len(records),
                    "new_message_count": len(records),
                    "status": "COMPLETE",
                    "legacy_import": True,
                }],
            },
        )
        write_message_store_atomic(target.message_store, records)
        _write_message_schema(target)
        _write_archive_state(target, records, snapshot_id)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    if original_hashes != {
        "chat.md": _sha256(legacy_chat),
        "export_meta.json": _sha256(legacy_meta_path),
    }:
        raise RuntimeError("legacy export changed during migration")
    return True


def create_snapshot(
    db,
    target: ArchiveTarget,
    self_username: str,
    work_root: Path,
    logger: logging.Logger,
    now: datetime | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
    collection: IncrementalResult | None = None,
    backup_mode: str = "quick",
    reconciliation: dict | None = None,
) -> SnapshotResult:
    now = now or datetime.now().astimezone()
    ensure_current_schema(target.root)
    verify_public_pagination_scope(db, target.username)
    incremental = collection or collect_incremental(db, target, work_root, logger)
    if (
        incremental.new_identities
        and backup_mode != "reconciliation"
        and target.message_store.is_file()
    ):
        backup_mutable_state(target.root, reason="quick_backup_new_messages")
    records = list(incremental.records)
    index = load_snapshot_index(target)
    parent = index["snapshots"][-1]["id"] if index["snapshots"] else None
    target.snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = allocate_snapshot_id(target.snapshots_dir, now)
    staging = target.snapshots_dir / (".tmp_" + snapshot_id)
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
            if record.media_asset:
                relative = media_relative_path(record) or Path("assets") / kind / record.media_asset
                source = _find_previous_asset(target, parent, relative)
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
                elif kind == "videos":
                    new_video_count += 1
            updated.append(replace(
                record,
                media_status=result.status,
                media_asset=asset_name,
                media_kind=kind,
            ))
        records = updated

        avatars = _capture_avatars(
            db, target, staging, parent, self_username, counts, logger,
            input_func, print_func,
        )
        stats, image_count, video_count = _render_snapshot(
            staging / "chat.md", target, records, avatars, now
        )
        metadata = {
            "status": "COMPLETE",
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": now.isoformat(),
            "parent_snapshot_id": parent,
            "chat_name": target.display_name,
            "username": target.username,
            "message_count": len(records),
            "new_message_count": len(incremental.new_identities),
            "image_count": image_count,
            "new_image_count": new_image_count,
            "video_count": video_count,
            "new_video_count": new_video_count,
            "reused_asset_count": counts.reused,
            "hardlinked_asset_count": counts.hardlinked,
            "copied_asset_count": counts.copied,
            "new_asset_count": counts.new,
            "duplicate_message_id_count": 0,
            "incremental_overlap_duplicate_count": incremental.duplicate_count,
            "incremental_source_rows_read": incremental.source_rows_read,
            "input_mode": incremental.mode,
            "backup_mode": backup_mode,
            "first_message_time": (
                datetime.fromtimestamp(stats.first_message_time).astimezone().isoformat()
                if stats.first_message_time else None
            ),
            "last_message_time": (
                datetime.fromtimestamp(stats.last_message_time).astimezone().isoformat()
                if stats.last_message_time else None
            ),
            "markdown_order_verified": stats.markdown_order_verified,
            "message_type_counts": stats.message_type_counts,
            "message_coverage": {
                "fully_supported": stats.fully_supported_counts,
                "partial": stats.partial_support_counts,
                "placeholder": stats.placeholder_counts,
                "placeholder_count": stats.unsupported_message_count,
            },
            "sender_counts": stats.sender_counts,
            "avatars": {
                role: {
                    "status": avatar.status,
                    "path": avatar.relative_path,
                    "remote_available": avatar.remote_available,
                }
                for role, avatar in avatars.items()
            },
        }
        if reconciliation is not None:
            metadata["reconciliation"] = reconciliation
        write_json_atomic(staging / "export_meta.json", metadata)
        validation = validate_snapshot(staging, records, snapshot_id)
        metadata["validation"] = validation
        write_json_atomic(staging / "export_meta.json", metadata)
        validate_snapshot(staging, records, snapshot_id)

        final = target.snapshots_dir / snapshot_id
        staging.replace(final)
        integrity_status = "BASELINE_CREATED"
        try:
            from .integrity import generate_manifest

            generate_manifest(target.root, snapshot_id, created_at=now)
        except Exception as exc:
            integrity_status = "INTEGRITY_MANIFEST_FAILED"
            logger.error(
                "integrity manifest failed snapshot_id=%s error_type=%s",
                snapshot_id,
                type(exc).__name__,
            )
        try:
            index["snapshots"].append({
                "id": snapshot_id,
                "created_at": now.isoformat(),
                "message_count": len(records),
                "new_message_count": len(incremental.new_identities),
                "status": "COMPLETE",
                "integrity_status": integrity_status,
            })
            write_json_atomic(target.root / "snapshot_index.json", index)
            if latest_snapshot_entry(target).get("id") != snapshot_id:
                raise RuntimeError("snapshot index update verification failed")
        except Exception:
            incomplete = target.snapshots_dir / (".tmp_" + snapshot_id)
            final.replace(incomplete)
            raise
        write_message_store_atomic(target.message_store, records)
        _write_message_schema(target)
        _write_archive_state(target, records, snapshot_id)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return SnapshotResult(
        snapshot_id=snapshot_id,
        path=final,
        message_count=len(records),
        new_message_count=len(incremental.new_identities),
        image_count=image_count,
        new_image_count=new_image_count,
        duplicate_count=0,
        asset_counts=counts,
        coverage_summary={
            "fully_supported": stats.fully_supported_counts,
            "partial": stats.partial_support_counts,
            "placeholder": stats.placeholder_counts,
        },
        video_count=video_count,
        new_video_count=new_video_count,
    )


def list_incomplete_snapshots(target: ArchiveTarget) -> list[Path]:
    if not target.snapshots_dir.is_dir():
        return []
    return sorted(
        path for path in target.snapshots_dir.iterdir()
        if path.is_dir() and path.name.startswith(".tmp_")
    )
