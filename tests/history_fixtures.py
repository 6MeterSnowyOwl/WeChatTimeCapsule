"""Operation-local SQLite and decoder doubles; no real backend construction."""
from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from dataclasses import dataclass
import shutil
import time

from wechat_exporter.history_contracts import ArchiveScope, SourceKey
from wechat_exporter.models import ChatMessage


RAW_COLUMNS = (
    "local_id", "local_type", "server_id", "real_sender_id", "create_time",
    "message_content", "source", "packed_info_data", "compress_content", "sort_seq",
)
TARGET_TABLE = "Msg_synthetic"
_SCHEMA = """
CREATE TABLE Msg_synthetic (
    local_id INTEGER PRIMARY KEY, local_type INTEGER, server_id INTEGER,
    real_sender_id INTEGER, create_time INTEGER, message_content TEXT,
    source BLOB, packed_info_data BLOB, compress_content BLOB, sort_seq INTEGER
)
"""
# Four SQL observations, three server IDs; the first ID is beyond float precision.
_ROWS = (
    (
        (1, 1, 9223372036854775807, 1, 1700000000, "synthetic alpha",
         b"\x00synthetic-source\xff", b"\x01\x02", b"\x00\xff", 10),
        (2, 1, 101, 2, 1700000001, "synthetic beta", b"", b"", None, 11),
    ),
    (
        (9, 1, 9223372036854775807, 1, 1700000000, "synthetic alpha",
         b"\x00synthetic-source\xff", b"\x01\x02", b"\x00\xff", 10),
        (10, 1, 102, 2, 1700000002, "synthetic gamma", b"", None, b"", 12),
    ),
)


class SyntheticHistory:
    """Only explicit source enumeration, read-only opening, and fake decoding.

    The fixed incarnation strings are test tokens, not production file identity.
    T2 owns snapshots, storage observations, digests, and retained-row tokens.
    """

    def __init__(self, operation_root: Path):
        self.operation_root = operation_root
        self.scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        self.source_keys = tuple(SourceKey(f"message/message_{index}.db", f"file-{index}")
                                 for index in range(3))
        self._connections: list[sqlite3.Connection] = []
        for index, key in enumerate(self.source_keys):
            path = operation_root / key.relative_db
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as connection:
                if index < len(_ROWS):
                    connection.execute(_SCHEMA)
                    connection.executemany(
                        "INSERT INTO Msg_synthetic VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _ROWS[index],
                    )
                else:
                    connection.execute("CREATE TABLE SupportedShardMetadata (version INTEGER)")
                connection.commit()

    def list_sources(self) -> tuple[SourceKey, ...]:
        return self.source_keys

    def _open(self, relative_db: str) -> sqlite3.Connection:
        if relative_db not in {key.relative_db for key in self.source_keys}:
            raise ValueError("SYNTHETIC_SOURCE_UNKNOWN")
        path = self.operation_root / relative_db
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        self._connections.append(connection)
        return connection

    def raw_rows(self, key: SourceKey) -> tuple[dict, ...]:
        if key not in self.source_keys:
            raise ValueError("SYNTHETIC_SOURCE_UNKNOWN")
        connection = self._open(key.relative_db)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TARGET_TABLE,),
            ).fetchone()
            if not present:
                return ()
            return tuple(dict(row) for row in connection.execute(
                "SELECT " + ",".join(RAW_COLUMNS) + " FROM Msg_synthetic ORDER BY sort_seq,create_time,local_id"
            ))
        finally:
            connection.close()
            self._connections.remove(connection)

    def decode_row(self, raw: dict) -> ChatMessage:
        # Explicit fake decoder: no pinned API calls or ambient settings.
        return ChatMessage.from_upstream({
            "local_id": raw["local_id"], "type": "文本", "sender_id": raw["real_sender_id"],
            "sender_username": "synthetic-sender", "create_time": raw["create_time"],
            "content": raw["message_content"], "sort_seq": raw["sort_seq"],
        })

    def close(self) -> None:
        for connection in self._connections:
            connection.close()
        self._connections.clear()


@contextmanager
def synthetic_history():
    with TemporaryDirectory(prefix="synthetic-history-") as directory:
        fixture = SyntheticHistory(Path(directory))
        try:
            yield fixture
        finally:
            fixture.close()


@dataclass(frozen=True)
class SyntheticProviderDescriptor:
    """Explicit spawn-safe test capability; carries no connections."""
    root: Path
    fault: str = ""
    version: str = "1.2.1"
    private: bool = True
    account_id: str = "synthetic-account"
    shared_cache_root: Path | None = None

    @property
    def storage_root(self):
        return self.root

    def build(self, workdir: Path, *, verification: bool = False):
        return SyntheticSnapshotProvider(self, workdir, verification)


