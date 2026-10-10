"""Strict v3 candidate index/read-only references, never an activation issuer.

ACTIVE commit is intentionally deferred to the genuine Task 7 authority
chain. Binding reads and compare decisions do not confer write permission.
"""
from __future__ import annotations
import os
from pathlib import Path
import uuid
from .physical_paths import prove_existing_root, snapshot_tree
from .track2_contracts import (CONTRACT_VERSION, AuthorityBinding, CandidateRecord,
    LockLease, V3Profile, canonical_bytes, contract_digest, decode_dto, dto_document)
from .v3_profile import (_closed, _identifier, _independent, _json_read, _overlap,
                         _key, _require, _settings)


def _entry(value, cls):
    _require(type(value) is dict and set(value) == {"record", "root"})
    _require(type(value["root"]) is str and Path(value["root"]).is_absolute())
    return decode_dto(cls, value["record"])


def _indexed_roots(profile, document):
    """Reprove stored identities and all role relationships, not DTO equality."""
    _, legacy = _settings(profile)
    namespace = prove_existing_root(profile.namespace_root)
    roots = []
    for role, cls in (("preserved_v2", AuthorityBinding), ("candidates", CandidateRecord)):
        for key, value in document[role].items():
            record = _entry(value, cls)
            path = Path(value["root"])
            handle = (_independent(path, legacy) if role == "candidates"
                      else prove_existing_root(path))
            _require(handle.identity == record.root_id, "ROOT_PROOF_FAILED")
            _require(not _overlap(handle, namespace), "ROOT_PROOF_FAILED")
            _require(all(not _overlap(handle, prior) for _, _, prior in roots), "BINDING_CONFLICT")
            roots.append((role, key, handle))
    return roots


def _disjoint_registration(profile, document, role, key, handle):
    # Only exact same-role/key re-registration may reuse its own physical root.
    for previous_role, previous_key, previous in _indexed_roots(profile, document):
        if (previous_role, previous_key) != (role, key):
            _require(not _overlap(handle, previous), "BINDING_CONFLICT")


def _load_registry(profile):
    _settings(profile)
    document = _json_read(profile.namespace_root / "registry.json")
    _require(type(document) is dict and set(document) == {"schema_version", "contract_version",
        "profile_id", "generation", "candidates", "preserved_v2"})
    _require(type(document["schema_version"]) is int and document["schema_version"] == 3
        and document["contract_version"] == CONTRACT_VERSION and document["profile_id"] == profile.profile_id
        and type(document["generation"]) is int and document["generation"] >= 0
        and type(document["candidates"]) is dict and type(document["preserved_v2"]) is dict)
    scopes, object_spellings, candidate_roots = {}, {}, set()
    _require(len({key.casefold() for key in document["candidates"]}) == len(document["candidates"]))
    for key, value in document["preserved_v2"].items():
        _identifier(key)
        record = _entry(value, AuthorityBinding)
        _require(record.object_id == key and record.kind == "PRESERVED_V2_READONLY"
            and record.generation == 0 and record.activation_approval_digest is None
            and record.activation_manifest_digest is None and record.activation_data_generation is None)
        scopes[key] = record.scope
        _require(key.casefold() not in object_spellings)
        object_spellings[key.casefold()] = key
    for key, value in document["candidates"].items():
        _identifier(key)
        record = _entry(value, CandidateRecord)
        _identifier(record.object_id)
        _identifier(record.operation_id)
        _require(record.archive_id == key and record.purpose == profile.purpose and record.state != "ACTIVE")
        _require(record.object_id not in scopes or scopes[record.object_id] == record.scope)
        spelling = object_spellings.get(record.object_id.casefold())
        _require(spelling is None or spelling == record.object_id)
        object_spellings[record.object_id.casefold()] = record.object_id
        _require(_key(record.root_id) not in candidate_roots)
        candidate_roots.add(_key(record.root_id))
        scopes[record.object_id] = record.scope
    _indexed_roots(profile, document)
    return document


def _replace_registry(profile, document, lease):
    from .namespace_lock import require_live_lease
    require_live_lease(profile, lease)
    _indexed_roots(profile, document)
    root = prove_existing_root(profile.namespace_root)
    destination = profile.namespace_root / "registry.json"
    pending = profile.namespace_root / ("registry.json.pending-" + uuid.uuid4().hex)
    try:
        with pending.open("xb") as file:
            file.write(canonical_bytes(document))
            file.flush(); os.fsync(file.fileno())
        require_live_lease(profile, lease)
        _require(prove_existing_root(profile.namespace_root) == root, "ROOT_PROOF_FAILED")
        _indexed_roots(profile, document)
        os.replace(pending, destination)
        _require(_load_registry(profile) == document)
    finally:
        # Remove only this still-uncommitted owned leaf on ordinary exception.
        # Process crash leftovers remain evidence and are never auto-recursed.
        if pending.exists():
            pending.unlink()


