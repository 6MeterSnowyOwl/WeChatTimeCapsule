"""Source-qualified local media helpers; explicit roots, no save/activation wiring."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

from .history_contracts import HistoryError, SourceLocator
from .message_identity import normalize_server_id
from .models import MediaResult

if TYPE_CHECKING:
    from .archive import ArchivedMessage
    from .conversation_reader import InventorySession


@dataclass(frozen=True)
class ValidatedRawMediaRow:
    locator: SourceLocator
    raw: Mapping


def _storage_class(value):
    if value is None:
        return "null"
    return {int: "integer", str: "text", bytes: "blob", float: "real"}.get(type(value), "invalid")


def resolve_media_row(session: InventorySession, record: ArchivedMessage) -> ValidatedRawMediaRow:
    """Retained current-session provenance is mandatory; an old locator is not proof."""
    def mismatch():
        raise HistoryError("SOURCE_IDENTITY_MISMATCH")

    if record.scope != session.inventory.scope or not record.source_refs:
        mismatch()
    candidates = []
    for ref in record.source_refs:
        matches = [row for row in session.inventory.rows if row.locator == ref]
        if len(matches) != 1 or ref.scope != record.scope:
            mismatch()
        row = matches[0]
        try:
            identity = normalize_server_id(row.server_id, ref.storage_classes[0])
            expected = normalize_server_id(ref.expected_server_id, ref.storage_classes[0])
        except HistoryError:
            mismatch()
        if (record.canonical_identity != identity or expected != identity or
                ref.create_time != record.message.create_time or ref.sort_seq != record.message.sort_seq):
            mismatch()
        candidates.append(row)
    row = candidates[0]
    ref = row.locator
    raw = session.resolve_raw_row(row.raw_row_token)
    fields = ("server_id", "local_id", "create_time", "sort_seq", "local_type")
    try:
        classes = tuple(_storage_class(raw[field]) for field in fields)
        server = normalize_server_id(raw["server_id"], classes[0])
        if (classes != ref.storage_classes or server != record.canonical_identity or
                str(raw["local_id"]) != ref.local_id or raw["create_time"] != ref.create_time or
                raw["sort_seq"] != ref.sort_seq or raw["local_type"] != ref.raw_local_type):
            mismatch()
        # Require the complete pinned raw SQL shape, including opaque blobs.
        for field in ("real_sender_id", "message_content", "source", "packed_info_data", "compress_content"):
            raw[field]
    except (KeyError, TypeError, ValueError, HistoryError):
        mismatch()
    return ValidatedRawMediaRow(ref, MappingProxyType(dict(raw)))


class BoundMediaDB:
    """Narrow pinned downloader facade. Never forward any database lookup."""
    _ATTRIBUTES = frozenset({"account_dir", "workdir", "account", "wxid", "cfg_dword"})

    def __init__(self, base_db: object, validated_row: ValidatedRawMediaRow):
        self._base = base_db
        self._validated = validated_row

    def __getattr__(self, name):
        if name not in self._ATTRIBUTES:
            raise AttributeError(name)
        return getattr(self._base, name)

    def get_message_row(self, user: str, local_id: int) -> dict:
        ref = self._validated.locator
        if user != ref.scope.conversation_id or type(local_id) is not int or str(local_id) != ref.local_id:
            raise HistoryError("SOURCE_IDENTITY_MISMATCH")
        raw = self._validated.raw
        return {"local_id": raw["local_id"], "local_type": raw["local_type"],
                "server_id": raw["server_id"], "sender_id": raw["real_sender_id"],
                "sender_username": "", "create_time": raw["create_time"],
                "content": raw["message_content"], "source": raw["source"],
                "packed_info": raw["packed_info_data"], "compress_content": raw["compress_content"],
                "sort_seq": raw["sort_seq"]}


def _asset_relative(record):
    kind = record.media_kind or {"图片": "images", "视频": "videos"}.get(record.message.message_type)
    name = record.media_asset
    if kind not in ("images", "videos") or not name or Path(name).name != name or name in (".", ".."):
        raise HistoryError("ARCHIVED_ASSET_INTEGRITY_FAILED")
    return Path("assets") / kind / name


def validate_archived_assets(root: Path, records: tuple[ArchivedMessage, ...], manifest: dict) -> None:
    """Check referenced current assets AND durable vault against supplied old facts.

    The explicit root is the synthetic archive root matching this manifest;
    this does not discover a root, generate a manifest, or repair any asset.
    """
    try:
        root = Path(root).resolve(strict=True)
        files = manifest["files"]
        for record in records:
            if not record.media_asset:
                continue
            relative = _asset_relative(record)
            fact = files[relative.as_posix()]
            if type(fact["size"]) is not int or fact["size"] <= 0:
                raise ValueError
            for location in (root / relative, root / "state" / relative):
                location.resolve(strict=True).relative_to(root)
                if (not location.is_file() or location.stat().st_size != fact["size"] or
                        hashlib.sha256(location.read_bytes()).hexdigest() != fact["sha256"]):
                    raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise HistoryError("ARCHIVED_ASSET_INTEGRITY_FAILED") from None


def _has_current_media_candidate(session: InventorySession, record: ArchivedMessage) -> bool:
    """Classify selected-inventory absence, never treat a historical ref as current.

    This validates supplied memory metadata, not missing-source identity proof.
    A quick inventory can establish only absence from its selected window.
    """
    def mismatch():
        raise HistoryError("SOURCE_IDENTITY_MISMATCH") from None

    def observation(raw_type, classes):
        if (type(raw_type) is not int or not isinstance(classes, (tuple, list)) or
                len(classes) != 5 or classes[0] not in ("integer", "text") or
                tuple(classes[1:]) != ("integer",) * 4):
            mismatch()
        return raw_type, tuple(classes)

    identity = record.canonical_identity
    if (record.scope != session.inventory.scope or not isinstance(identity, str) or
            type(record.source_refs) is not tuple or type(record.technical_observations) is not tuple):
        mismatch()
    try:
        if identity.startswith("server:"):
            if normalize_server_id(identity[7:], "text") != identity or record.binding_status != "server_bound":
                mismatch()
        elif identity.startswith("archive:"):
            token = UUID(identity[8:])
            if token.version != 4 or token.hex != identity[8:] or record.binding_status != "archive_only":
                mismatch()
        else:
            mismatch()
        observed = []
        for ref in record.source_refs:
            if (not isinstance(ref, SourceLocator) or ref.scope != record.scope or
                    normalize_server_id(ref.expected_server_id, ref.storage_classes[0]) != identity or
                    str(int(ref.local_id)) != ref.local_id or ref.create_time != record.message.create_time or
                    ref.sort_seq != record.message.sort_seq):
                mismatch()
            observed.append(observation(ref.raw_local_type, ref.storage_classes))
        for item in record.technical_observations:
            if not isinstance(item, dict):
                mismatch()
            observed.append(observation(item.get("raw_local_type"), item.get("storage_classes")))
        if observed and any(item != observed[0] for item in observed[1:]):
            mismatch()
        # No new observation is required to decline decoding an absent source.
        positions = {(ref.source_key, ref.table_name, ref.local_id) for ref in record.source_refs}
        for row in session.inventory.rows:
            current_identity = normalize_server_id(row.server_id, row.locator.storage_classes[0])
            position = (row.locator.source_key, row.locator.table_name, row.locator.local_id)
            if current_identity == identity or position in positions:
                return True  # Strict resolver must adjudicate candidate/ref contradictions.
    except (ValueError, TypeError, AttributeError, HistoryError):
        mismatch()
    return False


def export_record_media(session: InventorySession, record: ArchivedMessage, base_db: object,
                        staging_root: Path, *, archived_root: Path | None = None,
                        manifest: dict | None = None, logger: logging.Logger | None = None,
                        downloader_factory=None) -> MediaResult:
    """Optional in-memory helper, used only by explicit synthetic callers.

    Reused paths refer to archived_root; new paths refer to staging_root. The
    caller owns the open session and roots. Native temporary/key-cache folders
    are operation-owned and removed on return; final new staged files remain.
    """
    if record.media_asset:
        if archived_root is None or manifest is None:
            raise HistoryError("ARCHIVED_ASSET_INTEGRITY_FAILED")
        validate_archived_assets(archived_root, (record,), manifest)
        return MediaResult(record.media_status or "success", relative_path=_asset_relative(record).as_posix())
    if not _has_current_media_candidate(session, record):
        return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
    validated = resolve_media_row(session, record)  # Fatal checks precede downloader catches/writes.
    from .media import ImageExporter, VideoExporter
    kind = {3: ("images", ImageExporter), 43: ("videos", VideoExporter)}.get(validated.locator.raw_local_type)
    if kind is None:
        return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
    root = Path(staging_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Canonical/source lookup isolation also covers upstream persisted key cache.
        source_digest = hashlib.sha256(repr(validated.locator.source_key).encode()).hexdigest()[:16]
        with TemporaryDirectory(prefix="media-" + source_digest + "-", dir=root) as work:
            facade = BoundMediaDB(base_db, validated)
            facade.workdir = work
            options = {'downloader_factory': downloader_factory} if downloader_factory is not None else {}
            exporter = kind[1](facade, validated.locator.scope.conversation_id,
                               root / "assets" / kind[0], logger or logging.getLogger(__name__), **options)
            lookup_message = replace(record.message, local_id=validated.locator.local_id)
            return exporter.export(lookup_message, canonical_identity=record.canonical_identity)
    except OSError:
        # Bounded core fallback for canonical local materialization I/O failure.
        raise HistoryError("SOURCE_READ_FAILED") from None