class SyntheticSnapshotProvider:
    def __init__(self, descriptor, workdir, verification):
        self.descriptor = descriptor
        self.workdir = workdir
        self.verification = verification
        self.target_table = TARGET_TABLE
        self.opened = set()
        self.pages = 0
        if descriptor.fault == "hang_init":
            time.sleep(60)

    def list_sources(self):
        from wechat_exporter.history_provider import discover_sources
        if self.descriptor.fault == "listing":
            raise OSError("synthetic discovery failure")
        return discover_sources(self.descriptor.root)

    def freeze(self, relative_db, destination):
        # These faults happen after verification's initial key scan, precisely
        # at the source-open boundary; inventory construction remains normal.
        if self.verification and not self.opened:
            source = self.descriptor.root / relative_db
            if self.descriptor.fault == "verify_disappear_during_freeze":
                source.unlink()
            elif self.descriptor.fault == "verify_replace_during_freeze":
                source.rename(source.with_suffix(".preserved"))
                with closing(sqlite3.connect(source)) as connection:
                    connection.execute("CREATE TABLE Msg_synthetic (different INTEGER)")
            elif self.descriptor.fault == "verify_schema_during_freeze":
                with closing(sqlite3.connect(source)) as connection:
                    connection.execute("ALTER TABLE Msg_synthetic ADD COLUMN extra INTEGER")
            elif self.descriptor.fault == "verify_read_failure":
                raise sqlite3.OperationalError("synthetic verification read failure")
        if self.descriptor.fault == "open":
            raise sqlite3.OperationalError("synthetic open failure")
        if relative_db in self.opened:
            raise AssertionError("PRIVATE_CACHE_REOPENED")
        self.opened.add(relative_db)
        cache = self.workdir / Path(relative_db).name
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.descriptor.root / relative_db, cache)
        with closing(sqlite3.connect(cache)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def prepare_decoder(self):
        pass

    def before_schema(self):
        if self.descriptor.fault == "hang_schema" or (
                self.verification and self.descriptor.fault == "hang_verify"):
            time.sleep(60)

    def decode_row(self, raw):
        if self.descriptor.fault == "decode":
            raise ValueError("synthetic decode failure")
        return ChatMessage.from_upstream({
            "local_id": raw["local_id"], "type": "文本", "sender_id": raw["real_sender_id"],
            "sender_username": "synthetic-sender", "create_time": raw["create_time"],
            "content": raw["message_content"], "sort_seq": raw["sort_seq"],
        })

    def after_page(self):
        self.pages += 1
        if self.pages != 1:
            return
        if self.descriptor.fault == "rewrite_cache":
            for cache in self.workdir.glob("message_*.db"):
                with closing(sqlite3.connect(cache)) as connection:
                    connection.execute("DELETE FROM Msg_synthetic")
                    connection.commit()
        if self.descriptor.fault == "new_source":
            with closing(sqlite3.connect(self.descriptor.root / "message/message_9.db")) as c:
                c.execute("CREATE TABLE Metadata (v INTEGER)")
        if self.descriptor.fault == "lost_source":
            (self.descriptor.root / "message/message_2.db").unlink()
        if self.descriptor.fault == "schema_change":
            with closing(sqlite3.connect(self.descriptor.root / "message/message_0.db")) as c:
                c.execute("ALTER TABLE Msg_synthetic ADD COLUMN extra BLOB")

    def selected_count(self, actual):
        return actual + (self.descriptor.fault == "count")

    def close(self):
        pass


class SyntheticReaderDB:
    def __init__(self, root, **options):
        self._history_provider_descriptor = SyntheticProviderDescriptor(root, **options)
        self.shared_calls = 0

    def _forbidden(self, *args, **kwargs):
        self.shared_calls += 1
        raise AssertionError("SHARED_DATABASE_ACCESS")

    _open = get_nickname = get_messages = get_new_messages = _forbidden


@contextmanager
def synthetic_media_history(*, relocated=False, missing=False):
    """Four colliding SQL rows plus actual pinned local downloader cache files."""
    import hashlib
    from types import SimpleNamespace
    from wechat_exporter.conversation_reader import ConversationReader
    with TemporaryDirectory(prefix="synthetic-media-") as directory:
        root = Path(directory)
        storage, work, account = root / "sources", root / "work", root / "account"
        (storage / "message").mkdir(parents=True)
        work.mkdir()
        account.mkdir()
        scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        expected = (b"\x89PNG\r\n\x1a\nsynthetic-one", b"\x89PNG\r\n\x1a\nsynthetic-two",
                    b"synthetic-video-one", b"synthetic-video-two")
        for index, payload in enumerate(expected):
            digest = hashlib.md5(payload).hexdigest()
            kind = 3 if index < 2 else 43
            raw = (2 if relocated else 1, kind, 101 + index, 1, 1700000000,
                   b"\x00content:" + digest.encode() + b"\xff", b"\x00source\xff",
                   b"\x00packed:" + digest.encode() + b"\xff", b"\x00compressed\xff", 10)
            with closing(sqlite3.connect(storage / f"message/message_{index}.db")) as connection:
                connection.execute(_SCHEMA)
                connection.execute("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", raw)
                connection.commit()
            if not missing:
                if kind == 3:
                    cache = account / "msg/attach" / hashlib.md5(scope.conversation_id.encode()).hexdigest() / "synthetic-month" / (digest + ".dat")
                    payload = bytes(value ^ 0x88 for value in payload)
                else:
                    cache = account / "msg/video/synthetic-month" / (digest + ".mp4")
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(payload)
        db = SyntheticReaderDB(storage)
        db.account_dir, db.workdir = str(account), str(work)
        db.get_message_row = db._forbidden
        reader = ConversationReader(db, scope, work, page_size=1)
        with reader.open_inventory(mode="full") as session:
            yield SimpleNamespace(root=root, db=db, session=session, expected=expected,
                                  scope=scope, output=root / "staging")


def identity_row(*, server="101", local="1", source=0, scope=None,
                 create_time=1700000000, sort_seq=10, raw_type=1,
                 storage=("integer",) * 5, **representation):
    """Pure memory duplicated-server/representation fixture, no storage access."""
    from wechat_exporter.history_contracts import SourceLocator, SourceRow
    selected_scope = scope or ArchiveScope("synthetic-account", "synthetic-conversation")
    fields = dict(local_id=local, message_type="文本", sender_id=1,
                  sender_username="synthetic-sender", create_time=create_time,
                  content="synthetic representation", sort_seq=sort_seq,
                  subtype="", payload={"synthetic": "payload"})
    fields.update(representation)
    locator = SourceLocator(selected_scope,
                            SourceKey(f"message/message_{source}.db", f"file-{source}"),
                            TARGET_TABLE, local, server, create_time, sort_seq,
                            raw_type, storage)
    return SourceRow(ChatMessage(**fields), server, locator, f"synthetic-token-{source}-{local}")


def identity_inventory(*rows, scope=None):
    from wechat_exporter.history_contracts import (
        Inventory, InventoryDigests, SourceCount, SourceDescriptor,
    )
    selected_scope = scope or ArchiveScope("synthetic-account", "synthetic-conversation")
    keys = tuple(dict.fromkeys(row.locator.source_key for row in rows))
    counts = tuple(SourceCount(key, *(sum(row.locator.source_key == key for row in rows),) * 4)
                   for key in keys)
    return Inventory(selected_scope, "full", tuple(rows),
                     tuple(SourceDescriptor(key, TARGET_TABLE, True, "synthetic-schema")
                           for key in keys), counts, (), InventoryDigests("a" * 64, "b" * 64, "c" * 64))


def legacy_record(*, local="1", create_time=1700000000, sort_seq=10, **metadata):
    """Archived synthetic authority with representation deliberately unlike source."""
    from wechat_exporter.archive import ArchivedMessage
    message = identity_row(local=local, create_time=create_time, sort_seq=sort_seq,
                           content="synthetic archived body", message_type="图片",
                           payload={"synthetic": "archived payload"}).message
    return ArchivedMessage(message, "success", "synthetic-archived.png", "images",
                           **metadata)


@contextmanager
def synthetic_acceptance_history():
    """Independent SQL observations and preserved assets for core composition."""
    import hashlib
    from dataclasses import replace
    from types import SimpleNamespace
    from wechat_exporter.conversation_reader import ConversationReader
    with TemporaryDirectory(prefix="synthetic-acceptance-") as directory:
        root = Path(directory)
        storage, work, archive_root = root / "sources", root / "work", root / "preserved"
        (storage / "message").mkdir(parents=True)
        work.mkdir()
        # Duplicate lives on NEW server 103 with another local observation.
        # Neither old exact triple is repeated; I11 remains independently fatal.
        rows = (
            ((1, 1, 101, 1, 10, "synthetic source one", b"", b"", None, 500),
             (4, 1, 103, 1, 20, "synthetic new", b"", b"", None, 900)),
            ((2, 1, 102, 1, 30, "synthetic source two", b"", b"", None, 1),
             (9, 1, 103, 1, 20, "synthetic new", b"", b"", None, 900),
             (5, 1, 104, 1, 40, "synthetic later", b"", b"", None, 2)),
        )
        for index in range(3):
            with closing(sqlite3.connect(storage / f"message/message_{index}.db")) as connection:
                if index < 2:
                    connection.execute(_SCHEMA)
                    connection.executemany("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", rows[index])
                else:
                    connection.execute("CREATE TABLE SupportedShardMetadata (version INTEGER)")
                connection.commit()
        legacy = tuple(replace(legacy_record(local=str(index), create_time=timestamp, sort_seq=seq),
                               media_asset=f"preserved-{index}.png")
                       for index, timestamp, seq in ((1, 10, 500), (2, 30, 1), (3, 50, 20)))
        manifest = {"schema_version": 1, "capsule": "current", "files": {}}
        assets = {}
        for index, record in enumerate(legacy, 1):
            relative = "assets/images/" + record.media_asset
            payload = b"synthetic-preserved-asset-" + str(index).encode()
            assets[relative] = payload
            for path in (archive_root / relative, archive_root / "state" / relative):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            manifest["files"][relative] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        db = SyntheticReaderDB(storage)
        yield SimpleNamespace(root=root, storage=storage, work=work, archive_root=archive_root,
                              db=db, scope=scope, legacy=legacy, manifest=manifest,
                              asset_bytes=assets, staging=root / "staging",
                              reader=ConversationReader(db, scope, work, page_size=1))
