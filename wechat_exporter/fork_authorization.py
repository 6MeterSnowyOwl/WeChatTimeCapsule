"""Pure checks on supplied NONAUTHORITATIVE approval metadata.

These checks neither authenticate human approval nor produce a filesystem
capability. Current source/config/code freshness, handle revalidation, leases,
Stage B acceptance and explicit UI confirmation remain integration gates.
No filesystem/source reads, writes, activation, release or upload route exists.
"""
from dataclasses import fields
import ntpath

from .history_contracts import ArchiveScope, HistoryError, ScopeAuthorization
from .physical_paths import _overlaps
from .track2_contracts import (
    CONTRACT_VERSION, ActivationApproval, ArchiveOriginConfirmation, ForkApproval,
    ForkReceipt, OperationRootContext, RootSelection, Track2Error, TreeSnapshot, VerificationReceipt,
    canonical_bytes, contract_digest, decode_dto, dto_document,
)


def _require(condition, code="ROOT_PROOF_FAILED"):
    if not condition:
        raise Track2Error(code)


def _typed(value, cls):
    """Revalidate nested immutable DTO shape; no trust is inferred from type."""
    try:
        if type(value) is not cls or decode_dto(cls, dto_document(value)) != value:
            return False
        # DTO string shape alone does not guarantee canonical UTF8 encoding.
        # UnicodeEncodeError is a ValueError handled by this bounded boundary.
        canonical_bytes(value)
        return True
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return False


def _path(path):
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.normcase(ntpath.normpath(text))


def _overlap(first, second):
    a, b = _path(first), _path(second)
    return a == b or a in tuple(_path(p) for p in second.parents) or b in tuple(_path(p) for p in first.parents)


def _identity(identity):
    return identity.volume_serial, identity.file_id_128


def _snapshot_valid(snapshot):
    if not _typed(snapshot, TreeSnapshot):
        return False
    entries = snapshot.entries
    if entries[0].kind != "directory" or entries[0].identity != snapshot.root.identity:
        return False
    if len({_identity(e.identity) for e in entries}) != len(entries):
        return False
    directories = {e.relative_path for e in entries if e.kind == "directory"}
    for entry in entries[1:]:
        parent = entry.relative_path.rpartition("/")[0] or "."
        if parent not in directories:
            return False
    content = tuple((e.relative_path, e.kind, e.size, e.sha256) for e in entries)
    return (snapshot.content_digest == contract_digest((CONTRACT_VERSION, "content-tree", content))
        and snapshot.physical_digest == contract_digest((CONTRACT_VERSION, "physical-tree", snapshot.root, entries)))


def _selection_valid(roots):
    paths = tuple(getattr(roots, item.name) for item in fields(roots) if getattr(roots, item.name) is not None)
    return not any(_overlap(path, other) for index, path in enumerate(paths) for other in paths[index + 1:])


def authorize_fork(approval: ForkApproval, current_baseline: TreeSnapshot,
                   roots: RootSelection, *, context: OperationRootContext) -> ForkApproval:
    """Validate exact supplied fork metadata and return the original record.

Parent receipts cover candidate, selected safety backup and any other explicitly
approved new output roles. Existing auxiliary roots are allowed, but their live
existence/physical isolation cannot be established by this metadata-only API.
"""
    _require(_typed(approval, ForkApproval) and _typed(current_baseline, TreeSnapshot)
             and _typed(roots, RootSelection))
    _require(approval.input_baseline == current_baseline, "BASELINE_CHANGED")
    _require(_snapshot_valid(current_baseline) and _selection_valid(roots))
    _require(current_baseline.root.path == roots.input_v2)
    _require(not approval.safety_backup_required or roots.safety_backup is not None)
    _require(approval.purpose != "FORMAL" or (approval.b_receipt_digest is not None
             and approval.safety_backup_required and roots.safety_backup is not None))
    outputs = {getattr(roots, f.name) for f in fields(roots)
               if f.name != "input_v2" and getattr(roots, f.name) is not None}
    proofs = approval.destination_parent_proofs
    destinations = tuple(p.destination for p in proofs)
    _require(len({_path(p) for p in destinations}) == len(destinations))
    _require(set(destinations) <= outputs and roots.candidate_v3 in destinations)
    _require(roots.safety_backup is None or roots.safety_backup in destinations)
    for proof in proofs:
        _require(proof.destination_nonexistent is True
                 and proof.parent.path == proof.existing_parent
                 and proof.existing_parent.joinpath(*proof.requested_leaf_names) == proof.destination)
        # An enclosing parent can contain the input and sibling output. That
        # shared parent is legal; equality or a parent inside input is not.
        _require(current_baseline.root.final_path != proof.parent.final_path
                 and _identity(current_baseline.root.identity) != _identity(proof.parent.identity)
                 and _identity(current_baseline.root.identity) not in
                 {_identity(identity) for identity in proof.parent.ancestor_ids})
    _require(_typed(context, OperationRootContext), "OPERATION_CONTEXT_MISMATCH")
    _require(context.selection == roots and context.scope == approval.scope
        and context.object_id == approval.object_id and context.purpose == approval.purpose
        and context.legacy_namespace_digest == approval.legacy_namespace_digest
        and context.destination_parent_proofs == tuple(sorted(approval.destination_parent_proofs,
            key=lambda p: str(p.destination).casefold()))
        and dict(context.existing_role_proofs).get("input_v2") == current_baseline.root
        and context.freshness.code_digest == approval.code_digest
        and contract_digest(context) == approval.root_context_digest, "OPERATION_CONTEXT_MISMATCH")
    return approval


