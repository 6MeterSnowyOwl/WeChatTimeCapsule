"""Synthetic, temporary-only Windows root fixtures. No product services."""
from contextlib import contextmanager
from pathlib import Path
import ctypes
from ctypes import wintypes
import hashlib
import os
import subprocess
import tempfile

from wechat_exporter.track2_contracts import RootSelection, LegacyNamespaceProof

EMPTY_DIGEST = hashlib.sha256(b"synthetic-empty-metadata").hexdigest()

@contextmanager
def synthetic_roots():
    with tempfile.TemporaryDirectory(prefix="synthetic-track2-roots-") as temp:
        base = Path(temp)
        roots = [base / name for name in ("input", "candidate", "namespace", "staging", "reader", "backup")]
        for root in roots:
            root.mkdir()
        (roots[0] / "ordinary.txt").write_bytes(b"synthetic-payload")
        (roots[0] / "empty-directory").mkdir()
        for index in (1, 5):
            (roots[index] / "ordinary.txt").write_bytes(b"synthetic-payload")
            (roots[index] / "empty-directory").mkdir()
        legacy = base / "legacy"
        legacy.mkdir()
        source = base / "source"
        source.mkdir()
        (source / "ordinary-source.bin").write_bytes(b"synthetic-source")
        yield base, RootSelection(*roots), legacy, source

def legacy_proof(root, identity):
    return LegacyNamespaceProof((root,), (), EMPTY_DIGEST, EMPTY_DIGEST, ((root, identity),))

def junction(link, target):
    # Both exact paths must be inside the controlled fixture before cmd is used.
    if link.parent != target.parent or link.exists():
        raise AssertionError("BLOCKED: junction fixture boundary")
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, check=False)
    if result.returncode:
        raise AssertionError("BLOCKED: real junction unavailable")

def symlink(link, target, *, directory):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as error:
        raise AssertionError(f"BLOCKED: real Windows symlink unavailable; winerror={error.winerror}") from None


def native_handle_facts(path):
    """Independent raw Windows queries for local synthetic technical evidence."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise AssertionError("BLOCKED: independent ordinary handle unavailable")
    try:
        outputs = []
        for information_class, size in ((18, 24), (1, 24), (9, 8)):
            buffer = ctypes.create_string_buffer(size)
            if not kernel.GetFileInformationByHandleEx(handle, information_class, buffer, size):
                raise AssertionError("BLOCKED: independent raw handle metadata unavailable")
            outputs.append(buffer.raw)
        file_id, standard, tag = outputs
        return {"volume_serial": int.from_bytes(file_id[:8], "little"),
                "file_id_128": file_id[8:24].hex(),
                "link_count": int.from_bytes(standard[16:20], "little"),
                "directory": bool(standard[21]),
                "attributes": int.from_bytes(tag[:4], "little"),
                "reparse_tag": int.from_bytes(tag[4:8], "little")}
    finally:
        kernel.CloseHandle(handle)
