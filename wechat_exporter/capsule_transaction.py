"""Generic transaction for caller-owned, pre-existing synthetic directories.

No product format, root discovery, activation or legacy recovery integration.
The caller must serialize operations and exclude concurrent filesystem writers.
Files are flushed with fsync and journals atomically replaced before installation;
this supports process-interruption recovery, not a power-loss guarantee.
Managed parent directories must already exist. Evidence of unknown operations is
never removed. Committed evidence is retained until explicit verified recovery.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
import uuid

from .history_contracts import HistoryError


_META = ".synthetic-transaction"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", re.I)


def _require(condition: object) -> None:
    if not condition:
        raise HistoryError("TRANSACTION_RECOVERY_REQUIRED")


def _safe_stat(path: Path):
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode) and
             not getattr(info, "st_file_attributes", 0) & 0x400)
    _require(stat.S_ISDIR(info.st_mode) or
             (stat.S_ISREG(info.st_mode) and info.st_nlink == 1))
    return info


def _root(path: Path) -> Path:
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts)
    # Inspect every ancestor before resolve: resolve alone hides reparse points.
    for parent in reversed((path, *path.parents)):
        _require(stat.S_ISDIR(_safe_stat(parent).st_mode))
    return path.resolve(strict=True)


def _names(names) -> tuple[str, ...]:
    _require(isinstance(names, (tuple, list)) and len(names) > 0)
    seen = set()
    for name in names:
        _require(isinstance(name, str) and bool(name))
        parts = name.split("/")
        for part in parts:
            _require(part not in ("", ".", "..") and part[-1:] not in (".", " ")
                     and not re.search(r'[<>:"\\|?*~\x00-\x1f]', part)
                     and not _DEVICE.match(part))
        _require(parts[0].casefold() != _META.casefold())
        key = name.casefold()
        _require(key not in seen and not any(
            key.startswith(other + "/") or other.startswith(key + "/") for other in seen))
        seen.add(key)
    return tuple(names)


def _file(root: Path, name: str, *, required: bool = True) -> Path:
    """Walk exact spellings; reject aliases, non-files and missing parents."""
    current = root
    parts = name.split("/")
    for index, part in enumerate(parts):
        _require(stat.S_ISDIR(_safe_stat(current).st_mode))
        matches = [p.name for p in current.iterdir() if p.name.casefold() == part.casefold()]
        _require(not matches or matches == [part])
        current = current / part
        if not matches:
            _require(index == len(parts) - 1 and not required)
            return current
        info = _safe_stat(current)
        _require(stat.S_ISREG(info.st_mode) if index == len(parts) - 1
                 else stat.S_ISDIR(info.st_mode))
    return current


def _hash(path: Path) -> str:
    _require(stat.S_ISREG(_safe_stat(path).st_mode))
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy(source: Path, destination: Path, expected: str) -> None:
    _require(_hash(source) == expected)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    _require(_hash(destination) == expected)


def _journal(meta: Path, value: dict) -> None:
    # Random exclusive temporary name; never truncate another operation's file.
    temporary = meta / ("journal-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as output:
        output.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf8"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, meta / "journal.json")
    # File fsync after rename also makes a failed fsync abort before install.
    with (meta / "journal.json").open("r+b") as stored:
        os.fsync(stored.fileno())


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _read(root: Path) -> tuple[Path, dict]:
    meta = root / _META
    _require(stat.S_ISDIR(_safe_stat(meta).st_mode))
    path = _file(meta, "journal.json")
    with path.open("r", encoding="utf8") as source:
        value = json.load(source, object_pairs_hook=_object)
    _require(type(value) is dict and set(value) == {"version", "operation_id", "state", "files"})
    _require(type(value["version"]) is int and value["version"] == 1)
    _require(isinstance(value["operation_id"], str) and
             re.fullmatch(r"[0-9a-f]{32}", value["operation_id"]))
    _require(value["state"] in ("PREPARED", "COMMITTED", "ROLLED_BACK"))
    _require(type(value["files"]) is list and len(value["files"]) > 0)
    for entry in value["files"]:
        _require(type(entry) is dict and set(entry) == {"path", "existed", "pre_hash", "post_hash"})
        _require(type(entry["existed"]) is bool)
        _require(isinstance(entry["post_hash"], str) and _HASH.fullmatch(entry["post_hash"]))
        _require((isinstance(entry["pre_hash"], str) and _HASH.fullmatch(entry["pre_hash"]))
                 if entry["existed"] else entry["pre_hash"] is None)
    _names([entry["path"] for entry in value["files"]])
    allowed = {"journal.json"}
    for index, entry in enumerate(value["files"]):
        allowed.add(f"payload-{index}")
        if entry["existed"]:
            allowed.add(f"backup-{index}")
        _file(root, entry["path"], required=False)
    # A process may die while writing a private scratch or next-state journal.
    for child in meta.iterdir():
        _require(child.name in allowed or
                 re.fullmatch(r"(?:scratch|journal)-[0-9a-f]{32}\.tmp", child.name))
        _file(meta, child.name)
    return meta, value


def _verify_preimages(root: Path, entries: list) -> None:
    for entry in entries:
        path = _file(root, entry["path"], required=entry["existed"])
        _require(_hash(path) == entry["pre_hash"] if entry["existed"] else not path.exists())


def _verify_artifacts(meta: Path, entries: list) -> None:
    for index, entry in enumerate(entries):
        if entry["existed"]:
            _require(_hash(_file(meta, f"backup-{index}")) == entry["pre_hash"])
        _require(_hash(_file(meta, f"payload-{index}")) == entry["post_hash"])


def _verify_postimages(root: Path, entries: list, verifier: Callable[[Path], None]) -> None:
    for entry in entries:
        _require(_hash(_file(root, entry["path"])) == entry["post_hash"])
    verifier(root)
    # Treat the injected verifier as read-only, and detect accidental writes too.
    for entry in entries:
        _require(_hash(_file(root, entry["path"])) == entry["post_hash"])


def _install(meta: Path, source: Path, destination: Path, expected: str) -> None:
    scratch = meta / ("scratch-" + uuid.uuid4().hex + ".tmp")
    _copy(source, scratch, expected)
    os.replace(scratch, destination)


def _cleanup(meta: Path) -> None:
    # Called only after full journal/tree and terminal-state verification.
    # Keep terminal journal until last so cleanup interrupted between unlinks can
    # resume without requiring backups already removed by this same cleanup.
    for child in meta.iterdir():
        if child.name != "journal.json":
            child.unlink()
    (meta / "journal.json").unlink()
    meta.rmdir()


@dataclass(frozen=True)
class PreparedSyntheticTransaction:
    operation_id: str
    root: Path
    journal: Path
    hashes: dict[str, str]
    verify_postimage: Callable[[Path], None] = field(repr=False, compare=False)


def prepare_synthetic_transaction(target_root: Path, staging_root: Path,
                                  managed_files: tuple[str, ...],
                                  expected_hashes: dict[str, str], *,
                                  verify_postimage: Callable[[Path], None]
                                  ) -> PreparedSyntheticTransaction:
    try:
        root, stage = _root(target_root), _root(staging_root)
        _require(root != stage and root not in stage.parents and stage not in root.parents)
        names = _names(managed_files)
        _require(callable(verify_postimage) and type(expected_hashes) is dict
                 and set(expected_hashes) == set(names))
        _require(all(isinstance(h, str) and _HASH.fullmatch(h) for h in expected_hashes.values()))
        _require(not any(p.name.casefold() == _META.casefold() for p in root.iterdir()))
        entries = []
        for name in names:
            _require(_hash(_file(stage, name)) == expected_hashes[name])
            target = _file(root, name, required=False)
            existed = target.exists()
            entries.append({"path": name, "existed": existed,
                            "pre_hash": _hash(target) if existed else None,
                            "post_hash": expected_hashes[name]})
        meta = root / _META
        meta.mkdir()  # Exclusive ownership; never creates the supplied target.
        value = {"version": 1, "operation_id": uuid.uuid4().hex,
                 "state": "PREPARED", "files": entries}
        for index, entry in enumerate(entries):
            if entry["existed"]:
                _copy(_file(root, entry["path"]), meta / f"backup-{index}", entry["pre_hash"])
            _copy(_file(stage, entry["path"]), meta / f"payload-{index}", entry["post_hash"])
        _verify_preimages(root, entries)
        _journal(meta, value)
        return PreparedSyntheticTransaction(value["operation_id"], root, meta / "journal.json",
                                            dict(expected_hashes), verify_postimage)
    except Exception:
        # Incomplete preparation leaves orphan evidence, never infers rollback.
        raise HistoryError("TRANSACTION_RECOVERY_REQUIRED") from None


def commit_synthetic_transaction(prepared: PreparedSyntheticTransaction, *,
                                 before_install: Callable[[], None]) -> None:
    try:
        root = _root(prepared.root)
        meta, value = _read(root)
        entries = value["files"]
        _require(prepared.journal == meta / "journal.json" and
                 prepared.operation_id == value["operation_id"] and value["state"] == "PREPARED"
                 and prepared.hashes == {e["path"]: e["post_hash"] for e in entries})
        _verify_preimages(root, entries)
        _verify_artifacts(meta, entries)
        try:
            before_install()
        except Exception:
            # Callback failed before our first replacement; only verified own
            # operation artifacts may be cleaned, never caller staging/target.
            _verify_preimages(root, entries)
            meta, checked = _read(root)
            _require(checked == value)
            value["state"] = "ROLLED_BACK"
            _journal(meta, value)
            _cleanup(meta)
            raise
        _verify_preimages(root, entries)
        meta, checked = _read(root)
        _require(checked == value)
        _verify_artifacts(meta, entries)
        for index, entry in enumerate(entries):
            _install(meta, _file(meta, f"payload-{index}"),
                     _file(root, entry["path"], required=False), entry["post_hash"])
        _verify_postimages(root, entries, prepared.verify_postimage)
        value["state"] = "COMMITTED"
        _journal(meta, value)
    except Exception:
        raise HistoryError("TRANSACTION_RECOVERY_REQUIRED") from None


def recover_synthetic_transaction(target_root: Path, *,
                                  verify_postimage: Callable[[Path], None]) -> bool:
    try:
        root = _root(target_root)
        matches = [p.name for p in root.iterdir() if p.name.casefold() == _META.casefold()]
        if not matches:
            return False
        _require(matches == [_META] and callable(verify_postimage))
        meta, value = _read(root)
        entries = value["files"]
        if value["state"] == "PREPARED":
            # Validate ALL backups and destinations before the FIRST mutation.
            for index, entry in enumerate(entries):
                if entry["existed"]:
                    _require(_hash(_file(meta, f"backup-{index}")) == entry["pre_hash"])
                _file(root, entry["path"], required=False)
            for index, entry in enumerate(entries):
                destination = _file(root, entry["path"], required=False)
                if entry["existed"]:
                    _install(meta, _file(meta, f"backup-{index}"), destination, entry["pre_hash"])
                elif destination.exists():
                    destination.unlink()
            _verify_preimages(root, entries)
            value["state"] = "ROLLED_BACK"
            _journal(meta, value)
        elif value["state"] == "COMMITTED":
            _verify_postimages(root, entries, verify_postimage)
        else:
            _verify_preimages(root, entries)
        _cleanup(meta)
        return True
    except Exception:
        raise HistoryError("TRANSACTION_RECOVERY_REQUIRED") from None
