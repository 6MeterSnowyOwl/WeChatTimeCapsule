"""Pure candidate request/refusal and independently supplied evidence comparison.

build_candidate, verify_candidate and recover_candidate runners are NOT
IMPLEMENTED. Helpers perform no I/O, persistence, activation or state mutation;
None means a metadata check passed, never READY issuance or a write capability.
Current inputs must come from independent reviewed producers. Comparing supplied
metadata neither refreshes that metadata nor authenticates its producer.
"""
import json
import re

from .history_collection import _validate_counts
from .history_contracts import HistoryError, Inventory
from .track2_contracts import (BuildEvidence, CandidateRecord, FileIdentity,
    IntegrityFacts, Track2Error, canonical_bytes, contract_digest, decode_dto,
    dto_document)

_ACTIONS = {
    "BUILDING": frozenset({"build"}),
    "READY": frozenset({"verify", "request_activation"}),
    "ACTIVATION_PENDING": frozenset({"recover", "complete_activation"}),
    "ACTIVE": frozenset({"verify", "inspect_evidence"}),
    "FAILED": frozenset({"inspect_evidence", "request_fresh_candidate"}),
    "RECOVERY_REQUIRED": frozenset({"recover"}),
}


def _require(condition, code="CANDIDATE_NOT_READY"):
    if not condition:
        raise Track2Error(code)


def _typed(value, cls):
    # Strict existing DTO parser; no new DTO architecture or capability loader.
    try:
        return type(value) is cls and decode_dto(cls, dto_document(value)) == value
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return False


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _counts_valid(counts):
    return (type(counts) is tuple and bool(counts)
        and all(type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str
            and bool(pair[0].strip()) and type(pair[1]) is int and pair[1] >= 0
            for pair in counts)
        and len({pair[0] for pair in counts}) == len(counts))


def _evidence_valid(evidence):
    return (_typed(evidence, BuildEvidence) and evidence.integrity_verified is True
        and _counts_valid(evidence.counts))


def _evidence_digest(evidence):
    """Normalize canonical UTF-8 failure at this existing policy boundary."""
    try:
        return contract_digest(evidence)
    except UnicodeError:
        raise Track2Error("CANDIDATE_NOT_READY") from None


def admit_candidate_action(candidate: CandidateRecord, action: str, *, evidence=None,
                           intent_operation_id=None, intent_digest=None,
                           expected_intent_digest=None) -> None:
    """Admit only a named request against status metadata; issue no authority.

Pending-intent comparison checks only externally supplied operation ID/digests.
The T7 owner must independently load/validate the full exact approved durable
intent and binding before executing recovery. Cached ACTIVE cannot authorize
normal save: this slice has no live binding/lease/current-integrity issuer.
"""
    _require(_typed(candidate, CandidateRecord) and type(action) is str)
    state = candidate.state
    if state == "ACTIVE" and action == "normal_save":
        raise Track2Error("RUNTIME_BINDING_UNVERIFIED")
    code = ("TRACK2_RECOVERY_REQUIRED" if state in
            ("ACTIVATION_PENDING", "RECOVERY_REQUIRED") else "CANDIDATE_NOT_READY")
    _require(action in _ACTIONS[state], code)
    if state == "ACTIVATION_PENDING":
        _require(type(intent_operation_id) is str
            and intent_operation_id == candidate.operation_id
            and _digest(intent_digest) and _digest(expected_intent_digest)
            and intent_digest == expected_intent_digest, code)
    if state == "READY":
        _require(_digest(candidate.evidence_digest) and candidate.last_error is None)
        if action == "request_activation":
            _require(_evidence_valid(evidence)
                     and candidate.evidence_digest == _evidence_digest(evidence))


