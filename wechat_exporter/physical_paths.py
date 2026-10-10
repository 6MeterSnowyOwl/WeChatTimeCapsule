"""Read-only Windows physical proofs for explicitly selected local roots.

No string-prefix authorization, no fallback identity and no traversal through
reparse points. Open handles provide IDs, standard info, streams, final paths
and content. Snapshots include every ordinary directory, including empty ones.
This is a cooperative runtime check, not a hostile administrator race defense.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import fields
from datetime import datetime, timezone
from functools import wraps
import hashlib
import ntpath
import os
from pathlib import Path
import re
import struct

from .track2_contracts import (
    CONTRACT_VERSION, DestinationParentProof, FileIdentity, LegacyNamespaceProof,
    OwnedEvidencePaths, RootHandleProof, RootProof, RootSelection, Track2Error,
    TreeEntry, TreeSnapshot, canonical_bytes, contract_digest,
    ObjectHandleProof,
)


def _fail(code="ROOT_PROOF_FAILED"):
    raise Track2Error(code)


def _closed(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Track2Error:
            raise
        except (OSError, ValueError, TypeError, OverflowError, KeyError):
            raise Track2Error("ROOT_PROOF_FAILED") from None
    return call


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", ctypes.c_ubyte * 16)]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [("AllocationSize", ctypes.c_longlong), ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wintypes.DWORD), ("DeletePending", ctypes.c_ubyte),
                ("Directory", ctypes.c_ubyte)]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD)]


_INVALID_HANDLE = ctypes.c_void_p(-1).value
_kernel = None
if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel.GetFileType.restype = wintypes.DWORD
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _kernel.ReadFile.restype = wintypes.BOOL


def _local_path(path: Path) -> Path:
    # Normalization is syntax hygiene only. Identity/ancestor handles authorize.
    if not isinstance(path, Path) or not path.is_absolute() or _kernel is None:
        _fail()
    text = str(path)
    drive, tail = ntpath.splitdrive(text)
    if not re.fullmatch(r"[A-Za-z]:", drive) or ":" in tail or "\x00" in text:
        _fail()
    result = Path(ntpath.normpath(text))
    for name in result.parts[1:]:
        if name.endswith((" ", ".")) or any(c in name for c in '<>"|?*'):
            _fail()
    return result


@contextmanager
def _open(path: Path, *, content=False, ancestors=False):
    # OPEN_REPARSE_POINT prevents following the selected entry. Ancestors are
    # inspected individually too. Ancestor handles never impose broad locks.
    access = 0x80000000 if content else 0x80  # GENERIC_READ / FILE_READ_ATTRIBUTES
    handle = _kernel.CreateFileW(str(path), access, 7 if ancestors else 1, None,
                                 3, 0x02000000 | 0x00200000, None)
    if handle == _INVALID_HANDLE or handle is None:
        _fail()
    try:
        if _kernel.GetFileType(handle) != 1:  # FILE_TYPE_DISK only
            _fail()
        yield handle
    finally:
        _kernel.CloseHandle(handle)


def _query(handle, info_class, structure):
    info = structure()
    if not _kernel.GetFileInformationByHandleEx(handle, info_class, ctypes.byref(info), ctypes.sizeof(info)):
        _fail()
    return info


def _query_identity(handle) -> tuple[FileIdentity, _FILE_STANDARD_INFO]:
    info = _query(handle, 18, _FILE_ID_INFO)
    standard = _query(handle, 1, _FILE_STANDARD_INFO)
    tag = _query(handle, 9, _FILE_ATTRIBUTE_TAG_INFO)
    if tag.FileAttributes & (0x400 | 0x40) or tag.ReparseTag or standard.DeletePending:
        _fail()
    if bool(tag.FileAttributes & 0x10) != bool(standard.Directory):
        _fail()
    if standard.EndOfFile < 0 or not standard.NumberOfLinks:
        _fail()
    identity = FileIdentity(int(info.VolumeSerialNumber), bytes(info.FileId).hex(), int(standard.NumberOfLinks))
    if not standard.Directory and identity.link_count != 1:
        _fail()
    return identity, standard


def _final_path(handle) -> str:
    # Normalized volume GUID path, independent of drive-letter/case spelling.
    length = _kernel.GetFinalPathNameByHandleW(handle, None, 0, 1)
    if not length or length > 32768:
        _fail()
    buffer = ctypes.create_unicode_buffer(length + 1)
    received = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 1)
    if not received or received >= len(buffer):
        _fail()
    return buffer.value.casefold()


def _validate_streams(handle, *, directory: bool, size: int) -> None:
    # FileStreamInfo obtains every $DATA stream from this exact open handle.
    # ERROR_HANDLE_EOF is evidence of no directory streams. Any unsupported
    # driver response, malformed record, named stream or unknown data fails.
    capacity = 65536
    while True:
        buffer = ctypes.create_string_buffer(capacity)
        if _kernel.GetFileInformationByHandleEx(handle, 7, buffer, capacity):
            break
        error = ctypes.get_last_error()
        if error == 38 and directory:
            return
        if error in (122, 234) and capacity < 16 * 1024 * 1024:
            capacity *= 2
            continue
        _fail()
    raw = buffer.raw
    offset, streams = 0, []
    while True:
        if offset + 24 > capacity:
            _fail()
        next_offset, name_size, stream_size, _ = struct.unpack_from("<IIqq", raw, offset)
        if name_size % 2 or name_size < 2 or offset + 24 + name_size > capacity or stream_size < 0:
            _fail()
        name = raw[offset + 24: offset + 24 + name_size].decode("utf-16-le")
        streams.append((name, stream_size))
        if not next_offset:
            break
        if next_offset < 24 + name_size or next_offset % 8 or offset + next_offset >= capacity:
            _fail()
        offset += next_offset
    if directory or streams != [("::$DATA", size)]:
        _fail()


def _change_token(handle):
    basic = _query(handle, 0, _FILE_BASIC_INFO)
    return basic.LastWriteTime, basic.ChangeTime, basic.FileAttributes


def _key(identity: FileIdentity) -> tuple[int, str]:
    # Link count is evidence, not part of same-file identity.
    return identity.volume_serial, identity.file_id_128


def _root_handle(path: Path, handle) -> RootHandleProof:
    identity, standard = _query_identity(handle)
    if not standard.Directory:
        _fail()
    _validate_streams(handle, directory=True, size=standard.EndOfFile)
    ancestors, ancestor_paths = [], []
    for parent in path.parents:
        with _open(parent, ancestors=True) as ancestor:
            ancestor_id, parent_info = _query_identity(ancestor)
            if not parent_info.Directory:
                _fail()
            ancestors.append(ancestor_id)
            ancestor_paths.append(_final_path(ancestor))
    if len({_key(i) for i in (identity, *ancestors)}) != len(ancestors) + 1:
        _fail()
    return RootHandleProof(path, identity, _final_path(handle), tuple(ancestors), tuple(ancestor_paths))


@_closed
def prove_existing_root(root: Path) -> RootHandleProof:
    """Root/ancestors only; legacy discovery domains are never recursively read."""
    root = _local_path(root)
    with _open(root) as handle:
        return _root_handle(root, handle)


def _object_handle(path, handle):
    identity, standard = _query_identity(handle)
    _validate_streams(handle, directory=bool(standard.Directory), size=standard.EndOfFile)
    ancestors, ancestor_paths = [], []
    for parent in path.parents:
        with _open(parent, ancestors=True) as ancestor:
            ancestor_id, parent_info = _query_identity(ancestor)
            if not parent_info.Directory:
                _fail()
            ancestors.append(ancestor_id)
            ancestor_paths.append(_final_path(ancestor))
    if len({_key(i) for i in (identity, *ancestors)}) != len(ancestors) + 1:
        _fail()
    return ObjectHandleProof(path, identity, _final_path(handle), tuple(ancestors),
        tuple(ancestor_paths), bool(standard.Directory), int(standard.EndOfFile))


@_closed
def prove_existing_object(path: Path) -> ObjectHandleProof:
    """No-follow ordinary object evidence; files never become directory proofs."""
    path = _local_path(path)
    with _open(path) as handle:
        return _object_handle(path, handle)


@_closed
def read_existing_file(path: Path) -> tuple[ObjectHandleProof, bytes]:
    """Read admitted bytes from the same ordinary no-follow file handle."""
    path = _local_path(path)
    with _open(path, content=True) as handle:
        initial = _object_handle(path, handle)
        if initial.directory:
            _fail()
        token = _change_token(handle)
        chunks, total = [], 0
        buffer, received = ctypes.create_string_buffer(1024 * 1024), wintypes.DWORD()
        while True:
            if not _kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(received), None):
                _fail()
            if not received.value:
                break
            total += received.value
            if total > initial.size:
                _fail()
            chunks.append(buffer.raw[:received.value])
        if total != initial.size or _object_handle(path, handle) != initial or _change_token(handle) != token:
            _fail()
        return initial, b"".join(chunks)


def _read_hash(handle, size: int) -> str:
    digest, total = hashlib.sha256(), 0
    buffer, received = ctypes.create_string_buffer(1024 * 1024), wintypes.DWORD()
    while True:
        if not _kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(received), None):
            _fail()
        if not received.value:
            break
        total += received.value
        if total > size:
            _fail()
        digest.update(buffer.raw[:received.value])
    if total != size:
        _fail()
    return digest.hexdigest()


@_closed
def snapshot_tree(root: Path) -> TreeSnapshot:
    root = _local_path(root)
    entries, seen_ids = [], set()
    with _open(root) as root_handle:
        initial = _root_handle(root, root_handle)

        def visit(path, relative, handle):
            identity, standard = _query_identity(handle)
            key = _key(identity)
            if key in seen_ids:
                _fail()
            seen_ids.add(key)
            token = _change_token(handle)
            _validate_streams(handle, directory=bool(standard.Directory), size=standard.EndOfFile)
            if standard.Directory:
                entries.append(TreeEntry(relative, "directory", None, None, identity))
                with os.scandir(path) as iterator:
                    names = sorted(entry.name for entry in iterator)
                if len({name.casefold() for name in names}) != len(names):
                    _fail()
                for name in names:
                    child = _local_path(path / name)
                    child_relative = name if relative == "." else relative + "/" + name
                    with _open(child, content=True) as child_handle:
                        visit(child, child_relative, child_handle)
                with os.scandir(path) as iterator:
                    if names != sorted(entry.name for entry in iterator):
                        _fail()
            else:
                entries.append(TreeEntry(relative, "file", int(standard.EndOfFile),
                                         _read_hash(handle, standard.EndOfFile), identity))
            after, after_standard = _query_identity(handle)
            if identity != after or standard.EndOfFile != after_standard.EndOfFile or token != _change_token(handle):
                _fail()
            _validate_streams(handle, directory=bool(after_standard.Directory), size=after_standard.EndOfFile)

        visit(root, ".", root_handle)
        if initial != _root_handle(root, root_handle):
            _fail()
    ordered = tuple(sorted(entries, key=lambda entry: entry.relative_path))
    content = tuple((e.relative_path, e.kind, e.size, e.sha256) for e in ordered)
    content_digest = contract_digest((CONTRACT_VERSION, "content-tree", content))
    physical_digest = contract_digest((CONTRACT_VERSION, "physical-tree", initial, ordered))
    return TreeSnapshot(content_digest, physical_digest, len(ordered),
                        sum(e.kind == "file" for e in ordered), datetime.now(timezone.utc).isoformat(),
                        initial, ordered)


def _overlaps(first: RootHandleProof, second: RootHandleProof) -> bool:
    a, b = _key(first.identity), _key(second.identity)
    # Shared common ancestors are fine; either selected root inside the other
    # handle ancestry (or same final handle path) is not fine.
    return (a == b or first.final_path == second.final_path
            or a in {_key(i) for i in second.ancestor_ids}
            or b in {_key(i) for i in first.ancestor_ids})


@_closed
def prove_roots(selection: RootSelection, legacy: LegacyNamespaceProof,
                source_roots: tuple[Path, ...]) -> RootProof:
    if type(selection) is not RootSelection or type(legacy) is not LegacyNamespaceProof or type(source_roots) is not tuple:
        _fail()
    roles = tuple((item.name, getattr(selection, item.name)) for item in fields(selection)
                  if getattr(selection, item.name) is not None)
    if not source_roots or len(set(source_roots)) != len(source_roots):
        _fail()
    sources = tuple(("source:" + str(index), root) for index, root in enumerate(source_roots))
    evidence = tuple((role, prove_existing_root(path)) for role, path in (*roles, *sources))
    domains = tuple(dict.fromkeys((*legacy.default_roots, *legacy.known_roots)))
    legacy_ids = dict(legacy.physical_root_ids)
    if len(legacy_ids) != len(legacy.physical_root_ids) or set(legacy_ids) != set(domains):
        _fail()
    legacy_handles = tuple(prove_existing_root(path) for path in domains)
    for original_path, handle in zip(domains, legacy_handles):
        # The receipt binds the caller's original spelling. The opened handle
        # carries its normalized path; compare against the associated original
        # domain instead of looking up a differently spelled private Path.
        if handle.identity != legacy_ids[original_path]:
            _fail()
    for role, handle in evidence:
        # Preserved input may be registered v2. Outputs and sources must be
        # outside all legacy discovery domains. Equality exception is solely
        # the preserved read-only input, never any writable selected role.
        if role != "input_v2" and any(_overlaps(handle, domain) for domain in legacy_handles):
            _fail("LEGACY_NAMESPACE_OVERLAP")
    for index, (_, handle) in enumerate(evidence):
        if any(_overlaps(handle, other) for _, other in evidence[index + 1:]):
            _fail()
    snapshots = tuple((role, snapshot_tree(path)) for role, path in (*roles, *sources))
    if tuple((role, snapshot.root) for role, snapshot in snapshots) != evidence:
        _fail()
    seen, file_ids = set(), []
    for role, snapshot in snapshots:
        for entry in snapshot.entries:
            key = _key(entry.identity)
            if key in seen:
                _fail()
            seen.add(key)
            if entry.kind == "file":
                file_ids.append((role, entry.relative_path, entry.identity))
    # Reopen all selected identities at the end; no proof survives replacement.
    if evidence != tuple((role, prove_existing_root(path)) for role, path in (*roles, *sources)):
        _fail()
    root_ids = tuple((role, handle.identity) for role, handle in evidence)
    ancestor_ids = tuple((role, handle.ancestor_ids) for role, handle in evidence)
    topology = contract_digest((CONTRACT_VERSION, "root-topology", evidence, legacy_handles))
    return RootProof(selection, root_ids, ancestor_ids,
                     contract_digest((CONTRACT_VERSION, "disjoint-file-ids", tuple(file_ids))), topology,
                     source_roots, evidence, snapshots, contract_digest(legacy))


@_closed
def revalidate_root_proof(proof: RootProof, legacy: LegacyNamespaceProof) -> RootProof:
    if type(proof) is not RootProof or contract_digest(legacy) != proof.legacy_namespace_digest:
        _fail()
    current = prove_roots(proof.selection, legacy, proof.source_roots)
    if (current.topology_digest != proof.topology_digest or current.root_ids != proof.root_ids
            or current.ancestor_ids != proof.ancestor_ids):
        _fail()
    return current


def _exists_exact(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    # Permission or unsupported errors propagate to the fail-closed boundary.


@_closed
def prove_destination_parent(destination: Path) -> DestinationParentProof:
    destination = _local_path(destination)
    if _exists_exact(destination):
        _fail()
    missing, parent = [], destination
    while not _exists_exact(parent):
        missing.append(parent.name)
        if parent.parent == parent:
            _fail()
        parent = parent.parent
    proof = prove_existing_root(parent)
    # All missing intermediate leaves are approval-bound, not just final leaf.
    if any(_exists_exact(path) for path in (destination, *destination.parents)
           if path != parent and path not in parent.parents):
        _fail()
    return DestinationParentProof(destination, parent, tuple(reversed(missing)), proof, True)


@_closed
def revalidate_destination_parent(proof: DestinationParentProof) -> DestinationParentProof:
    if type(proof) is not DestinationParentProof:
        _fail()
    current = prove_destination_parent(proof.destination)
    if current != proof:
        _fail()
    return current


@_closed
def owned_evidence_paths(proof: RootProof, operation_id: str) -> OwnedEvidencePaths:
    """Derive the only permitted contained evidence; caller cannot redirect T7."""
    if type(proof) is not RootProof or type(operation_id) is not str:
        _fail()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
        _fail()
    ids = dict(proof.root_ids)
    return OwnedEvidencePaths(proof.selection.candidate_v3 / ".synthetic-transaction",
                              proof.selection.namespace / "operations" / operation_id,
                              ids["candidate_v3"], ids["namespace"])
