"""Pure-memory canonical identity policy (I01-I09); no archive I/O.

Only observed rows are returned. Migration owns legacy binding; collection owns
retention of absent records. Conflict DTOs produced here use fixed names only;
arbitrary caller-created IdentityConflict objects are not trusted output.
"""
from __future__ import annotations

from dataclasses import replace
import re

from .archive import ArchivedMessage
from .history_contracts import HistoryError, IdentityConflict, IdentityResolution, Inventory


_REPRESENTATION_FIELDS = (
    "message_type", "sender_id", "sender_username", "content", "subtype", "payload",
)


def normalize_server_id(value: int | str, sqlite_storage_class: str) -> str:
    """Accept exact nonzero decimal values, including reader's integer strings."""
    if sqlite_storage_class not in ("integer", "text"):
        raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
    if type(value) is int:
        valid = sqlite_storage_class == "integer"
    else:
        valid = isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is not None
    if not valid:
        raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
    try:
        number = int(value)
    except ValueError:
        raise HistoryError("INVALID_SERVER_ID", fields=("server_id",)) from None
    if number == 0:
        raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
    return "server:" + str(number)


def canonical_sort_key(record: ArchivedMessage) -> tuple[int, int, str]:
    if record.canonical_identity is None:
        raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
    return record.message.create_time, record.message.sort_seq, record.canonical_identity


def _check_invariants(left, right) -> None:
    fields = tuple(name for name in ("create_time", "sort_seq")
                   if getattr(left, name) != getattr(right, name))
    if fields:
        raise HistoryError("IDENTITY_INVARIANT_CONFLICT", fields=fields)


def _check_observations(locator, raw_type, classes) -> None:
    fields = tuple(name for name, different in (
        ("raw_local_type", locator.raw_local_type != raw_type),
        ("storage_classes", locator.storage_classes != tuple(classes)),
    ) if different)
    if fields:
        raise HistoryError("UNVERIFIED_IDENTITY_INVARIANT", fields=fields)


def resolve_canonical_rows(inventory: Inventory,
                           archived: tuple[ArchivedMessage, ...]) -> IdentityResolution:
    # Validate every row before canonical grouping or consulting authority.
    validated = []
    for row in inventory.rows:
        locator = row.locator
        if locator.scope != inventory.scope:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        identity = normalize_server_id(row.server_id, locator.storage_classes[0])
        expected = normalize_server_id(locator.expected_server_id, locator.storage_classes[0])
        if identity != expected or any(getattr(row.message, field) != getattr(locator, field)
                                       for field in ("local_id", "create_time", "sort_seq")):
            raise HistoryError("SOURCE_IDENTITY_MISMATCH")
        validated.append((identity, row))

    authorities = {}
    for record in archived:
        identity = record.canonical_identity
        # Unmapped v2/archive-only records cannot supply server authority.
        if identity is None or not identity.startswith("server:"):
            continue
        if record.scope != inventory.scope:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
        if normalize_server_id(identity[7:], "text") != identity:
            raise HistoryError("INVALID_SERVER_ID", fields=("server_id",))
        if identity in authorities:
            raise HistoryError("LEGACY_ALIAS_CONFLICT")
        authorities[identity] = record

    groups = {}
    for identity, row in validated:
        groups.setdefault(identity, []).append(row)

    records, conflicts = [], []
    for identity, rows in groups.items():
        first = rows[0]
        authority = authorities.get(identity)
        for row in rows:
            _check_invariants(first.locator, row.locator)
            _check_observations(row.locator, first.locator.raw_local_type,
                                first.locator.storage_classes)
        if authority is not None:
            _check_invariants(first.locator, authority.message)
            if not authority.source_refs and not authority.technical_observations:
                raise HistoryError("UNVERIFIED_IDENTITY_INVARIANT",
                                   fields=("raw_local_type", "storage_classes"))
            for ref in authority.source_refs:
                if ref.scope != inventory.scope:
                    raise HistoryError("IDENTITY_SCOPE_MISMATCH", fields=("scope",))
                if normalize_server_id(ref.expected_server_id, ref.storage_classes[0]) != identity:
                    raise HistoryError("SOURCE_IDENTITY_MISMATCH")
                _check_invariants(first.locator, ref)
                _check_observations(first.locator, ref.raw_local_type, ref.storage_classes)
            for observation in authority.technical_observations:
                _check_observations(first.locator, observation.get("raw_local_type"),
                                    observation.get("storage_classes", ()))

        reference = authority.message if authority is not None else first.message
        for row in rows:
            fields = tuple(field for field in _REPRESENTATION_FIELDS
                           if getattr(reference, field) != getattr(row.message, field))
            if not fields:
                continue
            if authority is None:
                raise HistoryError("REPRESENTATION_UNRESOLVED", fields=fields)
            conflicts.append(IdentityConflict("REPRESENTATION_CONFLICT", fields, False, 1))

        record = authority if authority is not None else ArchivedMessage(first.message)
        records.append(replace(
            record, canonical_identity=identity, scope=inventory.scope,
            binding_status="server_bound",
            source_refs=tuple(dict.fromkeys(row.locator for row in rows)),
            technical_observations=({"raw_local_type": first.locator.raw_local_type,
                                     "storage_classes": first.locator.storage_classes},),
        ))
    return IdentityResolution(tuple(sorted(records, key=canonical_sort_key)),
                              tuple(conflicts), len(validated) - len(groups))
