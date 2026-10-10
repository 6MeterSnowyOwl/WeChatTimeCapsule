"""Pure NONAUTHORITATIVE active-save admission comparison.

Supplied bindings and state are metadata, not authenticated current authority.
No I/O, namespace lease, root proof, runtime/origin proof, current integrity,
permit, receipt, persistence, recovery or v2 fallback is produced here. A future
real service must reopen and revalidate those facts under the reviewed lease.
"""
from .authority_activation import _scope_valid
from .candidate_lifecycle import _digest
from .fork_authorization import _typed
from .track2_contracts import (
    AuthorityBinding, FileIdentity, StateBundle, Track2Error, contract_digest,
)
from .v3_state import decode_state, encode_state


def _require(condition, code):
    if not condition:
        raise Track2Error(code)


def compare_active_save(binding, *, requested_object_id, requested_scope,
        requested_root_id, requested_archive_id, state, mode,
        expected_binding_generation, expected_binding_digest) -> AuthorityBinding:
    """Return the identical binding only when all supplied inputs agree.

Expected digest/generation are independently cached comparison inputs, never
derived here from the request. Historical activation data/manifest anchors
remain fixed while valid current data_generation may advance. Passing this
comparison is neither a save nor authorization to write the supplied root.
"""
    _require(_typed(binding, AuthorityBinding) and binding.kind == "ACTIVE_V3"
        and binding.generation > 0 and binding.activation_approval_digest is not None
        and binding.activation_manifest_digest is not None
        and binding.activation_data_generation is not None, "RUNTIME_BINDING_UNVERIFIED")
    _require(type(mode) is str and mode in ("quick", "full"), "RUNTIME_BINDING_UNVERIFIED")
    _require(type(requested_object_id) is str and type(requested_archive_id) is str
        and _scope_valid(requested_scope) and _typed(requested_root_id, FileIdentity)
        and requested_object_id == binding.object_id and requested_scope == binding.scope
        and requested_root_id == binding.root_id and requested_archive_id == binding.archive_id,
        "BINDING_CONFLICT")
    _require(type(expected_binding_generation) is int
        and expected_binding_generation == binding.generation
        and _digest(expected_binding_digest)
        and expected_binding_digest == contract_digest(binding), "BINDING_CONFLICT")
    _require(_typed(state, StateBundle), "TRACK2_RECOVERY_REQUIRED")
    _require(state.scope == binding.scope and state.archive_id == binding.archive_id,
        "BINDING_CONFLICT")
    try:
        # StateBundle construction is structural. Reuse the real T4 semantic
        # codec: canonical identity, alias/provenance, sort and metadata policy.
        validated = decode_state(encode_state(state), binding.scope)
        valid = validated == state
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        valid = False
    _require(valid and state.data_generation >= binding.activation_data_generation,
        "TRACK2_RECOVERY_REQUIRED")
    return binding