def source_inventory_digest(inventory: Inventory) -> str:
    """Hash complete normalized full inventory, including descriptors/counts.

Cached InventoryDigests alone cannot establish source stability. Reuse Core's
in-memory count consistency check without discovery, reading or a new resolver.
    The reader's random raw_row_token is a session lookup handle, excluded from
    cross-session comparison only. Full decoded messages/server IDs/locators and
    lossless raw-row InventoryDigests remain bound. Core tokens stay unchanged.
    This is a comparison fingerprint, never a live snapshot or source capability.
"""
    try:
        valid = (type(inventory) is Inventory and inventory.mode == "full"
            and decode_dto(Inventory, json.loads(canonical_bytes(inventory))) == inventory
            and all(_digest(d) for d in (inventory.digests.source_set_digest,
                inventory.digests.source_rows_digest, inventory.digests.scope_digest))
            and all(row.locator.scope == inventory.scope for row in inventory.rows))
        _require(valid, "SOURCE_CHANGED")
        _validate_counts(inventory)
        rows = tuple((row.message, row.server_id, row.locator) for row in inventory.rows)
        return contract_digest(("track2-v1", "candidate-source-inventory", inventory.scope,
            inventory.mode, rows, inventory.sources, inventory.counts,
            inventory.next_cursors, inventory.digests))
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, HistoryError):
        raise Track2Error("SOURCE_CHANGED") from None


def compare_candidate_evidence(candidate: CandidateRecord, evidence: BuildEvidence, *,
        current_root_id, current_fork_receipt_digest, current_baseline_digest,
        recorded_inventory, current_inventory, current_union_digest,
        current_manifest_digest, current_data_generation, current_counts,
        current_integrity, current_code_digest, current_contract_version,
        current_configuration_digest) -> None:
    """Check supplied READY evidence against independently obtained current facts.

No receipt/READY/capability is returned. Real fork/baseline/code/config/union,
root proofs, integrity and fresh source reads remain external integration gates;
this helper cannot authenticate booleans, refresh snapshots or hold an OS lease.
"""
    _require(_typed(candidate, CandidateRecord) and candidate.state == "READY"
        and candidate.last_error is None and _evidence_valid(evidence))
    _require(candidate.evidence_digest == _evidence_digest(evidence))
    _require(_typed(current_root_id, FileIdentity) and candidate.root_id == current_root_id,
             "BINDING_CONFLICT")
    _require(_digest(current_fork_receipt_digest)
        and evidence.fork_receipt_digest == current_fork_receipt_digest, "FORK_INCOMPLETE")
    _require(_digest(current_baseline_digest)
        and evidence.baseline_digest == current_baseline_digest, "BASELINE_CHANGED")
    recorded_digest = source_inventory_digest(recorded_inventory)
    current_digest = source_inventory_digest(current_inventory)
    _require(recorded_inventory.scope == current_inventory.scope == candidate.scope
        and evidence.source_digest == recorded_digest == current_digest, "SOURCE_CHANGED")
    _require(_digest(current_code_digest) and evidence.code_digest == current_code_digest
        and type(current_contract_version) is str
        and evidence.contract_version == current_contract_version
        and _digest(current_configuration_digest)
        and evidence.configuration_digest == current_configuration_digest)
    _require(_digest(current_union_digest) and evidence.union_digest == current_union_digest
        and _digest(current_manifest_digest)
        and evidence.current_manifest_digest == current_manifest_digest
        and type(current_data_generation) is int and current_data_generation >= 0
        and evidence.data_generation == current_data_generation
        and _counts_valid(current_counts) and evidence.counts == current_counts)
    _require(_typed(current_integrity, IntegrityFacts)
        and current_integrity.scope == candidate.scope
        and current_integrity.archive_id == candidate.archive_id
        and current_integrity.data_generation == current_data_generation
        and current_integrity.union_digest == current_union_digest
        and current_integrity.manifest_digest == current_manifest_digest
        and current_integrity.current_ok is True and current_integrity.history_ok is True
        and current_integrity.asset_closure_ok is True and current_integrity.transaction_clean is True)
