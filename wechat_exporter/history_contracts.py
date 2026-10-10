"""In-memory historical core contracts; no storage or identity policy.

Scopes come from the authorized caller. Source keys describe canonical relative
names only: discovery must independently verify file identity and reject reparse
points. A locator is an observation, never a persistent canonical identity.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Literal, Mapping

from .models import ChatMessage

if TYPE_CHECKING:
    from .archive import ArchivedMessage


def _require(condition: bool) -> None:
    if not condition:
        # Never interpolate rejected values, identities, paths, or exceptions.
        raise ValueError("INVALID_HISTORY_CONTRACT")


def _text(value: object) -> None:
    _require(isinstance(value, str) and bool(value.strip()))


def _integer(value: object) -> None:
    _require(type(value) is int)


def _count(value: object) -> None:
    _integer(value)
    _require(value >= 0)


def _mode(value: object) -> None:
    _require(value in ("full", "quick"))


@dataclass(frozen=True)
class ArchiveScope:
    account_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        _text(self.account_id)
        _text(self.conversation_id)


@dataclass(frozen=True)
class ScopeAuthorization:
    scope: ArchiveScope
    origin_confirmed: bool

    def __post_init__(self) -> None:
        _require(isinstance(self.scope, ArchiveScope))
        _require(type(self.origin_confirmed) is bool)
        # False is retained as evidence; migration owns LEGACY_SCOPE_UNVERIFIED.


@dataclass(frozen=True)
class SourceKey:
    relative_db: str
    incarnation: str

    def __post_init__(self) -> None:
        _require(isinstance(self.relative_db, str) and
                 re.fullmatch(r"message/message_[0-9]+\.db", self.relative_db) is not None)
        _text(self.incarnation)


@dataclass(frozen=True)
class SourceDescriptor:
    key: SourceKey
    target_table: str
    target_present: bool
    schema_fingerprint: str

    def __post_init__(self) -> None:
        _require(isinstance(self.key, SourceKey))
        _text(self.target_table)
        _require(type(self.target_present) is bool)
        _text(self.schema_fingerprint)


@dataclass(frozen=True)
class SourceLocator:
    scope: ArchiveScope
    source_key: SourceKey
    table_name: str
    local_id: str
    expected_server_id: str
    create_time: int
    sort_seq: int
    raw_local_type: int
    storage_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(isinstance(self.scope, ArchiveScope))
        _require(isinstance(self.source_key, SourceKey))
        _text(self.table_name)
        _text(self.local_id)
        # Zero/missing/malformed server policy belongs to the identity resolver.
        _require(isinstance(self.expected_server_id, str))
        for value in (self.create_time, self.sort_seq, self.raw_local_type):
            _integer(value)
        _require(type(self.storage_classes) is tuple and len(self.storage_classes) == 5)
        _require(all(value in ("integer", "text", "real", "blob", "null")
                     for value in self.storage_classes))
        # Ordered observations: server, local, time, sequence, raw local type.


@dataclass(frozen=True)
class SourceRow:
    message: ChatMessage
    server_id: str
    locator: SourceLocator
    raw_row_token: str

    def __post_init__(self) -> None:
        _require(isinstance(self.message, ChatMessage))
        _require(isinstance(self.server_id, str))
        _require(isinstance(self.locator, SourceLocator))
        _text(self.raw_row_token)


@dataclass(frozen=True)
class SourceCursor:
    source_key: SourceKey
    high_watermark: tuple[int, int, int]
    overlap_seq: int = 1

    def __post_init__(self) -> None:
        _require(isinstance(self.source_key, SourceKey))
        _require(type(self.high_watermark) is tuple and len(self.high_watermark) == 3)
        for value in self.high_watermark:
            _integer(value)
        _count(self.overlap_seq)


@dataclass(frozen=True)
class SourceCount:
    source_key: SourceKey
    raw_total: int
    raw_selected: int
    enumerated: int
    decoded: int

    def __post_init__(self) -> None:
        _require(isinstance(self.source_key, SourceKey))
        for value in (self.raw_total, self.raw_selected, self.enumerated, self.decoded):
            _count(value)
        # Reader, not DTO construction, owns SOURCE_COUNT_MISMATCH.


@dataclass(frozen=True)
class InventoryDigests:
    source_set_digest: str
    source_rows_digest: str
    scope_digest: str


@dataclass(frozen=True)
class Inventory:
    scope: ArchiveScope
    mode: Literal["full", "quick"]
    rows: tuple[SourceRow, ...]
    sources: tuple[SourceDescriptor, ...]
    counts: tuple[SourceCount, ...]
    next_cursors: tuple[SourceCursor, ...]
    digests: InventoryDigests

    def __post_init__(self) -> None:
        _require(isinstance(self.scope, ArchiveScope))
        _mode(self.mode)


@dataclass(frozen=True)
class IdentityConflict:
    category: str
    fields: tuple[str, ...]
    severe: bool
    count: int

    def __post_init__(self) -> None:
        _count(self.count)
        _require(type(self.severe) is bool)


@dataclass(frozen=True)
class IdentityResolution:
    records: tuple[ArchivedMessage, ...]
    conflicts: tuple[IdentityConflict, ...]
    canonical_duplicate_count: int

    def __post_init__(self) -> None:
        _count(self.canonical_duplicate_count)


@dataclass(frozen=True)
class MigrationPlan:
    records: tuple[ArchivedMessage, ...]
    legacy_matched_count: int
    archive_only_count: int
    staged_metadata: dict
    newly_discovered_count: int = 0

    def __post_init__(self) -> None:
        for value in (self.legacy_matched_count, self.archive_only_count,
                      self.newly_discovered_count):
            _count(value)


@dataclass(frozen=True)
class CountReport:
    mode: Literal["full", "quick"]
    count_scope: Literal["full", "quick_window"]
    raw_source_count: int
    raw_selected_count: int
    decoded_source_count: int
    canonical_duplicate_count: int
    legacy_matched_count: int
    archive_only_count: int | None
    archive_before_count: int
    newly_discovered_count: int
    conflict_count: int
    final_union_count: int
    earliest_timestamp: int | None
    latest_timestamp: int | None
    media_recovered_count: int
    media_missing_count: int
    integrity_result: str

    def __post_init__(self) -> None:
        _mode(self.mode)
        _require(self.count_scope == ("full" if self.mode == "full" else "quick_window"))
        for value in (self.raw_source_count, self.raw_selected_count,
                      self.decoded_source_count, self.canonical_duplicate_count,
                      self.legacy_matched_count, self.archive_before_count,
                      self.newly_discovered_count, self.conflict_count,
                      self.final_union_count, self.media_recovered_count,
                      self.media_missing_count):
            _count(value)
        if self.archive_only_count is None:
            _require(self.mode == "quick")
        else:
            _count(self.archive_only_count)
        for value in (self.earliest_timestamp, self.latest_timestamp):
            if value is not None:
                _integer(value)


@dataclass(frozen=True)
class HistoryCollection:
    records: tuple[ArchivedMessage, ...]
    new_identities: frozenset[str]
    inventory: Inventory
    migration: MigrationPlan | None
    counts: CountReport


HISTORY_ERROR_CODES = frozenset({
    "INVALID_SERVER_ID", "REPRESENTATION_UNRESOLVED", "IDENTITY_SCOPE_MISMATCH",
    "IDENTITY_INVARIANT_CONFLICT", "UNVERIFIED_IDENTITY_INVARIANT",
    "AMBIGUOUS_LEGACY_BINDING", "POSSIBLE_LEGACY_COUNTERPART", "LEGACY_ALIAS_CONFLICT",
    "INCOMPLETE_SOURCE_SET", "SOURCE_SCHEMA_CHANGED", "UNSUPPORTED_SOURCE_SCHEMA",
    "SOURCE_DISCOVERY_FAILED", "SOURCE_READ_FAILED", "SOURCE_COUNT_MISMATCH",
    "SOURCE_SNAPSHOT_UNSAFE", "UNSUPPORTED_UPSTREAM_VERSION", "UNSUPPORTED_UPSTREAM_API",
    "SOURCE_ROW_INVALID", "SOURCE_READ_TIMEOUT", "SOURCE_IDENTITY_MISMATCH",
    "ARCHIVED_ASSET_INTEGRITY_FAILED", "TRANSACTION_RECOVERY_REQUIRED",
    "UNSUPPORTED_ARCHIVE_SCHEMA", "ORIGINAL_GATE_BLOCKED", "LEGACY_SCOPE_UNVERIFIED",
    "FULL_INVENTORY_REQUIRED", "INVENTORY_MODE_MISMATCH", "SOURCE_SET_CHANGED",
})
_COUNT_FIELDS = frozenset({
    "raw_total", "raw_selected", "enumerated", "decoded", "count", "attempt_count",
    "raw_source_count", "raw_selected_count", "decoded_source_count",
    "canonical_duplicate_count", "legacy_matched_count", "archive_only_count",
    "archive_before_count", "newly_discovered_count", "conflict_count",
    "final_union_count", "media_recovered_count", "media_missing_count",
})
_TECHNICAL_FIELDS = frozenset({
    "scope", "source_key", "target_table", "target_present", "schema_fingerprint",
    "table_name", "local_id", "server_id", "expected_server_id", "create_time",
    "sort_seq", "raw_local_type", "storage_classes", "high_watermark", "overlap_seq",
    "mode", "count_scope", "content", "payload", "message_type", "subtype",
    "sender_id", "sender_username",
})
_HASH_FIELDS = frozenset({"source_set_digest", "source_rows_digest", "scope_digest",
                          "evidence_digest", "asset_digest"})
_SNAPSHOT_PHASES = frozenset({
    "path_validation", "work_root_validation", "source_discovery", "capability_binding",
    "provider_selection", "worker_admission", "worker_initialization", "source_listing",
    "metadata_snapshot", "shard_snapshot", "snapshot_verification", "inventory_decode",
    "source_stability", "snapshot_probe", "media_read",
})
_SNAPSHOT_BRANCHES = frozenset({
    "path_reparse_point", "work_storage_missing", "work_root_overlap", "work_path_oserror",
    "source_not_regular", "binding_missing", "runtime_type_mismatch", "runtime_state_shape",
    "runtime_path_shape", "capability_key_invalid", "metadata_path_invalid", "freeze_duplicate",
    "freeze_key_missing", "decoder_image_missing", "reader_account_missing", "worker_not_private",
    "metadata_selection_invalid", "selection_key_missing", "media_file_outside_domain",
    "media_file_identity", "media_walk_outside_domain", "image_probe_outside_domain",
    "image_decrypt_outside_domain",
})
_SNAPSHOT_EXCEPTION_CLASSES = frozenset({
    "HistoryError", "SourceSetChanged",
    "OSError", "FileNotFoundError", "PermissionError", "OperationalError", "DatabaseError",
    "IntegrityError", "ProgrammingError", "ValueError", "TypeError", "AttributeError",
    "RuntimeError", "AssertionError", "EOFError", "BrokenPipeError", "MemoryError",
    "KeyboardInterrupt", "SystemExit",
})
_MEDIA_REJECTION_CONDITIONS = frozenset({
    "MEDIA_IDENTITY_OBSERVATION_CHANGED", "MEDIA_NOT_REGULAR", "MEDIA_LINK_COUNT_UNEXPECTED",
    "MEDIA_SOURCE_MISSING",
})


def snapshot_diagnostic(value: object) -> dict:
    """Closed technical projection; never retain arbitrary text or private paths."""
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for name, allowed in (("phase", _SNAPSHOT_PHASES), ("branch", _SNAPSHOT_BRANCHES),
                          ("exception_class", _SNAPSHOT_EXCEPTION_CLASSES)):
        item = value.get(name)
        if type(item) is str and item in allowed:
            result[name] = item
    for name in ("worker_initialized", "snapshot_started", "snapshot_completed"):
        if name in value and (value[name] is None or type(value[name]) is bool):
            result[name] = value[name]
    condition = value.get("condition")
    if type(condition) is str and condition in _MEDIA_REJECTION_CONDITIONS:
        result["condition"] = condition
    if type(value.get("observed_is_file")) is bool:
        result["observed_is_file"] = value["observed_is_file"]
    if type(value.get("st_nlink")) is int and value["st_nlink"] >= 0:
        result["st_nlink"] = value["st_nlink"]
    return result


class HistoryError(RuntimeError):
    """A fixed code and allowlisted technical observations, never raw details."""

    def __init__(self, code: str, summary: dict | None = None, *,
                 counts: Mapping[str, int | None] | None = None,
                 fields: tuple[str, ...] = (), hashes: Mapping[str, str] | None = None,
                 diagnostic: Mapping[str, object] | None = None,
                 **discarded_details: object) -> None:
        _require(isinstance(code, str) and code in HISTORY_ERROR_CODES)
        _require(summary is None or isinstance(summary, dict))
        incoming = summary or {}
        incoming_counts = incoming.get("counts", {})
        incoming_hashes = incoming.get("hashes", {})
        incoming_fields = incoming.get("fields", ())
        _require(isinstance(incoming_counts, Mapping) and isinstance(incoming_hashes, Mapping))
        _require(isinstance(incoming_fields, (tuple, list)))
        _require(counts is None or isinstance(counts, Mapping))
        _require(hashes is None or isinstance(hashes, Mapping))
        _require(isinstance(fields, (tuple, list)))
        self.code = code
        combined_counts = {**incoming_counts, **(counts or {})}
        combined_hashes = {**incoming_hashes, **(hashes or {})}
        self._counts = {name: value for name, value in combined_counts.items()
                        if name in _COUNT_FIELDS and
                        (value is None or (type(value) is int and value >= 0))}
        self._fields = tuple(name for name in (*incoming_fields, *fields)
                             if isinstance(name, str) and name in _TECHNICAL_FIELDS)
        self._hashes = {name: value for name, value in combined_hashes.items()
                        if name in _HASH_FIELDS and isinstance(value, str)
                        and re.fullmatch(r"[0-9a-fA-F]{64}", value)}
        self._diagnostic_provided = diagnostic is not None or "diagnostic" in incoming
        self._diagnostic = {**snapshot_diagnostic(incoming.get("diagnostic")),
                            **snapshot_diagnostic(diagnostic)}
        super().__init__(code)

    @property
    def summary(self) -> dict:
        return self.to_summary()

    def to_summary(self) -> dict:
        result = {"code": self.code, "counts": dict(self._counts),
                  "fields": list(self._fields), "hashes": dict(self._hashes)}
        if self._diagnostic_provided:
            result["diagnostic"] = dict(self._diagnostic)
        return result


class SourceSetChanged(HistoryError):
    """Internal drift signal; collection owns the bounded restart budget."""

    def __init__(self, summary: dict | None = None, **details: object) -> None:
        super().__init__("SOURCE_SET_CHANGED", summary, **details)
