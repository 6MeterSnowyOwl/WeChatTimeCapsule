"""Pure-memory I10-I15 legacy mapping; persistence and union belong elsewhere.

Candidate cardinality counts source observations, including duplicated servers.
No message/media is replaced, no input metadata is mutated, and no raw-row token
is carried into mapped records. UUID stability applies to returned memory only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from uuid import UUID, uuid4

from .archive import ArchivedMessage
from .history_contracts import HistoryError, Inventory, MigrationPlan, ScopeAuthorization
from .message_identity import normalize_server_id, resolve_canonical_rows


def _check_complete_inventory(inventory: Inventory) -> None:
    if inventory.mode != "full":
        raise HistoryError("FULL_INVENTORY_REQUIRED")
    sources = {source.key: source for source in inventory.sources}
    counts = {count.source_key: count for count in inventory.counts}
    if (len(sources) != len(inventory.sources) or len(counts) != len(inventory.counts)
            or sources.keys() != counts.keys()):
        raise HistoryError("SOURCE_COUNT_MISMATCH")
    observed = Counter(row.locator.source_key for row in inventory.rows)
    if observed.keys() - sources.keys():
        raise HistoryError("SOURCE_COUNT_MISMATCH")
    for key, source in sources.items():
        count = counts[key]
        actual = observed[key]
        if (any(value != actual for value in (count.raw_total, count.raw_selected,
                                             count.enumerated, count.decoded))
                or (not source.target_present and actual)):
            raise HistoryError("SOURCE_COUNT_MISMATCH")
    for row in inventory.rows:
        if sources[row.locator.source_key].target_table != row.locator.table_name:
            raise HistoryError("SOURCE_IDENTITY_MISMATCH", fields=("table_name",))


def _triple(value) -> tuple[str, int, int]:
    return value.local_id, value.create_time, value.sort_seq


def _aliases(record: ArchivedMessage, scope, *, unresolved: bool) -> tuple[dict, ...]:
    aliases = list(record.legacy_aliases)
    if unresolved:
        message = record.message
        aliases.append({"scope": scope, "local_id": message.local_id,
                        "create_time": message.create_time, "sort_seq": message.sort_seq,
                        "identity": "local:" + message.local_id})
    validated, seen = [], set()
    for alias in aliases:
        if (not isinstance(alias, dict)
                or set(alias) != {"scope", "local_id", "create_time", "sort_seq", "identity"}
                or alias["scope"] != scope
                or not isinstance(alias["local_id"], str) or not alias["local_id"]
                or type(alias["create_time"]) is not int or type(alias["sort_seq"]) is not int
                or alias["identity"] != "local:" + alias["local_id"]):
            raise HistoryError("LEGACY_ALIAS_CONFLICT")
        triple = (alias["local_id"], alias["create_time"], alias["sort_seq"])
        if triple not in seen:
            seen.add(triple)
            validated.append(dict(alias))
    return tuple(validated)


def plan_identity_migration(legacy: tuple[ArchivedMessage, ...], inventory: Inventory,
                            authorization: ScopeAuthorization) -> MigrationPlan:
    """Validate a full inventory and every old alias before returning any mapping."""
    if authorization.scope != inventory.scope:
        raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
    if legacy and not authorization.origin_confirmed:
        raise HistoryError("LEGACY_SCOPE_UNVERIFIED")
    _check_complete_inventory(inventory)

    # These are the raw/decoded comparisons used by T3, before triple selection.
    # Group validation is deferred until mapped records can supply authority.
    rows_by_triple = defaultdict(list)
    source_positions = set()
    for row in inventory.rows:
        locator = row.locator
        if locator.scope != inventory.scope:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        identity = normalize_server_id(row.server_id, locator.storage_classes[0])
        expected = normalize_server_id(locator.expected_server_id, locator.storage_classes[0])
        if identity != expected or _triple(row.message) != _triple(locator):
            raise HistoryError("SOURCE_IDENTITY_MISMATCH")
        rows_by_triple[_triple(locator)].append((identity, row))
        source_positions.add((locator.create_time, locator.sort_seq))

    aliases_by_record, unresolved_flags = [], []
    alias_owners, canonical_owners = {}, {}
    position_owners = defaultdict(set)
    for index, record in enumerate(legacy):
        if record.scope is not None and record.scope != inventory.scope:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        identity = record.canonical_identity
        unresolved = identity is None or identity.startswith("archive:")
        if identity is not None:
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
            if identity in canonical_owners:
                raise HistoryError("LEGACY_ALIAS_CONFLICT")
            canonical_owners[identity] = index
        aliases = _aliases(record, inventory.scope, unresolved=unresolved)
        aliases_by_record.append(aliases)
        unresolved_flags.append(unresolved)
        for alias in aliases:
            triple = (alias["local_id"], alias["create_time"], alias["sort_seq"])
            if triple in alias_owners and alias_owners[triple] != index:
                raise HistoryError("LEGACY_ALIAS_CONFLICT")
            alias_owners[triple] = index
            if unresolved:
                position_owners[triple[1:]].add(index)
    if any(len(owners) > 1 and position in source_positions
           for position, owners in position_owners.items()):
        raise HistoryError("POSSIBLE_LEGACY_COUNTERPART")

    choices = []
    for index, (record, aliases, unresolved) in enumerate(
            zip(legacy, aliases_by_record, unresolved_flags)):
        if not unresolved:
            choices.append(None)
            continue
        candidates = [candidate for alias in aliases
                      for candidate in rows_by_triple[(alias["local_id"], alias["create_time"],
                                                       alias["sort_seq"])]]
        if len(candidates) > 1:
            raise HistoryError("AMBIGUOUS_LEGACY_BINDING", counts={"count": len(candidates)})
        if not candidates:
            if any((alias["create_time"], alias["sort_seq"]) in source_positions for alias in aliases):
                raise HistoryError("POSSIBLE_LEGACY_COUNTERPART")
            choices.append(None)
            continue
        identity, row = candidates[0]
        if identity in canonical_owners and canonical_owners[identity] != index:
            raise HistoryError("LEGACY_ALIAS_CONFLICT")
        canonical_owners[identity] = index
        choices.append((identity, row))

    mapped, matched = [], 0
    for record, aliases, choice in zip(legacy, aliases_by_record, choices):
        if choice is None:
            mapped.append(replace(record, legacy_aliases=aliases))
            continue
        identity, row = choice
        # Retain any historical evidence so promotion cannot erase the I09 gate.
        observations = record.technical_observations + ({
            "raw_local_type": row.locator.raw_local_type,
            "storage_classes": row.locator.storage_classes,
        },)
        mapped.append(replace(record, canonical_identity=identity, scope=inventory.scope,
                              binding_status="server_bound", legacy_aliases=aliases,
                              source_refs=record.source_refs + (row.locator,),
                              technical_observations=observations))
        matched += 1

    # Reuse the final T3 policy for same-server invariant/observation conflicts,
    # archived representation authority and refreshed refs. It returns observed
    # rows only; this planner deliberately retains all old records separately.
    resolved = resolve_canonical_rows(inventory, tuple(mapped))
    observed = {record.canonical_identity: record for record in resolved.records}
    result, archive_only = [], 0
    for record in mapped:
        identity = record.canonical_identity
        if identity is None or identity.startswith("archive:"):
            record = replace(record, canonical_identity=identity or "archive:" + uuid4().hex,
                             scope=inventory.scope, binding_status="archive_only")
            archive_only += 1
        elif identity in observed:
            record = observed[identity]
        result.append(record)
    return MigrationPlan(tuple(result), matched, archive_only, {}, newly_discovered_count=0)
