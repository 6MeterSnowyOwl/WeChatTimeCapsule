"""Private authenticated capability bridge; no backend constructor or key discovery."""
from __future__ import annotations
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from importlib.metadata import version
import inspect
import os
from pathlib import Path
import re
import sqlite3
import stat
from types import MethodType
from .history_contracts import HistoryError, SourceKey
from .models import ChatMessage


def require_plain_path(path: Path) -> None:
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "path_validation", "branch": "path_reparse_point"})


def validate_work_root(descriptor, work_root: Path) -> None:
    """Check known physical source/cache roots before creating any operation dir.

    Archive roots are deliberately not discovered here: the caller must supply
    an independent work root, and future save wiring must verify that boundary.
    """
    try:
        require_plain_path(work_root)
        working = work_root.resolve(strict=True)
        storage = getattr(descriptor,"storage_root",None)
        if storage is None:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "work_root_validation", "branch": "work_storage_missing"})
        for root in (storage,getattr(descriptor,"shared_cache_root",None)):
            if root is None:
                continue
            path = Path(root).absolute()
            # Existing ancestors must not hide a junction even for a not-yet
            # created shared cache directory.
            ancestor = path
            while not ancestor.exists():
                ancestor = ancestor.parent
            require_plain_path(ancestor)
            protected = path.resolve()
            if working == protected or working.is_relative_to(protected) or protected.is_relative_to(working):
                raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                    "phase": "work_root_validation", "branch": "work_root_overlap"})
    except OSError as error:
        raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "work_root_validation", "branch": "work_path_oserror",
            "exception_class": type(error).__name__}) from None


def discover_sources(storage_root: Path) -> tuple[SourceKey, ...]:
    """Strict fresh listing; identity deliberately excludes mtime/size/WAL."""
    try:
        directory = storage_root / "message"
        require_plain_path(directory)
        result = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not re.fullmatch(r"message_[0-9]+\.db", entry.name):
                    continue
                path = directory / entry.name
                require_plain_path(path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or not info.st_ino:
                    raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                        "phase": "source_discovery", "branch": "source_not_regular"})
                token = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()
                result.append(SourceKey("message/" + entry.name, token))
        return tuple(sorted(result, key=lambda item: item.relative_db))
    except OSError:
        raise HistoryError("SOURCE_DISCOVERY_FAILED") from None


