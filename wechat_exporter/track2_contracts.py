"""Immutable Track2 DTOs. No namespace, storage, or authority producer.

Wire reads are strict/versioned; RuntimeProof and process capabilities cannot
be loaded from disk. All local-evidence reprs hide private paths and content.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import ntpath
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType, UnionType
from typing import Any, Literal, Protocol, Union, get_args, get_origin, get_type_hints

from .archive import ArchivedMessage
from .history_contracts import ArchiveScope, CountReport, SourceLocator
from .models import ChatMessage

CONTRACT_VERSION = "track2-v1"
P0_CONTRACT_VERSION = "track2-p0-v1"
Purpose = Literal["SYNTHETIC", "REHEARSAL", "FORMAL"]
Counts = tuple[tuple[str, int], ...]
Metadata = tuple[tuple[str, str | int | bool | None], ...]
DigestPairs = tuple[tuple[str, str], ...]
ERROR_CODES = frozenset({
    "ROOT_PROOF_FAILED", "LEGACY_NAMESPACE_OVERLAP", "WRITER_BUSY",
    "FORK_INCOMPLETE", "BASELINE_CHANGED", "SOURCE_CHANGED", "BINDING_CONFLICT",
    "CANDIDATE_NOT_READY", "ACTIVATION_APPROVAL_REQUIRED",
    "RUNTIME_BINDING_UNVERIFIED", "TRACK2_RECOVERY_REQUIRED",
    "SOURCE_AUTHORIZATION_REQUIRED", "OPERATION_CONTEXT_MISMATCH",
    "OPERATION_CONTEXT_EXPIRED", "FOOTPRINT_NOT_APPROVED",
})


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("INVALID_TRACK2_CONTRACT")


class Track2Error(Exception):
    """Separate from Core's HistoryError allowlist; private data never included."""
    def __init__(self, code: str):
        _require(type(code) is str and code in ERROR_CODES)
        self.code = code
        super().__init__(code)


def _matches(value: object, hint: object) -> bool:
    origin, args = get_origin(hint), get_args(hint)
    if hint is Any:
        # Any applies only to Core's JSON payload values, never typed DTO fields.
        return (type(value) in (str, int, bool, type(None), dict, MappingProxyType, tuple, list)
                or type(value) in (_FrozenJSONArray, ArchiveScope)
                or (type(value) is float and math.isfinite(value)))
    if origin in (UnionType, Union):
        return any(_matches(value, arg) for arg in args)
    if origin is Literal:
        return any(type(value) is type(arg) and value == arg for arg in args)
    if origin is tuple:
        if type(value) is not tuple:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0]) for item in value)
        return len(args) == len(value) and all(_matches(item, arg) for item, arg in zip(value, args))
    if origin is dict or hint is dict:
        if type(value) not in (dict, MappingProxyType):
            return False
        key_hint, value_hint = args if args else (str, Any)
        return all(_matches(key, key_hint) and _matches(item, value_hint) for key, item in value.items())
    if hint is Path:
        return isinstance(value, Path) and value.is_absolute()
    if hint in (str, int, bool, type(None)):
        return type(value) is hint
    return isinstance(value, hint)


class Contract:
    def __post_init__(self) -> None:
        hints = get_type_hints(type(self))
        for item in fields(self):
            value = getattr(self, item.name)
            _require(_matches(value, hints[item.name]))
            if isinstance(value, str):
                _require(bool(value.strip()))
                if item.name.endswith("digest") or item.name == "sha256":
                    _require(re.fullmatch(r"[0-9a-f]{64}", value) is not None)
            if type(value) is int:
                _require(value >= 0)
            if item.name == "contract_version":
                _require(value == CONTRACT_VERSION)


@dataclass(frozen=True, repr=False)
class FileIdentity(Contract):
    volume_serial: int
    file_id_128: str
    link_count: int

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(0 < self.volume_serial < 2 ** 64 and self.link_count > 0)
        _require(re.fullmatch(r"[0-9a-f]{32}", self.file_id_128) is not None)
        _require(self.file_id_128 != "0" * 32)


