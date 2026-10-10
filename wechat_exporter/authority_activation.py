"""Pure NONAUTHORITATIVE activation comparisons and recovery classification.

No activate/recover_activation, filesystem I/O, durable intent, CAS writer,
READY issuer or WritePermit exists here. Independently supplied current facts
must come from reviewed producers under the real namespace lease. Successful
comparison is not human approval authentication, current re-verification,
atomicity, recovery execution or authority to write.
"""
from .candidate_lifecycle import (_counts_valid, _digest, compare_candidate_evidence)
from .fork_authorization import _typed, validate_formal_activation
from .history_contracts import ArchiveScope
from .track2_contracts import (ActivationApproval, AuthorityBinding, BuildEvidence,
    CandidateRecord, FileIdentity, IntegrityFacts, RecoveryOutcome, Track2Error,
    canonical_bytes, contract_digest)


def _require(condition, code="CANDIDATE_NOT_READY"):
    if not condition:
        raise Track2Error(code)


def _scope_valid(scope):
    try:
        if type(scope) is not ArchiveScope:
            return False
        scope.__post_init__()
        canonical_bytes(scope)
        return True
    except (ValueError, TypeError, AttributeError, RecursionError):
        return False


def _binding_valid(binding):
    if not _typed(binding, AuthorityBinding):
        return False
    anchors = (binding.activation_approval_digest, binding.activation_manifest_digest,
               binding.activation_data_generation)
    if binding.kind == "PRESERVED_V2_READONLY":
        return binding.generation == 0 and anchors == (None, None, None)
    return (binding.generation > 0 and all(value is not None for value in anchors))


def _candidate_chain(candidate, receipt, fork, approval):
    _require(_typed(candidate, CandidateRecord))
    validate_formal_activation(approval, receipt, fork)
    _require(candidate.purpose == fork.purpose == approval.purpose,
             "ACTIVATION_APPROVAL_REQUIRED")
    _require(candidate.archive_id == fork.archive_id == receipt.candidate_id
        and candidate.object_id == fork.object_id == approval.object_id
        and candidate.scope == fork.scope
        and candidate.root_id == receipt.root_id == approval.root_id
        and candidate.operation_id == fork.operation_id, "BINDING_CONFLICT")


def _historical_evidence(candidate, evidence, receipt, fork, approval):
    _candidate_chain(candidate, receipt, fork, approval)
    _require(_typed(evidence, BuildEvidence) and evidence.integrity_verified is True
        and _counts_valid(evidence.counts) and candidate.last_error is None)
    _require(candidate.evidence_digest == receipt.evidence_digest == contract_digest(evidence)
        and evidence.fork_receipt_digest == contract_digest(fork)
        and evidence.baseline_digest == receipt.baseline_digest
        and evidence.source_digest == receipt.source_digest
        and evidence.code_digest == receipt.code_digest == approval.code_digest
        and evidence.contract_version == receipt.contract_version == approval.contract_version
        and evidence.configuration_digest == receipt.configuration_digest
            == approval.configuration_digest)


def _active_anchor(candidate, evidence, approval, binding):
    _require(_binding_valid(binding) and binding.kind == "ACTIVE_V3"
        and binding.object_id == candidate.object_id and binding.scope == candidate.scope
        and binding.archive_id == candidate.archive_id and binding.root_id == candidate.root_id
        and binding.generation == approval.expected_binding_generation + 1
        and binding.activation_approval_digest == contract_digest(approval)
        and binding.activation_manifest_digest == evidence.current_manifest_digest
        and binding.activation_data_generation == evidence.data_generation, "BINDING_CONFLICT")


def compare_activation_request(candidate, evidence, receipt, fork, approval, *,
        requested_candidate_id, requested_object_id, requested_scope,
        namespace_purpose, **current_facts):
    """Compare READY chain and independent current facts; issue no receipt.

current_facts are exactly compare_candidate_evidence's named current inputs.
They must be freshly obtained externally, never copied from this saved evidence.
"""
    _require(_typed(candidate, CandidateRecord))
    code = ("TRACK2_RECOVERY_REQUIRED" if candidate.state in
            ("ACTIVATION_PENDING", "RECOVERY_REQUIRED") else "CANDIDATE_NOT_READY")
    _require(candidate.state == "READY", code)
    _candidate_chain(candidate, receipt, fork, approval)
    _require(type(namespace_purpose) is str
        and namespace_purpose == candidate.purpose == approval.purpose,
        "ACTIVATION_APPROVAL_REQUIRED")
    _require(type(requested_candidate_id) is str and type(requested_object_id) is str
        and _scope_valid(requested_scope)
        and requested_candidate_id == candidate.archive_id
        and requested_object_id == candidate.object_id
        and requested_scope == candidate.scope, "BINDING_CONFLICT")
    _historical_evidence(candidate, evidence, receipt, fork, approval)
    try:
        compare_candidate_evidence(candidate, evidence, **current_facts)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise Track2Error("CANDIDATE_NOT_READY") from None


