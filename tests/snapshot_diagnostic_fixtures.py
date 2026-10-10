"""Spawn-safe synthetic-only fixtures for the snapshot diagnostic boundary."""
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from history_fixtures import SyntheticProviderDescriptor, SyntheticSnapshotProvider
from wechat_exporter.history_provider import PrivateCapability, PrivateProvider


@dataclass(frozen=True)
class DiagnosticDescriptor(SyntheticProviderDescriptor):
    def build(self, workdir, *, verification=False):
        if self.fault == "build_error":
            raise ValueError("PRIVATE-TEXT-AND-PATH-C:/synthetic-only")
        if self.fault in ("missing_key", "missing_metadata_key"):
            capability = PrivateCapability(self.root, {},
                ("contact.db",) if self.fault == "missing_metadata_key" else (),
                account_id=self.account_id)
            return PrivateProvider(capability, workdir)
        return TechnicalOnlyProvider(self, workdir, verification)


class TechnicalOnlyProvider(SyntheticSnapshotProvider):
    def decode_row(self, raw):
        raise AssertionError("PROBE_MUST_NOT_DECODE_ROWS")

    def freeze(self, relative_db, destination):
        super().freeze(relative_db, destination)
        if self.descriptor.fault == "probe_new_source" and len(self.opened) == 1:
            with sqlite3.connect(self.descriptor.root / "message/message_9.db") as connection:
                connection.execute("CREATE TABLE TechnicalMetadata (version INTEGER)")


class DiagnosticDB:
    def __init__(self, root: Path, **options):
        self._history_provider_descriptor = DiagnosticDescriptor(root, **options)

