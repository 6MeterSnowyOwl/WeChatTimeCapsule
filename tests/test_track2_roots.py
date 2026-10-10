"""Behavior-level physical proofs using real synthetic Windows objects.

Each test names a safety break. Unsupported fixture capabilities FAIL with
BLOCKED, never skip into proof PASS. Fault injection below the adapter only
models unsupported OS evidence; the ordinary/link/stream fixtures are real.
"""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from wechat_exporter import physical_paths as physical
from wechat_exporter.archive import ArchivedMessage
from wechat_exporter.models import ChatMessage
from wechat_exporter.history_contracts import (
    ArchiveScope, SourceKey, SourceLocator, Inventory, InventoryDigests, SourceDescriptor,
    SourceRow, SourceCount,
    ScopeAuthorization,
)
from wechat_exporter.message_identity import resolve_canonical_rows
from wechat_exporter.identity_migration import plan_identity_migration
from wechat_exporter.track2_contracts import (
    FileIdentity, RootSelection, RootProof, Track2Error, StateBundle, canonical_bytes, contract_digest,
    decode_dto, dto_document,
)
from track2_fixtures import synthetic_roots, legacy_proof, junction, symlink, native_handle_facts


class PhysicalRootsTests(unittest.TestCase):
    def snapshot(self, root):
        result = physical.snapshot_tree(root)
        self.assertIsNotNone(result, "ordinary tree must produce a complete handle proof")
        return result

    def proof(self, selection, legacy_root, source):
        legacy = legacy_proof(legacy_root, self.snapshot(legacy_root).root.identity)
        result = physical.prove_roots(selection, legacy, (source,))
        self.assertIsNotNone(result, "independent roots must produce a proof")
        return result, legacy

    def rejects(self, callback, code="ROOT_PROOF_FAILED"):
        with self.assertRaises(Track2Error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_independent_byte_copies_have_equal_content_and_disjoint_handle_ids(self):
        with synthetic_roots() as (_, selection, legacy, source):
            before = self.snapshot(selection.input_v2)
            copied = self.snapshot(selection.candidate_v3)
            self.assertEqual(before.content_digest, copied.content_digest)
            self.assertNotEqual(before.physical_digest, copied.physical_digest)
            self.assertNotEqual(before.root.identity, copied.root.identity)
            self.assertEqual((3, 1), (before.entry_count, before.file_count))
            self.assertEqual([".", "empty-directory", "ordinary.txt"], [e.relative_path for e in before.entries])
            entry = before.entries[-1]
            self.assertEqual(17, entry.size)
            self.assertEqual(hashlib.sha256(b"synthetic-payload").hexdigest(), entry.sha256)
            self.assertEqual(1, entry.identity.link_count)
            self.assertEqual(32, len(entry.identity.file_id_128))
            native = native_handle_facts(selection.input_v2 / "ordinary.txt")
            self.assertEqual((native["volume_serial"], native["file_id_128"]),
                             (entry.identity.volume_serial, entry.identity.file_id_128))
            self.assertEqual(0, native["reparse_tag"])
            print("SYNTHETIC_ORDINARY_FILE_HANDLE=" + json.dumps(native, sort_keys=True))
            directory_native = native_handle_facts(selection.input_v2)
            self.assertEqual(before.root.identity.file_id_128, directory_native["file_id_128"])
            self.assertTrue(directory_native["directory"])
            print("SYNTHETIC_DIRECTORY_HANDLE=" + json.dumps(directory_native, sort_keys=True))
            self.assertTrue(before.root.final_path)
            self.assertTrue(before.root.ancestor_ids)
            self.proof(selection, legacy, source)
            self.assertEqual(b"synthetic-payload", (selection.input_v2 / "ordinary.txt").read_bytes())

    def test_repeated_proofs_ignore_capture_time_and_remain_read_only(self):
        with synthetic_roots() as (_, selection, legacy, source):
            first, legacy_proven = self.proof(selection, legacy, source)
            second = physical.prove_roots(selection, legacy_proven, (source,))
            self.assertEqual(first.topology_digest, second.topology_digest)
            a, b = self.snapshot(selection.input_v2), self.snapshot(selection.input_v2)
            self.assertEqual(a, b)
            self.assertNotEqual(a.captured_at, b.captured_at)
            altered = replace(a, captured_at="2000-01-01T00:00:00+00:00")
            self.assertEqual(canonical_bytes(a), canonical_bytes(altered))
            self.assertEqual(first, physical.revalidate_root_proof(first, legacy_proven))

    def test_same_root_roles_are_rejected_even_for_payload_equal_bytes(self):
        with synthetic_roots() as (_, selection, legacy, source):
            proven_legacy = legacy_proof(legacy, self.snapshot(legacy).root.identity)
            self.rejects(lambda: physical.prove_roots(replace(selection, candidate_v3=selection.input_v2), proven_legacy, (source,)))
            # Activation later retains this candidate ID; it is not a seventh root.
            proof, _ = self.proof(selection, legacy, source)
            self.assertEqual(self.snapshot(selection.candidate_v3).root.identity,
                             dict(proof.root_ids)["candidate_v3"])

    def test_case_aliases_are_physical_equality_not_independent_roots(self):
        with synthetic_roots() as (_, selection, legacy, source):
            proven_legacy = legacy_proof(legacy, self.snapshot(legacy).root.identity)
            alias = Path(str(selection.input_v2).upper())
            self.rejects(lambda: physical.prove_roots(replace(selection, candidate_v3=alias), proven_legacy, (source,)))

    def test_normalized_parent_child_overlap_is_rejected(self):
        with synthetic_roots() as (_, selection, legacy, source):
            child = selection.input_v2 / "child"
            child.mkdir()
            selected = replace(selection, safety_backup=child / ".." / "child")
            proven_legacy = legacy_proof(legacy, self.snapshot(legacy).root.identity)
            self.rejects(lambda: physical.prove_roots(selected, proven_legacy, (source,)))

    def test_source_and_legacy_discovery_overlap_is_rejected(self):
        with synthetic_roots() as (_, selection, legacy, source):
            proven_legacy = legacy_proof(legacy, self.snapshot(legacy).root.identity)
            self.rejects(lambda: physical.prove_roots(selection, proven_legacy, (selection.reader_work,)))
            self.rejects(lambda: physical.prove_roots(replace(selection, namespace=legacy), proven_legacy, (source,)), "LEGACY_NAMESPACE_OVERLAP")
            child = legacy / "nested-candidate"
            child.mkdir()
            self.rejects(lambda: physical.prove_roots(replace(selection, candidate_v3=child), proven_legacy, (source,)), "LEGACY_NAMESPACE_OVERLAP")

    def test_hardlinked_input_is_rejected_without_modifying_original(self):
        with synthetic_roots() as (_, selection, _, source):
            original = selection.input_v2 / "ordinary.txt"
            os.link(original, source / "linked.bin")
            self.assertEqual(2, original.stat().st_nlink)
            native = native_handle_facts(original)
            self.assertEqual(2, native["link_count"])
            print("SYNTHETIC_HARDLINK_HANDLE=" + json.dumps(native, sort_keys=True))
            self.rejects(lambda: physical.snapshot_tree(selection.input_v2))
            self.assertEqual(b"synthetic-payload", original.read_bytes())
            self.assertEqual(2, original.stat().st_nlink)

    def test_cross_tree_shared_file_ids_are_rejected(self):
        with synthetic_roots() as (_, selection, legacy, source):
            os.link(selection.input_v2 / "ordinary.txt", selection.staging / "shared.bin")
            # Assert real shared identity before invoking the proof adapter.
            self.assertEqual((selection.input_v2 / "ordinary.txt").stat().st_ino,
                             (selection.staging / "shared.bin").stat().st_ino)
            proven_legacy = legacy_proof(legacy, self.snapshot(legacy).root.identity)
            self.rejects(lambda: physical.prove_roots(selection, proven_legacy, (source,)))

    def test_junction_root_and_junction_entry_are_rejected(self):
        with synthetic_roots() as (base, selection, _, _):
            alias = base / "junction"
            junction(alias, selection.input_v2)
            try:
                self.assertTrue(alias.is_junction())
                native = native_handle_facts(alias)
                self.assertEqual(0xA0000003, native["reparse_tag"])
                print("SYNTHETIC_JUNCTION_HANDLE=" + json.dumps(native, sort_keys=True))
                self.rejects(lambda: physical.snapshot_tree(alias))
                self.rejects(lambda: physical.snapshot_tree(base))
            finally:
                os.rmdir(alias)

    def test_directory_and_file_symlinks_are_rejected(self):
        with synthetic_roots() as (base, selection, _, _):
            alias = base / "directory-symlink"
            symlink(alias, selection.input_v2, directory=True)
            try:
                self.assertTrue(alias.is_symlink())
                self.rejects(lambda: physical.snapshot_tree(alias))
                child = alias / "empty-directory"
                self.rejects(lambda: physical.snapshot_tree(child))
            finally:
                alias.unlink()
            file_link = selection.staging / "file-symlink"
            symlink(file_link, selection.input_v2 / "ordinary.txt", directory=False)
            try:
                self.assertTrue(file_link.is_symlink())
                self.rejects(lambda: physical.snapshot_tree(selection.staging))
            finally:
                file_link.unlink()

    def test_file_and_directory_ads_are_rejected(self):
        with synthetic_roots() as (_, selection, _, _):
            for target in (selection.input_v2 / "ordinary.txt", selection.staging):
                stream = Path(str(target) + ":synthetic-proof-stream")
                stream.write_bytes(b"synthetic-hidden-data")
                try:
                    self.assertEqual(b"synthetic-hidden-data", stream.read_bytes())
                    print("SYNTHETIC_ADS_PRESENT=" + ("file" if target.is_file() else "directory"))
                    self.rejects(lambda: physical.snapshot_tree(selection.input_v2 if target.is_file() else target))
                finally:
                    stream.unlink()

    def test_replaced_root_and_replaced_ancestor_invalidate_receipt(self):
        with synthetic_roots() as (base, selection, legacy, source):
            proof, proven_legacy = self.proof(selection, legacy, source)
            selection.candidate_v3.rename(base / "held-candidate")
            selection.candidate_v3.mkdir()
            self.rejects(lambda: physical.revalidate_root_proof(proof, proven_legacy))
            parent = base / "approved-parent"
            parent.mkdir()
            destination = parent / "new-candidate"
            parent_proof = physical.prove_destination_parent(destination)
            self.assertIsNotNone(parent_proof)
            parent.rename(base / "held-parent")
            parent.mkdir()
            self.rejects(lambda: physical.revalidate_destination_parent(parent_proof))

    def test_bootstrap_parent_proof_binds_missing_suffix_and_nonexistence(self):
        with synthetic_roots() as (base, _, _, _):
            destination = base / "future-parent" / "future-leaf"
            proof = physical.prove_destination_parent(destination)
            self.assertIsNotNone(proof, "missing destination needs a parent approval")
            self.assertEqual(("future-parent", "future-leaf"), proof.requested_leaf_names)
            self.assertEqual(base, proof.existing_parent)
            self.assertFalse(destination.exists())
            self.assertEqual(proof, physical.revalidate_destination_parent(proof))
            destination.parent.mkdir()
            self.rejects(lambda: physical.revalidate_destination_parent(proof))
            destination.mkdir()
            self.rejects(lambda: physical.prove_destination_parent(destination))

    def test_bootstrap_parent_symlink_and_stream_syntax_fail_closed(self):
        with synthetic_roots() as (base, selection, _, _):
            alias = base / "destination-alias"
            symlink(alias, selection.staging, directory=True)
            try:
                self.rejects(lambda: physical.prove_destination_parent(alias / "new"))
            finally:
                alias.unlink()
            self.rejects(lambda: physical.prove_destination_parent(base / "leaf:stream"))
            self.rejects(lambda: physical.snapshot_tree(Path("relative")))

    def test_destination_stream_syntax_is_rejected_independently(self):
        with synthetic_roots() as (base, _, _, _):
            self.rejects(lambda: physical.prove_destination_parent(base / "leaf:stream"))

    def test_relative_snapshot_path_is_rejected_independently(self):
        self.rejects(lambda: physical.snapshot_tree(Path("relative")))

    def test_normalized_absolute_legacy_domain_uses_matching_physical_identity_safely(self):
        with synthetic_roots() as (base, selection, legacy, source):
            (base / "holder").mkdir()
            spelling = base / "holder" / ".." / "legacy"
            actual_id = self.snapshot(legacy).root.identity
            legacy_receipt = legacy_proof(spelling, actual_id)
            result, error = None, None
            try:
                result = physical.prove_roots(selection, legacy_receipt, (source,))
            except Exception as caught:
                error = caught
            self.assertTrue(error is None, "valid matching legacy domain must succeed; observed " +
                            (type(error).__name__ if error is not None else "none"))
            self.assertIsInstance(result, RootProof)
            self.assertEqual(self.snapshot(selection.candidate_v3).root.identity,
                             dict(result.root_ids)["candidate_v3"])
            self.assertEqual(contract_digest(legacy_receipt), result.legacy_namespace_digest)
            print("SYNTHETIC_NORMALIZED_LEGACY_PROOF=PASS")

    def test_owned_transaction_and_namespace_intents_are_only_derived_children(self):
        with synthetic_roots() as (_, selection, legacy, source):
            (selection.candidate_v3 / ".synthetic-transaction").mkdir()
            (selection.namespace / "operations").mkdir()
            proof, _ = self.proof(selection, legacy, source)
            owned = physical.owned_evidence_paths(proof, "synthetic-operation")
            self.assertIsNotNone(owned)
            self.assertEqual(selection.candidate_v3 / ".synthetic-transaction", owned.transaction_root)
            self.assertEqual(selection.namespace / "operations" / "synthetic-operation", owned.operation_root)
            self.rejects(lambda: physical.owned_evidence_paths(proof, "../escape"))

    def test_unknown_id_or_stream_evidence_fails_closed(self):
        with synthetic_roots() as (_, selection, _, _):
            # The adapter seam is introduced in GREEN; create=True keeps RED an assertion failure.
            for seam in ("_query_identity", "_validate_streams"):
                with patch.object(physical, seam, side_effect=OSError("synthetic-unsupported"), create=True):
                    self.rejects(lambda: physical.snapshot_tree(selection.input_v2))


class StrictContractTests(unittest.TestCase):
    def accepted(self, callback, message):
        result, error_name = None, None
        try:
            result = callback()
        except Exception as caught:
            error_name = type(caught).__name__
        self.assertIsNone(error_name, message + "; observed " + str(error_name))
        return result

    def structured_bundle(self):
        scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        payload = {"card": {"title": "synthetic", "items": [
            {"label": "first", "flags": [True, None, 3, 1.25]}]}}
        alias = {"local_id": "legacy-1", "nested": {"ids": ["legacy"]}}
        observation = {"storage_classes": ["integer", "text"], "nested": {"seen": [1, 2]}}
        message = ChatMessage("1", "卡片", 7, "synthetic-sender", 1700000000,
                              "synthetic text", 10, "synthetic-card", payload)
        source = SourceLocator(scope, SourceKey("message/message_0.db", "synthetic-file"),
                               "Msg_synthetic", "1", "123", 1700000000, 10, 1,
                               ("integer", "text", "integer", "integer", "integer"))
        record = ArchivedMessage(message, "available", "synthetic.png", "images",
                                 "synthetic-canonical-1", scope, "BOUND", (source,),
                                 (alias,), (observation,))
        plain = ArchivedMessage(ChatMessage("2", "文本", None, "", 1700000001,
                                            "synthetic plain text", 11),
                                canonical_identity="synthetic-canonical-2", scope=scope)
        bundle = StateBundle(scope, "synthetic-archive", 1, (record, plain), (("origin", "synthetic"),))
        return bundle, payload, alias, observation

    def test_state_bundle_defensively_freezes_retained_nested_payload_aliases_observations(self):
        bundle, payload, alias, observation = self.structured_bundle()
        payload["card"]["items"][0]["label"] = "changed"
        alias["nested"]["ids"][0] = "changed"
        observation["nested"]["seen"].append(99)
        record = bundle.records[0]
        self.assertEqual("first", record.message.payload["card"]["items"][0]["label"])
        self.assertEqual("legacy", record.legacy_aliases[0]["nested"]["ids"][0])
        self.assertEqual((1, 2), tuple(record.technical_observations[0]["nested"]["seen"]))

    def test_state_bundle_exposed_nested_payload_aliases_observations_are_read_only(self):
        bundle, _, _, _ = self.structured_bundle()
        record = bundle.records[0]
        mappings = (record.message.payload, record.message.payload["card"]["items"][0],
                    record.legacy_aliases[0], record.legacy_aliases[0]["nested"],
                    record.technical_observations[0], record.technical_observations[0]["nested"])
        for mapping in mappings:
            with self.subTest(kind="mapping"):
                with self.assertRaises(TypeError):
                    mapping["unexpected"] = "mutation"
        arrays = (record.message.payload["card"]["items"],
                  record.message.payload["card"]["items"][0]["flags"],
                  record.legacy_aliases[0]["nested"]["ids"],
                  record.technical_observations[0]["nested"]["seen"])
        for array in arrays:
            with self.subTest(kind="array"):
                with self.assertRaises(TypeError):
                    array[0] = "mutation"

    def test_state_bundle_nonempty_records_roundtrip_and_stable_digest(self):
        bundle, payload, alias, observation = self.structured_bundle()
        document = self.accepted(lambda: dto_document(bundle), "structured accepted state must serialize")
        records = document["fields"]["records"]
        structured_message = records[0]["fields"]["message"]["fields"]
        self.assertEqual({"card": {"title": "synthetic", "items": [
            {"label": "first", "flags": [True, None, 3, 1.25]}]}}, structured_message["payload"])
        self.assertEqual("synthetic-card", structured_message["subtype"])
        self.assertIsNone(records[1]["fields"]["message"]["fields"]["payload"])
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")
        restored = self.accepted(lambda: decode_dto(StateBundle, encoded), "nonempty state must round-trip")
        self.assertEqual(bundle, restored)
        self.assertEqual(canonical_bytes(bundle), canonical_bytes(restored))
        digest = contract_digest(bundle)
        payload["card"]["items"].clear()
        alias.clear()
        observation["nested"]["seen"].append(999)
        self.assertEqual(digest, contract_digest(bundle))
        self.assertEqual("synthetic-canonical-1", restored.records[0].canonical_identity)
        self.assertEqual("synthetic-canonical-2", restored.records[1].canonical_identity)
        self.assertEqual("123", restored.records[0].source_refs[0].expected_server_id)
        self.assertEqual(7, restored.records[0].message.sender_id)
        self.assertIsNone(restored.records[1].message.sender_id)
        self.assertEqual("legacy-1", restored.records[0].legacy_aliases[0]["local_id"])
        self.assertEqual("text", restored.records[0].technical_observations[0]["storage_classes"][1])

    def test_state_bundle_plain_record_roundtrip_resolves_core_names(self):
        scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        record = ArchivedMessage(ChatMessage("plain", "文本", "sender", "", 1700000000, "text", 1),
                                 scope=scope, canonical_identity="synthetic-plain")
        bundle = StateBundle(scope, "synthetic-archive", 0, (record,), ())
        document = self.accepted(lambda: dto_document(bundle), "plain accepted state must serialize")
        restored = self.accepted(lambda: decode_dto(StateBundle, document), "Core TYPE_CHECKING names must resolve locally")
        self.assertEqual(bundle, restored)

    def test_state_bundle_structured_record_preserves_core_representation_equality(self):
        bundle, payload, _, _ = self.structured_bundle()
        authority = replace(bundle.records[0], canonical_identity="server:123", technical_observations=())
        state = StateBundle(bundle.scope, bundle.archive_id, 1, (authority,), ())
        source = authority.source_refs[0]
        source_message = replace(authority.message, payload=payload)
        inventory = Inventory(bundle.scope, "full", (SourceRow(source_message, "123", source, "synthetic-row"),),
                              (SourceDescriptor(source.source_key, source.table_name, True, "synthetic-schema"),),
                              (SourceCount(source.source_key, 1, 1, 1, 1),), (),
                              InventoryDigests("synthetic-set", "synthetic-rows", "synthetic-scope"))
        resolved = self.accepted(lambda: resolve_canonical_rows(inventory, state.records),
                                 "read-only structured records must retain Core comparison semantics")
        self.assertEqual((), resolved.conflicts, "identical source payload must not create REPRESENTATION_CONFLICT")
        self.assertEqual(payload, state.records[0].message.payload)
        self.assertEqual(state.records[0].message.payload, payload)

    def test_state_bundle_core_handoff_is_defensive_and_restores_json_container_types(self):
        bundle, _, _, _ = self.structured_bundle()
        baseline = contract_digest(bundle)
        handed = self.accepted(lambda: bundle.to_core_records(), "Core dict consumers need a defensive handoff")
        self.assertEqual(tuple, type(handed))
        self.assertEqual(ArchivedMessage, type(handed[0]))
        self.assertEqual(ChatMessage, type(handed[0].message))
        self.assertEqual(dict, type(handed[0].message.payload))
        self.assertEqual(list, type(handed[0].message.payload["card"]["items"]))
        self.assertEqual(dict, type(handed[0].legacy_aliases[0]))
        self.assertEqual(dict, type(handed[0].technical_observations[0]))
        handed[0].message.payload["card"]["items"][0]["label"] = "changed"
        handed[0].legacy_aliases[0]["nested"]["ids"][0] = "changed"
        handed[0].technical_observations[0]["nested"]["seen"].append(999)
        self.assertEqual(baseline, contract_digest(bundle))
        fresh = bundle.to_core_records()
        self.assertEqual("first", fresh[0].message.payload["card"]["items"][0]["label"])
        self.assertEqual("legacy", fresh[0].legacy_aliases[0]["nested"]["ids"][0])
        self.assertEqual([1, 2], fresh[0].technical_observations[0]["nested"]["seen"])

    def test_state_bundle_valid_core_alias_scope_roundtrip_and_migration_handoff(self):
        template, payload, _, _ = self.structured_bundle()
        source = template.records[0].source_refs[0]
        alias = {"scope": template.scope, "local_id": "legacy-1", "create_time": 1700000000,
                 "sort_seq": 10, "identity": "local:legacy-1"}
        legacy = replace(template.records[0], canonical_identity=None, scope=None,
                         legacy_aliases=(alias,), technical_observations=())
        bundle = self.accepted(lambda: StateBundle(template.scope, template.archive_id, 1, (legacy,), ()),
                               "approved Core alias with typed scope must remain accepted")
        document = dto_document(bundle)
        alias_wire = document["fields"]["records"][0]["fields"]["legacy_aliases"][0]
        self.assertEqual("ArchiveScope", alias_wire["scope"]["dto_type"])
        self.assertEqual({"account_id": "synthetic-account", "conversation_id": "synthetic-conversation"},
                         alias_wire["scope"]["fields"])
        restored = decode_dto(StateBundle, json.dumps(document))
        self.assertEqual(bundle, restored)
        self.assertEqual(bundle.scope, restored.records[0].legacy_aliases[0]["scope"])
        handed = bundle.to_core_records()
        source_message = replace(legacy.message, payload=payload)
        inventory = Inventory(bundle.scope, "full", (SourceRow(source_message, "123", source, "synthetic-row"),),
                              (SourceDescriptor(source.source_key, source.table_name, True, "synthetic-schema"),),
                              (SourceCount(source.source_key, 1, 1, 1, 1),), (),
                              InventoryDigests("synthetic-set", "synthetic-rows", "synthetic-scope"))
        migrated = self.accepted(lambda: plan_identity_migration(handed, inventory, ScopeAuthorization(bundle.scope, True)),
                                 "defensive handoff must retain approved Core migration alias semantics")
        self.assertEqual(1, migrated.legacy_matched_count)
        self.assertEqual("server:123", migrated.records[0].canonical_identity)
        digest = contract_digest(bundle)
        alias["local_id"] = "changed-retained"
        handed[0].legacy_aliases[0]["local_id"] = "changed-handed"
        self.assertEqual(digest, contract_digest(bundle))
        self.assertEqual("legacy-1", bundle.to_core_records()[0].legacy_aliases[0]["local_id"])
        bad = json.loads(json.dumps(document))
        bad["fields"]["records"][0]["fields"]["legacy_aliases"][0]["scope"]["fields"]["unknown"] = True
        with self.assertRaises(ValueError) as caught:
            decode_dto(StateBundle, bad)
        self.assertEqual("INVALID_TRACK2_CONTRACT", str(caught.exception))

    def test_state_bundle_rejects_nested_unknown_fields_and_wrong_scalar_types(self):
        bundle, _, _, _ = self.structured_bundle()
        original = self.accepted(lambda: dto_document(bundle), "structured state must produce a typed document")
        for kind in ("message-extra", "record-extra", "sender-bool", "time-text", "scope-account-int",
                     "locator-extra", "locator-storage-int", "version-bool", "payload-set"):
            document = json.loads(json.dumps(original))
            record = document["fields"]["records"][0]
            message = record["fields"]["message"]
            if kind == "message-extra":
                message["fields"]["unknown"] = True
            elif kind == "record-extra":
                record["fields"]["unknown"] = True
            elif kind == "sender-bool":
                message["fields"]["sender_id"] = True
            elif kind == "time-text":
                message["fields"]["create_time"] = "1700000000"
            elif kind == "scope-account-int":
                record["fields"]["scope"]["fields"]["account_id"] = 7
            elif kind == "locator-extra":
                record["fields"]["source_refs"][0]["fields"]["unknown"] = 7
            elif kind == "locator-storage-int":
                record["fields"]["source_refs"][0]["fields"]["storage_classes"][0] = 7
            elif kind == "version-bool":
                message["contract_version"] = True
            elif kind == "payload-set":
                message["fields"]["payload"]["unexpected"] = {1, 2}
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    decode_dto(StateBundle, document)
                self.assertEqual("INVALID_TRACK2_CONTRACT", str(caught.exception))

    def test_canonical_serializer_is_versioned_utf8_and_strict(self):
        value = FileIdentity(7, "01" * 16, 1)
        expected = ('{"contract_version":"track2-v1","dto_type":"FileIdentity",'
                    '"fields":{"file_id_128":"01010101010101010101010101010101",'
                    '"link_count":1,"volume_serial":7}}').encode("utf8")
        self.assertEqual(expected, canonical_bytes(value))
        document = dto_document(value)
        self.assertIsNotNone(document)
        self.assertEqual(value, decode_dto(FileIdentity, document))

    def test_unknown_fields_versions_types_and_invalid_ids_are_rejected(self):
        value = FileIdentity(7, "01" * 16, 1)
        document = dto_document(value)
        self.assertIsNotNone(document)
        for change in ({"extra": True}, {"contract_version": "unknown"}, {"dto_type": "RootSelection"}):
            self.assertRaises(ValueError, decode_dto, FileIdentity, {**document, **change})
        fields = document["fields"]
        self.assertRaises(ValueError, decode_dto, FileIdentity, {**document, "fields": {**fields, "extra": True}})
        for args in ((True, "01" * 16, 1), (7, "bad", 1), (7, "00" * 16, 1), (7, "01" * 16, 0)):
            self.assertRaises(ValueError, FileIdentity, *args)

    def test_immutable_root_selection_rejects_relative_and_diagnostics_hide_paths(self):
        with synthetic_roots() as (_, selection, _, _):
            with self.assertRaises(FrozenInstanceError):
                selection.input_v2 = Path("different")
            self.assertNotIn(str(selection.input_v2), repr(selection))
            self.assertRaises(ValueError, replace, selection, input_v2=Path("relative"))
            self.assertRaises(ValueError, Track2Error, "NOT_A_TRACK2_CODE")


if __name__ == "__main__":
    unittest.main()
