"""Independent synthetic codec fixtures; tests name the behavior they protect."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from wechat_exporter.archive import ArchivedMessage
from wechat_exporter.models import ChatMessage
from wechat_exporter.history_contracts import (
    ArchiveScope, SourceKey, SourceLocator, SourceRow, SourceDescriptor, SourceCount,
    ScopeAuthorization, Inventory, InventoryDigests, CountReport,
)
from wechat_exporter.identity_migration import plan_identity_migration
from wechat_exporter.track2_contracts import StateBundle, canonical_bytes
from wechat_exporter.v3_state import encode_state, decode_state, encode_schema, encode_messages
from wechat_exporter.v3_schema import inspect_v3_schema

SCOPE = ArchiveScope("synthetic-account", "synthetic-conversation")
ARCHIVE_ID = "archive:123456781234423482341234567890ab"


def fixture():
    records = []
    for local, server, time, seq, storage in (("7", "server:101", 1700000000, 1, "integer"),
                                             ("0007", "server:102", 1700000001, 2, "text")):
        message = ChatMessage(local, "图片", 7 if storage == "integer" else "7", "sender",
                              time, "保留的正文", seq, "card", {"items": [1, "1", None, {"n": 1.25}]})
        ref = SourceLocator(SCOPE, SourceKey("message/message_0.db", "synthetic-incarnation"),
                            "Msg_synthetic", local, server[7:], time, seq, 1,
                            (storage, storage, "integer", "integer", "integer"))
        alias = {"scope": SCOPE, "local_id": local, "create_time": time, "sort_seq": seq,
                 "identity": "local:" + local}
        records.append(ArchivedMessage(message, "success", "old-image.png", "images", server,
                                       SCOPE, "server_bound", (ref,), (alias,),
                                       ({"raw_local_type": 1, "storage_classes": ref.storage_classes},)))
    message = ChatMessage("lost-local", "文本", None, "sender", 1700000002, "消失的历史正文", 3)
    alias = {"scope": SCOPE, "local_id": "lost-local", "create_time": 1700000002,
             "sort_seq": 3, "identity": "local:lost-local"}
    records.append(ArchivedMessage(message, canonical_identity=ARCHIVE_ID, scope=SCOPE,
                                   binding_status="archive_only", legacy_aliases=(alias,)))
    return StateBundle(SCOPE, "synthetic-archive", 5, tuple(records),
                       (("message_count", 3), ("media_recovered_count", 0),
                        ("media_missing_count", 0), ("integrity_result", "NOT_CHECKED")))


def wire(bundle=None):
    bundle = fixture() if bundle is None else bundle
    documents = [json.loads(canonical_bytes(record)) for record in bundle.records]
    schema = {"schema_version": 3, "identity_contract_version": 1, "archive_id": bundle.archive_id,
              "scope": {
                  "contract_version": "track2-v1", "dto_type": "ArchiveScope",
                  "fields": {"account_id": SCOPE.account_id, "conversation_id": SCOPE.conversation_id}},
              "data_generation": bundle.data_generation, "metadata": [list(pair) for pair in bundle.metadata],
              "message_count": len(documents),
              "records_digest": hashlib.sha256(canonical_bytes(tuple(documents))).hexdigest()}
    return {"schema": schema, "records": documents}


def payload(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf8")


def write_fixture(root, document=None, target=None):
    document = wire() if document is None else document
    (root / "state").mkdir()
    (root / "state/schema.json").write_bytes(payload(document["schema"]))
    (root / "state/messages.jsonl").write_bytes(b"".join(payload(record) + b"\n" for record in document["records"]))
    if target is not None:
        (root / "target.json").write_bytes(payload(target))


class Track2StateTests(unittest.TestCase):
    def rejects(self, document, *, retain_integrity=False):
        # Semantic attacks carry a correct outer checksum so validation must
        # reach identity/alias/provenance rules rather than stop at byte drift.
        if not retain_integrity:
            document["schema"]["message_count"] = len(document["records"])
            document["schema"]["records_digest"] = hashlib.sha256(canonical_bytes(tuple(document["records"]))).hexdigest()
        with self.assertRaises(ValueError):
            decode_state(payload(document), SCOPE)

    def test_migrated_server_archive_uuid_and_payload_roundtrip_losslessly(self):
        # Break: calling the legacy serializer loses canonical/provenance/payload fields.
        bundle = fixture()
        encoded = encode_state(bundle)
        self.assertIsInstance(encoded, bytes)
        restored = decode_state(encoded, SCOPE)
        self.assertIsInstance(restored, StateBundle)
        self.assertEqual(bundle, restored)
        self.assertEqual(["server:101", "server:102", ARCHIVE_ID], [r.identity for r in restored.records])
        self.assertEqual(["7", "0007", "lost-local"], [r.message.local_id for r in restored.records])
        self.assertEqual([7, "7", None], [r.message.sender_id for r in restored.records])
        self.assertEqual("old-image.png", restored.records[0].media_asset)
        self.assertEqual({"items": [1, "1", None, {"n": 1.25}]}, restored.to_core_records()[0].message.payload)
        self.assertEqual(bundle.metadata, restored.metadata)
        self.assertEqual(encoded, encode_state(restored))

    def test_explicit_schema_scope_generation_and_record_shape(self):
        # Break: omitting durable version/scope/generation or silently coercing IDs.
        encoded = encode_state(fixture())
        self.assertIsInstance(encoded, bytes)
        actual = json.loads(encoded)
        self.assertEqual(wire(), actual)
        self.assertEqual("ArchivedMessage", actual["records"][0]["dto_type"])
        self.assertEqual("text", actual["records"][1]["fields"]["source_refs"][0]["fields"]["storage_classes"][1])

    def test_empty_migrated_state_is_supported(self):
        # Break: creating dummy identities or rejecting empty canonical state.
        bundle = StateBundle(SCOPE, "empty-archive", 0, (), ())
        restored = decode_state(payload(wire(bundle)), SCOPE)
        self.assertIsInstance(restored, StateBundle)
        self.assertEqual(bundle, restored)

    def test_wire_helpers_match_inspection_layout(self):
        # Break: splitting schema/messages changes order, bodies or retained metadata.
        bundle = fixture()
        schema, messages = encode_schema(bundle), encode_messages(bundle)
        self.assertIsInstance(schema, bytes)
        self.assertIsInstance(messages, bytes)
        self.assertEqual(wire()["schema"], json.loads(schema))
        self.assertEqual(wire()["records"], [json.loads(line) for line in messages.splitlines()])

    def test_unknown_schema_contract_identity_version_and_boolean_versions_refused(self):
        # Break: future or bool versions are accepted by integer equality.
        for field, value in (("schema_version", 2), ("schema_version", 99), ("schema_version", True),
                             ("identity_contract_version", 2), ("identity_contract_version", True),
                             ("data_generation", True), ("data_generation", -1)):
            with self.subTest(field=field, value=value):
                document = wire()
                document["schema"][field] = value
                self.rejects(document, retain_integrity=True)
        document = wire()
        document["records"][0]["contract_version"] = "future"
        self.rejects(document)

    def test_unknown_and_missing_wire_fields_refused(self):
        # Break: typo or injected authority metadata is silently ignored/defaulted.
        for where in ("root", "schema", "record", "message", "locator", "alias", "observation"):
            with self.subTest(where=where):
                document = wire()
                destinations = {"root": document, "schema": document["schema"],
                    "record": document["records"][0]["fields"],
                    "message": document["records"][0]["fields"]["message"]["fields"],
                    "locator": document["records"][0]["fields"]["source_refs"][0]["fields"],
                    "alias": document["records"][0]["fields"]["legacy_aliases"][0],
                    "observation": document["records"][0]["fields"]["technical_observations"][0]}
                destinations[where]["unrecognized"] = "ACTIVE"
                self.rejects(document)
        document = wire()
        del document["schema"]["data_generation"]
        self.rejects(document)

    def test_wrong_external_scope_refused(self):
        # Break: caller-requested conversation is replaced by state scope.
        with self.assertRaises(ValueError):
            decode_state(payload(wire()), ArchiveScope("other-account", "synthetic-conversation"))

    def test_record_alias_and_locator_scope_contradictions_refused(self):
        # Break: foreign account/conversation evidence is retained as local provenance.
        for where in ("record", "alias", "locator"):
            with self.subTest(where=where):
                document = wire()
                record = document["records"][0]["fields"]
                scoped = {"record": record["scope"], "alias": record["legacy_aliases"][0]["scope"],
                          "locator": record["source_refs"][0]["fields"]["scope"]}[where]
                scoped["fields"]["conversation_id"] = "other"
                self.rejects(document)

    def test_canonical_identity_status_and_uuid_contradictions_refused(self):
        # Break: v2 local IDs or noncanonical server/archive tokens become migrated state.
        for identity, status in ((None, ""), ("local:7", "server_bound"), ("server:00101", "server_bound"),
                                 ("server:0", "server_bound"), ("server:101", "archive_only"),
                                 ("archive:12345678-1234-4234-8234-1234567890ab", "archive_only"),
                                 ("archive:123456781234123482341234567890ab", "archive_only")):
            with self.subTest(identity=identity, status=status):
                document = wire()
                document["records"][0]["fields"].update(canonical_identity=identity, binding_status=status)
                self.rejects(document)

    def test_duplicate_canonical_records_and_alias_ownership_refused(self):
        # Break: deduplicating corrupt state silently deletes bodies.
        document = wire()
        document["records"].append(deepcopy(document["records"][0]))
        self.rejects(document)
        document = wire()
        document["records"][1]["fields"]["legacy_aliases"] = deepcopy(document["records"][0]["fields"]["legacy_aliases"])
        self.rejects(document)

    def test_canonical_order_is_checked_without_resorting(self):
        # Break: silently resorting hides contradictory state ordering.
        document = wire()
        document["records"].reverse()
        document["schema"]["records_digest"] = hashlib.sha256(canonical_bytes(tuple(document["records"]))).hexdigest()
        self.rejects(document)

    def test_locator_and_observation_invariants_refused(self):
        # Break: canonical body time/server is inconsistent with retained source evidence.
        for field, value in (("expected_server_id", "999"), ("create_time", 0), ("sort_seq", 99),
                             ("raw_local_type", 9)):
            with self.subTest(field=field):
                document = wire()
                document["records"][0]["fields"]["source_refs"][0]["fields"][field] = value
                self.rejects(document)

    def test_count_digest_and_metadata_contradictions_refused(self):
        # Break: accepting truncated records or duplicate metadata with ambiguous values.
        for field, value in (("message_count", 4), ("message_count", True), ("records_digest", "0" * 64),
                             ("metadata", [["message_count", 4]]),
                             ("metadata", [["message_count", 3], ["message_count", 3]]),
                             ("metadata", [["ACTIVE", True]])):
            with self.subTest(field=field):
                document = wire()
                document["schema"][field] = value
                self.rejects(document, retain_integrity=True)

    def test_duplicate_json_keys_nonfinite_and_invalid_types_refused(self):
        # Break: JSON last-key wins, NaN or bool integers bypass strict acceptance.
        original = payload(wire())
        bad = (original.replace(b'"schema_version":3', b'"schema_version":3,"schema_version":3'),
               original.replace(b'1.25', b'NaN'), original.replace(b'1.25', b'Infinity'),
               original.replace(b'1.25', b'1e999'),
               original.replace(b'"create_time":1700000000', b'"create_time":true'),
               b'\xff', original.decode("utf8"), None)
        for value in bad:
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    decode_state(value, SCOPE)

    def test_encoding_rejects_unmigrated_and_duplicate_records(self):
        # Break: the write codec bypasses checks that read codec enforces.
        bundle = fixture()
        for records in ((replace(bundle.to_core_records()[0], canonical_identity=None),),
                        (bundle.to_core_records()[0],) * 2, tuple(reversed(bundle.to_core_records()))):
            with self.subTest(count=len(records)):
                with self.assertRaises(ValueError):
                    encode_state(replace(bundle, records=records, metadata=()))

    def test_inspection_reads_schema_messages_and_target_without_writes(self):
        # Break: inspection invokes lock/recovery/migration or loads only schema metadata.
        with tempfile.TemporaryDirectory(prefix="track2-codec-") as name:
            root = Path(name)
            document = wire()
            target = {key: document["schema"][key] for key in
                      ("schema_version", "identity_contract_version", "archive_id", "scope", "data_generation")}
            write_fixture(root, document, target)
            before = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            actual = inspect_v3_schema(root)
            self.assertIsInstance(actual, StateBundle)
            self.assertEqual(fixture(), actual)
            after = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_inspection_refuses_missing_unknown_and_mismatched_target(self):
        # Break: optional target can contradict schema or required store be omitted.
        for kind in ("missing-store", "v2-schema", "target-scope", "target-id", "target-version", "target-generation"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="track2-codec-") as name:
                root = Path(name)
                document = wire()
                target = {key: deepcopy(document["schema"][key]) for key in
                          ("schema_version", "identity_contract_version", "archive_id", "scope", "data_generation")}
                if kind == "v2-schema": document["schema"]["schema_version"] = 2
                if kind == "target-scope": target["scope"]["fields"]["conversation_id"] = "other"
                if kind == "target-id": target["archive_id"] = "other"
                if kind == "target-version": target["schema_version"] = 2
                if kind == "target-generation": target["data_generation"] = 6
                write_fixture(root, document, target)
                if kind == "missing-store": (root / "state/messages.jsonl").unlink()
                with self.assertRaises(ValueError): inspect_v3_schema(root)

    def test_inspection_refuses_corrupt_jsonl_and_root(self):
        # Break: blank/malformed lines, truncated store or absent root accepted as empty.
        for suffix in (b"\n", b"not-json\n", b'{"unknown":true}\n'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(prefix="track2-codec-") as name:
                root = Path(name)
                write_fixture(root)
                store = root / "state/messages.jsonl"
                store.write_bytes(store.read_bytes() + suffix)
                with self.assertRaises(ValueError): inspect_v3_schema(root)
        with self.assertRaises(ValueError): inspect_v3_schema(Path("relative-root"))

    def test_inspection_semantic_conflict_has_uniform_rejection(self):
        # Break: a checksum-valid bad canonical record escapes the strict adapter.
        document = wire()
        document["records"][0]["fields"]["canonical_identity"] = "server:00101"
        document["schema"]["records_digest"] = hashlib.sha256(canonical_bytes(tuple(document["records"]))).hexdigest()
        with tempfile.TemporaryDirectory(prefix="track2-codec-") as name:
            root = Path(name)
            write_fixture(root, document)
            rejected = False
            try:
                inspect_v3_schema(root)
            except ValueError:
                rejected = True
            except Exception:
                pass
            self.assertTrue(rejected, "semantic disk conflicts must use strict ValueError refusal")

    def test_actual_core_migration_records_keep_body_refs_and_stable_uuid(self):
        # Break: codec assumptions reject real Core output or generate another UUID.
        first, _, old = fixture().to_core_records()
        source = first.source_refs[0]
        inventory = Inventory(SCOPE, "full", (SourceRow(first.message, "101", source, "synthetic-row"),),
            (SourceDescriptor(source.source_key, source.table_name, True, "synthetic-schema"),),
            (SourceCount(source.source_key, 1, 1, 1, 1),), (), InventoryDigests("set", "rows", "scope"))
        legacy = tuple(replace(record, canonical_identity=None, scope=None, binding_status="",
                               source_refs=(), legacy_aliases=(), technical_observations=()) for record in (first, old))
        migrated = plan_identity_migration(legacy, inventory, ScopeAuthorization(SCOPE, True))
        bundle = StateBundle(SCOPE, "synthetic-archive", 1, migrated.records, ())
        actual = decode_state(encode_state(bundle), SCOPE)
        self.assertIsInstance(actual, StateBundle)
        self.assertEqual("server:101", actual.records[0].identity)
        self.assertEqual(migrated.records[1].identity, actual.records[1].identity)
        self.assertEqual("old-image.png", actual.records[0].media_asset)
        self.assertEqual(first.message.content, actual.records[0].message.content)
        self.assertEqual((source,), actual.records[0].source_refs)
        self.assertEqual(bundle, decode_state(encode_state(actual), SCOPE))

    def test_full_count_report_cannot_claim_unknown_archive_only_count(self):
        # R1 break: a checksum-valid full report accepts quick-only Unknown.
        quick = CountReport("quick", "quick_window", 0, 0, 0, 0, 0, None,
                            3, 0, 0, 3, None, None, 0, 0, "NOT_CHECKED")
        valid = replace(fixture(), metadata=tuple(asdict(quick).items()))
        restored = decode_state(payload(wire(valid)), SCOPE)
        self.assertEqual(valid, restored, "actual Core quick Unknown metadata must remain accepted")
        document = wire(valid)
        document["schema"]["metadata"] = [[key, "full" if key in ("mode", "count_scope") else value]
                                              for key, value in document["schema"]["metadata"]]
        self.rejects(document)
        with self.assertRaises(ValueError):
            encode_state(replace(valid, metadata=tuple((key, "full" if key in ("mode", "count_scope") else value)
                                                       for key, value in valid.metadata)))

    def test_actual_core_negative_timestamp_metadata_roundtrips_losslessly(self):
        # R2 break: timestamps are treated as nonnegative counts rather than exact ints.
        historical = fixture().to_core_records()[2]
        historical = replace(historical, message=replace(historical.message, create_time=-20),
                             legacy_aliases=({**historical.legacy_aliases[0], "create_time": -20},))
        report = CountReport("full", "full", 0, 0, 0, 0, 0, 1,
                             1, 0, 0, 1, -20, -20, 0, 0, "NOT_CHECKED")
        bundle = StateBundle(SCOPE, "synthetic-archive", 1, (historical,), tuple(asdict(report).items()))
        restored, error_name = None, None
        try:
            restored = decode_state(encode_state(bundle), SCOPE)
        except Exception as error:
            error_name = type(error).__name__
        self.assertIsNone(error_name, "valid Core negative timestamps must roundtrip; observed " + str(error_name))
        self.assertEqual(bundle, restored)
        self.assertEqual(-20, dict(restored.metadata)["earliest_timestamp"])
        self.assertEqual(-20, dict(restored.metadata)["latest_timestamp"])

    def test_timestamp_metadata_rejects_boolean_without_coercion(self):
        # R2 boundary: accepting signed exact ints must not admit bool-as-int.
        for key in ("earliest_timestamp", "latest_timestamp"):
            document = wire()
            document["schema"]["metadata"] = [[key, True]]
            self.rejects(document)


if __name__ == "__main__":
    unittest.main()
