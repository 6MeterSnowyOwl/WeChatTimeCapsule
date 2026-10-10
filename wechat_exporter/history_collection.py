"""Archive-first memory union, count evidence and bounded session ownership.

No archive loading, media staging, transactions or persistence occur here.
The borrowed-session entry point leaves raw-row access with its caller; the
retry entry point returns memory data only and closes every acquired session.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from .archive import ArchivedMessage
from .history_contracts import (
    CountReport, HistoryCollection, HistoryError, Inventory, ScopeAuthorization, SourceSetChanged,
)
from .identity_migration import plan_identity_migration
from .message_identity import canonical_sort_key, normalize_server_id, resolve_canonical_rows

if TYPE_CHECKING:
    from .conversation_reader import InventorySession


def _validate_counts(inventory: Inventory) -> None:
    """Require one count proof per source, including supported absent tables."""
    sources = {source.key: source for source in inventory.sources}
    counts = {count.source_key: count for count in inventory.counts}
    observed = Counter(row.locator.source_key for row in inventory.rows)
    if (len(sources) != len(inventory.sources) or len(counts) != len(inventory.counts)
            or sources.keys() != counts.keys() or observed.keys() - sources.keys()):
        raise HistoryError("SOURCE_COUNT_MISMATCH")
    for key, source in sources.items():
        count, actual = counts[key], observed[key]
        if (any(value != actual for value in (count.raw_selected, count.enumerated, count.decoded))
                or count.raw_total < actual
                or (inventory.mode == "full" and count.raw_total != actual)
                or (not source.target_present and count.raw_total != 0)):
            raise HistoryError("SOURCE_COUNT_MISMATCH")
    for row in inventory.rows:
        if sources[row.locator.source_key].target_table != row.locator.table_name:
            raise HistoryError("SOURCE_IDENTITY_MISMATCH", fields=("table_name",))


def _validate_quick_archive(archived: tuple[ArchivedMessage, ...], inventory: Inventory) -> None:
    """Validate retained identities/aliases without claiming full binding evidence.

    T4 requires an actual full inventory. A partial inventory must not be passed
    to it under a relabeled mode, even for apparently unique promotion.
    """
    identities, owners = set(), {}
    source_positions = {(row.locator.create_time, row.locator.sort_seq) for row in inventory.rows}
    promotion_needed = False
    for index, record in enumerate(archived):
        if record.scope is not None and record.scope != inventory.scope:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        identity = record.canonical_identity
        if identity is None:
            raise HistoryError("FULL_INVENTORY_REQUIRED")
        if identity.startswith("server:"):
            if normalize_server_id(identity[7:], "text") != identity:
                raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
            if record.scope != inventory.scope or record.binding_status != "server_bound":
                raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        elif identity.startswith("archive:"):
            try:
                token = UUID(identity[8:])
            except (ValueError, AttributeError):
                raise HistoryError("LEGACY_ALIAS_CONFLICT") from None
            if (token.version != 4 or token.hex != identity[8:]
                    or record.scope != inventory.scope or record.binding_status != "archive_only"):
                raise HistoryError("LEGACY_ALIAS_CONFLICT")
        else:
            raise HistoryError("LEGACY_ALIAS_CONFLICT")
        if identity in identities:
            raise HistoryError("LEGACY_ALIAS_CONFLICT")
        identities.add(identity)
        aliases = record.legacy_aliases
        if record.binding_status == "archive_only":
            # T4 also considers the retained original position, even if not
            # explicitly duplicated in the alias tuple.
            message = record.message
            aliases = aliases + ({"scope": inventory.scope, "local_id": message.local_id,
                "create_time": message.create_time, "sort_seq": message.sort_seq,
                "identity": "local:" + message.local_id},)
        for alias in aliases:
            if (not isinstance(alias, dict)
                    or set(alias) != {"scope", "local_id", "create_time", "sort_seq", "identity"}
                    or alias["scope"] != inventory.scope
                    or not isinstance(alias["local_id"], str) or not alias["local_id"]
                    or type(alias["create_time"]) is not int or type(alias["sort_seq"]) is not int
                    or alias["identity"] != "local:" + alias["local_id"]):
                raise HistoryError("LEGACY_ALIAS_CONFLICT")
            triple = (alias["local_id"], alias["create_time"], alias["sort_seq"])
            if triple in owners and owners[triple] != index:
                raise HistoryError("LEGACY_ALIAS_CONFLICT")
            owners[triple] = index
            if record.binding_status == "archive_only" and triple[1:] in source_positions:
                promotion_needed = True
    if promotion_needed:
        raise HistoryError("FULL_INVENTORY_REQUIRED")


def collect_history(archived: tuple[ArchivedMessage, ...], session: InventorySession, *,
                    mode: str, authorization: ScopeAuthorization) -> HistoryCollection:
    """Borrow session, return a validated union; caller owns its eventual close."""
    inventory = session.inventory
    if mode not in ("full", "quick") or inventory.mode != mode:
        raise HistoryError("INVENTORY_MODE_MISMATCH")
    if authorization.scope != inventory.scope:
        raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
    if archived and not authorization.origin_confirmed:
        raise HistoryError("LEGACY_SCOPE_UNVERIFIED")
    _validate_counts(inventory)
    migration = None
    if mode == "full":
        migration = plan_identity_migration(archived, inventory, authorization)
        retained = migration.records
    else:
        _validate_quick_archive(archived, inventory)
        retained = archived

    # Migration may already have refreshed refs. The resolver still compares
    # each observed representation to the preserved message, so conflicts are
    # counted once here rather than lost or double-counted during mapping.
    resolution = resolve_canonical_rows(inventory, retained)
    union = {record.identity: record for record in retained}
    if len(union) != len(archived):
        raise HistoryError("LEGACY_ALIAS_CONFLICT")
    new_identities = frozenset(record.identity for record in resolution.records
                               if record.identity not in union)
    union.update((record.identity, record) for record in resolution.records)
    records = tuple(sorted(union.values(), key=canonical_sort_key))
    timestamps = [record.message.create_time for record in records]
    counts = CountReport(
        mode=mode, count_scope="full" if mode == "full" else "quick_window",
        raw_source_count=sum(count.raw_total for count in inventory.counts),
        raw_selected_count=sum(count.raw_selected for count in inventory.counts),
        decoded_source_count=sum(count.decoded for count in inventory.counts),
        canonical_duplicate_count=resolution.canonical_duplicate_count,
        legacy_matched_count=migration.legacy_matched_count if migration else 0,
        archive_only_count=migration.archive_only_count if migration else None,
        archive_before_count=len(archived), newly_discovered_count=len(new_identities),
        conflict_count=sum(conflict.count for conflict in resolution.conflicts),
        final_union_count=len(records), earliest_timestamp=min(timestamps, default=None),
        latest_timestamp=max(timestamps, default=None), media_recovered_count=0,
        media_missing_count=0, integrity_result="NOT_CHECKED",
    )
    session.assert_source_set_stable()
    return HistoryCollection(records, new_identities, inventory, migration, counts)


def collect_with_retry(open_session: Callable[[], InventorySession],
                       archived: tuple[ArchivedMessage, ...], *, mode: str,
                       authorization: ScopeAuthorization,
                       before_accept: Callable[[InventorySession, HistoryCollection], None] | None = None,
                       ) -> HistoryCollection:
    """Own at most two attempts; callback is read-only, never staging or commit.

    Returned inventory tokens describe expired observations, not a live lease.
    For caller-owned media staging use collect_history with an explicitly open
    session instead. Opening failures must clean resources within open_session.
    """
    for attempt in range(2):
        session = None
        try:
            session = open_session()
            collection = collect_history(archived, session, mode=mode, authorization=authorization)
            if before_accept is not None:
                before_accept(session, collection)
                session.assert_source_set_stable()
            return collection
        except SourceSetChanged:
            if attempt == 1:
                raise HistoryError("INCOMPLETE_SOURCE_SET", counts={"attempt_count": 2}) from None
        finally:
            if session is not None:
                session.close()
    raise AssertionError("UNREACHABLE_HISTORY_ATTEMPT")
