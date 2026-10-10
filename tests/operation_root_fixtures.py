"""Explicit fresh synthetic P1 fixtures; setup and cleanup are never audited."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
import hashlib
import json
import sys
import tempfile

from wechat_exporter import track2_contracts as dto
from wechat_exporter import operation_roots as operation
from wechat_exporter.physical_paths import prove_existing_root
from wechat_exporter.history_contracts import ArchiveScope

SCOPE = ArchiveScope("synthetic-account", "synthetic-conversation")
PROVIDER = "synthetic-provider"
BINDING = "1" * 64


def fact(name, **arguments):
    # A no-op fact seam only lets tests observe missing required rejection.
    return getattr(dto, name, lambda **kw: SimpleNamespace(**kw))(**arguments)


class MissingProducer:
    def __init__(self, *args):
        self.owner = tempfile.TemporaryDirectory(prefix="synthetic-p1-red-")
        self.base = Path(self.owner.name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.owner.cleanup()
    def prepare(self, *args, **kwargs):
        return None


@contextmanager
def operation_fixture():
    factory = getattr(operation, "SyntheticApprovalProducer", MissingProducer)
    with factory(SCOPE, PROVIDER, BINDING) as producer:
        base = producer.base
        source, legacy, input_root = (base / name for name in ("source", "legacy", "input"))
        for root in (source, legacy, input_root):
            root.mkdir()
        (source / "source.bin").write_bytes(b"synthetic-source")
        (input_root / "archive.bin").write_bytes(b"synthetic-archive")
        selection = dto.RootSelection(input_root, base / "candidate", base / "namespace",
            base / "staging", base / "reader", base / "backup")
        configs = tuple(fact("ConfigurationSelection", kind=kind,
            path=legacy / name, expected_digest=None) for kind, name in
            (("LEGACY_SETTINGS", "settings.json"), ("LEGACY_REGISTRY", "archive_registry.json")))
        inventory = lambda kind: dto.contract_digest(tuple((c.kind, c.path, c.expected_digest)
            for c in configs if c.kind == kind))
        legacy_proof = dto.LegacyNamespaceProof((legacy,), (), inventory("LEGACY_SETTINGS"),
            inventory("LEGACY_REGISTRY"), ((legacy, prove_existing_root(legacy).identity),))
        domains = (fact("SourceDomainSelection", domain_id="synthetic-db", kind="DB", root=source,
            provider_id=PROVIDER, scope=SCOPE, binding_evidence_digest=BINDING),)
        paths = [getattr(selection, role) for role in selection.__dataclass_fields__]
        paths += [source, legacy, *(c.path for c in configs)]
        metadata = set()
        for path in paths:
            metadata.update((path, *path.parents))
        reads = [fact("ReadFootprint", path=p, access="METADATA", recursive=False, domain_id=None)
            for p in sorted(metadata, key=str)]
        for path, domain_id in ((input_root, None), (source, "synthetic-db"), (legacy, None)):
            reads.extend(fact("ReadFootprint", path=path, access=access,
                recursive=True, domain_id=domain_id) for access in ("ENUMERATE", "CONTENT"))
        writes = tuple(fact("WriteFootprint", role=role, path=getattr(selection, role), recursive=True)
            for role in ("namespace", "candidate_v3", "staging", "reader_work", "safety_backup"))
        arguments = dict(operation_id="synthetic-operation", object_id="synthetic-object",
            read_footprints=tuple(reads), write_footprints=writes,
            configuration_objects=configs, protected_roots=(legacy,), protected_objects=())
        yield SimpleNamespace(producer=producer, base=base, source=source, legacy=legacy,
            selection=selection, legacy_proof=legacy_proof, domains=domains, arguments=arguments)


def prepare(fixture, **changes):
    return fixture.producer.prepare(fixture.selection, fixture.legacy_proof, fixture.domains,
        **{**fixture.arguments, **changes})


def existing_configurations(fixture):
    documents = ({"schema_version": 2, "archive_root": str(fixture.legacy), "known_archive_roots": []},
        {"schema_version": 1, "archives": [], "bootstrap_completed": False})
    configs, reads = [], list(fixture.arguments["read_footprints"])
    for selected, document in zip(fixture.arguments["configuration_objects"], documents):
        content = json.dumps(document).encode("utf8")
        selected.path.write_bytes(content)
        configs.append(replace(selected, expected_digest=hashlib.sha256(content).hexdigest()))
        reads.append(dto.ReadFootprint(selected.path, "CONTENT", False, None))
    fixture.arguments["configuration_objects"] = tuple(configs)
    fixture.arguments["read_footprints"] = tuple(reads)
    fixture.legacy_proof = replace(fixture.legacy_proof,
        settings_digest=dto.contract_digest(tuple((c.kind, c.path, c.expected_digest)
            for c in configs if c.kind == "LEGACY_SETTINGS")),
        registry_digest=dto.contract_digest(tuple((c.kind, c.path, c.expected_digest)
            for c in configs if c.kind == "LEGACY_REGISTRY")))


def existing_namespace(fixture, *, references=()):
    from wechat_exporter.track2_contracts import CandidateRecord, dto_document
    root = fixture.selection.namespace
    root.mkdir()
    for name in ("objects", "operations"):
        (root / name).mkdir()
    root_id = prove_existing_root(root).identity
    settings = dict(schema_version=3, contract_version="track2-v1", profile_id="synthetic-profile",
        purpose="SYNTHETIC", root_id=dto_document(root_id), legacy=dto_document(fixture.legacy_proof))
    candidates = {}
    for index, path in enumerate(references):
        record = CandidateRecord("candidate" + str(index), "other-object", SCOPE, "SYNTHETIC",
            prove_existing_root(path).identity, "READY", "2" * 64, None, "other-operation")
        candidates[record.archive_id] = {"record": dto_document(record), "root": str(path)}
    registry = dict(schema_version=3, contract_version="track2-v1", profile_id="synthetic-profile",
        generation=0, candidates=candidates, preserved_v2={})
    for name, document in (("settings.json", settings), ("registry.json", registry)):
        (root / name).write_bytes(json.dumps(document).encode("utf8"))
    reads = list(fixture.arguments["read_footprints"])
    reads.append(dto.ReadFootprint(root, "ENUMERATE", True, None))
    for path in (root / "settings.json", root / "registry.json",
            root / "objects" / fixture.arguments["object_id"] / "binding.json"):
        reads.append(dto.ReadFootprint(path, "CONTENT", False, None))
        for metadata in (path, *path.parents):
            if not any(r.path == metadata and r.access == "METADATA" for r in reads):
                reads.append(dto.ReadFootprint(metadata, "METADATA", False, None))
    fixture.arguments["read_footprints"] = tuple(reads)
    return root, settings, registry


def bind_metadata_context(approval, selection):
    """Explicit NONAUTHORITATIVE facts for original pure-policy fixture callers.

    Physical selected-root metadata is captured only during fixture setup;
    no issuer attachment, source read grant or executable permission is minted.
    """
    base = selection.input_v2.parent
    missing = {p.destination for p in approval.destination_parent_proofs}
    existing = [("input_v2", approval.input_baseline.root)]
    for role in selection.__dataclass_fields__:
        path = getattr(selection, role)
        if role != "input_v2" and path is not None and path not in missing:
            existing.append((role, prove_existing_root(path)))
    configs = (dto.ConfigurationSelection("LEGACY_SETTINGS", base / "metadata-only-settings.json", None),
        dto.ConfigurationSelection("LEGACY_REGISTRY", base / "metadata-only-registry.json", None))
    parent = __import__("wechat_exporter.physical_paths", fromlist=["prove_existing_object"]).prove_existing_object(base)
    witnesses = tuple(sorted((dto.ProtectedObjectProof(
        "CONFIGURATION_PARENT:" + c.kind + ":" + dto.contract_digest(c.path), parent) for c in configs),
        key=lambda p: (p.owner, str(p.proof.path).casefold())))
    writes = tuple(dto.WriteFootprint(role, getattr(selection, role), False)
        for role in sorted(selection.__dataclass_fields__) if role != "input_v2"
        and getattr(selection, role) is not None)
    context = dto.OperationRootContext("operation-root-v1", "synthetic-metadata-operation",
        approval.object_id, approval.purpose, approval.scope, selection,
        (dto.SourceDomainSelection("synthetic-metadata-db", "DB", base / "metadata-only-source",
            "synthetic-metadata-provider", approval.scope, "1" * 64),),
        tuple(sorted(configs, key=lambda c: (c.kind, str(c.path).casefold()))), (), writes,
        tuple(sorted(existing)), tuple(sorted(approval.destination_parent_proofs,
            key=lambda p: str(p.destination).casefold())), (), witnesses, approval.legacy_namespace_digest,
        "synthetic-metadata-provider", "1" * 64,
        dto.EvidenceFreshness(1, 300_000_000_001, approval.code_digest, "2" * 64,
            dto.contract_digest(None), 0, dto.contract_digest(None)))
    return replace(approval, root_context_digest=dto.contract_digest(context)), context


@contextmanager
def mutation_audit():
    events, enabled = [], [True]
    def audit(event, args):
        if not enabled[0]:
            return
        if event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir"):
            events.append(event)
        elif event == "open" and (bool(args[1]) and any(c in args[1] for c in "wax+")
                or args[2] & (0x1 | 0x2 | 0x40 | 0x200)):
            events.append(event)
    sys.addaudithook(audit)
    try:
        yield events
    finally:
        enabled[0] = False
