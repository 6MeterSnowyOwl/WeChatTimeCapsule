"""Spawn-safe explicit plaintext synthetic capability; no real account discovery."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .history_contracts import HistoryError
from .history_provider import discover_sources, open_frozen, require_plain_path
from .models import ChatMessage


@dataclass(frozen=True)
class SyntheticSQLiteDescriptor:
    storage_root: Path
    account_id: str
    target_table: str
    expected_shards: tuple[str, ...]
    shared_cache_root: Path | None = None
    version: str = '1.2.1'
    private: bool = True

    def build(self, workdir: Path, *, verification=False):
        return SyntheticSQLiteProvider(self)


class SyntheticSQLiteProvider:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.target_table = descriptor.target_table

    def list_sources(self):
        sources = discover_sources(self.descriptor.storage_root)
        if tuple(source.relative_db for source in sources) != self.descriptor.expected_shards:
            raise HistoryError('INCOMPLETE_SOURCE_SET')
        return sources

    def freeze(self, relative_db, destination):
        if relative_db not in self.descriptor.expected_shards:
            raise HistoryError('SOURCE_SNAPSHOT_UNSAFE')
        path = self.descriptor.storage_root / relative_db
        require_plain_path(path)
        # Synthetic sources are immutable journal-free databases. Never let a
        # readonly connection create a shared-memory sidecar or omit a WAL.
        if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise HistoryError('SOURCE_SNAPSHOT_UNSAFE')
        with closing(open_frozen(path)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def prepare_decoder(self):
        pass

    def decode_row(self, raw):
        names = {1: '文本', 3: '图片', 43: '视频'}
        if raw['local_type'] not in names:
            raise HistoryError('UNSUPPORTED_SOURCE_SCHEMA')
        return ChatMessage.from_upstream({'local_id': raw['local_id'],
            'type': names[raw['local_type']], 'sender_id': raw['real_sender_id'],
            'sender_username': 'synthetic-sender', 'create_time': raw['create_time'],
            'content': raw['message_content'], 'sort_seq': raw['sort_seq']})

    def close(self):
        pass