def _fork_consistent(fork):
    proof = fork.root_proof
    roots = proof.selection
    if not _selection_valid(roots) or not proof.source_roots:
        return False
    roles = tuple((f.name, getattr(roots, f.name)) for f in fields(roots) if getattr(roots, f.name) is not None)
    roles += tuple(("source:" + str(index), path) for index, path in enumerate(proof.source_roots))
    names = {role for role, _ in roles}
    maps = (proof.root_ids, proof.ancestor_ids, proof.root_evidence, proof.snapshots)
    if any(len(pairs) != len(names) or {role for role, _ in pairs} != names for pairs in maps):
        return False
    ids, ancestors, evidence, snapshots = (dict(pairs) for pairs in maps)
    handles = tuple(evidence[role] for role, _ in roles)
    if any(_overlaps(handle, other) for index, handle in enumerate(handles) for other in handles[index + 1:]):
        return False
    seen = set()
    files = []
    for role, path in roles:
        snapshot, handle = snapshots[role], evidence[role]
        if (not _snapshot_valid(snapshot) or snapshot.root != handle or handle.path != path
            or handle.identity != ids[role] or handle.ancestor_ids != ancestors[role]):
            return False
        for entry in snapshot.entries:
            key = _identity(entry.identity)
            if key in seen:
                return False
            seen.add(key)
            if entry.kind == "file":
                files.append((role, entry.relative_path, entry.identity))
    if proof.disjoint_file_ids_digest != contract_digest((CONTRACT_VERSION, "disjoint-file-ids", tuple(files))):
        return False
    if snapshots["input_v2"] != fork.input_baseline or snapshots["candidate_v3"] != fork.candidate_copy_snapshot:
        return False
    if fork.candidate_copy_snapshot.content_digest != fork.input_baseline.content_digest:
        return False
    if roots.safety_backup is None:
        return fork.safety_backup_snapshot is None and fork.purpose != "FORMAL"
    return (fork.safety_backup_snapshot == snapshots["safety_backup"]
        and fork.safety_backup_snapshot.content_digest == fork.input_baseline.content_digest)


def validate_formal_activation(approval: ActivationApproval, receipt: VerificationReceipt,
                               fork: ForkReceipt) -> None:
    """Check the exact supplied receipt chain; never activate or perform CAS.

The receipt digest covers its evidence/source fields. This function cannot
recompute current source, code/configuration or Stage B acceptance. Later T7
must recompute current receipts under the actual runtime/physical chain.
"""
    missing = "ACTIVATION_APPROVAL_REQUIRED"
    _require(_typed(approval, ActivationApproval) and _typed(receipt, VerificationReceipt)
             and _typed(fork, ForkReceipt), missing)
    _require(fork.purpose in ("SYNTHETIC", "FORMAL") and approval.purpose == fork.purpose, missing)
    _require(approval.fork_receipt_digest == contract_digest(fork)
             and approval.verification_digest == contract_digest(receipt)
             and approval.code_digest == receipt.code_digest
             and approval.contract_version == receipt.contract_version
             and approval.configuration_digest == receipt.configuration_digest, missing)
    _require(receipt.baseline_digest == contract_digest(fork.input_baseline), "BASELINE_CHANGED")
    _require(approval.object_id == fork.object_id
             and approval.candidate_id == receipt.candidate_id == fork.archive_id
             and approval.root_id == receipt.root_id == fork.candidate_copy_snapshot.root.identity
             and approval.expected_binding_digest == receipt.registry_digest
             and approval.expected_binding_generation == receipt.registry_generation
             and _fork_consistent(fork), "BINDING_CONFLICT")


def authorize_archive_origin(confirmation: ArchiveOriginConfirmation, approval: ForkApproval,
                             baseline: TreeSnapshot, scope: ArchiveScope) -> ScopeAuthorization:
    """Check archived-origin attestation metadata independently of RuntimeProof.

Returns only Core migration scope, never filesystem authority. Caller still
needs a reviewed explicit human confirmation producer; fixture DTOs supply
synthetic assertions only. This helper does not issue or load confirmations.
"""
    valid = (_typed(confirmation, ArchiveOriginConfirmation) and _typed(approval, ForkApproval)
        and _snapshot_valid(baseline) and type(scope) is ArchiveScope)
    if valid:
        valid = (confirmation.explicit_origin_confirmed is True
            and confirmation.scope == approval.scope == scope
            and approval.input_baseline == baseline
            and approval.archive_origin_confirmation_digest == contract_digest(confirmation)
            and confirmation.input_root_id == baseline.root.identity
            and confirmation.input_content_digest == baseline.content_digest
            and confirmation.input_physical_digest == baseline.physical_digest)
    if not valid:
        raise HistoryError("LEGACY_SCOPE_UNVERIFIED")
    return ScopeAuthorization(scope, True)
