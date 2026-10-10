"""GREEN_FIRST bounded synthetic read-only composition; never end-to-end proof.

Fixture setup writes only disposable ordinary Windows directories. No runtime
proof, fork receipt, READY issuer, permit, activation/save writer or fake session
is manufactured. Quoted candidate states and handmade bindings are metadata.
"""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from operation_root_fixtures import bind_metadata_context
import io
import json
from pathlib import Path
import tempfile
import unittest

from wechat_exporter.archive import load_message_store
from wechat_exporter.models import ChatMessage
from wechat_exporter.history_contracts import (ArchiveScope, HistoryError,
    Inventory, InventoryDigests, SourceCount, SourceDescriptor, SourceKey,
    SourceLocator, SourceRow)
from wechat_exporter.identity_migration import plan_identity_migration
from wechat_exporter.message_identity import resolve_canonical_rows
from wechat_exporter.physical_paths import (prove_destination_parent,
    prove_existing_root, snapshot_tree)
from wechat_exporter.track2_contracts import (ArchiveOriginConfirmation,
    AuthorityBinding, CandidateRecord, ForkApproval, LegacyNamespaceProof,
    RootSelection, StateBundle, Track2Error, contract_digest)
from wechat_exporter.fork_authorization import (authorize_archive_origin,
    authorize_fork)
from wechat_exporter.namespace_lock import authority_lock, require_live_lease
from wechat_exporter.v3_profile import open_v3_profile
from wechat_exporter.v3_registry import (read_binding, read_candidates,
    register_candidate, register_preserved_v2)
from wechat_exporter.v3_state import (decode_state, encode_messages,
    encode_schema, encode_state)
from wechat_exporter.v3_schema import inspect_v3_schema
from wechat_exporter.v3_capsule import plan_current_managed_leaves
from wechat_exporter.candidate_lifecycle import (admit_candidate_action,
    source_inventory_digest)
from wechat_exporter.authority_activation import compare_activation_request
from wechat_exporter.v3_save import compare_active_save
from wechat_exporter.v3_ui import Selection, V3Controller
from wechat_exporter.v3_entrypoint import main

SCOPE = ArchiveScope("synthetic-account", "synthetic-conversation")
ARCHIVE_ONLY = "archive:123456781234423482341234567890ab"
MEDIA = b"synthetic preserved media bytes\x00\xff"


class BoundedSyntheticAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-t10-readonly-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.v2, self.legacy, self.namespace = (self.base / name for name in
            ("preserved-v2", "legacy-settings", "WeChatTimeCapsule-v3"))
        for root in (self.v2, self.legacy):
            root.mkdir()
        for relative in ("state/assets/images", "snapshots/old/empty", "empty-directory"):
            (self.v2 / relative).mkdir(parents=True)
        self.old_messages = (
            ChatMessage("7", "图片", 7, "sender", 1700000000, "保留正文", 1,
                        "card", {"items": [1, "1", None]}),
            ChatMessage("lost", "文本", None, "sender", 1700000002, "archive-only body", 3),
        )
        # Literal v2 wire records are independent of the new codec serializer.
        self.v2_wire = (b'{"identity":"local:7","local_id":"7","message_type":"' +
            "图片".encode("utf8") + b'","sender_id":7,"sender_username":"sender",'
            b'"create_time":1700000000,"content":"' + "保留正文".encode("utf8") +
            b'","sort_seq":1,"subtype":"card","payload":{"items":[1,"1",null]},'
            b'"media":{"status":"success","asset_name":"old.png","kind":"images"}}\n'
            b'{"identity":"local:lost","local_id":"lost","message_type":"' +
            "文本".encode("utf8") + b'","sender_id":null,"sender_username":"sender",'
            b'"create_time":1700000002,"content":"archive-only body","sort_seq":3,"media":null}\n')
        (self.v2 / "state/messages.jsonl").write_bytes(self.v2_wire)
        (self.v2 / "state/assets/images/old.png").write_bytes(MEDIA)
        (self.legacy / "settings.json").write_bytes(b'{"known_archive_roots":[]}')
        (self.legacy / "archive_registry.json").write_bytes(b'{"archives":[],"bootstrap_completed":false}')
        self.v2_before, self.legacy_before = snapshot_tree(self.v2), snapshot_tree(self.legacy)
        self.addCleanup(self.assert_originals_unchanged)
        self.roots = RootSelection(self.v2, self.base / "codec-fixture",
            self.namespace, self.base / "staging", self.base / "reader", None)
        self.origin = ArchiveOriginConfirmation("synthetic-origin", SCOPE,
            self.v2_before.root.identity, self.v2_before.content_digest,
            self.v2_before.physical_digest, True)
        self.approval = ForkApproval("synthetic-metadata-only", "SYNTHETIC",
            "synthetic-object", SCOPE, self.v2_before, contract_digest(self.origin),
            tuple(prove_destination_parent(path) for path in (self.roots.candidate_v3,
                self.roots.namespace, self.roots.staging, self.roots.reader_work)), "1" * 64,
            False, "2" * 64, "track2-v1", None, "1" * 64)
        self.approval, self.root_context = bind_metadata_context(self.approval, self.roots)
        self.authorization = authorize_archive_origin(self.origin, self.approval,
            self.v2_before, SCOPE)
        keys = tuple(SourceKey("message/message_%d.db" % i, "synthetic-incarnation-%d" % i)
                     for i in (0, 1))
        rows = []
        for index, message, server in ((0, self.old_messages[0], "101"),
                (1, replace(self.old_messages[0], local_id="70"), "101"),
                (1, ChatMessage("7", "文本", 7, "sender", 1700000003, "new body", 4), "103")):
            locator = SourceLocator(SCOPE, keys[index], "Msg_synthetic", message.local_id,
                server, message.create_time, message.sort_seq, 1, ("integer",) * 5)
            rows.append(SourceRow(message, server, locator, "synthetic-row-%d" % len(rows)))
        self.inventory = Inventory(SCOPE, "full", tuple(rows),
            tuple(SourceDescriptor(key, "Msg_synthetic", True, "synthetic-schema") for key in keys),
            (SourceCount(keys[0], 1, 1, 1, 1), SourceCount(keys[1], 2, 2, 2, 2)), (),
            InventoryDigests("3" * 64, "4" * 64, "5" * 64))

    def assert_originals_unchanged(self):
        # Real Windows FileIDs, bytes and all directories are compared after
        # every phase and again through cleanup; no private archive is opened.
        self.assertEqual(self.v2_before, snapshot_tree(self.v2))
        self.assertEqual(self.legacy_before, snapshot_tree(self.legacy))
        self.assertEqual(self.v2_wire, (self.v2 / "state/messages.jsonl").read_bytes())
        self.assertEqual(MEDIA, (self.v2 / "state/assets/images/old.png").read_bytes())

    def canonical_bundle(self):
        loaded = tuple(load_message_store(self.v2 / "state/messages.jsonl"))
        self.assertEqual(self.old_messages, tuple(record.message for record in loaded))
        migration = plan_identity_migration(loaded, self.inventory, self.authorization)
        # Freeze the random archive-only UUID in test metadata only so the
        # independently stated expected identity remains exact across rounds.
        records = (migration.records[0], replace(migration.records[1], canonical_identity=ARCHIVE_ONLY))
        self.assert_originals_unchanged()
        return StateBundle(SCOPE, "synthetic-candidate", 1, records, ())

    def codec_fixture(self):
        bundle = self.canonical_bundle()
        root = self.roots.candidate_v3
        (root / "state").mkdir(parents=True)
        (root / "state/schema.json").write_bytes(encode_schema(bundle))
        (root / "state/messages.jsonl").write_bytes(encode_messages(bundle))
        return root, bundle

    def refusal(self, code, callback, error=Track2Error):
        with self.assertRaises(error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))
        self.assert_originals_unchanged()

    def test_namespace_real_lease_candidate_index_and_preserved_reference_never_activate(self):
        # Break: index registration begins issuing ACTIVE authority or touches v2.
        root, bundle = self.codec_fixture()
        legacy = LegacyNamespaceProof((self.legacy,), (), "6" * 64, "7" * 64,
            ((self.legacy, prove_existing_root(self.legacy).identity),))
        profile = open_v3_profile(self.namespace, "SYNTHETIC", legacy)
        candidate = CandidateRecord(bundle.archive_id, "synthetic-object", SCOPE,
            "SYNTHETIC", prove_existing_root(root).identity, "BUILDING", None, None, "synthetic-operation")
        preserved = AuthorityBinding("synthetic-object", SCOPE, 0,
            "PRESERVED_V2_READONLY", "synthetic-v2", self.v2_before.root.identity, None, None, None)
        with authority_lock(profile, "synthetic-register", timeout_seconds=0) as lease:
            require_live_lease(profile, lease)
            register_preserved_v2(profile, preserved, self.v2, lease)
            register_candidate(profile, candidate, root, lease)
            self.assertEqual((candidate,), read_candidates(profile, "synthetic-object"))
            self.assertEqual(preserved, read_binding(profile, "synthetic-object"))
            self.assert_originals_unchanged()
        self.refusal("WRITER_BUSY", lambda: register_candidate(profile, candidate, root, lease))
        self.assertFalse((self.namespace / "objects/synthetic-object/binding.json").exists())
        self.assertFalse((self.v2 / "authority.lock").exists())
        self.assertEqual(bundle, inspect_v3_schema(root))

    def test_core_migration_codec_retains_v2_body_payload_media_and_archive_only(self):
        # Break: new codec drops archived body, payload, alias or media authority.
        root, bundle = self.codec_fixture()
        restored = inspect_v3_schema(root)
        self.assertEqual(bundle, restored)
        self.assertEqual(["server:101", ARCHIVE_ONLY], [record.identity for record in restored.records])
        self.assertEqual(self.old_messages, tuple(record.message for record in restored.records))
        self.assertEqual(("success", "old.png", "images"),
            (restored.records[0].media_status, restored.records[0].media_asset, restored.records[0].media_kind))
        self.assertEqual(bundle, decode_state(encode_state(restored), SCOPE))
        self.assertEqual(("local:lost",), tuple(alias["identity"] for alias in restored.records[1].legacy_aliases))
        self.assert_originals_unchanged()

    def test_core_resolution_rebinds_both_shards_and_preserves_archive_first_content(self):
        # Break: shard-local IDs erase provenance or source text replaces archive.
        bundle = self.canonical_bundle()
        altered = replace(self.inventory, rows=tuple(replace(row,
            message=replace(row.message, content="source differs")) if row.server_id == "101" else row
            for row in self.inventory.rows))
        resolved = resolve_canonical_rows(altered, bundle.records)
        self.assertEqual(["server:101", "server:103"], [record.identity for record in resolved.records])
        self.assertEqual("保留正文", resolved.records[0].message.content)
        self.assertEqual("new body", resolved.records[1].message.content)
        self.assertEqual(["message/message_0.db", "message/message_1.db"],
            [ref.source_key.relative_db for ref in resolved.records[0].source_refs])
        self.assertEqual(("7", "7", "101", "103", 1700000000, 1700000003),
            (resolved.records[0].message.local_id, resolved.records[1].message.local_id,
             resolved.records[0].source_refs[0].expected_server_id,
             resolved.records[1].source_refs[0].expected_server_id,
             resolved.records[0].message.create_time, resolved.records[1].message.create_time))
        self.assertEqual(1, resolved.canonical_duplicate_count)
        self.assertEqual(2, sum(conflict.count for conflict in resolved.conflicts))
        # Core resolver returns source-observed rows only. It is not the union
        # runner; the separately retained archive-only record remains exact.
        self.assertEqual(ARCHIVE_ONLY, bundle.records[1].identity)
        self.assert_originals_unchanged()

    def test_complete_inventory_comparison_detects_same_count_source_changes(self):
        # Break: comparing cached digests/counts alone accepts altered source rows.
        original = source_inventory_digest(self.inventory)
        token_only = replace(self.inventory, rows=tuple(replace(row, raw_row_token="new-token-%d" % i)
            for i, row in enumerate(self.inventory.rows)))
        self.assertEqual(original, source_inventory_digest(token_only))
        for changed in (replace(self.inventory, rows=(replace(self.inventory.rows[0],
                message=replace(self.inventory.rows[0].message, content="changed")),) + self.inventory.rows[1:]),
                replace(self.inventory, sources=(replace(self.inventory.sources[0],
                    schema_fingerprint="changed"), self.inventory.sources[1]))):
            self.assertNotEqual(original, source_inventory_digest(changed))
        self.refusal("SOURCE_CHANGED", lambda: source_inventory_digest(replace(self.inventory,
            counts=(SourceCount(self.inventory.sources[0].key, 2, 2, 2, 2), self.inventory.counts[1]))))

    def test_exact_legacy_alias_in_two_shards_is_refused_without_guessing(self):
        # Break: two raw legacy counterpart candidates silently select one.
        loaded = tuple(load_message_store(self.v2 / "state/messages.jsonl"))
        second = self.inventory.rows[1]
        duplicate = replace(second, message=replace(second.message, local_id="7"),
            locator=replace(second.locator, local_id="7"))
        ambiguous = replace(self.inventory, rows=(self.inventory.rows[0], duplicate,
            self.inventory.rows[2]))
        self.refusal("AMBIGUOUS_LEGACY_BINDING", lambda: plan_identity_migration(
            loaded, ambiguous, self.authorization), HistoryError)

    def test_origin_and_fork_metadata_reject_changed_baseline_without_outputs(self):
        # Break: cached approval adopts a changed baseline or unconfirmed origin.
        self.assertIs(self.approval, authorize_fork(self.approval, self.v2_before, self.roots, context=self.root_context))
        self.refusal("BASELINE_CHANGED", lambda: authorize_fork(self.approval,
            replace(self.v2_before, content_digest="f" * 64), self.roots, context=self.root_context))
        self.refusal("LEGACY_SCOPE_UNVERIFIED", lambda: authorize_archive_origin(
            replace(self.origin, explicit_origin_confirmed=False), self.approval,
            self.v2_before, SCOPE), HistoryError)
        self.assertFalse(self.roots.candidate_v3.exists())
        self.assertFalse(self.namespace.exists())

    def test_codec_inspection_refuses_mismatched_target_without_repair(self):
        # Break: wrong scope/generation routes a codec fixture as healthy current.
        root, bundle = self.codec_fixture()
        schema = json.loads(encode_schema(bundle))
        target = {key: schema[key] for key in ("schema_version", "identity_contract_version",
            "archive_id", "scope", "data_generation")}
        target["data_generation"] = 2
        (root / "target.json").write_text(json.dumps(target), encoding="utf8")
        before = snapshot_tree(root)
        with self.assertRaisesRegex(ValueError, "^INVALID_V3_STATE$"):
            inspect_v3_schema(root)
        self.assertEqual(before, snapshot_tree(root))
        self.assert_originals_unchanged()

    def test_quoted_ready_and_active_cannot_replace_missing_activation_or_save_authority(self):
        # Break: decoded status becomes activation/write authority.
        root, bundle = self.codec_fixture()
        candidate = CandidateRecord(bundle.archive_id, "synthetic-object", SCOPE,
            "SYNTHETIC", prove_existing_root(root).identity, "READY", "1" * 64, None, "operation")
        self.refusal("ACTIVATION_APPROVAL_REQUIRED", lambda: compare_activation_request(
            candidate, None, None, None, None, requested_candidate_id=candidate.archive_id,
            requested_object_id=candidate.object_id, requested_scope=SCOPE, namespace_purpose="SYNTHETIC"))
        for status in ("READY", "ACTIVE"):
            cached = replace(candidate, state=status)
            self.refusal("RUNTIME_BINDING_UNVERIFIED", lambda: compare_active_save(cached,
                requested_object_id=candidate.object_id, requested_scope=SCOPE,
                requested_root_id=candidate.root_id, requested_archive_id=candidate.archive_id,
                state=bundle, mode="full", expected_binding_generation=0,
                expected_binding_digest=contract_digest(None)))
        self.refusal("RUNTIME_BINDING_UNVERIFIED", lambda: admit_candidate_action(
            replace(candidate, state="ACTIVE"), "normal_save"))

    def test_pure_save_comparison_uses_actual_codec_and_rejects_superseded_metadata(self):
        # Break: save admission ignores current semantic codec or cached generation.
        root, bundle = self.codec_fixture()
        binding = AuthorityBinding("synthetic-object", SCOPE, 2, "ACTIVE_V3",
            bundle.archive_id, prove_existing_root(root).identity, "1" * 64, "2" * 64, 1)
        args = dict(requested_object_id=binding.object_id, requested_scope=SCOPE,
            requested_root_id=binding.root_id, requested_archive_id=binding.archive_id,
            state=inspect_v3_schema(root), mode="full", expected_binding_generation=2,
            expected_binding_digest=contract_digest(binding))
        before = snapshot_tree(root)
        for mode in ("quick", "full"):
            self.assertIs(binding, compare_active_save(binding, **dict(args, mode=mode)))
        self.refusal("BINDING_CONFLICT", lambda: compare_active_save(replace(binding,
            generation=3), **args))
        self.refusal("TRACK2_RECOVERY_REQUIRED", lambda: compare_active_save(binding,
            **dict(args, state=replace(bundle, records=(replace(bundle.records[0],
                canonical_identity="invalid"), bundle.records[1])))))
        self.assertEqual(before, snapshot_tree(root))

    def test_managed_leaf_policy_excludes_preserved_vault_and_historical_evidence(self):
        # Break: immutable vault/history assets are included for mutable replacement.
        self.codec_fixture()
        old = ("state/assets/images/old.png", "snapshots/old/chat.md", "integrity/old.json")
        fixed = ("state/messages.jsonl", "state/schema.json", "archive_state.json",
            "target.json", "chat.md", "capsule_meta.json", "integrity/current.json")
        self.assertEqual(fixed, plan_current_managed_leaves(new_current_assets=(),
            existing_ordinary_leaves=old, prior_current_asset_references=()))
        self.refusal("TRACK2_RECOVERY_REQUIRED", lambda: plan_current_managed_leaves(
            new_current_assets=("assets/images/old.png",), existing_ordinary_leaves=old,
            prior_current_asset_references=("assets/images/old.png",)))

    def test_dedicated_cli_and_controller_refuse_without_namespace_or_source_writes(self):
        # Break: explicit metadata selection starts an unavailable real operation.
        root, bundle = self.codec_fixture()
        candidate = CandidateRecord(bundle.archive_id, "synthetic-object", SCOPE,
            "SYNTHETIC", prove_existing_root(root).identity, "READY", "1" * 64, None, "operation")
        controller = V3Controller(Selection(self.namespace, candidate.object_id,
            candidate.archive_id, "SYNTHETIC"))
        before = snapshot_tree(root)
        for state in ("READY", "ACTIVE"):
            metadata = replace(candidate, state=state)
            view = controller.present(metadata)
            self.assertFalse(view.normal_save_authorized)
            self.assertFalse(view.operations_available)
            self.assertEqual("RUNTIME_BINDING_UNVERIFIED", controller.request_save(metadata).code)
        self.assertEqual("ACTIVATION_APPROVAL_REQUIRED", controller.request_activation(
            candidate, {"approved": True}, confirmed=True).code)
        for action, code in (("build", "OPERATION_UNAVAILABLE"), ("save", "OPERATION_UNAVAILABLE"),
                ("activate", "ACTIVATION_APPROVAL_REQUIRED"), ("recover", "OPERATION_UNAVAILABLE")):
            stream = io.StringIO()
            with redirect_stdout(stream), redirect_stderr(stream):
                status = main([action, "--namespace", str(self.namespace), "--object-id",
                    candidate.object_id, "--candidate-id", candidate.archive_id, "--purpose", "SYNTHETIC"])
            self.assertEqual(3, status)
            self.assertIn(code, stream.getvalue())
            self.assertNotIn(str(self.namespace), stream.getvalue())
            self.assert_originals_unchanged()
        self.assertFalse(self.namespace.exists())
        self.assertEqual(before, snapshot_tree(root))


if __name__ == "__main__":
    unittest.main()
