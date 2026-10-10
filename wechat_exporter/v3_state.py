"""Strict canonical persistence adapter; no migration or authority producer.

The complete wire is {schema, records}; disk splits the identical schema into
state/schema.json and ordered ArchivedMessage DTO lines into messages.jsonl.
Identity version 1 is the approved historical Core contract (2026-10-08 plan
section 2); Core currently exports no numeric identity-version constant.
"""
from dataclasses import fields, replace
import hashlib
import json
import math

from .archive import ArchivedMessage
from .history_contracts import (
    ArchiveScope, CountReport, HistoryError, Inventory, InventoryDigests,
    ScopeAuthorization, SourceRow,
)
from .identity_migration import plan_identity_migration
from .message_identity import canonical_sort_key, resolve_canonical_rows
from .track2_contracts import CONTRACT_VERSION, StateBundle, canonical_bytes, decode_dto

SCHEMA_VERSION = 3
IDENTITY_CONTRACT_VERSION = 1
SCHEMA_FIELDS = frozenset({"schema_version", "identity_contract_version", "archive_id", "scope",
                           "data_generation", "metadata", "message_count", "records_digest"})
METADATA_FIELDS = frozenset(item.name for item in fields(CountReport)) | {"message_count"}
_OPTIONAL_COUNTS = {"archive_only_count", "earliest_timestamp", "latest_timestamp"}
_TEXT_METADATA = {"mode", "count_scope", "integrity_result"}


def _require(condition):
    if not condition:
        raise ValueError("INVALID_V3_STATE")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    _require(math.isfinite(number))
    return number


def _reject_constant(value):
    raise ValueError("INVALID_V3_STATE")


def _load_json(payload):
    _require(type(payload) is bytes)
    try:
        return json.loads(payload.decode("utf8"), object_pairs_hook=_unique_pairs,
                          parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("INVALID_V3_STATE") from None


def _metadata(metadata, records):
    seen = set()
    for key, value in metadata:
        _require(key in METADATA_FIELDS and key not in seen)
        seen.add(key)
        if key in _TEXT_METADATA:
            _require(type(value) is str and bool(value))
        elif key in _OPTIONAL_COUNTS and value is None:
            continue
        elif key in ("earliest_timestamp", "latest_timestamp"):
            _require(type(value) is int)
        else:
            _require(type(value) is int and value >= 0)
        if key in ("message_count", "final_union_count"):
            _require(value == len(records))
        if key == "archive_only_count":
            _require(value == sum(record.binding_status == "archive_only" for record in records))
        if key == "mode":
            _require(value in ("full", "quick"))
        if key == "count_scope":
            _require(value in ("full", "quick_window"))
    values = dict(metadata)
    if "archive_only_count" in values and values["archive_only_count"] is None:
        _require(values.get("mode") == "quick")
    if "mode" in values and "count_scope" in values:
        _require(values["count_scope"] == ("full" if values["mode"] == "full" else "quick_window"))


def _validate(bundle):
    _require(type(bundle) is StateBundle)
    records = bundle.to_core_records()
    _metadata(bundle.metadata, records)
    _require(all(record.scope == bundle.scope and record.canonical_identity is not None for record in records))
    _require(tuple(sorted(records, key=canonical_sort_key)) == records)
    empty = Inventory(bundle.scope, "full", (), (), (), (), InventoryDigests("state", "state", "state"))
    # Core owns canonical token/UUID/status and alias ownership policy. The
    # validated result is discarded: this adapter never generates/promotes IDs.
    # The synthetic authorization argument is only a Core validator input for
    # already-migrated records; it attests no origin and confers no write right.
    checked = plan_identity_migration(records, empty, ScopeAuthorization(bundle.scope, True))
    _require(checked.records == records)
    observed = []
    for record in records:
        for observation in record.technical_observations:
            _require(set(observation) == {"raw_local_type", "storage_classes"})
            _require(type(observation["raw_local_type"]) is int)
            classes = observation["storage_classes"]
            _require(type(classes) is list and len(classes) == 5)
            _require(all(value in ("integer", "text", "real", "blob", "null") for value in classes))
        for locator in record.source_refs:
            _require(record.binding_status == "server_bound")
            observed.append(SourceRow(replace(record.message, local_id=locator.local_id),
                                      record.canonical_identity[7:], locator, "state-validation"))
    if observed:
        # The existing resolver checks scope, normalized server IDs, body time/
        # sequence, technical observations and same-server conflicts. It does
        # not replace/dedupe the persisted records: only validation is consumed.
        inventory = replace(empty, rows=tuple(observed))
        resolve_canonical_rows(inventory, records)
    return bundle


def _documents(bundle):
    _validate(bundle)
    records = [json.loads(canonical_bytes(record)) for record in bundle.records]
    schema = {
        "schema_version": SCHEMA_VERSION, "identity_contract_version": IDENTITY_CONTRACT_VERSION,
        "archive_id": bundle.archive_id, "scope": json.loads(canonical_bytes(bundle.scope)),
        "data_generation": bundle.data_generation, "metadata": [list(pair) for pair in bundle.metadata],
        "message_count": len(records), "records_digest": hashlib.sha256(canonical_bytes(tuple(records))).hexdigest(),
    }
    return schema, records


def encode_state(bundle: StateBundle) -> bytes:
    try:
        schema, records = _documents(bundle)
        return canonical_bytes({"schema": schema, "records": records})
    except (ValueError, TypeError, HistoryError, AttributeError, RecursionError):
        raise ValueError("INVALID_V3_STATE") from None


def encode_schema(bundle: StateBundle) -> bytes:
    """Task5's fixed state/schema.json leaf; no persistence side effect."""
    return canonical_bytes(_load_json(encode_state(bundle))["schema"])


def encode_messages(bundle: StateBundle) -> bytes:
    """Task5's fixed state/messages.jsonl leaf; trailing LF for each record."""
    return b"".join(canonical_bytes(record) + b"\n" for record in _load_json(encode_state(bundle))["records"])


def _decode_document(document, scope):
    _require(type(scope) is ArchiveScope)
    _require(type(document) is dict and set(document) == {"schema", "records"})
    schema, records = document["schema"], document["records"]
    _require(type(schema) is dict and set(schema) == SCHEMA_FIELDS)
    _require(type(schema["schema_version"]) is int and schema["schema_version"] == SCHEMA_VERSION)
    _require(type(schema["identity_contract_version"]) is int
             and schema["identity_contract_version"] == IDENTITY_CONTRACT_VERSION)
    _require(type(records) is list and type(schema["message_count"]) is int
             and schema["message_count"] == len(records))
    _require(type(schema["records_digest"]) is str
             and schema["records_digest"] == hashlib.sha256(canonical_bytes(tuple(records))).hexdigest())
    wire = {"contract_version": CONTRACT_VERSION, "dto_type": "StateBundle", "fields": {
        key: schema[key] for key in ("scope", "archive_id", "data_generation", "metadata")}}
    wire["fields"]["records"] = records
    bundle = decode_dto(StateBundle, wire)
    _require(bundle.scope == scope)
    return _validate(bundle)


def decode_state(payload: bytes, scope: ArchiveScope) -> StateBundle:
    try:
        return _decode_document(_load_json(payload), scope)
    except (ValueError, TypeError, HistoryError, KeyError, AttributeError, RecursionError):
        raise ValueError("INVALID_V3_STATE") from None