@_closed
def read_binding(profile: V3Profile, object_id: str) -> AuthorityBinding | None:
    _identifier(object_id)
    registry = _load_registry(profile)
    path = profile.namespace_root / "objects" / object_id / "binding.json"
    if path.exists():
        binding = decode_dto(AuthorityBinding, _json_read(path))
        _require(binding.object_id == object_id and binding.kind == "ACTIVE_V3"
            and binding.generation > 0 and binding.activation_approval_digest is not None
            and binding.activation_manifest_digest is not None and binding.activation_data_generation is not None)
        preserved = registry["preserved_v2"].get(object_id)
        _require(preserved is None or _entry(preserved, AuthorityBinding).scope == binding.scope)
        return binding
    value = registry["preserved_v2"].get(object_id)
    return None if value is None else _entry(value, AuthorityBinding)


@_closed
def register_candidate(profile: V3Profile, candidate: CandidateRecord, root: Path, lease: LockLease) -> None:
    from .namespace_lock import require_live_lease
    require_live_lease(profile, lease)
    _require(type(candidate) is CandidateRecord and candidate.state != "ACTIVE"
        and candidate.purpose == profile.purpose, "BINDING_CONFLICT")
    _identifier(candidate.object_id); _identifier(candidate.archive_id)
    _identifier(candidate.operation_id)
    _, legacy = _settings(profile)
    handle = _independent(root, legacy)
    _require(snapshot_tree(root).root == handle, "ROOT_PROOF_FAILED")
    _require(not _overlap(handle, prove_existing_root(profile.namespace_root)), "ROOT_PROOF_FAILED")
    _require(handle.identity == candidate.root_id, "ROOT_PROOF_FAILED")
    registry = _load_registry(profile)
    existing = read_binding(profile, candidate.object_id)
    _require(existing is None or existing.scope == candidate.scope, "BINDING_CONFLICT")
    for value in registry["candidates"].values():
        record = _entry(value, CandidateRecord)
        _require(record.object_id != candidate.object_id or record.scope == candidate.scope, "BINDING_CONFLICT")
        _require(record.object_id.casefold() != candidate.object_id.casefold()
            or record.object_id == candidate.object_id, "BINDING_CONFLICT")
        _require(record.archive_id.casefold() != candidate.archive_id.casefold()
            or record.archive_id == candidate.archive_id, "BINDING_CONFLICT")
        _require(record.archive_id == candidate.archive_id or _key(record.root_id) != _key(candidate.root_id),
                 "BINDING_CONFLICT")
    for key in registry["preserved_v2"]:
        _require(key.casefold() != candidate.object_id.casefold() or key == candidate.object_id, "BINDING_CONFLICT")
    value = {"record": dto_document(candidate), "root": str(root)}
    previous = registry["candidates"].get(candidate.archive_id)
    _require(previous is None or previous == value, "BINDING_CONFLICT")
    _disjoint_registration(profile, registry, "candidates", candidate.archive_id, handle)
    if previous is None:
        registry["candidates"][candidate.archive_id] = value
        registry["generation"] += 1
        _replace_registry(profile, registry, lease)


@_closed
def read_candidates(profile: V3Profile, object_id: str) -> tuple[CandidateRecord, ...]:
    _identifier(object_id)
    registry = _load_registry(profile)
    return tuple(record for key, value in sorted(registry["candidates"].items())
        if (record := _entry(value, CandidateRecord)).object_id == object_id)


@_closed
def register_preserved_v2(profile: V3Profile, binding: AuthorityBinding, root: Path, lease: LockLease) -> None:
    from .namespace_lock import require_live_lease
    require_live_lease(profile, lease)
    _require(type(binding) is AuthorityBinding and binding.kind == "PRESERVED_V2_READONLY"
        and binding.generation == 0 and binding.activation_approval_digest is None
        and binding.activation_manifest_digest is None and binding.activation_data_generation is None,
        "BINDING_CONFLICT")
    _identifier(binding.object_id)
    handle = prove_existing_root(root)
    _require(handle.identity == binding.root_id and
        not _overlap(handle, prove_existing_root(profile.namespace_root)), "ROOT_PROOF_FAILED")
    registry = _load_registry(profile)
    existing = read_binding(profile, binding.object_id)
    _require(existing is None or existing == binding, "BINDING_CONFLICT")
    _require(all(record.scope == binding.scope for record in read_candidates(profile, binding.object_id)),
             "BINDING_CONFLICT")
    for key in registry["preserved_v2"]:
        _require(key.casefold() != binding.object_id.casefold() or key == binding.object_id, "BINDING_CONFLICT")
    for value in registry["candidates"].values():
        record = _entry(value, CandidateRecord)
        _require(record.object_id.casefold() != binding.object_id.casefold()
            or record.object_id == binding.object_id, "BINDING_CONFLICT")
    value = {"record": dto_document(binding), "root": str(root)}
    previous = registry["preserved_v2"].get(binding.object_id)
    _require(previous is None or previous == value, "BINDING_CONFLICT")
    _disjoint_registration(profile, registry, "preserved_v2", binding.object_id, handle)
    if previous is None:
        registry["preserved_v2"][binding.object_id] = value
        registry["generation"] += 1
        _replace_registry(profile, registry, lease)


def _binding_matches(profile, object_id, expected_digest, expected_generation):
    binding = read_binding(profile, object_id)
    digest = contract_digest(binding) if binding is not None else contract_digest(None)
    generation = binding.generation if binding is not None else 0
    return (type(expected_generation) is int and generation == expected_generation
            and digest == expected_digest)
