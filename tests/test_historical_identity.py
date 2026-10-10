"""Synthetic I01-I09: exact identities, authority, and fail-closed conflicts."""
from dataclasses import asdict, replace
import json
import unittest

from history_fixtures import identity_inventory, identity_row
from wechat_exporter.archive import ArchivedMessage
from wechat_exporter.history_contracts import ArchiveScope, HistoryError
from wechat_exporter.message_identity import (
    canonical_sort_key, normalize_server_id, resolve_canonical_rows,
)


class HistoricalIdentityTests(unittest.TestCase):
    def authority(self, row=None):
        row = row or identity_row()
        return ArchivedMessage(row.message, "success", "synthetic.png", "images",
                               canonical_identity="server:" + row.server_id,
                               scope=row.locator.scope, binding_status="server_bound",
                               source_refs=(row.locator,))

    def assert_error(self, code, rows, archived=()):
        with self.assertRaises(HistoryError) as caught:
            resolve_canonical_rows(identity_inventory(*rows), archived)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def test_nonzero_lossless_server_id(self):
        for value, storage, expected in (
            (9007199254740993, "integer", "server:9007199254740993"),
            ("9007199254740993", "integer", "server:9007199254740993"),
            ("009007199254740993", "text", "server:9007199254740993"),
            (-7, "integer", "server:-7"), ("-0007", "text", "server:-7"),
        ):
            with self.subTest(storage=storage, case=expected.startswith("server:-")):
                self.assertEqual(normalize_server_id(value, storage), expected)
        result = resolve_canonical_rows(identity_inventory(identity_row(server="9007199254740993")), ())
        self.assertEqual(result.records[0].identity, "server:9007199254740993")
        self.assertEqual(result.records[0].binding_status, "server_bound")

    def test_invalid_server_id_leaves_archive_unchanged(self):
        archived = self.authority()
        before = asdict(archived)
        cases = ((0, "integer"), ("0", "text"), ("-000", "text"), (None, "null"),
                 (True, "integer"), (1.0, "real"), (1.0, "integer"),
                 ("1e3", "text"), ("1.0", "text"), ("+1", "text"),
                 (" 1", "text"), ("1\n", "text"), ("", "text"),
                 ("١", "text"), ("1", "blob"))
        for number, (value, storage) in enumerate(cases):
            with self.subTest(case=number):
                with self.assertRaises(HistoryError) as caught:
                    normalize_server_id(value, storage)
                self.assertEqual(caught.exception.code, "INVALID_SERVER_ID")
        for number, (value, storage) in enumerate((("0", "integer"), ("", "null"),
                                                   ("1.0", "real"), ("1e3", "text"))):
            with self.subTest(row_case=number):
                self.assert_error("INVALID_SERVER_ID",
                                  (identity_row(server=value, storage=(storage,) + ("integer",) * 4),),
                                  (archived,))
        self.assertEqual(asdict(archived), before)

    def test_distinct_servers_with_same_local_are_not_merged(self):
        result = resolve_canonical_rows(identity_inventory(identity_row(), identity_row(server="102", source=1)), ())
        self.assertEqual(len(result.records), 2)
        self.assertEqual({record.identity for record in result.records}, {"server:101", "server:102"})
        self.assertEqual(result.canonical_duplicate_count, 0)

    def test_same_server_duplicate_sources(self):
        rows = (identity_row(), identity_row(local="9", source=1))
        result = resolve_canonical_rows(identity_inventory(*rows), ())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.canonical_duplicate_count, 1)
        self.assertEqual(set(result.records[0].source_refs), {row.locator for row in rows})
        self.assertEqual(result.conflicts, ())

    def test_bound_server_relocation(self):
        archived = self.authority()
        row = identity_row(local="88", source=2)
        result = resolve_canonical_rows(identity_inventory(row), (archived,))
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record.identity, "server:101")
        self.assertEqual(record.message, archived.message)
        self.assertEqual(record.source_refs, (row.locator,))
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.canonical_duplicate_count, 0)

    def test_archived_representation_wins(self):
        archived = self.authority()
        changes = dict(content="synthetic changed body", payload={"synthetic": "changed"},
                       message_type="图片", subtype="changed", sender_id=2,
                       sender_username="synthetic-other")
        row = identity_row(local="9", source=1, **changes)
        result = resolve_canonical_rows(identity_inventory(row), (archived,))
        self.assertEqual(result.records[0].message, archived.message)
        self.assertEqual(result.records[0].media_asset, archived.media_asset)
        self.assertEqual(result.records[0].media_status, archived.media_status)
        self.assertEqual(result.records[0].media_kind, archived.media_kind)
        self.assertEqual(sum(conflict.count for conflict in result.conflicts), 1)
        self.assertFalse(any(conflict.severe for conflict in result.conflicts))

    def test_first_save_representation_ambiguity(self):
        for field, value in dict(content="changed", payload={"different": 1}, sender_id=2,
                                 sender_username="changed", message_type="图片", subtype="new").items():
            with self.subTest(field=field):
                self.assert_error("REPRESENTATION_UNRESOLVED", (identity_row(), identity_row(source=1, **{field: value})))

    def test_cross_scope_server_rejected(self):
        for scope in (ArchiveScope("other-account", "synthetic-conversation"),
                      ArchiveScope("synthetic-account", "other-conversation")):
            self.assert_error("IDENTITY_SCOPE_MISMATCH", (identity_row(scope=scope),))
            archived = self.authority(identity_row(scope=scope))
            self.assert_error("IDENTITY_SCOPE_MISMATCH", (identity_row(),), (archived,))

    def test_same_server_time_or_seq_conflict(self):
        for field, value in (("create_time", 1700000001), ("sort_seq", 11)):
            with self.subTest(field=field):
                changed = identity_row(source=1, **{field: value})
                self.assert_error("IDENTITY_INVARIANT_CONFLICT", (identity_row(), changed))
                self.assert_error("IDENTITY_INVARIANT_CONFLICT", (changed,), (self.authority(),))

    def test_raw_type_observation_requires_evidence(self):
        cases = [dict(raw_type=49)]
        for index in range(5):
            classes = ["integer"] * 5
            classes[index] = "text"
            cases.append(dict(storage=tuple(classes)))
        for number, changes in enumerate(cases):
            with self.subTest(case=number):
                changed = identity_row(source=1, **changes)
                self.assertEqual(changed.message.message_type, "文本")
                self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (identity_row(), changed))
                self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (changed,), (self.authority(),))

    def test_invalid_locator_server_cannot_hide_behind_authority(self):
        row = identity_row()
        self.assert_error("INVALID_SERVER_ID", (replace(row, locator=replace(row.locator, expected_server_id="0")),), (self.authority(),))
        self.assert_error("SOURCE_IDENTITY_MISMATCH", (replace(row, locator=replace(row.locator, expected_server_id="102")),), (self.authority(),))

    def test_observed_authority_requires_historical_technical_evidence(self):
        authority = replace(self.authority(), source_refs=())
        self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (identity_row(),), (authority,))
        self.assertEqual(resolve_canonical_rows(identity_inventory(), (authority,)).records, ())
        observed = replace(authority, technical_observations=({
            "raw_local_type": 1, "storage_classes": ("integer",) * 5,
        },))
        self.assertEqual(resolve_canonical_rows(identity_inventory(identity_row()), (observed,)).records[0].message,
                         authority.message)
        changed = replace(observed, technical_observations=({
            "raw_local_type": 49, "storage_classes": ("integer",) * 5,
        },))
        self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (identity_row(),), (changed,))

    def test_decoded_technical_fields_must_match_locator(self):
        row = identity_row()
        for field, value in (("create_time", 1700000001), ("sort_seq", 11), ("local_id", "8")):
            with self.subTest(field=field):
                self.assert_error("SOURCE_IDENTITY_MISMATCH", (replace(row, message=replace(row.message, **{field: value})),))

    def test_canonical_chronology_is_time_then_sequence_then_identity(self):
        rows = (identity_row(server="103", create_time=1700000001, sort_seq=1),
                identity_row(server="102", source=1, sort_seq=99),
                identity_row(server="101", source=2, sort_seq=99))
        result = resolve_canonical_rows(identity_inventory(*rows), ())
        self.assertEqual([r.identity for r in result.records], ["server:101", "server:102", "server:103"])
        self.assertEqual(canonical_sort_key(result.records[0]), (1700000000, 99, "server:101"))

    def test_v2_defaults_and_round_trip_remain_unchanged(self):
        record = ArchivedMessage(identity_row().message, "success", "synthetic.png", "images")
        self.assertEqual(record.identity, "local:1")
        self.assertEqual(ArchivedMessage.from_json_dict(record.to_json_dict()), record)
        self.assertIsNone(getattr(record, "canonical_identity", None))
        self.assertEqual(record.message.stable_key, (10, 1700000000, "1"))

    def test_observed_rows_only_retention_belongs_to_t5(self):
        self.assertEqual(resolve_canonical_rows(identity_inventory(), (self.authority(),)).records, ())

    def test_unmapped_legacy_is_not_representation_authority(self):
        legacy = ArchivedMessage(identity_row(content="legacy representation").message)
        result = resolve_canonical_rows(identity_inventory(identity_row()), (legacy,))
        self.assertEqual(result.records[0].identity, "server:101")
        self.assertEqual(result.records[0].message.content, "synthetic representation")

    def test_conflict_producer_outputs_only_fixed_technical_fields(self):
        sentinel = "synthetic-body-path-key-identity"
        result = resolve_canonical_rows(identity_inventory(identity_row(content=sentinel)), (self.authority(),))
        summaries = [asdict(conflict) for conflict in result.conflicts]
        self.assertEqual(summaries, [dict(category="REPRESENTATION_CONFLICT", fields=("content",), severe=False, count=1)])
        self.assertNotIn(sentinel, json.dumps(summaries))
        self.assertNotIn("101", json.dumps(summaries))
        error = self.assert_error("REPRESENTATION_UNRESOLVED", (identity_row(), identity_row(source=1, content=sentinel)))
        self.assertNotIn(sentinel, json.dumps(error.summary))
        self.assertNotIn("101", json.dumps(error.summary))


if __name__ == "__main__":
    unittest.main()
