"""Synthetic-only privacy, lifecycle, IPC and count-only snapshot probe tests."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import multiprocessing
import sqlite3
import unittest
from unittest.mock import patch

from history_fixtures import synthetic_history, SyntheticReaderDB
from snapshot_diagnostic_fixtures import DiagnosticDB, DiagnosticDescriptor, TechnicalOnlyProvider
from wechat_exporter import history_contracts, history_reader_worker
from wechat_exporter.conversation_reader import ConversationReader
from wechat_exporter.history_contracts import HistoryError, SourceSetChanged
from wechat_exporter.history_provider import PrivateCapability, PrivateProvider, validate_work_root


class SnapshotDiagnosticTests(unittest.TestCase):
    def reader(self, fixture, *, fault="", private=True, **options):
        work = TemporaryDirectory(prefix="synthetic-diagnostic-work-")
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        db = DiagnosticDB(fixture.operation_root, fault=fault, private=private)
        return ConversationReader(db, fixture.scope, root, **options), root

    def test_legacy_summary_keeps_original_shape(self):
        self.assertEqual(HistoryError("SOURCE_READ_FAILED").to_summary(),
            {"code": "SOURCE_READ_FAILED", "counts": {}, "fields": [], "hashes": {}})

    def test_projection_drops_private_and_unrecognized_values(self):
        project = getattr(history_contracts, "snapshot_diagnostic", None)
        self.assertTrue(callable(project), "closed snapshot diagnostic projection is missing")
        self.assertEqual(project({
            "phase": "shard_snapshot", "branch": "freeze_key_missing",
            "exception_class": "OperationalError", "worker_initialized": True,
            "snapshot_started": False, "snapshot_completed": None,
            "path": "C:/PRIVATE", "message": "PRIVATE", "traceback": "PRIVATE",
        }), {"phase": "shard_snapshot", "branch": "freeze_key_missing",
            "exception_class": "OperationalError", "worker_initialized": True,
            "snapshot_started": False, "snapshot_completed": None})
        self.assertEqual(project({"phase": "C:/PRIVATE", "branch": "PRIVATE",
            "exception_class": "PRIVATE", "worker_initialized": 1,
            "snapshot_started": "true", "snapshot_completed": []}), {})
        self.assertEqual(project("PRIVATE"), {})
        self.assertEqual(project({"exception_class": "HistoryError"}), {"exception_class": "HistoryError"})

    def test_history_error_rehydrates_only_closed_diagnostic(self):
        error = HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
            "phase": "metadata_snapshot", "branch": "freeze_duplicate", "path": "PRIVATE"})
        self.assertEqual(error.to_summary().get("diagnostic"),
            {"phase": "metadata_snapshot", "branch": "freeze_duplicate"})
        self.assertEqual(HistoryError(error.code, error.to_summary()).to_summary(), error.to_summary())

    def test_duplicate_freeze_and_missing_key_have_distinct_branches(self):
        with synthetic_history() as fixture, TemporaryDirectory(prefix="synthetic-provider-") as directory:
            provider = PrivateProvider(PrivateCapability(fixture.operation_root, {},
                account_id=fixture.scope.account_id), Path(directory))
            try:
                with self.assertRaises(HistoryError) as missing:
                    provider.freeze("message/message_0.db", Path(directory) / "frozen.db")
                provider._opened.add("message/message_0.db")
                with self.assertRaises(HistoryError) as duplicate:
                    provider.freeze("message/message_0.db", Path(directory) / "frozen.db")
                self.assertEqual(missing.exception.to_summary().get("diagnostic", {}).get("branch"),
                                 "freeze_key_missing")
                self.assertEqual(duplicate.exception.to_summary().get("diagnostic", {}).get("branch"),
                                 "freeze_duplicate")
            finally:
                provider.close()

    def test_work_root_rejections_identify_actual_branch(self):
        with synthetic_history() as fixture:
            with self.assertRaises(HistoryError) as raised:
                validate_work_root(DiagnosticDescriptor(fixture.operation_root), fixture.operation_root)
            self.assertEqual(raised.exception.to_summary().get("diagnostic", {}).get("branch"), "work_root_overlap")

    def test_spawned_private_admission_error_preserves_false_lifecycle(self):
        with synthetic_history() as fixture:
            reader, root = self.reader(fixture, private=False)
            with self.assertRaises(HistoryError) as raised:
                reader.open_inventory(mode="full")
            self.assertEqual(raised.exception.to_summary().get("diagnostic"), {
                "phase": "worker_admission", "branch": "worker_not_private",
                "exception_class": "HistoryError",
                "worker_initialized": False, "snapshot_started": False, "snapshot_completed": False})
            self.assertEqual(list(root.iterdir()), [])

    def test_spawned_freeze_error_preserves_observed_lifecycle_and_phase(self):
        for fault, phase in (("missing_key", "shard_snapshot"), ("missing_metadata_key", "metadata_snapshot")):
            with self.subTest(fault=fault), synthetic_history() as fixture:
                reader, root = self.reader(fixture, fault=fault)
                with self.assertRaises(HistoryError) as raised:
                    reader.open_inventory(mode="full")
                self.assertEqual(raised.exception.code, "SOURCE_SNAPSHOT_UNSAFE")
                self.assertEqual(raised.exception.to_summary().get("diagnostic"), {
                    "phase": phase, "branch": "freeze_key_missing",
                    "exception_class": "HistoryError",
                    "worker_initialized": True, "snapshot_started": True, "snapshot_completed": False})
                self.assertEqual(list(root.iterdir()), [])

    def test_spawned_generic_error_keeps_original_error_code_and_safe_class(self):
        with synthetic_history() as fixture:
            reader, root = self.reader(fixture, fault="build_error")
            with self.assertRaises(HistoryError) as raised:
                reader.open_inventory(mode="full")
            summary = raised.exception.to_summary()
            self.assertEqual(raised.exception.code, "SOURCE_READ_FAILED")
            self.assertEqual(summary.get("diagnostic"), {
                "phase": "worker_initialization", "exception_class": "ValueError",
                "worker_initialized": False, "snapshot_started": False, "snapshot_completed": False})
            self.assertNotIn("PRIVATE", str(summary))
            self.assertEqual(list(root.iterdir()), [])

    def test_probe_queries_only_schema_integrity_and_exact_target_count(self):
        probe = getattr(history_reader_worker, "run_snapshot_probe", None)
        self.assertTrue(callable(probe), "technical-only snapshot worker operation is missing")
        with synthetic_history() as fixture, TemporaryDirectory(prefix="synthetic-probe-") as directory:
            root = Path(directory)
            cache = root / "cache"; cache.mkdir()
            provider = TechnicalOnlyProvider(DiagnosticDescriptor(fixture.operation_root), cache, False)
            statements = []
            def restricted_open(path):
                from wechat_exporter.history_provider import open_frozen
                connection = open_frozen(path)
                connection.set_trace_callback(statements.append)
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ and table != "sqlite_master":
                        if table != "Msg_synthetic" or column != "":
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                return connection
            try:
                with patch.object(history_reader_worker, "open_frozen", restricted_open):
                    result = probe(provider, root, fixture.scope)
            finally:
                provider.close()
            self.assertEqual(result, {"shards": [
                {"ordinal": 0, "target_present": True, "raw_total": 2, "integrity": "verified"},
                {"ordinal": 1, "target_present": True, "raw_total": 2, "integrity": "verified"},
                {"ordinal": 2, "target_present": False, "raw_total": 0, "integrity": "verified"}]})
            self.assertEqual(sum(statement.upper() == "PRAGMA QUICK_CHECK" for statement in statements), 3)

    def test_spawned_probe_returns_closed_counts_and_cleans_all_private_work(self):
        with synthetic_history() as fixture:
            reader, root = self.reader(fixture)
            probe = getattr(reader, "probe_snapshots", None)
            self.assertTrue(callable(probe), "reader snapshot probe API is missing")
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (fixture.operation_root / "message").iterdir()}
            self.assertEqual(probe(), {"worker_initialized": True, "snapshot_started": True,
                "snapshot_completed": True, "shards": [
                {"ordinal": 0, "target_present": True, "raw_total": 2, "integrity": "verified"},
                {"ordinal": 1, "target_present": True, "raw_total": 2, "integrity": "verified"},
                {"ordinal": 2, "target_present": False, "raw_total": 0, "integrity": "verified"}]})
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (fixture.operation_root / "message").iterdir()})
            self.assertFalse([p for p in multiprocessing.active_children() if p.name.startswith("history-")])

    def test_probe_source_drift_fails_without_retry(self):
        with synthetic_history() as fixture:
            reader, root = self.reader(fixture, fault="probe_new_source")
            probe = getattr(reader, "probe_snapshots", None)
            self.assertTrue(callable(probe), "reader snapshot probe API is missing")
            with self.assertRaises(SourceSetChanged):
                probe()
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_inventory_success_still_decodes_and_cleans(self):
        with synthetic_history() as fixture, TemporaryDirectory(prefix="synthetic-existing-") as directory:
            root = Path(directory)
            db = SyntheticReaderDB(fixture.operation_root)
            with ConversationReader(db, fixture.scope, root).open_inventory(mode="full") as session:
                self.assertEqual(sum(count.decoded for count in session.inventory.counts), 4)
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(db.shared_calls, 0)


if __name__ == "__main__":
    unittest.main()
