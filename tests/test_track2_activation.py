"""NONAUTHORITATIVE pure comparisons, not race/crash/commit proof.

Breaks caught: admitting stale READY/approval, candidate-index CAS substitution,
historical anchors confused with fresh integrity, or guessing recovery authority.
Only reused setup creates synthetic TemporaryDirectory files. No live producer.
"""
import copy
from dataclasses import replace
import sys
import unittest

import test_track2_authorization as approval_fixtures
import test_track2_candidate as candidate_fixtures
from wechat_exporter import authority_activation as activation
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.track2_contracts import (BuildEvidence, CandidateRecord,
    AuthorityBinding, FileIdentity, IntegrityFacts, Track2Error, contract_digest)

D1, D2, D3, D4, D5 = (c * 64 for c in "12345")
OTHER = ArchiveScope("synthetic-other", "synthetic-other-peer")


class ActivationComparisonTests(unittest.TestCase):
    def setUp(self):
        fixture = approval_fixtures.ApprovalMetadataTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.approval, self.receipt, self.fork = fixture.chain()
        inv = candidate_fixtures.inventory()
        inv = replace(inv, scope=self.fork.scope, rows=tuple(replace(row,
            locator=replace(row.locator, scope=self.fork.scope)) for row in inv.rows))
        source = candidate_fixtures.independent_source_digest(inv)
        self.evidence = BuildEvidence(contract_digest(self.fork),
            contract_digest(self.fork.input_baseline), source, D3, D4, 1,
            (("final_union_count", 2),), True, D2, "track2-v1", D4)
        self.receipt = replace(self.receipt, evidence_digest=contract_digest(self.evidence),
            source_digest=source, registry_digest=contract_digest(None), registry_generation=0)
        self.approval = replace(self.approval, verification_digest=contract_digest(self.receipt),
            expected_binding_digest=contract_digest(None), expected_binding_generation=0)
        self.candidate = CandidateRecord(self.fork.archive_id, self.fork.object_id,
            self.fork.scope, "SYNTHETIC", self.receipt.root_id, "READY",
            contract_digest(self.evidence), None, self.fork.operation_id)
        self.integrity = IntegrityFacts(self.fork.scope, self.fork.archive_id,
            1, D3, D4, True, True, True, True)
        self.facts = dict(current_root_id=self.receipt.root_id,
            current_fork_receipt_digest=contract_digest(self.fork),
            current_baseline_digest=contract_digest(self.fork.input_baseline),
            recorded_inventory=inv, current_inventory=inv, current_union_digest=D3,
            current_manifest_digest=D4, current_data_generation=1,
            current_counts=self.evidence.counts, current_integrity=self.integrity,
            current_code_digest=D2, current_contract_version="track2-v1",
            current_configuration_digest=D4)
        self.binding = AuthorityBinding(self.fork.object_id, self.fork.scope,
            1, "ACTIVE_V3", self.fork.archive_id, self.receipt.root_id,
            contract_digest(self.approval), D4, 1)

    def denies(self, callback, code):
        with self.assertRaises(Track2Error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def accepted(self, callback, want=None):
        try:
            result = callback()
        except Track2Error as error:
            self.fail("Expected accepted comparison, got " + error.code)
        self.assertIs(result, want)

    def request(self, **changes):
        arguments = dict(candidate=self.candidate, evidence=self.evidence,
            receipt=self.receipt, fork=self.fork, approval=self.approval,
            requested_candidate_id=self.candidate.archive_id,
            requested_object_id=self.candidate.object_id, requested_scope=self.candidate.scope,
            namespace_purpose="SYNTHETIC", **self.facts)
        arguments.update(changes)
        return activation.compare_activation_request(**arguments)

    def idem(self, **changes):
        arguments = dict(candidate=replace(self.candidate, state="ACTIVE"),
            evidence=self.evidence, receipt=self.receipt, fork=self.fork,
            approval=self.approval, binding=self.binding,
            current_root_id=self.receipt.root_id, current_integrity=self.integrity,
            current_manifest_digest=D4, current_data_generation=1, current_union_digest=D3)
        arguments.update(changes)
        return activation.compare_idempotent_activation(**arguments)

    def recovery(self, **changes):
        arguments = dict(current_binding=None, old_binding=None, new_binding=self.binding,
            candidate=replace(self.candidate, state="ACTIVATION_PENDING"),
            evidence=self.evidence, receipt=self.receipt, fork=self.fork,
            approval=self.approval, operation_id=self.fork.operation_id,
            intent_present=True, intent_digest=D1, expected_intent_digest=D1)
        arguments.update(changes)
        return activation.classify_activation_recovery(**arguments)

    def test_exact_ready_chain_is_comparison_only(self):
        self.accepted(self.request)

    def test_all_six_candidate_states_have_exact_request_refusals(self):
        for state, code in (("BUILDING", "CANDIDATE_NOT_READY"),
            ("READY", None), ("ACTIVATION_PENDING", "TRACK2_RECOVERY_REQUIRED"),
            ("ACTIVE", "CANDIDATE_NOT_READY"), ("FAILED", "CANDIDATE_NOT_READY"),
            ("RECOVERY_REQUIRED", "TRACK2_RECOVERY_REQUIRED")):
            with self.subTest(state=state):
                callback = lambda: self.request(candidate=replace(self.candidate, state=state))
                self.accepted(callback) if code is None else self.denies(callback, code)

    def test_each_requested_identity_must_match(self):
        for field, value in (("requested_candidate_id", "wrong"),
            ("requested_object_id", "wrong"), ("requested_scope", OTHER)):
            with self.subTest(field=field):
                self.denies(lambda: self.request(**{field: value}), "BINDING_CONFLICT")

    def test_candidate_scope_object_root_purpose_and_operation_must_match_fork(self):
        for field, value in (("scope", OTHER), ("object_id", "wrong"),
            ("archive_id", "wrong"), ("root_id", FileIdentity(7, "aa" * 16, 1)),
            ("operation_id", "wrong")):
            with self.subTest(field=field):
                self.denies(lambda: self.request(candidate=replace(self.candidate,
                    **{field: value})), "BINDING_CONFLICT")
        self.denies(lambda: self.request(candidate=replace(self.candidate,
            purpose="REHEARSAL")), "ACTIVATION_APPROVAL_REQUIRED")

    def test_missing_or_rehearsal_and_cross_namespace_evidence_is_refused(self):
        for field in ("approval", "receipt", "fork"):
            self.denies(lambda: self.request(**{field: None}), "ACTIVATION_APPROVAL_REQUIRED")
        self.denies(lambda: self.request(namespace_purpose="FORMAL"), "ACTIVATION_APPROVAL_REQUIRED")
        self.denies(lambda: self.request(fork=replace(self.fork, purpose="REHEARSAL")),
                    "ACTIVATION_APPROVAL_REQUIRED")

    def test_stale_candidate_evidence_and_dirty_current_integrity_are_refused(self):
        self.denies(lambda: self.request(candidate=replace(self.candidate, evidence_digest=D5)),
                    "CANDIDATE_NOT_READY")
        self.denies(lambda: self.request(current_integrity=replace(self.integrity,
            transaction_clean=False)), "CANDIDATE_NOT_READY")

    def test_each_current_source_code_config_baseline_and_fork_fact_is_refused(self):
        for field, value, code in (("current_fork_receipt_digest", D5, "FORK_INCOMPLETE"),
            ("current_baseline_digest", D5, "BASELINE_CHANGED"),
            ("current_code_digest", D5, "CANDIDATE_NOT_READY"),
            ("current_configuration_digest", D5, "CANDIDATE_NOT_READY"),
            ("current_contract_version", "obsolete", "CANDIDATE_NOT_READY"),
            ("current_root_id", FileIdentity(7, "aa" * 16, 1), "BINDING_CONFLICT"),
            ("current_inventory", None, "SOURCE_CHANGED")):
            with self.subTest(field=field):
                self.denies(lambda: self.request(**{field: value}), code)

    def test_refreshed_receipt_cannot_disagree_with_build_evidence(self):
        for field in ("evidence_digest", "source_digest", "baseline_digest",
                      "code_digest", "configuration_digest"):
            changed = replace(self.receipt, **{field: D5})
            approved = replace(self.approval, verification_digest=contract_digest(changed),
                code_digest=changed.code_digest, configuration_digest=changed.configuration_digest)
            self.denies(lambda: self.request(receipt=changed, approval=approved),
                "BASELINE_CHANGED" if field == "baseline_digest" else "CANDIDATE_NOT_READY")

    def test_absent_binding_uses_canonical_null_and_zero(self):
        self.accepted(lambda: activation.compare_expected_binding(None, self.approval,
            object_id=self.candidate.object_id, scope=self.candidate.scope))
        for digest, generation in ((D2, 0), (contract_digest(None), 3)):
            self.denies(lambda: activation.compare_expected_binding(None,
                replace(self.approval, expected_binding_digest=digest,
                        expected_binding_generation=generation),
                object_id=self.candidate.object_id, scope=self.candidate.scope), "BINDING_CONFLICT")

    def test_preserved_and_active_binding_cas_compare_digest_generation(self):
        preserved = replace(self.binding, kind="PRESERVED_V2_READONLY", generation=0,
            activation_approval_digest=None, activation_manifest_digest=None,
            activation_data_generation=None)
        for binding in (preserved, self.binding):
            approved = replace(self.approval, expected_binding_digest=contract_digest(binding),
                expected_binding_generation=binding.generation)
            self.accepted(lambda: activation.compare_expected_binding(binding, approved,
                object_id=self.candidate.object_id, scope=self.candidate.scope))
            self.denies(lambda: activation.compare_expected_binding(binding,
                replace(approved, expected_binding_generation=binding.generation + 1),
                object_id=self.candidate.object_id, scope=self.candidate.scope), "BINDING_CONFLICT")

    def test_cas_rejects_wrong_scope_object_corrupt_and_boolean_generation(self):
        forged = copy.copy(self.approval)
        object.__setattr__(forged, "expected_binding_generation", False)
        for binding, approval, object_id, scope in (({}, self.approval, self.fork.object_id, self.fork.scope),
            (None, forged, self.fork.object_id, self.fork.scope),
            (None, self.approval, "wrong", self.fork.scope)):
            self.denies(lambda: activation.compare_expected_binding(binding, approval,
                object_id=object_id, scope=scope), "BINDING_CONFLICT")

    def test_exact_active_and_healthy_later_save_are_idempotent_comparisons(self):
        self.accepted(self.idem, True)
        later = replace(self.integrity, data_generation=9, manifest_digest=D5, union_digest=D1)
        self.accepted(lambda: self.idem(current_integrity=later, current_data_generation=9,
            current_manifest_digest=D5, current_union_digest=D1), True)

    def test_wrong_approval_root_binding_or_state_is_never_noop(self):
        for field, value in (("generation", 2), ("archive_id", "other"),
            ("root_id", FileIdentity(7, "aa" * 16, 1)), ("scope", OTHER),
            ("object_id", "wrong"), ("activation_approval_digest", D5),
            ("activation_manifest_digest", D5), ("activation_data_generation", 2)):
            self.denies(lambda: self.idem(binding=replace(self.binding, **{field: value})), "BINDING_CONFLICT")
        self.denies(lambda: self.idem(approval=replace(self.approval, approval_id="another")),
                    "BINDING_CONFLICT")
        self.denies(lambda: self.idem(candidate=self.candidate), "CANDIDATE_NOT_READY")

    def test_idempotency_requires_separate_fresh_current_integrity(self):
        for facts in (None, replace(self.integrity, current_ok=False),
            replace(self.integrity, history_ok=False), replace(self.integrity, asset_closure_ok=False),
            replace(self.integrity, transaction_clean=False), replace(self.integrity, scope=OTHER),
            replace(self.integrity, archive_id="wrong"), replace(self.integrity, manifest_digest=D5),
            replace(self.integrity, data_generation=0)):
            self.denies(lambda: self.idem(current_integrity=facts), "CANDIDATE_NOT_READY")

    def test_old_binding_requires_reverification_and_cancel_never_v2_fallback(self):
        old = replace(self.binding, kind="PRESERVED_V2_READONLY", generation=0,
            activation_approval_digest=None, activation_manifest_digest=None, activation_data_generation=None)
        receipt = replace(self.receipt, registry_digest=contract_digest(old))
        approval = replace(self.approval, expected_binding_digest=contract_digest(old),
                           verification_digest=contract_digest(receipt))
        new = replace(self.binding, activation_approval_digest=contract_digest(approval))
        result = self.recovery(current_binding=old, old_binding=old, new_binding=new,
                               receipt=receipt, approval=approval)
        self.assertEqual(("BLOCKED", "OLD_BINDING_REVERIFY_CANCEL_REQUIRED",
            "TRACK2_RECOVERY_REQUIRED"), (result.state, result.action, result.error_code))

    def test_new_binding_requires_postcommit_readback_and_index_repair(self):
        result = self.recovery(current_binding=self.binding)
        self.assertEqual(("BLOCKED", "NEW_BINDING_READBACK_REPAIR_REQUIRED",
            "TRACK2_RECOVERY_REQUIRED"), (result.state, result.action, result.error_code))

    def test_absent_unknown_binding_and_orphan_intent_are_blocked(self):
        for changes in ({}, {"current_binding": replace(self.binding, generation=2)},
                        {"intent_present": False, "current_binding": self.binding}):
            result = self.recovery(**changes)
            self.assertEqual(("BLOCKED", "INSPECT_EVIDENCE_REQUIRED", "TRACK2_RECOVERY_REQUIRED"),
                             (result.state, result.action, result.error_code))

    def test_corrupt_or_contradictory_recovery_evidence_fails_closed(self):
        for changes in ({"current_binding": {}}, {"old_binding": {}},
            {"new_binding": replace(self.binding, generation=2)}, {"intent_digest": D5},
            {"operation_id": "wrong"}, {"approval": None}, {"intent_present": 1}):
            result = self.recovery(**changes)
            self.assertEqual(("FAILED", "INSPECT_EVIDENCE_REQUIRED", "TRACK2_RECOVERY_REQUIRED"),
                             (result.state, result.action, result.error_code))

    def test_malformed_utf8_in_every_comparison_is_allowlisted(self):
        malformed = replace(self.candidate, archive_id="synthetic-\ud800")
        self.denies(lambda: self.request(candidate=malformed), "CANDIDATE_NOT_READY")
        malformed_approval = replace(self.approval, approval_id="synthetic-\ud800")
        self.denies(lambda: self.request(approval=malformed_approval), "ACTIVATION_APPROVAL_REQUIRED")
        self.denies(lambda: activation.compare_expected_binding(None, malformed_approval,
            object_id=self.candidate.object_id, scope=self.candidate.scope), "BINDING_CONFLICT")
        self.denies(lambda: self.idem(approval=malformed_approval), "ACTIVATION_APPROVAL_REQUIRED")
        result = self.recovery(approval=malformed_approval)
        self.assertEqual("FAILED", result.state)

    def test_pure_helpers_have_no_io_or_input_mutation(self):
        before = tuple(contract_digest(item) for item in (self.candidate, self.evidence,
            self.receipt, self.fork, self.approval, self.binding))
        events, enabled = [], [False]
        def audit(event, args):
            if enabled[0] and (event == "open" or event.startswith(("os.", "subprocess.", "socket."))):
                events.append(event)
        sys.addaudithook(audit)
        enabled[0] = True
        try:
            self.accepted(self.request)
            activation.compare_expected_binding(None, self.approval,
                object_id=self.candidate.object_id, scope=self.candidate.scope)
            self.accepted(self.idem, True)
            self.recovery(current_binding=self.binding)
        finally:
            enabled[0] = False
        self.assertEqual([], events)
        self.assertEqual(before, tuple(contract_digest(item) for item in (self.candidate,
            self.evidence, self.receipt, self.fork, self.approval, self.binding)))


if __name__ == "__main__":
    unittest.main()