@dataclass(frozen=True, repr=False)
class RootSelection(Contract):
    input_v2: Path
    candidate_v3: Path
    namespace: Path
    staging: Path
    reader_work: Path
    safety_backup: Path | None


@dataclass(frozen=True, repr=False)
class RootHandleProof(Contract):
    path: Path
    identity: FileIdentity
    final_path: str
    ancestor_ids: tuple[FileIdentity, ...]
    ancestor_final_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(len(self.ancestor_ids) == len(self.ancestor_final_paths))


@dataclass(frozen=True, repr=False)
class TreeEntry(Contract):
    relative_path: str
    kind: Literal["directory", "file"]
    size: int | None
    sha256: str | None
    identity: FileIdentity

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(self.relative_path == "." or (
            not PurePosixPath(self.relative_path).is_absolute()
            and ".." not in PurePosixPath(self.relative_path).parts
            and "\\" not in self.relative_path and ":" not in self.relative_path))
        _require((self.kind == "directory" and self.size is None and self.sha256 is None)
                 or (self.kind == "file" and self.size is not None and self.sha256 is not None))
        _require(self.identity.link_count == 1 or self.kind == "directory")


@dataclass(frozen=True, repr=False)
class TreeSnapshot(Contract):
    content_digest: str
    physical_digest: str
    entry_count: int
    file_count: int
    captured_at: str = field(compare=False, metadata={"audit_only": True})
    root: RootHandleProof
    entries: tuple[TreeEntry, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(self.entry_count == len(self.entries))
        _require(self.file_count == sum(e.kind == "file" for e in self.entries))
        _require(bool(self.entries) and self.entries[0].relative_path == ".")
        _require(len({e.relative_path.casefold() for e in self.entries}) == self.entry_count)
        _require(tuple(e.relative_path for e in self.entries) == tuple(sorted(e.relative_path for e in self.entries)))
        try:
            _require(datetime.fromisoformat(self.captured_at).tzinfo is not None)
        except (TypeError, ValueError):
            raise ValueError("INVALID_TRACK2_CONTRACT") from None


@dataclass(frozen=True, repr=False)
class DestinationParentProof(Contract):
    destination: Path
    existing_parent: Path
    requested_leaf_names: tuple[str, ...]
    parent: RootHandleProof
    destination_nonexistent: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(self.destination_nonexistent is True and bool(self.requested_leaf_names))
        _require(self.parent.path == self.existing_parent)
        for name in self.requested_leaf_names:
            _require(name not in (".", "..") and not any(c in name for c in '/\\:'))


@dataclass(frozen=True, repr=False)
class LegacyNamespaceProof(Contract):
    default_roots: tuple[Path, ...]
    known_roots: tuple[Path, ...]
    settings_digest: str
    registry_digest: str
    physical_root_ids: tuple[tuple[Path, FileIdentity], ...]


@dataclass(frozen=True, repr=False)
class ConfigurationSelection(Contract):
    kind: Literal["LEGACY_SETTINGS", "LEGACY_REGISTRY", "OTHER_V3_SETTINGS", "OTHER_V3_REGISTRY"]
    path: Path
    expected_digest: str | None


@dataclass(frozen=True, repr=False)
class SourceDomainSelection(Contract):
    domain_id: str
    kind: Literal["DB", "MEDIA", "METADATA", "CACHE"]
    root: Path
    provider_id: str
    scope: ArchiveScope
    binding_evidence_digest: str


@dataclass(frozen=True, repr=False)
class ReadFootprint(Contract):
    path: Path
    access: Literal["METADATA", "ENUMERATE", "CONTENT", "PROVIDER_SESSION"]
    recursive: bool
    domain_id: str | None


@dataclass(frozen=True, repr=False)
class ObjectHandleProof(Contract):
    path: Path
    identity: FileIdentity
    final_path: str
    ancestor_ids: tuple[FileIdentity, ...]
    ancestor_final_paths: tuple[str, ...]
    directory: bool
    size: int

    def __post_init__(self):
        super().__post_init__()
        _require(len(self.ancestor_ids) == len(self.ancestor_final_paths))
        _require(self.directory or self.identity.link_count == 1)


@dataclass(frozen=True, repr=False)
class ProtectedObjectProof(Contract):
    owner: str
    proof: ObjectHandleProof


@dataclass(frozen=True, repr=False)
class WriteFootprint(Contract):
    role: Literal["namespace", "candidate_v3", "staging", "reader_work", "safety_backup"]
    path: Path
    recursive: bool


@dataclass(frozen=True, repr=False)
class EvidenceFreshness(Contract):
    issued_at_ns: int
    expires_at_ns: int
    code_digest: str
    configuration_digest: str
    expected_binding_digest: str
    expected_binding_generation: int
    registry_digest: str

    def __post_init__(self):
        super().__post_init__()
        _require(0 < self.expires_at_ns - self.issued_at_ns <= 300_000_000_000)


@dataclass(frozen=True, repr=False)
class OperationRootContext(Contract):
    context_version: Literal["operation-root-v1"]
    operation_id: str
    object_id: str
    purpose: Purpose
    scope: ArchiveScope
    selection: RootSelection
    source_domains: tuple[SourceDomainSelection, ...]
    configuration_objects: tuple[ConfigurationSelection, ...]
    read_footprints: tuple[ReadFootprint, ...]
    write_footprints: tuple[WriteFootprint, ...]
    existing_role_proofs: tuple[tuple[str, RootHandleProof], ...]
    destination_parent_proofs: tuple[DestinationParentProof, ...]
    protected_roots: tuple[RootHandleProof, ...]
    protected_objects: tuple[ProtectedObjectProof, ...]
    legacy_namespace_digest: str
    provider_id: str
    binding_evidence_digest: str
    freshness: EvidenceFreshness

    def __post_init__(self):
        super().__post_init__()
        _require(bool(self.source_domains) and sum(d.kind == "DB" for d in self.source_domains) == 1)
        def ordered_unique(items, key):
            keys = tuple(key(item) for item in items)
            _require(keys == tuple(sorted(keys)) and len(set(keys)) == len(keys))
        ordered_unique(self.source_domains, lambda d: d.domain_id)
        ordered_unique(self.configuration_objects, lambda c: (c.kind, str(c.path).casefold()))
        ordered_unique(self.read_footprints, lambda r: (str(r.path).casefold(), r.access))
        ordered_unique(self.write_footprints, lambda w: w.role)
        ordered_unique(self.existing_role_proofs, lambda p: p[0])
        ordered_unique(self.destination_parent_proofs, lambda p: str(p.destination).casefold())
        ordered_unique(self.protected_roots, lambda p: str(p.path).casefold())
        ordered_unique(self.protected_objects, lambda p: (p.owner, str(p.proof.path).casefold()))
        _require(len({str(d.root).casefold() for d in self.source_domains}) == len(self.source_domains))
        _require(len({str(c.path).casefold() for c in self.configuration_objects}) == len(self.configuration_objects))
        _require({c.kind for c in self.configuration_objects} >= {"LEGACY_SETTINGS", "LEGACY_REGISTRY"})
        _require(all(d.provider_id == self.provider_id and d.scope == self.scope
            and d.binding_evidence_digest == self.binding_evidence_digest for d in self.source_domains))
        selected = {name: getattr(self.selection, name) for name in self.selection.__dataclass_fields__
            if getattr(self.selection, name) is not None}
        existing = dict(self.existing_role_proofs)
        missing = {p.destination for p in self.destination_parent_proofs}
        _require("input_v2" in existing and set(existing) <= set(selected))
        _require(all(proof.path == selected[role] for role, proof in existing.items()))
        _require(set(selected.values()) == {p.path for p in existing.values()} | missing)
        _require(not ({p.path for p in existing.values()} & missing))
        _require(len(set(selected.values())) == len(selected))
        _require({w.role: w.path for w in self.write_footprints} ==
            {role: path for role, path in selected.items() if role != "input_v2"})
        absent = {"CONFIGURATION_PARENT:" + c.kind + ":" + contract_digest(c.path): c.path
            for c in self.configuration_objects if c.expected_digest is None}
        witnesses = tuple(p for p in self.protected_objects if p.owner.startswith("CONFIGURATION_PARENT:"))
        _require(len(witnesses) == len(absent) and {p.owner for p in witnesses} == set(absent))
        for witness in witnesses:
            target, parent = absent[witness.owner], witness.proof.path
            _require(witness.proof.directory)
            for path in (target, parent):
                drive, tail = ntpath.splitdrive(str(path))
                _require(re.fullmatch(r"[A-Za-z]:", drive) is not None and ":" not in tail
                    and "\x00" not in str(path) and path == Path(ntpath.normpath(str(path)))
                    and ".." not in path.parts)
                _require(all(not name.endswith((" ", ".")) and not any(c in name for c in '<>"|?*')
                    for name in path.parts[1:]))
            suffix = target.relative_to(parent).parts
            _require(bool(suffix) and all(name not in (".", "..") for name in suffix)
                and parent.joinpath(*suffix) == target)


@dataclass(frozen=True, repr=False)
class RootProof(Contract):
    selection: RootSelection
    root_ids: tuple[tuple[str, FileIdentity], ...]
    ancestor_ids: tuple[tuple[str, tuple[FileIdentity, ...]], ...]
    disjoint_file_ids_digest: str
    topology_digest: str
    source_roots: tuple[Path, ...]
    root_evidence: tuple[tuple[str, RootHandleProof], ...]
    snapshots: tuple[tuple[str, TreeSnapshot], ...]
    legacy_namespace_digest: str


@dataclass(frozen=True, repr=False)
class OwnedEvidencePaths(Contract):
    transaction_root: Path
    operation_root: Path
    candidate_root_id: FileIdentity
    namespace_root_id: FileIdentity


@dataclass(frozen=True, repr=False)
class ArchiveOriginConfirmation(Contract):
    confirmation_id: str
    scope: ArchiveScope
    input_root_id: FileIdentity
    input_content_digest: str
    input_physical_digest: str
    explicit_origin_confirmed: bool


@dataclass(frozen=True, repr=False)
class ForkApproval(Contract):
    approval_id: str
    purpose: Purpose
    object_id: str
    scope: ArchiveScope
    input_baseline: TreeSnapshot
    archive_origin_confirmation_digest: str
    destination_parent_proofs: tuple[DestinationParentProof, ...]
    legacy_namespace_digest: str
    safety_backup_required: bool
    code_digest: str
    contract_version: str
    b_receipt_digest: str | None
    root_context_digest: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require(self.purpose != "FORMAL" or (self.b_receipt_digest is not None and self.safety_backup_required))


@dataclass(frozen=True, repr=False)
class ForkReceipt(Contract):
    operation_id: str
    archive_id: str
    object_id: str
    scope: ArchiveScope
    purpose: Purpose
    root_proof: RootProof
    input_baseline: TreeSnapshot
    candidate_copy_snapshot: TreeSnapshot
    safety_backup_snapshot: TreeSnapshot | None
    approval_digest: str
    root_context_digest: str


@dataclass(frozen=True, repr=False)
class RuntimeProof(Contract):
    scope: ArchiveScope
    verified_provider_id: str
    binding_evidence_digest: str
    synthetic: bool = False


class RuntimeBindingVerifier(Protocol):
    def verify(self, base_db: object, scope: ArchiveScope) -> RuntimeProof: ...


@dataclass(frozen=True, repr=False)
class BuildEvidence(Contract):
    fork_receipt_digest: str
    baseline_digest: str
    source_digest: str
    union_digest: str
    current_manifest_digest: str
    data_generation: int
    counts: Counts
    integrity_verified: bool
    code_digest: str
    contract_version: str
    configuration_digest: str


@dataclass(frozen=True, repr=False)
class CandidateRecord(Contract):
    archive_id: str
    object_id: str
    scope: ArchiveScope
    purpose: Purpose
    root_id: FileIdentity
    state: Literal["BUILDING", "READY", "ACTIVATION_PENDING", "ACTIVE", "FAILED", "RECOVERY_REQUIRED"]
    evidence_digest: str | None
    last_error: str | None
    operation_id: str


@dataclass(frozen=True, repr=False)
class VerificationReceipt(Contract):
    candidate_id: str
    root_id: FileIdentity
    evidence_digest: str
    baseline_digest: str
    registry_generation: int
    registry_digest: str
    source_digest: str
    code_digest: str
    contract_version: str
    configuration_digest: str


@dataclass(frozen=True, repr=False)
class ActivationApproval(Contract):
    approval_id: str
    object_id: str
    candidate_id: str
    root_id: FileIdentity
    fork_receipt_digest: str
    verification_digest: str
    expected_binding_digest: str
    expected_binding_generation: int
    code_digest: str
    contract_version: str
    configuration_digest: str
    purpose: Literal["FORMAL", "SYNTHETIC"]


@dataclass(frozen=True, repr=False)
class AuthorityBinding(Contract):
    object_id: str
    scope: ArchiveScope
    generation: int
    kind: Literal["PRESERVED_V2_READONLY", "ACTIVE_V3"]
    archive_id: str
    root_id: FileIdentity
    activation_approval_digest: str | None
    activation_manifest_digest: str | None
    activation_data_generation: int | None


@dataclass(frozen=True, repr=False)
class LockLease(Contract):
    namespace_id: FileIdentity
    owner_operation_id: str


@dataclass(frozen=True, repr=False)
class WritePermit(Contract):
    lock_lease: LockLease
    object_id: str
    root_id: FileIdentity
    archive_id: str
    binding_generation: int
    purpose: Purpose


@dataclass(frozen=True, repr=False)
class SaveReceipt(Contract):
    archive_id: str
    binding_generation: int
    current_data_generation: int
    counts: Counts
    manifest_digest: str
    integrity_verified: bool
    transaction_clean: bool


@dataclass(frozen=True, repr=False)
class VaultAssetIntent(Contract):
    intent_id: str
    archive_id: str
    root_id: FileIdentity
    scope: ArchiveScope
    relative_vault_path: str
    canonical_identity: str
    source_provenance_digest: str
    size: int
    sha256: str
    code_digest: str
    contract_version: str
    phase: Literal["INSTALLING", "INSTALLED"]


@dataclass(frozen=True, repr=False)
class V3Profile(Contract):
    namespace_root: Path
    profile_id: str
    purpose: Purpose


@dataclass(frozen=True, repr=False)
class StateBundle(Contract):
    scope: ArchiveScope
    archive_id: str
    data_generation: int
    records: tuple[ArchivedMessage, ...]
    metadata: Metadata

    def __post_init__(self) -> None:
        super().__post_init__()
        # Core's frozen envelope can retain mutable JSON mappings. Freeze a
        # defensive copy at this boundary while keeping the Core record types,
        # scalar fields, scope/locator semantics and ordered arrays intact.
        object.__setattr__(self, "records", tuple(_freeze_record(record) for record in self.records))

    def to_core_records(self) -> tuple[ArchivedMessage, ...]:
        """Independent Core handoff; callers needing dict/list APIs use this.

        Migration/collection callers must pass this projection, not frozen
        alias mappings, into Core. Returned JSON containers are independent
        per call; changes to them cannot alter this accepted state's digest.
        This performs no identity resolution, authorization, or persistence.
        """
        return tuple(replace(record,
                             message=replace(record.message, payload=_thaw_data(record.message.payload)),
                             legacy_aliases=tuple(_thaw_data(alias) for alias in record.legacy_aliases),
                             technical_observations=tuple(_thaw_data(fact) for fact in record.technical_observations))
                     for record in self.records)


@dataclass(frozen=True, repr=False)
class StagedCapsule(Contract):
    staging_root: Path
    managed_files: tuple[str, ...]
    expected_hashes: DigestPairs
    state_bundle: StateBundle
    count_report: CountReport
    new_asset_facts: tuple[VaultAssetIntent, ...]
    union_digest: str


@dataclass(frozen=True, repr=False)
class IntegrityFacts(Contract):
    scope: ArchiveScope
    archive_id: str
    data_generation: int
    union_digest: str
    manifest_digest: str
    current_ok: bool
    history_ok: bool
    asset_closure_ok: bool
    transaction_clean: bool


@dataclass(frozen=True, repr=False)
class RecoveryOutcome(Contract):
    state: Literal["CLEAN", "RECOVERED", "BLOCKED", "FAILED"]
    action: str
    evidence_digest: str | None
    error_code: str | None


_CAPABILITIES = (LockLease, WritePermit, RuntimeProof)


def _wire_version(cls):
    return P0_CONTRACT_VERSION if cls in (ForkApproval, ForkReceipt, ConfigurationSelection,
        SourceDomainSelection, ReadFootprint, ObjectHandleProof, ProtectedObjectProof,
        WriteFootprint, EvidenceFreshness, OperationRootContext) else CONTRACT_VERSION


class _FrozenJSONArray(tuple):
    """Immutable ordered JSON array with Core list-compatible equality."""
    __slots__ = ()
    __hash__ = None

    def __eq__(self, other):
        if type(other) not in (list, tuple, _FrozenJSONArray):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal


def _hints(cls: type) -> dict:
    # archive.py keeps these Core names under TYPE_CHECKING. Resolve only the
    # reviewed types locally; never mutate that module or evaluate wire names.
    return get_type_hints(cls, localns={"ArchiveScope": ArchiveScope, "SourceLocator": SourceLocator})


def _freeze_json(value: object, *, wire=False, provenance=False, parents=frozenset()) -> object:
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float:
        _require(math.isfinite(value))
        return value
    if provenance and type(value) is ArchiveScope:
        _validate_core(value)
        return value
    _require(id(value) not in parents)
    nested_parents = parents | {id(value)}
    if type(value) is dict or (not wire and type(value) is MappingProxyType):
        _require(all(type(key) is str for key in value))
        return MappingProxyType({key: _freeze_json(item, wire=wire, provenance=provenance, parents=nested_parents)
                                 for key, item in value.items()})
    if type(value) is list or (not wire and type(value) in (tuple, _FrozenJSONArray)):
        return _FrozenJSONArray(_freeze_json(item, wire=wire, provenance=provenance, parents=nested_parents)
                                for item in value)
    raise ValueError("INVALID_TRACK2_CONTRACT")


def _validate_core(value: object) -> None:
    hints = _hints(type(value))
    for item in fields(value):
        _require(_matches(getattr(value, item.name), hints[item.name]))


def _freeze_record(record: ArchivedMessage) -> ArchivedMessage:
    _require(type(record) is ArchivedMessage and type(record.message) is ChatMessage)
    _validate_core(record)
    _validate_core(record.message)
    message = replace(record.message, payload=_freeze_json(record.message.payload))
    return replace(record, message=message,
                   legacy_aliases=tuple(_freeze_json(alias, provenance=True) for alias in record.legacy_aliases),
                   technical_observations=tuple(_freeze_json(fact, provenance=True) for fact in record.technical_observations))


def _thaw_data(value: object) -> object:
    if type(value) in (dict, MappingProxyType):
        return {key: _thaw_data(item) for key, item in value.items()}
    if type(value) in (list, tuple, _FrozenJSONArray):
        return [_thaw_data(item) for item in value]
    # Scalar/ArchiveScope values are themselves immutable and retain Core types.
    return value


def _encode_json(value: object, *, audit: bool, provenance: bool) -> object:
    # Validate/defensively freeze once; canonical JSON output restores the
    # original object/array wire shape rather than serializing tuple pairs.
    immutable = _freeze_json(value, provenance=provenance)

    def plain(item):
        if type(item) is MappingProxyType:
            return {key: plain(nested) for key, nested in item.items()}
        if type(item) in (tuple, _FrozenJSONArray):
            return [plain(nested) for nested in item]
        if type(item) is ArchiveScope:
            return _encode(item, audit=audit)
        return item
    return plain(immutable)


def _encode(value: object, *, audit: bool, provenance=False) -> object:
    if isinstance(value, _CAPABILITIES):
        raise ValueError("INVALID_TRACK2_CONTRACT")
    if type(value) in (dict, MappingProxyType):
        return _encode_json(value, audit=audit, provenance=provenance)
    if is_dataclass(value):
        if type(value) in (ArchivedMessage, ChatMessage):
            _validate_core(value)
        return {"contract_version": _wire_version(type(value)), "dto_type": type(value).__name__,
                "fields": {item.name: _encode(getattr(value, item.name), audit=audit,
                                              provenance=type(value) is ArchivedMessage and item.name in
                                              ("legacy_aliases", "technical_observations"))
                           for item in fields(value) if audit or not item.metadata.get("audit_only")}}
    if isinstance(value, Path):
        return str(value)
    if type(value) in (tuple, _FrozenJSONArray):
        return [_encode(item, audit=audit, provenance=provenance) for item in value]
    _require(type(value) in (str, int, bool, type(None)))
    return value


def dto_document(value: Contract) -> dict:
    _require(isinstance(value, Contract))
    return _encode(value, audit=True)


def canonical_bytes(value: object) -> bytes:
    """Canonical digest excludes audit_only facts; EvidenceFreshness stays bound."""
    document = _encode(value, audit=False)
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf8")


def contract_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _decode_value(hint: object, value: object) -> object:
    origin, args = get_origin(hint), get_args(hint)
    if hint is Any:
        return _freeze_json(value, wire=True)
    if origin in (UnionType, Union):
        for arg in args:
            try:
                result = _decode_value(arg, value)
                if _matches(result, arg):
                    return result
            except (ValueError, TypeError):
                pass
        raise ValueError("INVALID_TRACK2_CONTRACT")
    if origin is tuple:
        _require(type(value) is list)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(args[0], item) for item in value)
        _require(len(args) == len(value))
        return tuple(_decode_value(arg, item) for arg, item in zip(args, value))
    if origin is dict or hint is dict:
        _require(type(value) is dict)
        key_hint, value_hint = args if args else (str, Any)
        return MappingProxyType({_decode_value(key_hint, key):
                                 _decode_value(ArchiveScope if hint is dict and key == "scope" else value_hint, item)
                                 for key, item in value.items()})
    if hint is Path:
        _require(type(value) is str)
        result = Path(value)
    elif isinstance(hint, type) and is_dataclass(hint):
        result = decode_dto(hint, value)
    else:
        result = value
    _require(_matches(result, hint))
    return result


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def decode_dto(cls: type, data: object):
    """Strict wire read; never a trust producer or process-capability loader."""
    _require(is_dataclass(cls) and cls not in _CAPABILITIES)
    try:
        if type(data) in (str, bytes):
            data = json.loads(data, object_pairs_hook=_unique_pairs)
        _require(type(data) is dict and set(data) == {"contract_version", "dto_type", "fields"})
        _require(type(data["contract_version"]) is str and data["contract_version"] == _wire_version(cls))
        _require(type(data["dto_type"]) is str and data["dto_type"] == cls.__name__)
        values = data["fields"]
        _require(type(values) is dict and set(values) == {item.name for item in fields(cls)})
        hints = _hints(cls)
        result = cls(**{name: _decode_value(hints[name], value) for name, value in values.items()})
        if cls is ArchivedMessage:
            return _freeze_record(result)
        if cls is ChatMessage:
            _validate_core(result)
            return replace(result, payload=_freeze_json(result.payload))
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, NameError, RecursionError):
        raise ValueError("INVALID_TRACK2_CONTRACT") from None
