from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .archive import (
    ArchiveTarget,
    ArchivedMessage,
    IncrementalResult,
    SnapshotResult,
    _full_scan,
    _validate_records,
    create_snapshot,
    load_message_store,
)
from .migrations import backup_mutable_state, ensure_current_schema
from .current_capsule import CapsuleResult, create_current_capsule
from .utils import write_json_atomic


CONFLICT_FIELDS = (
    "create_time",
    "sender_id",
    "sender_username",
    "message_type",
    "content",
    "sort_seq",
)


@dataclass(frozen=True)
class ReconciliationConflict:
    identity: str
    fields: tuple[str, ...]
    severe: bool

    def to_report(self) -> dict:
        return {
            "type": "STATE_CONFLICT",
            "message_identity": self.identity,
            "conflict_fields": list(self.fields),
            "severity": "FAIL_SAFE" if self.severe else "ARCHIVE_FIRST",
        }


@dataclass(frozen=True)
class ReconciliationResult:
    collection: IncrementalResult
    database_message_count: int
    archive_before_count: int
    matched_existing: int
    newly_discovered: int
    conflicts: tuple[ReconciliationConflict, ...]
    enriched_existing: int = 0


def _conflict_fields(archived: ArchivedMessage, current: ArchivedMessage) -> tuple[str, ...]:
    return tuple(
        field
        for field in CONFLICT_FIELDS
        if getattr(archived.message, field) != getattr(current.message, field)
    )


def collect_reconciliation(
    db,
    target: ArchiveTarget,
    work_root: Path,
    logger,
    progress: Callable[[int], None] | None = None,
) -> ReconciliationResult:
    ensure_current_schema(target.root)
    try:
        existing = load_message_store(target.message_store)
    except FileNotFoundError:
        existing = []
    scanned, source_duplicates = _full_scan(
        db, target.username, work_root, progress=progress
    )

    by_identity = {record.identity: record for record in existing}
    new_identities: set[str] = set()
    conflicts: list[ReconciliationConflict] = []
    matched = 0
    enriched = 0
    for current in scanned:
        archived = by_identity.get(current.identity)
        if archived is None:
            by_identity[current.identity] = current
            new_identities.add(current.identity)
            continue
        if archived.message.payload is None and current.message.payload is not None:
            enriched_message = replace(
                archived.message,
                subtype=current.message.subtype,
                payload=current.message.payload,
            )
            archived = replace(archived, message=enriched_message)
            by_identity[current.identity] = archived
            enriched += 1
        fields = _conflict_fields(archived, current)
        if not fields:
            matched += 1
            continue
        conflicts.append(
            ReconciliationConflict(
                current.identity,
                fields,
                severe="sort_seq" in fields,
            )
        )
        logger.warning(
            "STATE_CONFLICT identity=%s fields=%s severity=%s",
            current.identity,
            ",".join(fields),
            "FAIL_SAFE" if "sort_seq" in fields else "ARCHIVE_FIRST",
        )

    merged = sorted(by_identity.values(), key=lambda item: item.message.stable_key)
    _validate_records(merged)
    collection = IncrementalResult(
        tuple(merged), frozenset(new_identities), source_duplicates,
        "reconciliation", len(scanned),
    )
    return ReconciliationResult(
        collection=collection,
        database_message_count=len(scanned),
        archive_before_count=len(existing),
        matched_existing=matched,
        newly_discovered=len(new_identities),
        conflicts=tuple(conflicts),
        enriched_existing=enriched,
    )


def _report_path(target: ArchiveTarget, now: datetime) -> Path:
    parent = target.root / "reports"
    parent.mkdir(parents=True, exist_ok=True)
    base = "reconciliation_" + now.strftime("%Y%m%d_%H%M%S")
    candidate = parent / f"{base}.json"
    suffix = 1
    while candidate.exists():
        candidate = parent / f"{base}_{suffix:02d}.json"
        suffix += 1
    return candidate