def open_frozen(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    def decode(value):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    connection.text_factory = decode
    return connection


def pinned_api():
    if version("wechatauto-replica") != "1.2.1":
        raise HistoryError("UNSUPPORTED_UPSTREAM_VERSION")
    from wechatauto.param import WxParam
    WxParam.ENABLE_FILE_LOGGER = False
    from wechatauto.db import WeChatDB
    signatures = {"_open": ("self", "rel"), "_msg_row_to_dict": ("self", "r"),
                  "_sender_id_index": ("self",), "get_nickname": ("self", "user"),
                  "_db_path": ("self", "rel"), "_wal_path": ("self", "rel"),
                  "_decrypt_file": ("self", "src", "dst", "key"),
                  "_merge_wal": ("self", "dst", "wal_path", "key", "from_frame"),
                  "_check_merged": ("dst",)}
    for name, parameters in signatures.items():
        method = getattr(WeChatDB, name, None)
        if not callable(method) or tuple(inspect.signature(method).parameters) != parameters:
            raise HistoryError("UNSUPPORTED_UPSTREAM_API")
    return WeChatDB


@dataclass(frozen=True, repr=False)
class PrivateCapability:
    storage_root: Path
    keys: dict = field(repr=False)
    metadata: tuple[str, ...] = ()
    version: str = "1.2.1"
    private: bool = True
    account_id: str = ""
    shared_cache_root: Path | None = None

    def build(self, workdir: Path, *, verification: bool = False):
        return PrivateProvider(self, workdir)


@dataclass(frozen=True, repr=False)
class AuthenticatedHistoryDB:
    """Caller-supplied verified runtime binding; not inferred from a directory.

    Construction asserts the caller already authenticated this account. This
    adapter neither discovers nor authenticates accounts. Production save wiring
    must explicitly supply the binding in a later reviewed integration.
    """
    base_db: object
    account_id: str


def capability_descriptor(db):
    """Copy authentication in memory; never invoke shared database methods."""
    descriptor = getattr(db, "_history_provider_descriptor", None)
    if descriptor is not None:
        return descriptor  # Explicit synthetic injection, never an implicit fallback.
    upstream = pinned_api()
    if not isinstance(db, AuthenticatedHistoryDB) or not db.account_id:
        raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "capability_binding", "branch": "binding_missing"})
    if type(db.base_db) is not upstream:
        raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "capability_binding", "branch": "runtime_type_mismatch"})
    state = vars(db.base_db)
    if not isinstance(state.get("_keys"), dict) or not isinstance(state.get("_db_files"), (list, tuple)):
        raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "capability_binding", "branch": "runtime_state_shape"})
    if not isinstance(state.get("account_dir"), str) or not isinstance(state.get("workdir"), str):
        raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "capability_binding", "branch": "runtime_path_shape"})
    keys = {}
    for relative, key in state["_keys"].items():
        if not isinstance(relative, str) or not isinstance(key, bytes) or len(key) not in (32, 48):
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "capability_binding", "branch": "capability_key_invalid"})
        keys[relative.replace("\\", "/")] = key
    metadata = []
    for relative, *_ in state["_db_files"]:
        relative = str(relative).replace("\\", "/")
        parts = relative.split("/")
        if parts[-1] in ("message_resource.db", "contact.db"):
            if any(part in ("", ".", "..") or ":" in part for part in parts):
                raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                    "phase": "capability_binding", "branch": "metadata_path_invalid"})
            metadata.append(relative)
    return PrivateCapability(Path(state["account_dir"]) / "db_storage", keys, tuple(metadata),
                             account_id=db.account_id,shared_cache_root=Path(state["workdir"]))


class PrivateProvider:
    """Exporter-owned object delegates only audited pinned upstream internals."""
    def __init__(self, descriptor: PrivateCapability, workdir: Path):
        self.api = pinned_api()
        self.descriptor = descriptor
        self.workdir = str(workdir)
        self._keys = {name.replace("/", os.sep): value for name, value in descriptor.keys.items()}
        self._db_files, self._images, self._opened = [], {}, set()
        self._sender_id_cache = None
        self.WAL_HEADER_SZ, self.WAL_FRAME_SZ = self.api.WAL_HEADER_SZ, self.api.WAL_FRAME_SZ
        for name in ("_wal_path", "_decrypt_file", "_merge_wal", "_sender_id_index", "get_nickname"):
            setattr(self, name, MethodType(getattr(self.api, name), self))
        self._check_merged = self.api._check_merged

    def list_sources(self):
        return discover_sources(self.descriptor.storage_root)

    def _db_path(self, relative):
        return str(self.descriptor.storage_root / relative)

    def freeze(self, relative_db, destination):
        canonical = relative_db.replace("\\", "/")
        relative = canonical.replace("/", os.sep)
        if canonical in self._opened:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "shard_snapshot", "branch": "freeze_duplicate"})
        if relative not in self._keys:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "shard_snapshot", "branch": "freeze_key_missing"})
        require_plain_path(self.descriptor.storage_root / canonical)
        self._opened.add(canonical)
        # Missing keys never reach upstream's diagnostic/process-discovery path.
        with closing(self.api._open(self, relative)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        self._images[canonical] = destination

    def _open(self, relative):
        path = self._images.get(relative.replace("\\", "/"))
        if path is None:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "inventory_decode", "branch": "decoder_image_missing"})
        return open_frozen(path)

    def prepare_decoder(self):
        for index, relative in enumerate(self.descriptor.metadata):
            self.freeze(relative, Path(self.workdir).parent / f"metadata-{index}.db")
            self._db_files.append((relative, str(self.descriptor.storage_root / relative), 0))

    def decode_row(self, raw):
        return ChatMessage.from_upstream(self.api._msg_row_to_dict(self, raw))

    def close(self):
        self._keys.clear()
        self._images.clear()
