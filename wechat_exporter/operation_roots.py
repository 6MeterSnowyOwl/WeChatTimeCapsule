"""Owned synthetic P1 admission. No namespace writer or real source producer."""
from __future__ import annotations
from dataclasses import fields
from functools import wraps
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
import weakref
from types import CodeType, FunctionType

from . import track2_contracts as contracts

from . import physical_paths as physical
from .history_contracts import ArchiveScope
from .track2_contracts import (ArchiveOriginConfirmation, AuthorityBinding, CandidateRecord, ConfigurationSelection,
    EvidenceFreshness, ForkApproval, LegacyNamespaceProof, OperationRootContext,
    ProtectedObjectProof, ReadFootprint, RootSelection, SourceDomainSelection, Track2Error, WriteFootprint,
    CONTRACT_VERSION, canonical_bytes, contract_digest, decode_dto, dto_document)


def _require(value, code="OPERATION_CONTEXT_MISMATCH"):
    if not value:
        raise Track2Error(code)


def _closed(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Track2Error:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError, RecursionError):
            raise Track2Error("OPERATION_CONTEXT_MISMATCH") from None
    return call


def _typed(value, cls):
    return type(value) is cls and decode_dto(cls, dto_document(value)) == value


def _path(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def _inside(path, root):
    normalized = Path(ntpath.normpath(str(path)))
    return _path(normalized) == _path(root) or _path(root) in {_path(p) for p in normalized.parents}


def _roles(selection):
    return tuple((f.name, getattr(selection, f.name)) for f in fields(selection)
        if getattr(selection, f.name) is not None)


def _covered(reads, path, access, *, recursive=False, domain_id=None):
    return any(r.access == access and r.domain_id == domain_id
        and (not recursive or r.recursive) and (r.path == path or r.recursive and _inside(path, r.path))
        for r in reads)


def _metadata(reads, paths):
    for path in paths:
        for current in (path, *path.parents):
            # Every ancestor is an exact finite metadata object, no tree grant.
            _require(any(r.path == current and r.access == "METADATA" and not r.recursive
                and r.domain_id is None for r in reads), "FOOTPRINT_NOT_APPROVED")


def _inventory(configs, kind):
    return contract_digest(tuple((c.kind, c.path, c.expected_digest) for c in configs if c.kind == kind))


def _exact_content(reads, path):
    _require(any(r.path == path and r.access == "CONTENT" and not r.recursive
        and r.domain_id is None for r in reads), "FOOTPRINT_NOT_APPROVED")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(content):
    return json.loads(content, object_pairs_hook=_pairs,
        parse_constant=lambda _: _require(False))


def _reference(path, base, protected, reads):
    _require(type(path) is str and Path(path).is_absolute())
    path = physical._local_path(Path(path))
    _require(_inside(path, base), "SOURCE_AUTHORIZATION_REQUIRED")
    _require(path in protected, "FOOTPRINT_NOT_APPROVED")
    _metadata(reads, (path,))
    for access in ("ENUMERATE", "CONTENT"):
        _require(_covered(reads, path, access, recursive=True), "FOOTPRINT_NOT_APPROVED")
    return path


def _legacy_references(kind, document, legacy, base, protected, reads):
    _require(type(document) is dict)
    references = []
    if kind == "LEGACY_SETTINGS":
        allowed = {"schema_version", "archive_root", "known_archive_roots", "onboarding_completed",
            "window_size", "last_selected_target", "selected_account_identity", "suppressed_first_backup_notices"}
        _require(set(document) <= allowed and type(document.get("schema_version")) is int
            and document["schema_version"] == 2 and type(document.get("known_archive_roots")) is list)
        references = [document.get("archive_root"), *document["known_archive_roots"]]
    elif kind == "LEGACY_REGISTRY":
        _require(set(document) == {"schema_version", "archives", "bootstrap_completed"}
            and type(document["schema_version"]) is int and document["schema_version"] == 1
            and type(document["archives"]) is list and type(document["bootstrap_completed"]) is bool)
        for value in document["archives"]:
            _require(type(value) is dict and set(value) == {"archive_id", "path"}
                and type(value["archive_id"]) is str and bool(value["archive_id"]))
            references.append(value["path"])
    for value in references:
        path = _reference(value, base, protected, reads)
        _require(path in (*legacy.default_roots, *legacy.known_roots), "FOOTPRINT_NOT_APPROVED")


def _namespace_facts(root, reads, object_id, base, protected, legacy):
    """Read exact metadata, parse references before any referenced-root IO."""
    _require(_covered(reads, root, "ENUMERATE", recursive=True), "FOOTPRINT_NOT_APPROVED")
    leaves = (root / "settings.json", root / "registry.json")
    binding_path = root / "objects" / object_id / "binding.json"
    _metadata(reads, (*leaves, binding_path))
    for path in leaves:
        _exact_content(reads, path)
    root_proof = physical.prove_existing_root(root)
    documents, files = [], []
    for path in leaves:
        proof, content = physical.read_existing_file(path)
        files.append((proof, hashlib.sha256(content).hexdigest()))
        documents.append(_json(content))
    settings, registry = documents
    _require(type(settings) is dict and set(settings) == {"schema_version", "contract_version",
        "profile_id", "purpose", "root_id", "legacy"})
    _require(type(settings["schema_version"]) is int and settings["schema_version"] == 3
        and settings["contract_version"] == CONTRACT_VERSION and settings["purpose"] == "SYNTHETIC"
        and type(settings["profile_id"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", settings["profile_id"]))
    from .track2_contracts import FileIdentity
    _require(decode_dto(FileIdentity, settings["root_id"]) == root_proof.identity, "ROOT_PROOF_FAILED")
    _require(decode_dto(LegacyNamespaceProof, settings["legacy"]) == legacy)
    _require(type(registry) is dict and set(registry) == {"schema_version", "contract_version",
        "profile_id", "generation", "candidates", "preserved_v2"})
    _require(type(registry["schema_version"]) is int and registry["schema_version"] == 3
        and registry["contract_version"] == CONTRACT_VERSION and registry["profile_id"] == settings["profile_id"]
        and type(registry["generation"]) is int and registry["generation"] >= 0
        and type(registry["candidates"]) is dict and type(registry["preserved_v2"]) is dict)
    references, records = [], {}
    for role, cls in (("candidates", CandidateRecord), ("preserved_v2", AuthorityBinding)):
        for key, entry in registry[role].items():
            _require(type(key) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key)
                and type(entry) is dict and set(entry) == {"record", "root"})
            record = decode_dto(cls, entry["record"])
            if role == "candidates":
                _require(record.archive_id == key and record.purpose == "SYNTHETIC" and record.state != "ACTIVE")
                records[record.archive_id] = (record, entry["root"])
            else:
                _require(record.object_id == key and record.kind == "PRESERVED_V2_READONLY"
                    and record.generation == 0 and record.activation_approval_digest is None
                    and record.activation_manifest_digest is None and record.activation_data_generation is None)
            path = _reference(entry["root"], base, protected, reads)
            references.append((path, record.root_id))
    # Metadata-only traversal never hashes the byte-locked zero-byte lock leaf.
    seen, metadata = set(), []
    def visit(path):
        proof = physical.prove_existing_object(path)
        key = physical._key(proof.identity)
        _require(key not in seen, "ROOT_PROOF_FAILED")
        seen.add(key)
        metadata.append(proof)
        if path in (root / "objects", root / "operations"):
            _require(proof.directory, "ROOT_PROOF_FAILED")
        elif path in (root / "settings.json", root / "registry.json"):
            _require(not proof.directory, "ROOT_PROOF_FAILED")
        elif path == root / "authority.lock":
            _require(not proof.directory and proof.size == 0, "ROOT_PROOF_FAILED")
        if proof.directory:
            names = sorted(entry.name for entry in os.scandir(path))
            _require(len({name.casefold() for name in names}) == len(names), "ROOT_PROOF_FAILED")
            if path == root:
                _require(set(names) >= {"settings.json", "registry.json", "objects", "operations"})
                _require(all(name in {"settings.json", "registry.json", "objects", "operations", "authority.lock"}
                    or re.fullmatch(r"registry\.json\.pending-[0-9a-f]{32}", name) for name in names))
            for name in names:
                visit(path / name)
        elif path == root / "authority.lock":
            _require(proof.size == 0, "ROOT_PROOF_FAILED")
        elif path.name == "binding.json" and path.parent.parent == root / "objects":
            _exact_content(reads, path)
            _metadata(reads, (path,))
            current, content = physical.read_existing_file(path)
            _require(current == proof, "ROOT_PROOF_FAILED")
            binding = decode_dto(AuthorityBinding, _json(content))
            _require(binding.object_id == path.parent.name and binding.kind == "ACTIVE_V3"
                and binding.generation > 0 and binding.activation_approval_digest is not None
                and binding.activation_manifest_digest is not None and binding.activation_data_generation is not None)
            _require(binding.archive_id in records, "FOOTPRINT_NOT_APPROVED")
            candidate, reference_path = records[binding.archive_id]
            _require(candidate.root_id == binding.root_id and candidate.scope == binding.scope)
            _reference(reference_path, base, protected, reads)
            files.append((current, hashlib.sha256(content).hexdigest()))
    visit(root)
    binding = None
    if physical._exists_exact(binding_path):
        _exact_content(reads, binding_path)
        _, content = physical.read_existing_file(binding_path)
        binding = decode_dto(AuthorityBinding, _json(content))
    elif object_id in registry["preserved_v2"]:
        binding = decode_dto(AuthorityBinding, registry["preserved_v2"][object_id]["record"])
    _require(physical.prove_existing_root(root) == root_proof, "ROOT_PROOF_FAILED")
    return root_proof, registry, binding, tuple(files), tuple(metadata), tuple(references)


class SyntheticApprovalProducer:
    """A factory-owned TemporaryDirectory is the only available source issuer."""
    @_closed
    def __init__(self, scope: ArchiveScope, provider_id: str, binding_evidence_digest: str):
        _require(type(scope) is ArchiveScope and type(provider_id) is str and bool(provider_id.strip()))
        _require(type(binding_evidence_digest) is str and re.fullmatch("[0-9a-f]{64}", binding_evidence_digest))
        self._owner = tempfile.TemporaryDirectory(prefix="synthetic-operation-roots-")
        self._base = Path(self._owner.name)
        self._base_proof = physical.prove_existing_root(self._base)
        # Default tempfile finalization is path-only; replace it immediately.
        self._owner._finalizer.detach()
        self._finalizer = weakref.finalize(self, _safe_owned_cleanup, self._owner, self._base_proof)
        self._scope, self._provider_id, self._binding = scope, provider_id, binding_evidence_digest
        self._closed_owner = False
        self._records = {}

    @property
    def base(self) -> Path:
        return self._base

    def __enter__(self):
        self._check_owner()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _check_owner(self):
        _require(not self._closed_owner and self._owner.name == str(self._base), "SOURCE_AUTHORIZATION_REQUIRED")
        _require(physical.prove_existing_root(self._base) == self._base_proof, "ROOT_PROOF_FAILED")

    @_closed
    def close(self):
        if self._closed_owner:
            return
        try:
            self._check_owner()
        except Track2Error:
            # Do not let tempfile's finalizer later remove a replaced base.
            self._owner._finalizer.detach()
            self._finalizer.detach()
            self._closed_owner = True
            self._records.clear()
            raise
        self._closed_owner = True
        self._records.clear()
        self._finalizer.detach()
        self._owner.cleanup()

    @_closed
    def prepare(self, selection: RootSelection, legacy: LegacyNamespaceProof,
            source_domains: tuple[SourceDomainSelection, ...], *, operation_id: str,
            object_id: str, read_footprints: tuple[ReadFootprint, ...],
            write_footprints: tuple[WriteFootprint, ...],
            configuration_objects: tuple[ConfigurationSelection, ...],
            protected_roots: tuple[Path, ...], protected_objects: tuple[Path, ...],
            lifetime_seconds: float=300) -> tuple[OperationRootContext, ForkApproval]:
        _require(_typed(selection, RootSelection) and _typed(legacy, LegacyNamespaceProof))
        for value, cls in ((source_domains, SourceDomainSelection), (read_footprints, ReadFootprint),
                (write_footprints, WriteFootprint), (configuration_objects, ConfigurationSelection)):
            _require(type(value) is tuple and all(_typed(item, cls) for item in value))
        _require(type(protected_roots) is tuple and type(protected_objects) is tuple)
        explicit_paths = tuple(p for _, p in _roles(selection)) + tuple(d.root for d in source_domains)
        explicit_paths += tuple(c.path for c in configuration_objects) + protected_roots + protected_objects
        explicit_paths += legacy.default_roots + legacy.known_roots + tuple(p for p, _ in legacy.physical_root_ids)
        explicit_paths += tuple(r.path for r in read_footprints) + tuple(w.path for w in write_footprints)
        for path in explicit_paths:
            _require(physical._local_path(path) == path and ".." not in path.parts, "ROOT_PROOF_FAILED")
        _require(not any(_inside(d.root, path) or _inside(path, d.root)
            for d in source_domains for _, path in _roles(selection)), "ROOT_PROOF_FAILED")
        for items, key in ((source_domains, lambda d: d.domain_id),
                (source_domains, lambda d: _path(d.root)),
                (read_footprints, lambda r: (_path(r.path), r.access)),
                (write_footprints, lambda w: w.role),
                (configuration_objects, lambda c: _path(c.path)),
                (protected_roots, _path), (protected_objects, _path)):
            keys = tuple(key(item) for item in items)
            _require(len(set(keys)) == len(keys))
        _require(all(r.access != "METADATA" or not r.recursive and r.domain_id is None
            for r in read_footprints))
        _require(type(operation_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id))
        _require(type(object_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", object_id))
        _require(type(lifetime_seconds) in (int, float) and math.isfinite(lifetime_seconds)
            and 0 < lifetime_seconds <= 300)
        _require(bool(source_domains) and sum(d.kind == "DB" for d in source_domains) == 1)
        _require(all(d.provider_id == self._provider_id and d.scope == self._scope
            and d.binding_evidence_digest == self._binding for d in source_domains))
        _require({c.kind for c in configuration_objects} >= {"LEGACY_SETTINGS", "LEGACY_REGISTRY"})
        paths = tuple(p for _, p in _roles(selection)) + tuple(d.root for d in source_domains)
        paths += tuple(c.path for c in configuration_objects) + protected_roots + protected_objects
        paths += legacy.default_roots + legacy.known_roots
        _require(all(isinstance(p, Path) and p.is_absolute() and _inside(p, self._base)
            and p != self._base for p in paths), "SOURCE_AUTHORIZATION_REQUIRED")
        _require(all(r.access == "METADATA" or _inside(r.path, self._base)
            for r in read_footprints), "SOURCE_AUTHORIZATION_REQUIRED")
        _require(all(_inside(r.path, self._base) or r.path in self._base.parents
            for r in read_footprints), "SOURCE_AUTHORIZATION_REQUIRED")
        _require({w.role: w.path for w in write_footprints} ==
            {role: path for role, path in _roles(selection) if role != "input_v2"})
        _metadata(read_footprints, paths)
        # Required tree coverage is checked before *any* target existence/read.
        for path, domain in ((selection.input_v2, None),
                *((d.root, d.domain_id) for d in source_domains), *((p, None) for p in protected_roots)):
            for access in ("ENUMERATE", "CONTENT"):
                _require(_covered(read_footprints, path, access, recursive=True, domain_id=domain),
                    "FOOTPRINT_NOT_APPROVED")
        for config in configuration_objects:
            if config.expected_digest is not None:
                _exact_content(read_footprints, config.path)
        for path in protected_objects:
            _exact_content(read_footprints, path)
        self._check_owner()
        # Existing writable roles require full snapshots; namespace is special.
        existing, missing, snapshots = [], [], []
        absent_configurations = []
        namespace_facts = None
        for role, path in _roles(selection):
            destination = _capture_missing(self._base, path)
            if destination is None:
                if role == "namespace":
                    namespace_facts = _namespace_facts(path, read_footprints, object_id, self._base,
                        protected_roots, legacy)
                else:
                    for access in ("ENUMERATE", "CONTENT"):
                        _require(_covered(read_footprints, path, access, recursive=True), "FOOTPRINT_NOT_APPROVED")
                proof = physical.prove_existing_root(path)
                existing.append((role, proof))
            else:
                _require(role != "input_v2", "ROOT_PROOF_FAILED")
                missing.append(destination)
        files, configuration_documents = [], []
        for config in configuration_objects:
            if config.expected_digest is None:
                captured = _capture_missing(self._base, config.path)
                _require(captured is not None)
                _require(captured.destination == config.path
                    and captured.existing_parent.joinpath(*captured.requested_leaf_names) == config.path,
                    "ROOT_PROOF_FAILED")
                witness = physical.prove_existing_object(captured.existing_parent)
                _require(witness.directory and witness.identity == captured.parent.identity,
                    "ROOT_PROOF_FAILED")
                absent_configurations.append((config, captured, witness))
                continue
            present = physical._exists_exact(config.path)
            _require(present == (config.expected_digest is not None))
            if present:
                proof, content = physical.read_existing_file(config.path)
                digest = hashlib.sha256(content).hexdigest()
                _require(digest == config.expected_digest)
                files.append((proof, digest, config.kind))
                document = _json(content)
                configuration_documents.append((config, document))
                if config.kind.startswith("LEGACY_"):
                    _legacy_references(config.kind, document, legacy, self._base,
                        protected_roots, read_footprints)
        other_namespaces = []
        for config, _ in configuration_documents:
            if config.kind == "OTHER_V3_SETTINGS":
                other_root = config.path.parent
                _require(config.path.name == "settings.json" and other_root in protected_roots,
                    "FOOTPRINT_NOT_APPROVED")
                _require(any(c.kind == "OTHER_V3_REGISTRY" and c.path == other_root / "registry.json"
                    and c.expected_digest is not None for c in configuration_objects))
                other_namespaces.append(_namespace_facts(other_root, read_footprints, object_id,
                    self._base, protected_roots, legacy))
        _require(all(any(c.kind == "OTHER_V3_SETTINGS" and c.path.parent == config.path.parent
            for c in configuration_objects) for config, _ in configuration_documents
            if config.kind == "OTHER_V3_REGISTRY"))
        # Parse inventories above before following any declared registry references.
        snapshots = [(role, physical.snapshot_tree(proof.path)) for role, proof in existing if role != "namespace"]
        source_proofs = tuple((d.domain_id, physical.snapshot_tree(d.root)) for d in source_domains)
        namespace_roots = {facts[0].path for facts in other_namespaces}
        protected = tuple(physical.snapshot_tree(p) for p in protected_roots if p not in namespace_roots)
        for path in protected_objects:
            proof, content = physical.read_existing_file(path)
            files.append((proof, hashlib.sha256(content).hexdigest(), "explicit-protected"))
        for facts in (*other_namespaces, *((namespace_facts,) if namespace_facts else ())):
            for proof, digest in facts[3]:
                if facts is namespace_facts:
                    # Current namespace metadata is owned, never an external protection grant.
                    continue
                files.append((proof, digest, "other-namespace"))
            for path, expected_id in facts[5]:
                _require(physical.prove_existing_root(path).identity == expected_id, "ROOT_PROOF_FAILED")
        configs = tuple(sorted(configuration_objects, key=lambda c: (c.kind, str(c.path).casefold())))
        _require(legacy.settings_digest == _inventory(configs, "LEGACY_SETTINGS")
            and legacy.registry_digest == _inventory(configs, "LEGACY_REGISTRY"))
        legacy_handles = tuple(physical.prove_existing_root(p) for p in (*legacy.default_roots, *legacy.known_roots))
        _require(dict(legacy.physical_root_ids) == {p.path: p.identity for p in legacy_handles})
        fresh_legacy = LegacyNamespaceProof(legacy.default_roots, legacy.known_roots,
            _inventory(configs, "LEGACY_SETTINGS"), _inventory(configs, "LEGACY_REGISTRY"),
            tuple((p.path, p.identity) for p in legacy_handles))
        _require(fresh_legacy == legacy)
        handles = tuple(proof for _, proof in existing) + tuple(s.root for _, s in source_proofs)
        _require(not any(physical._overlaps(a, b) for i, a in enumerate(handles) for b in handles[i + 1:]),
            "ROOT_PROOF_FAILED")
        for role, proof in existing:
            if role != "input_v2":
                _require(not any(physical._overlaps(proof, p) for p in legacy_handles), "LEGACY_NAMESPACE_OVERLAP")
        for _, snapshot in source_proofs:
            _require(not any(physical._overlaps(snapshot.root, p) for p in legacy_handles), "LEGACY_NAMESPACE_OVERLAP")
        for proof, _, _ in files:
            _require(not any(_inside(proof.path, p) or _inside(p, proof.path)
                for _, p in _roles(selection)), "ROOT_PROOF_FAILED")
            _require(not any(_inside(proof.path, d.root) or _inside(d.root, proof.path)
                for d in source_domains), "ROOT_PROOF_FAILED")
        for config in configuration_objects:
            _require(not any(_inside(config.path, p) or _inside(p, config.path)
                for _, p in _roles(selection)), "ROOT_PROOF_FAILED")
            _require(not any(_inside(config.path, d.root) or _inside(d.root, config.path)
                for d in source_domains), "ROOT_PROOF_FAILED")
        for destination in missing:
            for protected_handle in (*handles, *(s.root for s in protected), *legacy_handles,
                    *(facts[0] for facts in other_namespaces)):
                _require(not _inside(destination.destination, protected_handle.path)
                    and not _inside(protected_handle.path, destination.destination), "ROOT_PROOF_FAILED")
        for i, (_, path) in enumerate(_roles(selection)):
            _require(not any(_inside(path, other) or _inside(other, path)
                for _, other in _roles(selection)[i + 1:]), "ROOT_PROOF_FAILED")
        baseline = dict(snapshots)["input_v2"]
        now = time.time_ns()
        code = _code_digest()
        current_binding = namespace_facts[2] if namespace_facts else None
        freshness = EvidenceFreshness(now, now + int(lifetime_seconds * 1e9), code,
            contract_digest((configs, legacy, selection, protected_roots, protected_objects)),
            contract_digest(current_binding), current_binding.generation if current_binding else 0,
            contract_digest(namespace_facts[1] if namespace_facts else None))
        protected_handles = tuple(s.root for s in protected) + tuple(f[0] for f in other_namespaces)
        for role, handle in existing:
            if role != "input_v2":
                _require(not any(physical._overlaps(handle, other) for other in protected_handles),
                    "ROOT_PROOF_FAILED")
        object_facts = {}
        for proof, digest, owner in files:
            previous = object_facts.get(proof.path)
            _require(previous is None or previous.proof == proof, "ROOT_PROOF_FAILED")
            object_facts[proof.path] = ProtectedObjectProof(owner, proof)
        parent_witnesses = tuple(ProtectedObjectProof(_configuration_owner(config), witness)
            for config, _, witness in absent_configurations)
        context = OperationRootContext("operation-root-v1", operation_id, object_id, "SYNTHETIC",
            self._scope, selection, tuple(sorted(source_domains, key=lambda d: d.domain_id)), configs,
            tuple(sorted(read_footprints, key=lambda r: (str(r.path).casefold(), r.access))),
            tuple(sorted(write_footprints, key=lambda w: w.role)), tuple(sorted(existing)),
            tuple(sorted(missing, key=lambda p: str(p.destination).casefold())),
            tuple(sorted(protected_handles, key=lambda p: str(p.path).casefold())),
            tuple(sorted((*object_facts.values(), *parent_witnesses),
                key=lambda p: (p.owner, str(p.proof.path).casefold()))),
            contract_digest(legacy), self._provider_id, self._binding, freshness)
        origin = ArchiveOriginConfirmation(uuid.uuid4().hex, self._scope, baseline.root.identity,
            baseline.content_digest, baseline.physical_digest, True)
        approval = ForkApproval(uuid.uuid4().hex, "SYNTHETIC", object_id, self._scope, baseline,
            contract_digest(origin), context.destination_parent_proofs, contract_digest(legacy),
            selection.safety_backup is not None, code, CONTRACT_VERSION, None, contract_digest(context))
        self._records[id(approval)] = dict(approval=approval, context=context,
            approval_bytes=canonical_bytes(approval), context_bytes=canonical_bytes(context),
            pid=os.getpid(), thread=threading.get_ident(),
            deadline=time.monotonic_ns() + int(lifetime_seconds * 1e9),
            snapshots=tuple(snapshots), sources=source_proofs, protected=protected,
            legacy_handles=legacy_handles, files=tuple(files), namespace_facts=namespace_facts,
            other_namespaces=tuple(other_namespaces), legacy=legacy,
            absent_configurations=tuple(absent_configurations))
        object.__setattr__(approval, "_operation_issuer", self)
        return context, approval


def _configuration_owner(config):
    return "CONFIGURATION_PARENT:" + config.kind + ":" + contract_digest(config.path)


def _capture_missing(base, target):
    _require(physical._local_path(target) == target and ".." not in target.parts, "ROOT_PROOF_FAILED")
    _require(_inside(target, base) and target != base, "ROOT_PROOF_FAILED")
    suffix = target.relative_to(base).parts
    current = base
    for index, component in enumerate(suffix):
        parent = physical.prove_existing_root(current)
        child = current / component
        if not physical._exists_exact(child):
            _require(current.joinpath(*suffix[index:]) == target, "ROOT_PROOF_FAILED")
            return contracts.DestinationParentProof(target, current, suffix[index:], parent, True)
        current = child
    return None


def _revalidate_missing(proof):
    _require(type(proof) is contracts.DestinationParentProof
        and proof.existing_parent.joinpath(*proof.requested_leaf_names) == proof.destination,
        "ROOT_PROOF_FAILED")
    _require(physical.prove_existing_root(proof.existing_parent) == proof.parent, "ROOT_PROOF_FAILED")
    _require(not physical._exists_exact(proof.existing_parent / proof.requested_leaf_names[0]),
        "ROOT_PROOF_FAILED")


def _code_material(value):
    if type(value) is CodeType:
        return (value.co_code.hex(), value.co_names, value.co_varnames,
            tuple(_code_material(item) for item in value.co_consts))
    if type(value) is tuple:
        return tuple(_code_material(item) for item in value)
    if type(value) in (set, frozenset):
        return (type(value).__name__, tuple(sorted((_code_material(item) for item in value), key=repr)))
    return repr(value)


def _safe_owned_cleanup(owner, expected):
    """Implicit lifetime cleanup retains evidence on any ownership uncertainty."""
    try:
        if owner.name == str(expected.path) and physical.prove_existing_root(expected.path) == expected:
            owner.cleanup()
    except (Track2Error, OSError, ValueError, TypeError, AttributeError):
        # No path-only cleanup, exception string or follow-up discovery.
        return


def _code_digest():
    # Hash loaded executable code without granting reads outside owned storage.
    routines = []
    for namespace, expected_module in ((globals(), __name__), (vars(physical), physical.__name__),
            (vars(contracts), contracts.__name__)):
        for name, value in sorted(namespace.items()):
            if type(value) is FunctionType and value.__module__ in (
                    __name__, physical.__name__, contracts.__name__):
                routines.append((expected_module, name, _function_material(value)))
            elif isinstance(value, type) and value.__module__ == expected_module:
                for member_name, member in sorted(vars(value).items()):
                    if type(member) is FunctionType:
                        routines.append((expected_module, name + "." + member_name, _function_material(member)))
                    elif type(member) is property:
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                routines.append((expected_module, name + "." + member_name + "." + accessor,
                                    _function_material(function)))
                    elif type(member) in (staticmethod, classmethod):
                        routines.append((expected_module, name + "." + member_name,
                            _function_material(member.__func__)))
    return contract_digest(tuple(routines))


def _loaded_value(value, seen):
    if type(value) is FunctionType:
        return _function_material(value, seen)
    if type(value) is property:
        return tuple(_loaded_value(getattr(value, name), seen) for name in ("fget", "fset", "fdel"))
    if type(value) in (tuple, list):
        return tuple(_loaded_value(item, seen) for item in value)
    if type(value) is dict:
        return tuple((repr(key), _loaded_value(item, seen)) for key, item in sorted(value.items()))
    if type(value) in (set, frozenset):
        return tuple(sorted(repr(item) for item in value))
    if isinstance(value, type):
        return (value.__module__, value.__qualname__)
    return repr(value)


def _function_material(function, seen=frozenset()):
    label = (function.__module__, function.__qualname__)
    if id(function) in seen:
        return ("recursive-function", label)
    seen = seen | {id(function)}
    code = _code_material(function.__code__)
    wrapped = getattr(function, "__wrapped__", None)
    closure = tuple(_loaded_value(cell.cell_contents, seen) for cell in (function.__closure__ or ()))
    return (code, _loaded_value(function.__defaults__, seen),
        _loaded_value(function.__kwdefaults__, seen), closure,
        _function_material(wrapped, seen) if type(wrapped) is FunctionType else None)


@_closed
def require_operation_admission(context: OperationRootContext, approval: ForkApproval, *,
        action: str, role: str, target: Path) -> None:
    producer = getattr(approval, "_operation_issuer", None)
    _require(type(approval) is ForkApproval and approval.purpose == "SYNTHETIC"
        and type(producer) is SyntheticApprovalProducer and not producer._closed_owner
        and id(approval) in producer._records,
        "SOURCE_AUTHORIZATION_REQUIRED")
    record = producer._records[id(approval)]
    _require(record["approval"] is approval and record["pid"] == os.getpid()
        and record["thread"] == threading.get_ident(), "SOURCE_AUTHORIZATION_REQUIRED")
    _require(_typed(context, OperationRootContext) and _typed(approval, ForkApproval))
    _require(canonical_bytes(context) == record["context_bytes"]
        and canonical_bytes(approval) == record["approval_bytes"]
        and approval.root_context_digest == contract_digest(context)
        and context.purpose == approval.purpose == "SYNTHETIC"
        and context.object_id == approval.object_id and context.scope == approval.scope
        and context.freshness.code_digest == _code_digest())
    _require(time.monotonic_ns() < record["deadline"]
        and time.time_ns() < context.freshness.expires_at_ns, "OPERATION_CONTEXT_EXPIRED")
    _require(type(action) is str and type(role) is str and isinstance(target, Path)
        and target.is_absolute())
    allowed = {"WORK_CREATE": ("staging", "reader_work"),
        "FORK_ROOT_CREATE": ("candidate_v3", "safety_backup"),
        "NAMESPACE_INITIALIZE": ("namespace",)}
    _require(action in allowed and role in allowed[action]
        and target == getattr(context.selection, role)
        and any(w.role == role and w.path == target for w in context.write_footprints)
        and target in {p.destination for p in context.destination_parent_proofs})
    # Exact facts/action/issuer checks above perform no target IO.
    producer._check_owner()
    for proof in context.destination_parent_proofs:
        _revalidate_missing(proof)
    witnesses = {p.owner: p.proof for p in context.protected_objects
        if p.owner.startswith("CONFIGURATION_PARENT:")}
    _require(len(witnesses) == len(record["absent_configurations"]), "ROOT_PROOF_FAILED")
    for config, captured, witness in record["absent_configurations"]:
        _require(witnesses.get(_configuration_owner(config)) == witness
            and witness.directory and captured.destination == config.path
            and captured.existing_parent == witness.path, "ROOT_PROOF_FAILED")
        _require(physical.prove_existing_object(witness.path) == witness, "ROOT_PROOF_FAILED")
        _revalidate_missing(captured)
    for role_name, proof in context.existing_role_proofs:
        _require(physical.prove_existing_root(proof.path) == proof, "ROOT_PROOF_FAILED")
    for _, snapshot in (*record["snapshots"], *record["sources"]):
        _require(physical.prove_existing_root(snapshot.root.path) == snapshot.root, "ROOT_PROOF_FAILED")
    for snapshot in record["protected"]:
        _require(physical.prove_existing_root(snapshot.root.path) == snapshot.root, "ROOT_PROOF_FAILED")
    for proof in record["legacy_handles"]:
        _require(physical.prove_existing_root(proof.path) == proof, "ROOT_PROOF_FAILED")
    for proof, digest, _ in record["files"]:
        _require(physical.prove_existing_object(proof.path) == proof, "ROOT_PROOF_FAILED")
        current, content = physical.read_existing_file(proof.path)
        _require(current == proof and hashlib.sha256(content).hexdigest() == digest, "ROOT_PROOF_FAILED")
    for facts in (*record["other_namespaces"], *((record["namespace_facts"],) if record["namespace_facts"] else ())):
        current = _namespace_facts(facts[0].path, context.read_footprints, context.object_id,
            producer.base, tuple(p.path for p in context.protected_roots), record["legacy"])
        _require(current == facts, "ROOT_PROOF_FAILED")
    for role_name, snapshot in record["snapshots"]:
        current = physical.snapshot_tree(snapshot.root.path)
        _require(current == snapshot, "BASELINE_CHANGED" if role_name == "input_v2" else "ROOT_PROOF_FAILED")
    for _, snapshot in record["sources"]:
        _require(physical.snapshot_tree(snapshot.root.path) == snapshot, "SOURCE_CHANGED")
    for snapshot in record["protected"]:
        _require(physical.snapshot_tree(snapshot.root.path) == snapshot, "ROOT_PROOF_FAILED")
    _require(not producer._closed_owner and producer._records.get(id(approval)) is record
        and record["approval"] is approval and record["pid"] == os.getpid()
        and record["thread"] == threading.get_ident(), "SOURCE_AUTHORIZATION_REQUIRED")
    _require(context.freshness.code_digest == _code_digest())
    _require(time.monotonic_ns() < record["deadline"]
        and time.time_ns() < context.freshness.expires_at_ns, "OPERATION_CONTEXT_EXPIRED")