def _report_payload(
    target: ArchiveTarget,
    result: ReconciliationResult,
    started_at: datetime,
    completed_at: datetime,
    snapshot_id: str | None,
) -> dict:
    return {
        "schema_version": 1,
        "target": target.username,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "database_message_count": result.database_message_count,
        "archive_before_count": result.archive_before_count,
        "matched_existing": result.matched_existing,
        "newly_discovered": result.newly_discovered,
        "enriched_existing": result.enriched_existing,
        "conflict_count": len(result.conflicts),
        "archive_after_count": len(result.collection.records),
        "snapshot_id": snapshot_id,
        "conflicts": [conflict.to_report() for conflict in result.conflicts],
    }


def reconcile_and_create_snapshot(
    db,
    target: ArchiveTarget,
    self_username: str,
    work_root: Path,
    logger,
    *,
    now: datetime | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> tuple[SnapshotResult, ReconciliationResult, Path]:
    now = now or datetime.now().astimezone()
    started_at = now
    reconciliation = collect_reconciliation(
        db,
        target,
        work_root,
        logger,
        progress=lambda count: print_func(f"  已扫描 {count} 条消息"),
    )
    report_path = _report_path(target, now)
    severe = [conflict for conflict in reconciliation.conflicts if conflict.severe]
    if severe:
        write_json_atomic(
            report_path,
            _report_payload(
                target, reconciliation, started_at, datetime.now().astimezone(), None
            ),
        )
        raise RuntimeError(
            "完整复核发现 sort key 冲突，已 fail safe；详情见 reconciliation report。"
        )
    if reconciliation.newly_discovered or reconciliation.enriched_existing:
        reason = (
            "reconciliation_new_messages"
            if reconciliation.newly_discovered
            else "reconciliation_message_enrichment"
        )
        backup_mutable_state(target.root, reason=reason)

    summary = {
        "scanned_messages": reconciliation.database_message_count,
        "matched_existing": reconciliation.matched_existing,
        "newly_discovered": reconciliation.newly_discovered,
        "enriched_existing": reconciliation.enriched_existing,
        "conflicts": len(reconciliation.conflicts),
    }
    snapshot = create_snapshot(
        db,
        target,
        self_username,
        work_root,
        logger,
        now=now,
        input_func=input_func,
        print_func=print_func,
        collection=reconciliation.collection,
        backup_mode="reconciliation",
        reconciliation=summary,
    )
    write_json_atomic(
        report_path,
        _report_payload(
            target,
            reconciliation,
            started_at,
            datetime.now().astimezone(),
            snapshot.snapshot_id,
        ),
    )
    return snapshot, reconciliation, report_path


def reconcile_and_update_capsule(
    db,
    target: ArchiveTarget,
    self_username: str,
    work_root: Path,
    logger,
    *,
    now: datetime | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> tuple[CapsuleResult, ReconciliationResult, Path]:
    now = now or datetime.now().astimezone()
    reconciliation = collect_reconciliation(
        db,
        target,
        work_root,
        logger,
        progress=lambda count: print_func(f"  已扫描 {count} 条消息"),
    )
    report_path = _report_path(target, now)
    severe = [conflict for conflict in reconciliation.conflicts if conflict.severe]
    if severe:
        write_json_atomic(
            report_path,
            _report_payload(
                target, reconciliation, now, datetime.now().astimezone(), None
            ),
        )
        raise RuntimeError(
            "完整复核发现 sort key 冲突，已 fail safe；详情见 reconciliation report。"
        )
    if reconciliation.newly_discovered or reconciliation.enriched_existing:
        reason = (
            "reconciliation_new_messages"
            if reconciliation.newly_discovered
            else "reconciliation_message_enrichment"
        )
        backup_mutable_state(target.root, reason=reason)
    summary = {
        "scanned_messages": reconciliation.database_message_count,
        "matched_existing": reconciliation.matched_existing,
        "newly_discovered": reconciliation.newly_discovered,
        "enriched_existing": reconciliation.enriched_existing,
        "conflicts": len(reconciliation.conflicts),
    }
    capsule = create_current_capsule(
        db,
        target,
        self_username,
        work_root,
        logger,
        now=now,
        input_func=input_func,
        print_func=print_func,
        collection=reconciliation.collection,
        save_mode="reconciliation",
        reconciliation=summary,
    )
    payload = _report_payload(
        target, reconciliation, now, datetime.now().astimezone(), None
    )
    payload["current_capsule_updated"] = True
    write_json_atomic(report_path, payload)
    return capsule, reconciliation, report_path