def compare_expected_binding(current_binding, approval, *, object_id, scope):
    """Compare current binding digest/generation, never the candidate index.

None is the reviewed T2 convention contract_digest(None), generation zero.
No binding is read, replaced, reserved or returned by this pure CAS admission.
"""
    _require(_typed(approval, ActivationApproval) and type(object_id) is str
        and approval.object_id == object_id and _scope_valid(scope), "BINDING_CONFLICT")
    _require(current_binding is None or (_binding_valid(current_binding)
        and current_binding.object_id == object_id and current_binding.scope == scope),
        "BINDING_CONFLICT")
    generation = 0 if current_binding is None else current_binding.generation
    _require(approval.expected_binding_generation == generation
        and approval.expected_binding_digest == contract_digest(current_binding), "BINDING_CONFLICT")


def compare_idempotent_activation(candidate, evidence, receipt, fork, approval,
        binding, *, current_root_id, current_integrity, current_manifest_digest,
        current_data_generation, current_union_digest):
    """Return True for exact approved active anchor plus healthy fresh facts.

Historical evidence/receipt authenticate no current producer. Later saves may
advance data_generation/change manifest/union; their separate current facts
must agree with current_integrity while frozen activation anchors stay exact.
This no-op decision never provides normal-save permission.
"""
    _require(_typed(candidate, CandidateRecord) and candidate.state == "ACTIVE")
    _historical_evidence(candidate, evidence, receipt, fork, approval)
    _active_anchor(candidate, evidence, approval, binding)
    _require(_typed(current_root_id, FileIdentity) and current_root_id == binding.root_id,
             "BINDING_CONFLICT")
    _require(_typed(current_integrity, IntegrityFacts)
        and current_integrity.scope == binding.scope
        and current_integrity.archive_id == binding.archive_id
        and type(current_data_generation) is int
        and current_data_generation >= evidence.data_generation
        and current_integrity.data_generation == current_data_generation
        and _digest(current_manifest_digest)
        and current_integrity.manifest_digest == current_manifest_digest
        and _digest(current_union_digest) and current_integrity.union_digest == current_union_digest
        and current_integrity.current_ok is True and current_integrity.history_ok is True
        and current_integrity.asset_closure_ok is True
        and current_integrity.transaction_clean is True)
    return True


def classify_activation_recovery(current_binding, old_binding, new_binding, *,
        candidate, evidence, receipt, fork, approval, operation_id,
        intent_present, intent_digest, expected_intent_digest):
    """Classify supplied old/new/unknown binding against the exact intent chain.

All valid pending cases remain BLOCKED until actual re-verification/cancellation
or postcommit read-back/repair. No RECOVERED/CLEAN is emitted: this signature
contains no independent completed-recovery proof. FAILED is corrupt supplied
evidence, not permission to delete it. Missing binding remains unknown even if
old_binding was absent. Digests compare supplied intent bytes only; they do not
prove durable intent origin, completeness or actual binding-file atomicity.
"""
    inspect = "INSPECT_EVIDENCE_REQUIRED"
    try:
        _require(type(intent_present) is bool, "TRACK2_RECOVERY_REQUIRED")
        _require(current_binding is None or _binding_valid(current_binding), "BINDING_CONFLICT")
        _historical_evidence(candidate, evidence, receipt, fork, approval)
        _require(candidate.state in ("ACTIVATION_PENDING", "RECOVERY_REQUIRED")
            and type(operation_id) is str and operation_id == candidate.operation_id,
            "TRACK2_RECOVERY_REQUIRED")
        compare_expected_binding(old_binding, approval, object_id=candidate.object_id,
                                 scope=candidate.scope)
        _active_anchor(candidate, evidence, approval, new_binding)
        _require(_digest(intent_digest) and _digest(expected_intent_digest)
            and intent_digest == expected_intent_digest, "TRACK2_RECOVERY_REQUIRED")
        if not intent_present or current_binding is None:
            return RecoveryOutcome("BLOCKED", inspect, None, "TRACK2_RECOVERY_REQUIRED")
        if current_binding == old_binding:
            action = "OLD_BINDING_REVERIFY_CANCEL_REQUIRED"
        elif current_binding == new_binding:
            action = "NEW_BINDING_READBACK_REPAIR_REQUIRED"
        else:
            action = inspect
        return RecoveryOutcome("BLOCKED", action, intent_digest, "TRACK2_RECOVERY_REQUIRED")
    except (Track2Error, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return RecoveryOutcome("FAILED", inspect, None, "TRACK2_RECOVERY_REQUIRED")
