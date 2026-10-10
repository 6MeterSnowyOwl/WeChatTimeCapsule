"""Synthetic completeness, immutable snapshot and fail-closed regressions."""
from contextlib import closing
from dataclasses import replace
import hashlib
import multiprocessing
from pathlib import Path
import shutil
import sqlite3
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from history_fixtures import synthetic_history, SyntheticReaderDB, _SCHEMA, _ROWS
from wechat_exporter.conversation_reader import ConversationReader
from wechat_exporter.history_contracts import ArchiveScope, HistoryError, SourceSetChanged


class HistoricalReaderTests(unittest.TestCase):
    def test_verification_drift_during_freeze_precedes_open_and_schema_errors(self):
        for fault in ("verify_disappear_during_freeze", "verify_replace_during_freeze"):
            with self.subTest(fault=fault), synthetic_history() as f:
                reader, db, root = self.reader(f, fault=fault)
                with self.assertRaises(SourceSetChanged) as raised:
                    with reader.open_inventory(mode="full"):
                        pass
                self.assertEqual(raised.exception.code,"SOURCE_SET_CHANGED")
                self.assertEqual(db.shared_calls,0)
                self.assertEqual(list(root.iterdir()),[])
                self.assertFalse([p for p in multiprocessing.active_children() if p.name.startswith("history-")])

    def test_verification_same_instance_errors_remain_non_drift(self):
        for fault, expected in (("verify_schema_during_freeze","SOURCE_SCHEMA_CHANGED"),
                                ("verify_read_failure","SOURCE_READ_FAILED")):
            with self.subTest(fault=fault), synthetic_history() as f:
                reader, db, root = self.reader(f, fault=fault)
                with self.assertRaises(HistoryError) as raised:
                    with reader.open_inventory(mode="full"):
                        pass
                self.assertNotIsInstance(raised.exception,SourceSetChanged)
                self.assertEqual(raised.exception.code,expected)
                self.assertEqual(db.shared_calls,0)
                self.assertEqual(list(root.iterdir()),[])
                self.assertFalse([p for p in multiprocessing.active_children() if p.name.startswith("history-")])

    def test_source_and_shared_cache_work_roots_rejected_before_mutation(self):
        for location in ("source", "shared_cache"):
            with self.subTest(location=location), synthetic_history() as f:
                reader, db, _ = self.reader(f)
                source = f.operation_root
                if location == "source":
                    reader.work_root = source
                else:
                    cache = reader.work_root  # Separate from source: catches cache-specific guard.
                    db._history_provider_descriptor = replace(db._history_provider_descriptor,shared_cache_root=cache)
                    reader.work_root = cache
                before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source/"message").iterdir()}
                entries = set(reader.work_root.iterdir())
                with patch("wechat_exporter.conversation_reader.TemporaryDirectory",wraps=TemporaryDirectory) as create:
                    with self.assertRaises(HistoryError) as raised:
                        with reader.open_inventory(mode="full"):
                            pass
                    create.assert_not_called()
                self.assertEqual(raised.exception.code,"SOURCE_SNAPSHOT_UNSAFE")
                self.assertEqual(set(reader.work_root.iterdir()),entries)
                self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source/"message").iterdir()})

    def test_worker_cleanup_uses_one_grace_deadline(self):
        from wechat_exporter.conversation_reader import _Worker
        class Pipe:
            def send_bytes(self,value): pass
            def close(self): pass
        class Process:
            def __init__(self): self.clock=0.; self.joins=[]; self.killed=False
            def is_alive(self): return not self.killed
            def join(self,timeout): self.joins.append(timeout); self.clock += timeout
            def terminate(self): pass
            def kill(self): self.killed=True
            def close(self): pass
        worker = object.__new__(_Worker)
        worker.pipe, worker.process, worker.grace = Pipe(), Process(), 5.
        with patch("wechat_exporter.conversation_reader.time.monotonic",side_effect=lambda:worker.process.clock):
            worker.close()
        self.assertLessEqual(sum(worker.process.joins),5.)
        self.assertFalse(worker.process.is_alive())

    def test_quick_local_id_must_be_unique_outside_window_too(self):
        with synthetic_history() as f:
            reader, _, _ = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                cursor = replace(s.inventory.next_cursors[0],overlap_seq=0)
            with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                c.execute("ALTER TABLE Msg_synthetic RENAME TO old")
                c.execute(_SCHEMA.replace(" PRIMARY KEY", ""))
                c.execute("INSERT INTO Msg_synthetic SELECT * FROM old")
                duplicate = list(_ROWS[0][0]); duplicate[0] = 2
                c.execute("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)",duplicate); c.commit()
            with self.assertRaises(HistoryError) as raised:
                with reader.open_inventory(mode="quick",cursors=(cursor,)):
                    pass
            self.assertEqual(raised.exception.code,"SOURCE_ROW_INVALID")

    def test_constraint_change_is_schema_drift(self):
        with synthetic_history() as f:
            reader, _, root = self.reader(f)
            with reader.open_inventory(mode="full") as session:
                with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                    c.execute("CREATE UNIQUE INDEX additional_constraint ON Msg_synthetic(server_id)")
                with self.assertRaises(HistoryError) as raised:
                    session.assert_source_set_stable()
                self.assertEqual(raised.exception.code,"SOURCE_SCHEMA_CHANGED")
            self.assertEqual(list(root.iterdir()),[])

    def test_replaced_source_precedes_schema_error(self):
        with synthetic_history() as f:
            reader, _, root = self.reader(f)
            with reader.open_inventory(mode="full") as session:
                original = f.operation_root / "message/message_0.db"
                original.rename(original.with_suffix(".preserved"))
                with closing(sqlite3.connect(original)) as c:
                    c.execute("CREATE TABLE Msg_synthetic (different INTEGER)")
                with self.assertRaises(SourceSetChanged):
                    session.assert_source_set_stable()
            self.assertEqual(list(root.iterdir()),[])

    def test_missing_explicit_binding_and_strict_listing(self):
        from wechat_exporter.history_provider import capability_descriptor, discover_sources
        with self.assertRaises(HistoryError) as raised:
            capability_descriptor(object())
        self.assertEqual(raised.exception.code,"SOURCE_SNAPSHOT_UNSAFE")
        with synthetic_history() as f:
            with self.assertRaises(HistoryError) as raised:
                discover_sources(f.operation_root / "does-not-exist")
            self.assertEqual(raised.exception.code,"SOURCE_DISCOVERY_FAILED")

    def test_parent_open_failure_reaps_worker_and_cleans_images(self):
        with synthetic_history() as f:
            reader, _, root = self.reader(f)
            with patch("wechat_exporter.conversation_reader.open_frozen", side_effect=sqlite3.OperationalError("synthetic")):
                with self.assertRaises(HistoryError) as raised:
                    reader.open_inventory(mode="full")
            self.assertEqual(raised.exception.code,"SOURCE_READ_FAILED")
            self.assertEqual(list(root.iterdir()),[])
            self.assertFalse([p for p in multiprocessing.active_children() if p.name.startswith("history-")])

    def test_scope_binding_mismatch_fails_before_source_read(self):
        with synthetic_history() as f:
            reader, db, root = self.reader(f)
            reader.scope = ArchiveScope("different-account", f.scope.conversation_id)
            with self.assertRaises(HistoryError) as raised:
                with reader.open_inventory(mode="full"):
                    pass
            self.assertEqual(raised.exception.code, "IDENTITY_SCOPE_MISMATCH")
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(db.shared_calls, 0)

    def test_private_pinned_decoder_frozen_metadata_and_missing_key_guard(self):
        from wechat_exporter.history_provider import PrivateCapability, PrivateProvider
        with synthetic_history() as f:
            for name, statement, insert in (
                ("contact.db", "CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)",
                 "INSERT INTO contact VALUES ('3','synthetic-nickname','')"),
                ("message_resource.db", "CREATE TABLE SenderName2Id(user_name TEXT)",
                 "INSERT INTO SenderName2Id(rowid,user_name) VALUES (1,'synthetic-index')")):
                with closing(sqlite3.connect(f.operation_root / name)) as c:
                    c.execute(statement); c.execute(insert); c.commit()
            root = f.operation_root / "private"; root.mkdir()
            capability = PrivateCapability(f.operation_root,
                {name:b"k"*32 for name in ("contact.db","message_resource.db")},
                ("contact.db","message_resource.db"), account_id=f.scope.account_id)
            provider = PrivateProvider(capability, root)
            # Encryption is the only replaced boundary; real pinned _open,
            # decoder, sender index and nickname queries all run privately.
            provider._decrypt_file = lambda source,destination,key: shutil.copyfile(source,destination)
            try:
                provider.prepare_decoder()
                for name, table in (("contact.db","contact"),("message_resource.db","SenderName2Id")):
                    with closing(sqlite3.connect(root/name)) as c:
                        c.execute(f"DELETE FROM {table}"); c.commit()
                raw = dict(zip(__import__('history_fixtures').RAW_COLUMNS,_ROWS[0][0]))
                self.assertEqual(provider.decode_row(raw).sender_username,"synthetic-index")
                raw["real_sender_id"] = 3
                self.assertEqual(provider.decode_row(raw).sender_username,"synthetic-nickname")
                with patch.object(provider.api,"_auto_diagnose_key_failure",side_effect=AssertionError("DIAGNOSTIC")) as diagnose, \
                     patch.object(provider.api,"extract_keys",side_effect=AssertionError("KEY_DISCOVERY")) as extract:
                    with self.assertRaises(HistoryError) as raised:
                        provider.freeze("message/message_0.db",root/"missing.db")
                    self.assertEqual(raised.exception.code,"SOURCE_SNAPSHOT_UNSAFE")
                    self.assertEqual((diagnose.call_count,extract.call_count),(0,0))
            finally:
                provider.close()

    def test_pinned_api_signature_gate(self):
        from wechat_exporter.history_provider import pinned_api
        from wechatauto.db import WeChatDB
        with patch.object(WeChatDB,"_open",lambda self: None):
            with self.assertRaises(HistoryError) as raised:
                pinned_api()
            self.assertEqual(raised.exception.code,"UNSUPPORTED_UPSTREAM_API")

    def test_attempt_deadline_does_not_reset_at_verification(self):
        with synthetic_history() as f:
            reader, _, root = self.reader(f,deadline_seconds=3)
            session = reader.open_inventory(mode="full")
            time.sleep(3)
            with self.assertRaises(HistoryError) as raised:
                session.assert_source_set_stable()
            self.assertEqual(raised.exception.code,"SOURCE_READ_TIMEOUT")
            self.assertEqual(list(root.iterdir()),[])

    def reader(self, fixture, **options):
        db = SyntheticReaderDB(fixture.operation_root, fault=options.pop("fault", ""),
                               version=options.pop("version", "1.2.1"),
                               private=options.pop("private", True))
        work = TemporaryDirectory(prefix="synthetic-history-work-")
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        return ConversationReader(db, fixture.scope, root, **options), db, root

    def test_all_shards_and_oldest_first_input(self):
        with synthetic_history() as f:
            with closing(sqlite3.connect(f.operation_root / "message/message_1.db")) as c:
                row = list(_ROWS[1][1]); row[0] = 11; row[2] = 103
                c.execute("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", row); c.commit()
            reader, db, root = self.reader(f, page_size=1)
            with reader.open_inventory(mode="full") as session:
                inv = session.inventory
                self.assertEqual((len(inv.sources), sum(c.raw_total for c in inv.counts),
                                  sum(c.decoded for c in inv.counts)), (3, 5, 5))
                self.assertEqual([r.message.local_id for r in inv.rows], ["1", "2", "9", "10", "11"])
                raw = session.resolve_raw_row(inv.rows[0].raw_row_token)
                self.assertEqual(raw["source"], b"\x00synthetic-source\xff")
                self.assertEqual(raw["server_id"], 9223372036854775807)
                self.assertEqual(inv.rows[0].locator.storage_classes, ("integer",)*5)
                session.assert_source_set_stable()
            self.assertEqual(db.shared_calls, 0)
            self.assertEqual(list(root.iterdir()), [])
            with self.assertRaises(HistoryError):
                session.resolve_raw_row(inv.rows[0].raw_row_token)

    def test_tie_bucket_exceeds_page_size(self):
        with synthetic_history() as f:
            with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                c.execute("DELETE FROM Msg_synthetic")
                rows = [(i,1,1000+i,2,1700000000,"synthetic",b"",b"",None,42) for i in range(1,1202)]
                c.executemany("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", rows); c.commit()
            reader, _, _ = self.reader(f, page_size=200)
            with reader.open_inventory(mode="full") as session:
                self.assertEqual((session.inventory.counts[0].enumerated,
                                  session.inventory.counts[0].decoded), (1201,1201))
                self.assertEqual(session.inventory.next_cursors[0].high_watermark, (42,1700000000,1201))

    def test_snapshot_pages_ignore_cache_reopen(self):
        with synthetic_history() as f:
            reader, _, _ = self.reader(f, page_size=1, fault="rewrite_cache")
            with reader.open_inventory(mode="full") as s:
                self.assertEqual(len(s.inventory.rows), 4)
                self.assertEqual(s.resolve_raw_row(s.inventory.rows[1].raw_row_token)["server_id"], 101)

    def test_version_and_snapshot_gate(self):
        for options, expected in (({"version":"9"},"UNSUPPORTED_UPSTREAM_VERSION"),
                                  ({"private":False},"SOURCE_SNAPSHOT_UNSAFE")):
            with self.subTest(expected=expected), synthetic_history() as f:
                reader, _, root = self.reader(f, **options)
                with self.assertRaises(HistoryError) as raised:
                    reader.open_inventory(mode="full")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(list(root.iterdir()), [])

    def test_listing_open_schema_decode_count_and_timeout_fail_closed(self):
        cases = (("listing","SOURCE_DISCOVERY_FAILED"), ("open","SOURCE_READ_FAILED"),
                 ("missing_column","UNSUPPORTED_SOURCE_SCHEMA"), ("schema_change","SOURCE_SCHEMA_CHANGED"),
                 ("decode","SOURCE_READ_FAILED"), ("count","SOURCE_COUNT_MISMATCH"),
                 ("hang_init","SOURCE_READ_TIMEOUT"), ("hang_schema","SOURCE_READ_TIMEOUT"),
                 ("hang_verify","SOURCE_READ_TIMEOUT"))
        for fault, expected in cases:
            with self.subTest(fault=fault), synthetic_history() as f:
                if fault == "missing_column":
                    with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                        c.execute("ALTER TABLE Msg_synthetic DROP COLUMN server_id")
                before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (f.operation_root/"message").iterdir()}
                reader, db, root = self.reader(f, fault=fault,
                    deadline_seconds=1.5 if fault.startswith("hang") else 30, worker_grace_seconds=.5)
                start = time.monotonic()
                with self.assertRaises(HistoryError) as raised:
                    reader.open_inventory(mode="full")
                self.assertEqual(raised.exception.code, expected)
                self.assertLess(time.monotonic()-start, 12)
                self.assertEqual(db.shared_calls, 0)
                self.assertEqual(list(root.iterdir()), [])
                self.assertFalse([p for p in multiprocessing.active_children() if p.name.startswith("history-")])
                if fault != "schema_change":
                    self.assertEqual(before, {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (f.operation_root/"message").iterdir()})

    def test_source_set_checked_after_inventory(self):
        for fault in ("new_source", "lost_source"):
            with self.subTest(fault=fault), synthetic_history() as f:
                reader, _, root = self.reader(f, fault=fault)
                with self.assertRaises(SourceSetChanged):
                    reader.open_inventory(mode="full")
                self.assertEqual(list(root.iterdir()), [])

    def test_quick_inclusive_overlap_and_growth_is_not_drift(self):
        with synthetic_history() as f:
            reader, _, _ = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                cursor = s.inventory.next_cursors[0]
                with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                    row = list(_ROWS[0][1]); row[0]=0; row[2]=99
                    c.execute("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", row); c.commit()
                s.assert_source_set_stable()
                self.assertEqual(len(s.inventory.rows), 4)
            with reader.open_inventory(mode="quick", cursors=(replace(cursor, overlap_seq=0),)) as s:
                self.assertEqual((s.inventory.counts[0].raw_total,s.inventory.counts[0].raw_selected), (3,2))
                self.assertEqual([r.message.local_id for r in s.inventory.rows[:2]], ["0","2"])

    def test_digests_bind_raw_bytes_and_scope(self):
        with synthetic_history() as f:
            reader, _, _ = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                first = s.inventory.digests
            with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                c.execute("UPDATE Msg_synthetic SET source=? WHERE local_id=1", (b"changed",)); c.commit()
            with reader.open_inventory(mode="full") as s:
                self.assertEqual(s.inventory.digests.source_set_digest, first.source_set_digest)
                self.assertNotEqual(s.inventory.digests.source_rows_digest, first.source_rows_digest)
                self.assertEqual(s.inventory.digests.scope_digest, first.scope_digest)

    def test_noninteger_and_duplicate_local_id_rejected(self):
        for fault in ("text_time", "duplicate"):
            with self.subTest(fault=fault), synthetic_history() as f:
                with closing(sqlite3.connect(f.operation_root / "message/message_0.db")) as c:
                    if fault == "text_time":
                        c.execute("UPDATE Msg_synthetic SET create_time='bad'")
                    else:
                        c.execute("ALTER TABLE Msg_synthetic RENAME TO old")
                        c.execute(_SCHEMA.replace(" PRIMARY KEY", ""))
                        c.execute("INSERT INTO Msg_synthetic SELECT * FROM old")
                        c.execute("INSERT INTO Msg_synthetic SELECT * FROM old")
                    c.commit()
                reader, _, _ = self.reader(f)
                with self.assertRaises(HistoryError) as raised:
                    reader.open_inventory(mode="full")
                self.assertEqual(raised.exception.code, "SOURCE_ROW_INVALID")


if __name__ == "__main__":
    unittest.main()
