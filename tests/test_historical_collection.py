"""Synthetic collection behavior, SQL-reader integration and ownership tests."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from history_fixtures import (identity_inventory, identity_row, legacy_record,
                             synthetic_history, SyntheticReaderDB, _SCHEMA)
from wechat_exporter.conversation_reader import ConversationReader
from wechat_exporter.history_collection import collect_history, collect_with_retry
from wechat_exporter.history_contracts import (ArchiveScope, HistoryCollection, HistoryError,
                                              ScopeAuthorization, SourceSetChanged)
from wechat_exporter.identity_migration import plan_identity_migration


class MemorySession:
    """Fault injection at the resource/verification boundary, no data-policy mocks."""
    def __init__(self, inventory, failure=None):
        self.inventory, self.failure = inventory, failure
        self.closed = False

    def assert_source_set_stable(self):
        if self.failure is not None:
            raise self.failure

    def close(self):
        self.closed = True


class HistoricalCollectionTests(unittest.TestCase):
    def collect(self, records=(), inventory=None, *, mode=None, authorization=None, session=None):
        inv = inventory if inventory is not None else identity_inventory()
        session = session or MemorySession(inv)
        result = collect_history(tuple(records), session, mode=mode or session.inventory.mode,
            authorization=authorization or ScopeAuthorization(session.inventory.scope, True))
        self.assertIsInstance(result, HistoryCollection, "validated memory union required")
        return result

    def mapped(self, records, inv=None):
        inv = inv if inv is not None else identity_inventory()
        return plan_identity_migration(tuple(records), inv, ScopeAuthorization(inv.scope, True)).records

    def error(self, code, records=(), inv=None, **kwargs):
        before = deepcopy(tuple(asdict(r) for r in records))
        with self.assertRaises(HistoryError) as caught:
            self.collect(records, inv, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(tuple(asdict(r) for r in records), before)

    def test_first_save_all_history(self):
        result = self.collect(inventory=identity_inventory(identity_row(), identity_row(
            server="102", local="1", source=1, create_time=1700000001)))
        self.assertEqual(result.new_identities, frozenset(("server:101", "server:102")))
        self.assertEqual((result.counts.archive_before_count, result.counts.newly_discovered_count,
                          result.counts.final_union_count), (0,2,2))

    def test_archive_first_union_and_repeat_new_zero(self):
        old = legacy_record()
        inv = identity_inventory(identity_row(), identity_row(server="102", local="2",
            create_time=1700000001, sort_seq=11))
        before = deepcopy(asdict(old))
        result = self.collect((old,), inv)
        self.assertEqual(result.new_identities, frozenset(("server:102",)))
        self.assertEqual((result.counts.archive_before_count, result.counts.legacy_matched_count,
                          result.counts.newly_discovered_count, result.counts.final_union_count), (1,1,1,2))
        self.assertIs(result.records[0].message, old.message)
        self.assertEqual(result.records[0].media_asset, old.media_asset)
        self.assertEqual(asdict(old), before)
        again = self.collect(result.records, inv)
        self.assertEqual(again.new_identities, frozenset())
        self.assertEqual((again.counts.legacy_matched_count, again.counts.newly_discovered_count), (0,0))

    def test_conflict_count_after_migration_and_duplicate_sources(self):
        inv = identity_inventory(identity_row(), identity_row(source=1, local="9"))
        result = self.collect((legacy_record(),), inv)
        self.assertEqual((result.counts.conflict_count, result.counts.canonical_duplicate_count,
                          result.counts.decoded_source_count, result.counts.final_union_count), (2,1,2,1))
        self.assertEqual(len(result.records[0].source_refs), 2)
        self.assertEqual(result.counts.newly_discovered_count, 0)

    def test_archive_only_exact_rebind_is_not_new(self):
        records = self.mapped((legacy_record(),))
        result = self.collect(records, identity_inventory(identity_row()))
        self.assertEqual(result.records[0].identity, "server:101")
        self.assertEqual(result.records[0].legacy_aliases, records[0].legacy_aliases)
        self.assertIs(result.records[0].message, records[0].message)
        self.assertEqual((result.counts.legacy_matched_count, result.counts.newly_discovered_count,
                          result.counts.archive_only_count), (1,0,0))

    def test_bound_server_relocation(self):
        records = self.mapped((legacy_record(),), identity_inventory(identity_row()))
        row = identity_row(local="99", source=1)
        result = self.collect(records, identity_inventory(row))
        self.assertEqual(result.new_identities, frozenset())
        self.assertEqual(result.records[0].source_refs, (row.locator,))
        self.assertIs(result.records[0].message, records[0].message)

    def test_missing_bound_source_and_archive_only_preserved(self):
        bound = self.mapped((legacy_record(),), identity_inventory(identity_row()))
        opaque = self.mapped((legacy_record(local="9", create_time=1700000009),))
        for mode in ("full", "quick"):
            with self.subTest(mode=mode):
                records = bound + opaque
                result = self.collect(records, replace(identity_inventory(), mode=mode))
                self.assertEqual(result.records, records)
                self.assertEqual((result.counts.final_union_count, result.counts.newly_discovered_count), (2,0))
                self.assertEqual(result.counts.archive_only_count, 1 if mode == "full" else None)
                if mode == "quick":
                    self.assertIsNone(result.migration)

    def test_chronology_with_inverted_seq_and_identity_ties(self):
        inv = identity_inventory(identity_row(server="103", local="3", create_time=30, sort_seq=1),
            identity_row(server="102", local="2", create_time=10, sort_seq=500),
            identity_row(server="101", local="1", create_time=10, sort_seq=500))
        result = self.collect(inventory=inv)
        self.assertEqual([r.identity for r in result.records], ["server:101","server:102","server:103"])
        self.assertEqual((result.counts.earliest_timestamp, result.counts.latest_timestamp), (10,30))

    def test_empty_counts_and_integrity_not_claimed(self):
        result = self.collect()
        self.assertEqual((result.counts.earliest_timestamp, result.counts.latest_timestamp), (None,None))
        self.assertEqual((result.counts.media_recovered_count, result.counts.media_missing_count,
                          result.counts.integrity_result), (0,0,"NOT_CHECKED"))

    def test_duplicates_use_validated_server_not_local(self):
        inv = identity_inventory(identity_row(server="00101"), identity_row(local="9", source=1),
                                 identity_row(server="102", local="1", source=2))
        result = self.collect(inventory=inv)
        self.assertEqual((result.counts.decoded_source_count, result.counts.canonical_duplicate_count,
                          result.counts.final_union_count), (3,1,2))

    def test_quick_count_scope_and_window(self):
        inv = identity_inventory(identity_row())
        inv = replace(inv, mode="quick", counts=(replace(inv.counts[0], raw_total=8),))
        result = self.collect(inventory=inv)
        self.assertEqual((result.counts.raw_source_count, result.counts.raw_selected_count,
                          result.counts.decoded_source_count), (8,1,1))
        self.assertEqual(result.counts.count_scope, "quick_window")
        self.assertIsNone(result.counts.archive_only_count)

    def test_legacy_and_promotion_quick_require_full(self):
        self.error("FULL_INVENTORY_REQUIRED", (legacy_record(),), replace(identity_inventory(), mode="quick"))
        records = self.mapped((legacy_record(),))
        for local in ("1", "9"):
            with self.subTest(exact=local == "1"):
                self.error("FULL_INVENTORY_REQUIRED", records,
                           replace(identity_inventory(identity_row(local=local)), mode="quick"))

    def test_mode_and_scope_validation(self):
        self.error("INVENTORY_MODE_MISMATCH", mode="quick")
        other = ArchiveScope("synthetic-other", "synthetic-conversation")
        records = self.mapped((legacy_record(),))
        for mode in ("full", "quick"):
            inv = replace(identity_inventory(), mode=mode)
            with self.subTest(mode=mode):
                self.error("IDENTITY_SCOPE_MISMATCH", inv=inv, authorization=ScopeAuthorization(other, True))
                self.error("IDENTITY_SCOPE_MISMATCH", (replace(records[0], scope=other),), inv)
        self.error("LEGACY_SCOPE_UNVERIFIED", (legacy_record(),),
                   authorization=ScopeAuthorization(identity_inventory().scope, False))

    def test_quick_alias_conflicts_and_invalid_canonical_cannot_hide_outside_window(self):
        record = self.mapped((legacy_record(),))[0]
        inv = replace(identity_inventory(), mode="quick")
        alias = {**record.legacy_aliases[0], "identity": "invalid"}
        self.error("LEGACY_ALIAS_CONFLICT", (replace(record, legacy_aliases=(alias,)),), inv)
        self.error("LEGACY_ALIAS_CONFLICT", (record, record), inv)
        other = self.mapped((legacy_record(local="9", create_time=1700000009),))[0]
        self.error("LEGACY_ALIAS_CONFLICT", (record, replace(other, legacy_aliases=record.legacy_aliases)), inv)
        self.error("LEGACY_ALIAS_CONFLICT", (replace(record, canonical_identity="archive:invalid"),), inv)
        bound = self.mapped((legacy_record(),), identity_inventory(identity_row()))[0]
        self.error("INVALID_SERVER_ID", (replace(bound, canonical_identity="server:00101"),), inv)

    def test_count_proof_inputs_fail_closed(self):
        inv = identity_inventory(identity_row())
        cases = [replace(inv, counts=()), replace(inv, sources=()),
                 replace(inv, sources=inv.sources*2), replace(inv, counts=inv.counts*2),
                 replace(inv, counts=(replace(inv.counts[0], raw_total=0),)),
                 replace(inv, sources=(replace(inv.sources[0], target_present=False),)),
                 replace(inv, counts=(replace(inv.counts[0], decoded=0),))]
        for mode in ("full", "quick"):
            for number, broken in enumerate(cases):
                with self.subTest(mode=mode, case=number):
                    self.error("SOURCE_COUNT_MISMATCH", inv=replace(broken, mode=mode))
        self.error("SOURCE_COUNT_MISMATCH", inv=replace(inv, counts=(replace(inv.counts[0], raw_total=2),)))
        self.error("SOURCE_IDENTITY_MISMATCH", inv=replace(inv,
            sources=(replace(inv.sources[0], target_table="OtherSynthetic"),)))

    def test_identity_errors_remain_fatal(self):
        cases = (("INVALID_SERVER_ID", (identity_row(server="0"),)),
            ("IDENTITY_INVARIANT_CONFLICT", (identity_row(),identity_row(source=1, create_time=1))),
            ("UNVERIFIED_IDENTITY_INVARIANT", (identity_row(),identity_row(source=1, raw_type=49))),
            ("REPRESENTATION_UNRESOLVED", (identity_row(),identity_row(source=1, content="alternate"))))
        for code, rows in cases:
            with self.subTest(code=code):
                self.error(code, inv=identity_inventory(*rows))

    def test_retry_budget_post_inventory_and_pre_accept(self):
        for location in ("open", "verify", "callback", "after_callback"):
            for failures in (1,2):
                with self.subTest(location=location, failures=failures):
                    sessions, attempts = [], []
                    def opening():
                        attempts.append(None)
                        if location == "open" and len(attempts) <= failures:
                            raise SourceSetChanged()
                        session = MemorySession(identity_inventory(identity_row(
                            server="101" if len(attempts) == 1 else "102")),
                            SourceSetChanged() if location == "verify" and len(attempts) <= failures else None)
                        sessions.append(session)
                        return session
                    def callback(session, collection):
                        self.assertIsInstance(collection, HistoryCollection)
                        if len(attempts) <= failures:
                            if location == "callback":
                                raise SourceSetChanged()
                            if location == "after_callback":
                                session.failure = SourceSetChanged()
                    def run():
                        return collect_with_retry(opening, (), mode="full", before_accept=callback,
                            authorization=ScopeAuthorization(identity_inventory().scope, True))
                    if failures == 2:
                        with self.assertRaises(HistoryError) as caught:
                            run()
                        self.assertEqual(caught.exception.code, "INCOMPLETE_SOURCE_SET")
                    else:
                        result = run()
                        self.assertIsInstance(result, HistoryCollection)
                        self.assertEqual(result.new_identities, frozenset(("server:102",)))
                    self.assertEqual(len(attempts), 2)
                    self.assertTrue(all(session.closed for session in sessions))

    def test_non_drift_never_retries_and_closes_owned_sessions(self):
        for code in ("SOURCE_READ_FAILED", "SOURCE_SCHEMA_CHANGED", "SOURCE_COUNT_MISMATCH",
                     "SOURCE_READ_TIMEOUT", "SOURCE_SET_CHANGED"):
            for location in ("open", "verify", "callback"):
                with self.subTest(code=code, location=location):
                    attempts, sessions = [], []
                    def opening():
                        attempts.append(None)
                        if location == "open":
                            raise HistoryError(code)
                        session = MemorySession(identity_inventory(), HistoryError(code) if location == "verify" else None)
                        sessions.append(session)
                        return session
                    def callback(*args):
                        raise HistoryError(code)
                    with self.assertRaises(HistoryError) as caught:
                        collect_with_retry(opening, (), mode="full", before_accept=callback,
                            authorization=ScopeAuthorization(identity_inventory().scope, True))
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(len(attempts), 1)
                    self.assertTrue(all(session.closed for session in sessions))

    def test_unexpected_callback_exception_closes_without_retry(self):
        sessions = []
        def opening():
            sessions.append(MemorySession(identity_inventory()))
            return sessions[-1]
        def callback(*args):
            raise ValueError("SYNTHETIC_CALLBACK_FAILURE")
        with self.assertRaisesRegex(ValueError, "SYNTHETIC_CALLBACK_FAILURE"):
            collect_with_retry(opening, (), mode="full", before_accept=callback,
                authorization=ScopeAuthorization(identity_inventory().scope, True))
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].closed)

    def reader(self, fixture):
        work = TemporaryDirectory(prefix="synthetic-collection-work-")
        self.addCleanup(work.cleanup)
        db = SyntheticReaderDB(fixture.operation_root)
        return ConversationReader(db, fixture.scope, Path(work.name), page_size=1), db, Path(work.name)

    def source(self, fixture, index, rows, *, recreate=False):
        path = fixture.operation_root / f"message/message_{index}.db"
        if recreate and path.exists():
            path.rename(path.with_suffix(".preserved"))
        with closing(sqlite3.connect(path)) as c:
            c.execute(_SCHEMA if recreate or index >= 3 else "DELETE FROM Msg_synthetic")
            c.executemany("INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            c.commit()

    def raw(self, local, server, time, seq):
        return (local,1,server,1,time,"synthetic",b"",b"",None,seq)

    def test_delayed_seq100_after_seq500_full_ignores_cursors_archive_dates(self):
        with synthetic_history() as f:
            self.source(f, 0, (self.raw(1,501,500,500),))
            self.source(f, 1, ())
            reader, db, root = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                first = self.collect(session=s)
                cursors = s.inventory.next_cursors
            self.source(f, 0, (self.raw(1,501,500,500),self.raw(2,502,100,100)))
            with reader.open_inventory(mode="full", cursors=cursors) as s:
                result = self.collect(first.records, session=s)
            self.assertEqual([r.message.create_time for r in result.records], [100,500])
            self.assertEqual(result.new_identities, frozenset(("server:502",)))
            self.assertEqual((result.counts.raw_source_count, result.counts.earliest_timestamp,
                              result.counts.latest_timestamp), (2,100,500))
            self.assertEqual(db.shared_calls, 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_new_shard_full_inventory_on_quick(self):
        with synthetic_history() as f:
            reader, db, root = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                first = self.collect(session=s)
                cursors = s.inventory.next_cursors
            self.source(f, 3, (self.raw(1,901,1,1),self.raw(2,902,2,2)))
            with reader.open_inventory(mode="quick", cursors=cursors) as s:
                result = self.collect(first.records, session=s)
            self.assertEqual(result.new_identities, frozenset(("server:901","server:902")))
            self.assertEqual(result.counts.final_union_count, 5)
            self.assertIsNone(result.counts.archive_only_count)
            self.assertEqual(db.shared_calls, 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_overlap_ties_and_cursor_incarnation_reset(self):
        with synthetic_history() as f:
            self.source(f, 0, (self.raw(5,505,50,50),self.raw(6,506,50,50)))
            self.source(f, 1, ())
            reader, db, root = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                first = self.collect(session=s)
                cursors = s.inventory.next_cursors
            self.source(f, 0, (self.raw(1,501,49,49),self.raw(2,502,50,50),
                               self.raw(5,505,50,50),self.raw(6,506,50,50)))
            with reader.open_inventory(mode="quick", cursors=cursors) as s:
                overlap = self.collect(first.records, session=s)
                cursors = s.inventory.next_cursors
            self.assertEqual(overlap.new_identities, frozenset(("server:501","server:502")))
            self.assertEqual(overlap.counts.raw_selected_count, 4)
            self.source(f, 0, (self.raw(1,701,1,1),), recreate=True)
            with reader.open_inventory(mode="quick", cursors=cursors) as s:
                reset = self.collect(overlap.records, session=s)
                self.assertEqual(s.inventory.next_cursors[0].high_watermark, (1,1,1))
            self.assertEqual(reset.new_identities, frozenset(("server:701",)))
            self.assertEqual(reset.counts.final_union_count, 5)
            self.assertEqual(db.shared_calls, 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_borrowed_session_keeps_raw_rows_and_retry_owns_closure(self):
        with synthetic_history() as f:
            reader, db, root = self.reader(f)
            with reader.open_inventory(mode="full") as s:
                result = self.collect(session=s)
                self.assertIsInstance(s.resolve_raw_row(result.inventory.rows[0].raw_row_token), dict)
            owned = []
            def opening():
                session = reader.open_inventory(mode="full")
                owned.append(session)
                return session
            def callback(session, collection):
                self.assertIsInstance(session.resolve_raw_row(collection.inventory.rows[0].raw_row_token), dict)
            result = collect_with_retry(opening, (), mode="full", before_accept=callback,
                authorization=ScopeAuthorization(f.scope, True))
            self.assertIsInstance(result, HistoryCollection)
            self.assertEqual(len(result.records), 3)
            with self.assertRaises(HistoryError):
                owned[0].resolve_raw_row(result.inventory.rows[0].raw_row_token)
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(db.shared_calls, 0)


if __name__ == "__main__":
    unittest.main()
