"""Behavioral boundaries for synthetic historical inventory contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
import sqlite3
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from wechat_exporter.history_contracts import (
    ArchiveScope, ScopeAuthorization, SourceKey, SourceDescriptor,
    SourceLocator, SourceRow, SourceCursor, SourceCount, InventoryDigests,
    Inventory, IdentityConflict, IdentityResolution, MigrationPlan,
    CountReport, HistoryCollection, HistoryError, SourceSetChanged,
)
from history_fixtures import synthetic_history


def count_report(**overrides):
    values = dict(
        mode="full", count_scope="full", raw_source_count=4,
        raw_selected_count=4, decoded_source_count=4,
        canonical_duplicate_count=1, legacy_matched_count=0,
        archive_only_count=0, archive_before_count=0,
        newly_discovered_count=3, conflict_count=0, final_union_count=3,
        earliest_timestamp=1700000000, latest_timestamp=1700000002,
        media_recovered_count=0, media_missing_count=0,
        integrity_result="PASS",
    )
    values.update(overrides)
    return CountReport(**values)


def locator(source_key=None, **overrides):
    values = dict(
        scope=ArchiveScope("synthetic-account", "synthetic-conversation"),
        source_key=source_key or SourceKey("message/message_0.db", "file-0"),
        table_name="Msg_synthetic", local_id="1",
        expected_server_id="9223372036854775807", create_time=1700000000,
        sort_seq=10, raw_local_type=1,
        storage_classes=("integer", "integer", "integer", "integer", "integer"),
    )
    values.update(overrides)
    return SourceLocator(**values)


class HistoricalContractsTests(unittest.TestCase):
    def test_scope_is_required(self):
        # Break caught: accepting empty, absent, or non-text account/conversation.
        for account, conversation in (("", "c"), ("a", ""), (None, "c"),
                                      ("a", None), (" ", "c"), (1, "c")):
            with self.subTest(case=(type(account).__name__, type(conversation).__name__)):
                with self.assertRaises(ValueError):
                    ArchiveScope(account, conversation)
        with self.assertRaises(TypeError):
            ArchiveScope()
        with self.assertRaises(ValueError):
            locator(scope=None)

    def test_scope_authorization_rejects_missing_binding(self):
        # Break caught: creating an authorization without an explicit scope.
        with self.assertRaises(ValueError):
            ScopeAuthorization(None, True)
        scope = ArchiveScope("a", "c")
        self.assertFalse(ScopeAuthorization(scope, False).origin_confirmed)
        with self.assertRaises(ValueError):
            ScopeAuthorization(scope, "yes")

    def test_source_key_rejects_noncanonical_paths(self):
        # Break caught: letting untrusted DTO paths escape the shard namespace.
        invalid = ("/message/message_0.db", "C:/message/message_0.db",
                   "message/../message_0.db", "message\\message_0.db",
                   "./message/message_0.db", "message//message_0.db",
                   "message/message_x.db", "message/message_0.db/", "")
        for value in invalid:
            with self.subTest(case=invalid.index(value)):
                with self.assertRaises(ValueError) as caught:
                    SourceKey(value, "synthetic-file")
                self.assertNotIn(value, str(caught.exception)) if value else None
        with self.assertRaises(ValueError):
            SourceKey("message/message_0.db", "")

    def test_locator_not_canonical_identity(self):
        # Break caught: coercing a 64-bit server value or deriving it from shard IDs.
        first = locator()
        second = locator(SourceKey("message/message_9.db", "file-9"), local_id="80")
        self.assertEqual(first.expected_server_id, "9223372036854775807")
        self.assertEqual(second.expected_server_id, "9223372036854775807")
        self.assertNotEqual(first.source_key, second.source_key)
        with self.assertRaises(ValueError):
            locator(expected_server_id=9223372036854775807)

    def test_storage_classes_preserve_order_and_require_five(self):
        # Break caught: silently sorting storage observations or accepting invalid classes.
        classes = ("text", "integer", "real", "null", "blob")
        self.assertEqual(locator(storage_classes=classes).storage_classes, classes)
        for value in (classes[:4], classes + ("integer",), ("bad",) * 5, list(classes)):
            with self.assertRaises(ValueError):
                locator(storage_classes=value)

    def test_locator_requires_technical_integers(self):
        # Break caught: accepting lossy/bool integer observations.
        for field in ("create_time", "sort_seq", "raw_local_type"):
            for value in (True, 1.2, "1"):
                with self.subTest(field=field, kind=type(value).__name__):
                    with self.assertRaises(ValueError):
                        locator(**{field: value})

    def test_count_dimensions_are_distinct(self):
        # Break caught: conflating SQL rows, decoded rows, duplicates, and final union.
        counts = count_report()
        self.assertEqual((counts.raw_source_count, counts.decoded_source_count,
                          counts.canonical_duplicate_count, counts.final_union_count),
                         (4, 4, 1, 3))
        quick = count_report(mode="quick", count_scope="quick_window", archive_only_count=None)
        self.assertIsNone(quick.archive_only_count)

    def test_count_validation_rejects_bool_negative_and_wrong_scope(self):
        # Break caught: allowing invalid report dimensions while retaining None only for quick.
        count_fields = ("raw_source_count", "raw_selected_count", "decoded_source_count",
                        "canonical_duplicate_count", "legacy_matched_count", "archive_only_count",
                        "archive_before_count", "newly_discovered_count", "conflict_count",
                        "final_union_count", "media_recovered_count", "media_missing_count")
        for name in count_fields:
            for value in (-1, True, 1.5):
                with self.subTest(field=name, kind=type(value).__name__):
                    with self.assertRaises(ValueError):
                        count_report(**{name: value})
        for values in (dict(mode="quick"), dict(count_scope="quick_window"),
                       dict(mode="bad"), dict(archive_only_count=None)):
            with self.assertRaises(ValueError):
                count_report(**values)

    def test_source_count_defers_mismatch_to_reader(self):
        # Break caught: DTO prematurely disallowing counts a reader must diagnose.
        key = SourceKey("message/message_0.db", "file-0")
        counts = SourceCount(key, 8, 4, 3, 2)
        self.assertEqual((counts.raw_total, counts.raw_selected, counts.enumerated, counts.decoded),
                         (8, 4, 3, 2))
        for field in ("raw_total", "raw_selected", "enumerated", "decoded"):
            with self.assertRaises(ValueError):
                replace(counts, **{field: True})

    def test_error_summary_drops_secrets(self):
        # Break caught: reflecting arbitrary exception details, identifiers, paths, or keys.
        secret = "synthetic-body-id-path-key-secret"
        error = HistoryError(
            "SOURCE_READ_FAILED", counts={"decoded_source_count": 4, "body": secret},
            fields=("storage_classes", secret),
            hashes={"source_rows_digest": "a" * 64, "db_key": secret},
            body=secret, identity=secret, path=secret, key=secret,
        )
        summary = error.to_summary()
        self.assertEqual(summary, {
            "code": "SOURCE_READ_FAILED", "counts": {"decoded_source_count": 4},
            "fields": ["storage_classes"], "hashes": {"source_rows_digest": "a" * 64},
        })
        self.assertNotIn(secret, json.dumps(summary))
        self.assertNotIn(secret, str(error))
        summary["counts"]["decoded_source_count"] = 999
        self.assertEqual(error.to_summary()["counts"]["decoded_source_count"], 4)
        with self.assertRaises(ValueError) as caught:
            HistoryError(secret)
        self.assertNotIn(secret, str(caught.exception))

    def test_error_codes_accept_plan_fatal_branches(self):
        # Break caught: losing downstream failclosed codes at the public error boundary.
        codes = (
            "INVALID_SERVER_ID", "REPRESENTATION_UNRESOLVED", "IDENTITY_SCOPE_MISMATCH",
            "IDENTITY_INVARIANT_CONFLICT", "UNVERIFIED_IDENTITY_INVARIANT",
            "AMBIGUOUS_LEGACY_BINDING", "POSSIBLE_LEGACY_COUNTERPART", "LEGACY_ALIAS_CONFLICT",
            "INCOMPLETE_SOURCE_SET", "SOURCE_SCHEMA_CHANGED", "UNSUPPORTED_SOURCE_SCHEMA",
            "SOURCE_DISCOVERY_FAILED", "SOURCE_READ_FAILED", "SOURCE_COUNT_MISMATCH",
            "SOURCE_SNAPSHOT_UNSAFE", "UNSUPPORTED_UPSTREAM_VERSION", "UNSUPPORTED_UPSTREAM_API",
            "SOURCE_ROW_INVALID", "SOURCE_READ_TIMEOUT", "SOURCE_IDENTITY_MISMATCH",
            "ARCHIVED_ASSET_INTEGRITY_FAILED", "TRANSACTION_RECOVERY_REQUIRED",
            "UNSUPPORTED_ARCHIVE_SCHEMA", "ORIGINAL_GATE_BLOCKED", "LEGACY_SCOPE_UNVERIFIED",
            "FULL_INVENTORY_REQUIRED", "INVENTORY_MODE_MISMATCH",
        )
        for code in codes:
            self.assertEqual(HistoryError(code).to_summary()["code"], code)
        self.assertEqual(SourceSetChanged().to_summary()["code"], "SOURCE_SET_CHANGED")

    def test_error_summary_argument_and_unknown_quick_count(self):
        # Break caught: silently discarding the planned summary API or an unknown quick count.
        summary = {"counts": {"raw_source_count": 4, "archive_only_count": None,
                              "decoded_source_count": True, "conflict_count": -1},
                   "fields": ["sort_seq", "synthetic-secret"],
                   "hashes": {"scope_digest": "b" * 64, "source_rows_digest": "synthetic-secret"},
                   "body": "synthetic-secret"}
        expected = {"code": "SOURCE_COUNT_MISMATCH",
                    "counts": {"raw_source_count": 4, "archive_only_count": None},
                    "fields": ["sort_seq"], "hashes": {"scope_digest": "b" * 64}}
        with self.subTest(api="keyword"):
            self.assertEqual(HistoryError("SOURCE_COUNT_MISMATCH", summary=summary).to_summary(), expected)
        with self.subTest(api="positional"):
            self.assertEqual(HistoryError("SOURCE_COUNT_MISMATCH", summary).to_summary(), expected)
        with self.subTest(api="summary-readback"):
            error = HistoryError("SOURCE_COUNT_MISMATCH", summary=summary)
            self.assertEqual(error.summary, expected)
        with self.subTest(api="source-set-keyword"):
            self.assertEqual(SourceSetChanged(summary=summary).to_summary()["counts"], expected["counts"])
        with self.subTest(api="source-set-positional"):
            self.assertEqual(SourceSetChanged(summary).to_summary()["counts"], expected["counts"])

    def test_fixture_has_no_real_backend(self):
        # Break caught: any real backend/settings construction or loss of SQL raw byte fields.
        backend = ModuleType("wechatauto.db")
        def forbidden(*args, **kwargs):
            raise AssertionError("REAL_BACKEND_CONSTRUCTOR")
        backend.WeChatDB = forbidden
        with patch.dict(sys.modules, {"wechatauto.db": backend}), \
             patch("wechat_exporter.service.AppSettings", side_effect=forbidden):
            with synthetic_history() as fixture:
                self.assertEqual(len(fixture.source_keys), 3)
                rows = fixture.raw_rows(fixture.source_keys[0])
                self.assertEqual(len(rows), 2)
                self.assertEqual(tuple(rows[0]), (
                    "local_id", "local_type", "server_id", "real_sender_id", "create_time",
                    "message_content", "source", "packed_info_data", "compress_content", "sort_seq",
                ))
                self.assertEqual(rows[0]["source"], b"\x00synthetic-source\xff")
                self.assertEqual(rows[0]["packed_info_data"], b"\x01\x02")
                self.assertEqual(fixture.decode_row(rows[0]).content, "synthetic alpha")
                second = fixture.raw_rows(fixture.source_keys[1])
                self.assertEqual(second[0]["server_id"], rows[0]["server_id"])
                self.assertEqual(len(second), 2)
                self.assertEqual(fixture.raw_rows(fixture.source_keys[2]), ())

    def test_contracts_frozen_and_inventory_accepts_absent_target(self):
        # Break caught: mutating inventory scope or silently dropping supported empty shards.
        scope = ArchiveScope("a", "c")
        key = SourceKey("message/message_2.db", "file-2")
        source = SourceDescriptor(key, "Msg_synthetic", False, "b" * 64)
        counts = SourceCount(key, 0, 0, 0, 0)
        inventory = Inventory(scope, "full", (), (source,), (counts,), (),
                              InventoryDigests("a" * 64, "b" * 64, "c" * 64))
        self.assertFalse(inventory.sources[0].target_present)
        with self.assertRaises(FrozenInstanceError):
            inventory.scope = ArchiveScope("other", "c")
        with self.assertRaises(ValueError):
            replace(inventory, scope=None)
        with self.assertRaises(ValueError):
            replace(inventory, mode="unknown")

    def test_fixture_capabilities_are_read_only_and_close(self):
        # Break caught: writable provider handles or SQLite creation handles preventing cleanup.
        with synthetic_history() as fixture:
            temporary_root = fixture.operation_root
            connection = fixture._open("message/message_0.db")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM Msg_synthetic").fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM Msg_synthetic")
            with self.assertRaises(ValueError):
                fixture._open("../unknown.db")
        self.assertFalse(temporary_root.exists())

    def test_cursor_and_migration_counts_validate(self):
        # Break caught: accepting bool/lossy cursor positions or negative migration/conflict counts.
        key = SourceKey("message/message_0.db", "file-0")
        cursor = SourceCursor(key, (10, 1700000000, 1))
        self.assertEqual(cursor.overlap_seq, 1)
        for position in ((10, 1), (10, True, 1), (10, 1.2, 1)):
            with self.assertRaises(ValueError):
                SourceCursor(key, position)
        with self.assertRaises(ValueError):
            MigrationPlan((), -1, 0, {})
        with self.assertRaises(ValueError):
            IdentityConflict("representation", ("content",), False, True)


if __name__ == "__main__":
    unittest.main()
