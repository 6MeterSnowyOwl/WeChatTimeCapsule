"""Explicit v3-only profiles; legacy metadata is retained, never constructed.

An interrupted first initialization is deliberately fail-closed. No old loader,
default path or repair fallback is used. Paths are private local evidence.
"""
from __future__ import annotations
from functools import wraps
import json
import os
from pathlib import Path
import re
import uuid
from . import physical_paths as physical
from .physical_paths import (prove_existing_root, prove_destination_parent,
    revalidate_destination_parent)
from .track2_contracts import (CONTRACT_VERSION, FileIdentity, LegacyNamespaceProof,
    Track2Error, V3Profile, canonical_bytes, decode_dto, dto_document)


def _closed(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Track2Error:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise Track2Error("TRACK2_RECOVERY_REQUIRED") from None
    return call


def _require(value, code="TRACK2_RECOVERY_REQUIRED"):
    if not value:
        raise Track2Error(code)


def _identifier(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None)
    _require(value.upper().split(".")[0] not in
             {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
              *("LPT" + str(i) for i in range(1, 10))})
    return value


def _key(identity):
    return identity.volume_serial, identity.file_id_128


def _overlap(first, second):
    a, b = _key(first.identity), _key(second.identity)
    return (a == b or first.final_path == second.final_path
        or a in {_key(i) for i in second.ancestor_ids}
        or b in {_key(i) for i in first.ancestor_ids})


def _legacy_handles(legacy):
    _require(type(legacy) is LegacyNamespaceProof, "ROOT_PROOF_FAILED")
    roots = tuple(dict.fromkeys((*legacy.default_roots, *legacy.known_roots)))
    ids = dict(legacy.physical_root_ids)
    _require(bool(roots) and len(ids) == len(legacy.physical_root_ids)
             and set(ids) == set(roots), "ROOT_PROOF_FAILED")
    handles = tuple(prove_existing_root(root) for root in roots)
    _require(all(handle.identity == ids[root] for root, handle in zip(roots, handles)),
             "ROOT_PROOF_FAILED")
    return handles


def _independent(root, legacy):
    handle = prove_existing_root(root)
    _require(not any(_overlap(handle, domain) for domain in _legacy_handles(legacy)),
             "LEGACY_NAMESPACE_OVERLAP")
    return handle


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json_read(path):
    # Caller first validates every namespace entry's handle/link/stream evidence.
    return json.loads(path.read_bytes(), object_pairs_hook=_pairs,
                      parse_constant=lambda _: _require(False))


def _exclusive_json(path, document):
    with path.open("xb") as file:
        file.write(canonical_bytes(document))
        file.flush()
        os.fsync(file.fileno())


def _namespace_root(root):
    """Check every owned entry without reading a byte-locked authority file.

    The authority leaf is empty and has no content to hash. All entries still
    receive real no-follow ID/link/stream checks using Task1's reviewed adapter.
    Namespace metadata validation is not a payload copy/content proof.
    """
    handle = prove_existing_root(root)
    allowed = {"settings.json", "registry.json", "authority.lock", "objects", "operations"}
    for path in root.iterdir():
        # A crash-before-replace leaves an owned registry pending leaf. It is
        # never promoted/read as registry or authority; committed old/new JSON
        # alone is authoritative. No automatic abandoned-file cleanup occurs.
        _require(path.name in allowed or re.fullmatch(r"registry\.json\.pending-[0-9a-f]{32}", path.name))
    seen = set()
    def visit(path):
        with physical._open(path) as opened:
            identity, standard = physical._query_identity(opened)
            key = _key(identity)
            _require(key not in seen, "ROOT_PROOF_FAILED")
            seen.add(key)
            token = physical._change_token(opened)
            physical._validate_streams(opened, directory=bool(standard.Directory), size=standard.EndOfFile)
            if standard.Directory:
                names = sorted(entry.name for entry in os.scandir(path))
                for name in names:
                    visit(path / name)
                _require(names == sorted(entry.name for entry in os.scandir(path)), "ROOT_PROOF_FAILED")
            else:
                _require(path != root / "authority.lock" or standard.EndOfFile == 0, "ROOT_PROOF_FAILED")
            after, current = physical._query_identity(opened)
            _require(identity == after and standard.EndOfFile == current.EndOfFile
                and token == physical._change_token(opened), "ROOT_PROOF_FAILED")
    visit(root)
    _require(prove_existing_root(root) == handle, "ROOT_PROOF_FAILED")
    return handle


@_closed
def _settings(profile):
    _require(type(profile) is V3Profile)
    root = _namespace_root(profile.namespace_root)
    document = _json_read(profile.namespace_root / "settings.json")
    _require(type(document) is dict and set(document) == {
        "schema_version", "contract_version", "profile_id", "purpose", "root_id", "legacy"})
    _require(type(document["schema_version"]) is int and document["schema_version"] == 3
        and document["contract_version"] == CONTRACT_VERSION
        and document["profile_id"] == profile.profile_id
        and document["purpose"] == profile.purpose)
    _identifier(document["profile_id"])
    root_id = decode_dto(FileIdentity, document["root_id"])
    _require(root_id == root.identity, "ROOT_PROOF_FAILED")
    legacy = decode_dto(LegacyNamespaceProof, document["legacy"])
    _independent(profile.namespace_root, legacy)
    # Both files and both owned directories are compulsory even for empty profile.
    _require((profile.namespace_root / "objects").is_dir()
             and (profile.namespace_root / "operations").is_dir()
             and (profile.namespace_root / "registry.json").is_file())
    return document, legacy


@_closed
def open_v3_profile(root: Path, purpose: str, legacy: LegacyNamespaceProof) -> V3Profile:
    _require(isinstance(root, Path) and root.is_absolute(), "ROOT_PROOF_FAILED")
    _require(type(purpose) is str and purpose in ("SYNTHETIC", "REHEARSAL", "FORMAL"))
    domains = _legacy_handles(legacy)
    if not root.exists():
        parent = prove_destination_parent(root)
        # For a missing descendant, a domain ancestor is unsafe. A common parent
        # is allowed: leaf ancestry is checked again after each owned creation.
        _require(not any(_key(domain.identity) in {
            _key(parent.parent.identity), *(_key(i) for i in parent.parent.ancestor_ids)}
            for domain in domains), "LEGACY_NAMESPACE_OVERLAP")
        revalidate_destination_parent(parent)
        current = parent.existing_parent
        for leaf in parent.requested_leaf_names:
            current = current / leaf
            current.mkdir()
            _independent(current, legacy)
        root = current
    handle = _independent(root, legacy)
    _namespace_root(root)
    if not list(root.iterdir()):
        profile = V3Profile(root, uuid.uuid4().hex, purpose)
        (root / "objects").mkdir()
        (root / "operations").mkdir()
        _require(prove_existing_root(root).identity == handle.identity, "ROOT_PROOF_FAILED")
        _exclusive_json(root / "settings.json", {"schema_version": 3,
            "contract_version": CONTRACT_VERSION, "profile_id": profile.profile_id,
            "purpose": purpose, "root_id": dto_document(handle.identity), "legacy": dto_document(legacy)})
        _exclusive_json(root / "registry.json", {"schema_version": 3,
            "contract_version": CONTRACT_VERSION, "profile_id": profile.profile_id,
            "generation": 0, "candidates": {}, "preserved_v2": {}})
    else:
        doc = _json_read(root / "settings.json")
        _require(type(doc) is dict and type(doc.get("profile_id")) is str)
        profile = V3Profile(root, doc["profile_id"], purpose)
    _, loaded_legacy = _settings(profile)
    _require(loaded_legacy == legacy)
    # Lazy dependency avoids importing or constructing a legacy registry.
    from .v3_registry import _load_registry
    _load_registry(profile)
    return profile


@_closed
def read_legacy_namespace(profile: V3Profile) -> LegacyNamespaceProof:
    _, legacy = _settings(profile)
    from .v3_registry import _load_registry
    _load_registry(profile)
    return legacy
