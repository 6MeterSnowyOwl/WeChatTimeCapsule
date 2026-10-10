"""Synthetic metadata checks; fixture DTOs are NONAUTHORITATIVE approvals.

Breaks caught: dropping an exact approval field comparison, treating B as C,
allowing overlapping destinations, or confusing source proof with origin.
Only fixture setup writes TemporaryDirectory; validators perform no I/O.
"""
from dataclasses import replace
from operation_root_fixtures import bind_metadata_context
import copy
import sys
import tempfile
from pathlib import Path
import unittest

from wechat_exporter import fork_authorization as auth
from wechat_exporter import physical_paths as physical
from wechat_exporter.history_contracts import ArchiveScope, HistoryError, ScopeAuthorization
from wechat_exporter.track2_contracts import (
    CONTRACT_VERSION, ActivationApproval, ArchiveOriginConfirmation, FileIdentity,
    ForkApproval, ForkReceipt, LegacyNamespaceProof, RootSelection, RuntimeProof,
    Track2Error, VerificationReceipt, canonical_bytes, contract_digest,
)

D1, D2, D3, D4 = (c * 64 for c in "1234")


class ApprovalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-task11-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.roots = RootSelection(*(self.base / name for name in
            ("input", "candidate", "namespace", "staging", "reader", "backup")))
        for path in (self.roots.input_v2, self.roots.namespace, self.roots.staging, self.roots.reader_work):
            path.mkdir()
        (self.roots.input_v2 / "synthetic.txt").write_bytes(b"synthetic archived payload")
        self.scope = ArchiveScope("synthetic-account", "synthetic-conversation")
        self.baseline = physical.snapshot_tree(self.roots.input_v2)
        self.origin = ArchiveOriginConfirmation("synthetic-origin", self.scope,
            self.baseline.root.identity, self.baseline.content_digest,
            self.baseline.physical_digest, True)
        self.approval = ForkApproval("synthetic-fixture-approval", "SYNTHETIC", "synthetic-object",
            self.scope, self.baseline, contract_digest(self.origin),
            tuple(physical.prove_destination_parent(path) for path in
                  (self.roots.candidate_v3, self.roots.safety_backup)),
            D1, True, D2, CONTRACT_VERSION, None, D1)
        self.approval, self.context = bind_metadata_context(self.approval, self.roots)

    def denies(self, callback, code="ROOT_PROOF_FAILED", error=Track2Error):
        with self.assertRaises(error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def chain(self, purpose="SYNTHETIC"):
        for path in (self.roots.candidate_v3, self.roots.safety_backup):
            path.mkdir()
            (path / "synthetic.txt").write_bytes(b"synthetic archived payload")
        legacy, source = self.base / "legacy", self.base / "source"
        legacy.mkdir()
        source.mkdir()
        legacy_proof = LegacyNamespaceProof((legacy,), (), D1, D1,
            ((legacy, physical.prove_existing_root(legacy).identity),))
        proof = physical.prove_roots(self.roots, legacy_proof, (source,))
        fork_approval = replace(self.approval, purpose=purpose,
            b_receipt_digest=D3 if purpose == "FORMAL" else None)
        fork = ForkReceipt("synthetic-operation", "synthetic-candidate", "synthetic-object",
            self.scope, purpose, proof, self.baseline,
            physical.snapshot_tree(self.roots.candidate_v3),
            physical.snapshot_tree(self.roots.safety_backup), contract_digest(fork_approval),
            fork_approval.root_context_digest)
        receipt = VerificationReceipt(fork.archive_id, fork.candidate_copy_snapshot.root.identity,
            D1, contract_digest(self.baseline), 3, D2, D3, D2, CONTRACT_VERSION, D4)
        approval = ActivationApproval("synthetic-activation", fork.object_id, receipt.candidate_id,
            receipt.root_id, contract_digest(fork), contract_digest(receipt),
            receipt.registry_digest, receipt.registry_generation, receipt.code_digest,
            CONTRACT_VERSION, receipt.configuration_digest, purpose if purpose != "REHEARSAL" else "FORMAL")
        return approval, receipt, fork

    def test_exact_synthetic_fork_returns_the_same_immutable_metadata(self):
        before = canonical_bytes(self.approval)
        self.assertIs(self.approval, auth.authorize_fork(self.approval, self.baseline, self.roots, context=self.context))
        self.assertEqual(before, canonical_bytes(self.approval))
        self.assertFalse(self.roots.candidate_v3.exists())
        self.assertFalse(self.roots.safety_backup.exists())

    def test_absent_or_non_typed_fork_approval_fails_closed(self):
        for approval in (None, {"b_receipt_digest": D1}, RuntimeProof(self.scope, "synthetic", D1, True)):
            with self.subTest(kind=type(approval).__name__):
                self.denies(lambda: auth.authorize_fork(approval, self.baseline, self.roots, context=self.context))

    def test_changed_content_or_physical_baseline_is_not_reset(self):
        for field in ("content_digest", "physical_digest"):
            with self.subTest(field=field):
                changed = replace(self.baseline, **{field: D4})
                self.denies(lambda: auth.authorize_fork(self.approval, changed, self.roots, context=self.context), "BASELINE_CHANGED")

    def test_snapshot_entry_change_even_with_retained_digests_is_rejected(self):
        changed = replace(self.baseline, entries=(self.baseline.entries[0],
            replace(self.baseline.entries[1], size=999)))
        self.denies(lambda: auth.authorize_fork(self.approval, changed, self.roots, context=self.context), "BASELINE_CHANGED")

    def test_snapshot_root_identity_and_root_entry_must_agree(self):
        malformed = replace(self.baseline, root=replace(self.baseline.root,
            identity=FileIdentity(7, "ab" * 16, 1)))
        self.denies(lambda: auth.authorize_fork(replace(self.approval, input_baseline=malformed),
            malformed, self.roots, context=self.context))

    def test_retargeted_input_or_candidate_root_is_not_approved(self):
        for field in ("input_v2", "candidate_v3"):
            with self.subTest(field=field):
                roots = replace(self.roots, **{field: self.base / "unapproved"})
                self.denies(lambda: auth.authorize_fork(self.approval, self.baseline, roots, context=self.context))

    def test_in_place_equal_ancestor_and_case_alias_outputs_are_denied(self):
        for path in (self.roots.input_v2, self.roots.input_v2 / "child", self.base,
                     Path(str(self.roots.input_v2).upper())):
            with self.subTest(path_role="synthetic-overlap"):
                self.denies(lambda: auth.authorize_fork(self.approval, self.baseline,
                    replace(self.roots, candidate_v3=path), context=self.context))

    def test_duplicate_unrelated_and_missing_candidate_parent_proofs_are_denied(self):
        a, b = self.approval.destination_parent_proofs
        unrelated = physical.prove_destination_parent(self.base / "unrelated")
        for proofs in ((a, a, b), (b,), (a, b, unrelated)):
            with self.subTest(count=len(proofs)):
                self.denies(lambda: auth.authorize_fork(replace(self.approval,
                    destination_parent_proofs=proofs), self.baseline, self.roots, context=self.context))

    def test_parent_requested_leaf_chain_must_exactly_reach_destination(self):
        a, b = self.approval.destination_parent_proofs
        altered = replace(a, requested_leaf_names=("wrong",))
        self.denies(lambda: auth.authorize_fork(replace(self.approval,
            destination_parent_proofs=(altered, b)), self.baseline, self.roots, context=self.context))

    def test_formal_requires_b_and_separately_selected_safety_destination(self):
        formal = replace(self.approval, purpose="FORMAL", b_receipt_digest=D3)
        formal, self.context = bind_metadata_context(formal, self.roots)
        self.assertIs(formal, auth.authorize_fork(formal, self.baseline, self.roots, context=self.context))
        self.denies(lambda: auth.authorize_fork(formal, self.baseline,
            replace(self.roots, safety_backup=None), context=self.context))
        self.denies(lambda: auth.authorize_fork(replace(formal,
            destination_parent_proofs=(formal.destination_parent_proofs[0],)), self.baseline, self.roots, context=self.context))
        # B alone is a digest and cannot serve as the typed C approval.
        self.denies(lambda: auth.authorize_fork(D3, self.baseline, self.roots, context=self.context))

    def test_explicit_rehearsal_without_safety_is_metadata_only(self):
        roots = replace(self.roots, safety_backup=None)
        approval = replace(self.approval, purpose="REHEARSAL", safety_backup_required=False,
            destination_parent_proofs=(self.approval.destination_parent_proofs[0],))
        approval, self.context = bind_metadata_context(approval, roots)
        self.assertIs(approval, auth.authorize_fork(approval, self.baseline, roots, context=self.context))

    def test_all_explicit_new_output_roles_can_be_parent_approved(self):
        new_namespace = self.base / "new-namespace"
        roots = replace(self.roots, namespace=new_namespace)
        approval = replace(self.approval, destination_parent_proofs=(
            *self.approval.destination_parent_proofs, physical.prove_destination_parent(new_namespace)))
        approval, self.context = bind_metadata_context(approval, roots)
        self.assertIs(approval, auth.authorize_fork(approval, self.baseline, roots, context=self.context))

    def test_exact_synthetic_activation_chain_is_metadata_only(self):
        approval, receipt, fork = self.chain()
        self.assertIsNone(auth.validate_formal_activation(approval, receipt, fork))
        self.assertFalse((self.roots.namespace / "objects").exists())

    def test_exact_formal_activation_chain_is_accepted(self):
        approval, receipt, fork = self.chain("FORMAL")
        self.assertIsNone(auth.validate_formal_activation(approval, receipt, fork))

    def test_rehearsal_and_missing_activation_approval_are_denied(self):
        approval, receipt, fork = self.chain("REHEARSAL")
        self.denies(lambda: auth.validate_formal_activation(approval, receipt, fork), "ACTIVATION_APPROVAL_REQUIRED")
        self.denies(lambda: auth.validate_formal_activation(None, receipt, fork), "ACTIVATION_APPROVAL_REQUIRED")

    def test_activation_each_approved_receipt_code_configuration_field_must_match(self):
        approval, receipt, fork = self.chain()
        changes = {"fork_receipt_digest": D4, "verification_digest": D4,
            "code_digest": D4, "configuration_digest": D1, "purpose": "FORMAL"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.denies(lambda: auth.validate_formal_activation(replace(approval, **{field: value}),
                    receipt, fork), "ACTIVATION_APPROVAL_REQUIRED")

    def test_activation_object_candidate_root_registry_bindings_must_match(self):
        approval, receipt, fork = self.chain()
        changes = {"object_id": "wrong-object", "candidate_id": "wrong-candidate",
            "root_id": FileIdentity(7, "bc" * 16, 1),
            "expected_binding_digest": D3, "expected_binding_generation": 4}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.denies(lambda: auth.validate_formal_activation(replace(approval, **{field: value}),
                    receipt, fork), "BINDING_CONFLICT")

    def test_activation_verification_baseline_must_bind_full_input_snapshot(self):
        approval, receipt, fork = self.chain()
        wrong = replace(receipt, baseline_digest=D4)
        approval = replace(approval, verification_digest=contract_digest(wrong))
        self.denies(lambda: auth.validate_formal_activation(approval, wrong, fork), "BASELINE_CHANGED")

    def test_activation_changed_verification_fields_cannot_hide_under_old_approval(self):
        approval, receipt, fork = self.chain()
        changes = {"evidence_digest": D4, "source_digest": D4, "code_digest": D4,
            "configuration_digest": D1, "registry_digest": D3, "registry_generation": 4}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.denies(lambda: auth.validate_formal_activation(approval,
                    replace(receipt, **{field: value}), fork), "ACTIVATION_APPROVAL_REQUIRED")

    def test_activation_internal_root_proof_scope_and_snapshot_consistency(self):
        approval, receipt, fork = self.chain()
        proof = fork.root_proof
        candidate_id = FileIdentity(7, "cd" * 16, 1)
        modifications = [replace(proof, root_ids=tuple((role, candidate_id if role == "candidate_v3" else value)
            for role, value in proof.root_ids)), replace(proof, snapshots=tuple((role, self.baseline if role == "candidate_v3" else value)
            for role, value in proof.snapshots)), replace(proof, root_ids=(*proof.root_ids, proof.root_ids[0]))]
        for changed in modifications:
            with self.subTest(kind="internal-root-binding"):
                wrong = replace(fork, root_proof=changed)
                current_approval = replace(approval, fork_receipt_digest=contract_digest(wrong))
                self.denies(lambda: auth.validate_formal_activation(current_approval, receipt, wrong), "BINDING_CONFLICT")

    def test_origin_exact_confirmation_returns_only_core_scope_authorization(self):
        result = auth.authorize_archive_origin(self.origin, self.approval, self.baseline, self.scope)
        self.assertEqual(ScopeAuthorization(self.scope, True), result)
        self.assertIs(type(result), ScopeAuthorization)

    def test_origin_missing_false_wrong_scope_root_and_each_baseline_digest(self):
        changes = [None, replace(self.origin, explicit_origin_confirmed=False),
            replace(self.origin, scope=ArchiveScope("other-account", "other-conversation")),
            replace(self.origin, input_root_id=FileIdentity(7, "de" * 16, 1)),
            replace(self.origin, input_content_digest=D4), replace(self.origin, input_physical_digest=D4)]
        for confirmation in changes:
            with self.subTest(kind="origin-mismatch"):
                approval = self.approval if confirmation is None else replace(self.approval,
                    archive_origin_confirmation_digest=contract_digest(confirmation))
                self.denies(lambda: auth.authorize_archive_origin(confirmation, approval, self.baseline,
                    self.scope), "LEGACY_SCOPE_UNVERIFIED", HistoryError)

    def test_origin_stale_receipt_or_approval_scope_cannot_be_replaced_by_runtime_proof(self):
        valid_source = RuntimeProof(self.scope, "synthetic-verified-source", D1, True)
        for confirmation, approval, baseline, scope in [
            (valid_source, self.approval, self.baseline, self.scope),
            (self.origin, replace(self.approval, archive_origin_confirmation_digest=D4), self.baseline, self.scope),
            (self.origin, self.approval, replace(self.baseline, physical_digest=D4), self.scope),
            (self.origin, replace(self.approval, scope=ArchiveScope("other", "other")), self.baseline, self.scope),
            (self.origin, self.approval, self.baseline, ArchiveScope("other", "other"))]:
            with self.subTest(kind="independent-origin-chain"):
                self.denies(lambda: auth.authorize_archive_origin(confirmation, approval, baseline, scope),
                    "LEGACY_SCOPE_UNVERIFIED", HistoryError)

    def test_forged_missing_b_safety_invalid_purpose_or_contract_are_rejected(self):
        formal = replace(self.approval, purpose="FORMAL", b_receipt_digest=D3)
        for field, value in (("b_receipt_digest", None), ("safety_backup_required", False),
                             ("purpose", "UNKNOWN"), ("contract_version", "obsolete-v0")):
            with self.subTest(field=field):
                forged = copy.copy(formal)
                object.__setattr__(forged, field, value)
                self.denies(lambda: auth.authorize_fork(forged, self.baseline, self.roots, context=self.context))

    def test_obsolete_contract_activation_is_stable_approval_required(self):
        approval, receipt, fork = self.chain()
        for obj in (approval, receipt):
            with self.subTest(kind=type(obj).__name__):
                forged = copy.copy(obj)
                object.__setattr__(forged, "contract_version", "obsolete-v0")
                self.denies(lambda: auth.validate_formal_activation(
                    forged if obj is approval else approval,
                    forged if obj is receipt else receipt, fork), "ACTIVATION_APPROVAL_REQUIRED")

    def test_validators_do_no_io_and_issue_no_registry_or_write_capability(self):
        approval, receipt, fork = self.chain()
        events, monitoring = [], [False]
        def audit(event, args):
            if monitoring[0] and (event == "open" or event.startswith(("os.", "subprocess.", "socket."))):
                events.append(event)
        sys.addaudithook(audit)
        monitoring[0] = True
        try:
            returned = auth.authorize_fork(self.approval, self.baseline, self.roots, context=self.context)
            result = auth.validate_formal_activation(approval, receipt, fork)
            origin = auth.authorize_archive_origin(self.origin, self.approval, self.baseline, self.scope)
        finally:
            monitoring[0] = False
        self.assertEqual([], events)
        self.assertIs(returned, self.approval)
        self.assertIsNone(result)
        self.assertIs(type(origin), ScopeAuthorization)
        self.assertFalse((self.roots.namespace / "registry.json").exists())

    def test_activation_recomputed_receipt_cannot_retarget_candidate_or_safety_copy(self):
        approval, receipt, fork = self.chain("FORMAL")
        wrong = replace(fork, safety_backup_snapshot=None)
        approved = replace(approval, fork_receipt_digest=contract_digest(wrong))
        self.denies(lambda: auth.validate_formal_activation(approved, receipt, wrong), "BINDING_CONFLICT")

    def test_activation_self_consistent_handle_ancestry_and_final_path_overlap_is_denied(self):
        # Break caught: trusting internally consistent hashes while a selected
        # or source handle equals/is inside another selected or source handle.
        approval, receipt, fork = self.chain()
        original = fork.root_proof
        handles = dict(original.root_evidence)
        modifications = (
            ("source:0", "ancestor_ids", (handles["candidate_v3"].identity, *handles["source:0"].ancestor_ids)),
            ("candidate_v3", "ancestor_ids", (handles["source:0"].identity, *handles["candidate_v3"].ancestor_ids)),
            ("source:0", "final_path", handles["candidate_v3"].final_path),
            ("candidate_v3", "final_path", handles["namespace"].final_path),
        )
        for role, field, value in modifications:
            with self.subTest(role=role, field=field):
                changed_handle = replace(handles[role], **{field: value}, **(
                    {"ancestor_final_paths": (handles["candidate_v3" if role == "source:0" else "source:0"].final_path,
                        *handles[role].ancestor_final_paths)} if field == "ancestor_ids" else {}))
                snapshots = dict(original.snapshots)
                changed_snapshot = replace(snapshots[role], root=changed_handle)
                changed_snapshot = replace(changed_snapshot, physical_digest=contract_digest(
                    (CONTRACT_VERSION, "physical-tree", changed_handle, changed_snapshot.entries)))
                proof = replace(original,
                    ancestor_ids=tuple((r, changed_handle.ancestor_ids if r == role else v) for r, v in original.ancestor_ids),
                    root_evidence=tuple((r, changed_handle if r == role else v) for r, v in original.root_evidence),
                    snapshots=tuple((r, changed_snapshot if r == role else v) for r, v in original.snapshots))
                proof = replace(proof, topology_digest=contract_digest((CONTRACT_VERSION,
                    "root-topology", proof.root_evidence,
                    (physical.prove_existing_root(self.base / "legacy"),))))
                wrong = replace(fork, root_proof=proof,
                    candidate_copy_snapshot=changed_snapshot if role == "candidate_v3" else fork.candidate_copy_snapshot)
                exact_approval = replace(approval, fork_receipt_digest=contract_digest(wrong))
                self.denies(lambda: auth.validate_formal_activation(exact_approval, receipt, wrong), "BINDING_CONFLICT")

    def test_fork_destination_parent_equal_input_final_path_is_denied(self):
        # Break caught: distinct supplied IDs obscure contradictory final paths.
        candidate, backup = self.approval.destination_parent_proofs
        parent = replace(candidate.parent, final_path=self.baseline.root.final_path)
        wrong = replace(self.approval, destination_parent_proofs=(replace(candidate, parent=parent), backup))
        self.denies(lambda: auth.authorize_fork(wrong, self.baseline, self.roots, context=self.context))

    def test_fork_common_parent_ancestor_remains_legal(self):
        # Break caught: treating a common enclosing output parent as an overwrite.
        candidate, backup = self.approval.destination_parent_proofs
        self.assertEqual(candidate.parent.identity, backup.parent.identity)
        self.assertIn(candidate.parent.identity, self.baseline.root.ancestor_ids)
        self.assertIs(self.approval, auth.authorize_fork(self.approval, self.baseline, self.roots, context=self.context))

    def encoding_denies(self, callback, code, error=Track2Error):
        # Record the real boundary result, making a leaked encoder exception
        # an assertion RED rather than a test ERROR. Never predigest bad data.
        observed = (None, None)
        try:
            callback()
        except Exception as caught:
            observed = (type(caught), getattr(caught, "code", None))
        self.assertEqual((error, code), observed)

    def test_fork_unencodable_matching_snapshot_uses_stable_root_error(self):
        # Break caught: nested DTO roundtrip accepts data canonical UTF8 cannot encode.
        for surrogate in (chr(0xD800), chr(0xDFFF)):
            with self.subTest(kind="unpaired-surrogate"):
                bad = replace(self.baseline, entries=(self.baseline.entries[0],
                    replace(self.baseline.entries[1], relative_path=surrogate)))
                approval = replace(self.approval, input_baseline=bad)
                self.encoding_denies(lambda: auth.authorize_fork(approval, bad, self.roots, context=self.context), "ROOT_PROOF_FAILED")

    def test_fork_unencodable_approval_id_cannot_be_accepted(self):
        for surrogate in (chr(0xD800), chr(0xDFFF)):
            with self.subTest(kind="approval-id-encoding"):
                bad = replace(self.approval, approval_id=surrogate)
                self.encoding_denies(lambda: auth.authorize_fork(bad, self.baseline, self.roots, context=self.context), "ROOT_PROOF_FAILED")

    def test_activation_unencodable_fork_operation_id_uses_stable_approval_error(self):
        approval, receipt, fork = self.chain()
        for surrogate in (chr(0xD800), chr(0xDFFF)):
            with self.subTest(kind="fork-operation-encoding"):
                bad = replace(fork, operation_id=surrogate)
                self.encoding_denies(lambda: auth.validate_formal_activation(approval, receipt, bad),
                    "ACTIVATION_APPROVAL_REQUIRED")

    def test_activation_unencodable_approval_id_cannot_be_accepted(self):
        approval, receipt, fork = self.chain()
        for surrogate in (chr(0xD800), chr(0xDFFF)):
            with self.subTest(kind="approval-id-encoding"):
                bad = replace(approval, approval_id=surrogate)
                self.encoding_denies(lambda: auth.validate_formal_activation(bad, receipt, fork),
                    "ACTIVATION_APPROVAL_REQUIRED")

    def test_origin_unencodable_confirmation_id_uses_core_stable_error(self):
        for surrogate in (chr(0xD800), chr(0xDFFF)):
            with self.subTest(kind="origin-confirmation-encoding"):
                bad = replace(self.origin, confirmation_id=surrogate)
                self.encoding_denies(lambda: auth.authorize_archive_origin(bad, self.approval,
                    self.baseline, self.scope), "LEGACY_SCOPE_UNVERIFIED", HistoryError)

    def test_origin_unencodable_matching_snapshot_uses_core_stable_error(self):
        bad = replace(self.baseline, entries=(self.baseline.entries[0],
            replace(self.baseline.entries[1], relative_path=chr(0xD800))))
        approval = replace(self.approval, input_baseline=bad)
        self.encoding_denies(lambda: auth.authorize_archive_origin(self.origin, approval,
            bad, self.scope), "LEGACY_SCOPE_UNVERIFIED", HistoryError)

    def test_fork_and_origin_accept_valid_cjk_emoji_owned_fixture_metadata(self):
        # Break caught: overbroad ASCII-only rejection of valid UTF8 metadata/files.
        (self.roots.input_v2 / "档案雪鸮🦉.txt").write_bytes("合成归档資料🦉".encode("utf8"))
        baseline = physical.snapshot_tree(self.roots.input_v2)
        scope = ArchiveScope("合成账户🦉", "合成会話🦉")
        origin = replace(self.origin, confirmation_id="起源确认🦉", scope=scope,
            input_content_digest=baseline.content_digest, input_physical_digest=baseline.physical_digest)
        approval = replace(self.approval, approval_id="档案审批🦉", object_id="合成対象🦉",
            scope=scope, input_baseline=baseline, archive_origin_confirmation_digest=contract_digest(origin))
        approval, self.context = bind_metadata_context(approval, self.roots)
        self.assertIs(approval, auth.authorize_fork(approval, baseline, self.roots, context=self.context))
        self.assertEqual(ScopeAuthorization(scope, True), auth.authorize_archive_origin(origin,
            approval, baseline, scope))
        self.assertFalse(self.roots.candidate_v3.exists())
        self.assertFalse(self.roots.safety_backup.exists())

    def test_activation_accepts_valid_cjk_emoji_metadata_on_owned_fixture_chain(self):
        approval, receipt, fork = self.chain()
        fork = replace(fork, operation_id="合成操作🦉", archive_id="候选归档🦉",
            object_id="合成対象🦉", scope=ArchiveScope("合成账户🦉", "会話🦉"))
        receipt = replace(receipt, candidate_id=fork.archive_id)
        approval = replace(approval, approval_id="激活审批🦉", object_id=fork.object_id,
            candidate_id=fork.archive_id, fork_receipt_digest=contract_digest(fork),
            verification_digest=contract_digest(receipt))
        self.assertIsNone(auth.validate_formal_activation(approval, receipt, fork))
        self.assertFalse((self.roots.namespace / "registry.json").exists())


if __name__ == "__main__":
    unittest.main()
