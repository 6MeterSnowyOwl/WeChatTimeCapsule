"""Bounded Windows OS authority lock and live, process-local lease issuer.

Zero-byte authority.lock is stable and never unlinked/recreated. LockFileEx
locks the first byte beyond EOF; OS handle close/death releases ownership.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import math
import os
import threading
import time
from typing import Iterator
from . import physical_paths as physical
from .track2_contracts import LockLease, Track2Error, V3Profile
from .v3_profile import _closed, _identifier, _settings, _require


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if _kernel is not None:
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL
_live = {}
_guard = threading.RLock()


def _busy():
    raise Track2Error("WRITER_BUSY")


@contextmanager
def authority_lock(profile: V3Profile, operation_id: str, timeout_seconds: float=30) -> Iterator[LockLease]:
    if (type(timeout_seconds) not in (float, int) or not 0 <= timeout_seconds <= 30
        or not math.isfinite(timeout_seconds) or _kernel is None):
        _busy()
    try:
        _identifier(operation_id)
    except Track2Error:
        _busy()
    document, _ = _settings(profile)
    root = physical.prove_existing_root(profile.namespace_root)
    lock_path = profile.namespace_root / "authority.lock"
    # READ access is sufficient for LockFileEx; share READ keeps handle proofs
    # usable, never share DELETE. OPEN_REPARSE_POINT rejects link substitution.
    handle = _kernel.CreateFileW(str(lock_path), 0x80000000, 1, None, 4, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        _busy()
    acquired, lease = False, None
    overlapped = _OVERLAPPED()
    try:
        lock_identity, standard = physical._query_identity(handle)
        _require(not standard.Directory and standard.EndOfFile == 0, "ROOT_PROOF_FAILED")
        physical._validate_streams(handle, directory=False, size=0)
        deadline = time.monotonic() + timeout_seconds
        while not _kernel.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(overlapped)):
            if ctypes.get_last_error() != 33 or time.monotonic() >= deadline:
                _busy()
            time.sleep(min(.025, max(0, deadline - time.monotonic())))
        acquired = True
        after, _ = physical._query_identity(handle)
        _require(after == lock_identity and physical.prove_existing_root(profile.namespace_root) == root,
                 "ROOT_PROOF_FAILED")
        _require(_settings(profile)[0] == document, "ROOT_PROOF_FAILED")
        lease = LockLease(root.identity, operation_id)
        with _guard:
            _live[id(lease)] = (lease, os.getpid(), threading.get_ident(), profile,
                                handle, lock_identity)
        yield lease
    finally:
        if lease is not None:
            with _guard:
                _live.pop(id(lease), None)
        if acquired:
            _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        _kernel.CloseHandle(handle)


@_closed
def require_live_lease(profile, lease) -> None:
    with _guard:
        evidence = _live.get(id(lease))
        if (evidence is None or evidence[0] is not lease or evidence[1] != os.getpid()
            or evidence[2] != threading.get_ident() or evidence[3] != profile):
            _busy()
        _settings(profile)
        _require(physical.prove_existing_root(profile.namespace_root).identity == lease.namespace_id,
                 "ROOT_PROOF_FAILED")
        identity, _ = physical._query_identity(evidence[4])
        _require(identity == evidence[5], "ROOT_PROOF_FAILED")
