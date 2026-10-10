"""NONAUTHORITATIVE synthetic metadata fixtures: no real writers or producers.

Breaks caught: trusting cached ACTIVE, accepting altered complete evidence,
calling a verifier outside synthetic scope, accepting a late result, or retrying
after a durable boundary. The injected verifier lives only in this new file.
"""
import copy
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest

from history_fixtures import SyntheticReaderDB, synthetic_history
from wechat_exporter.conversation_reader import ConversationReader

from wechat_exporter import candidate_lifecycle as lifecycle
from wechat_exporter import history_runtime as runtime
from wechat_exporter.history_contracts import (ArchiveScope, HistoryError, Inventory,
    InventoryDigests, ScopeAuthorization, SourceCount, SourceDescriptor, SourceKey,
    SourceLocator, SourceRow, SourceSetChanged)
from wechat_exporter.history_provider import AuthenticatedHistoryDB
from wechat_exporter.models import ChatMessage
from wechat_exporter.track2_contracts import (CONTRACT_VERSION, ArchiveOriginConfirmation,
    BuildEvidence, CandidateRecord, FileIdentity, ForkApproval, IntegrityFacts,
    RootHandleProof, RuntimeProof, Track2Error, TreeEntry, TreeSnapshot, V3Profile,
    contract_digest)

D1, D2, D3, D4, D5 = (c * 64 for c in "12345")
SCOPE = ArchiveScope("synthetic-account", "synthetic-peer")
OTHER_SCOPE = ArchiveScope("synthetic-other", "synthetic-other-peer")
ROOT_ID = FileIdentity(7, "ab" * 16, 1)


class SyntheticVerifier:
    """Explicit injection only; never an actual authentication producer."""
    def __init__(self, proof):
        self.proof = proof
        self.calls = []

    def verify(self, base_db, scope):
        self.calls.append((base_db, scope))
        return self.proof


def inventory():
    key = SourceKey("message/message_0.db", "synthetic-incarnation")
    descriptor = SourceDescriptor(key, "Msg_synthetic", True, "synthetic-schema")
    rows = []
    for local, server in (("1", "101"), ("2", "102")):
        message = ChatMessage(local, "文本", 1, "synthetic-sender", 1700000000,
                              "synthetic-body-" + local, 10 + int(local))
        locator = SourceLocator(SCOPE, key, "Msg_synthetic", local, server,
            message.create_time, message.sort_seq, 1, ("integer",) * 5)
        rows.append(SourceRow(message, server, locator, "synthetic-row-" + local))
    return Inventory(SCOPE, "full", tuple(rows), (descriptor,),
        (SourceCount(key, 2, 2, 2, 2),), (), InventoryDigests(D1, D2, D3))


def independent_source_digest(inv):
    # Literal normalized material: opaque uuid4 row tokens are session handles.
    rows = tuple((row.message, row.server_id, row.locator) for row in inv.rows)
    return contract_digest(("track2-v1", "candidate-source-inventory", inv.scope,
        inv.mode, rows, inv.sources, inv.counts, inv.next_cursors, inv.digests))


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.inv = inventory()
        self.counts = (("raw_source_count", 2), ("decoded_source_count", 2),
                       ("final_union_count", 2))
        self.evidence = BuildEvidence(D1, D2, independent_source_digest(self.inv), D3,
            D4, 1, self.counts, True, D5, CONTRACT_VERSION, D1)
        self.candidate = CandidateRecord("synthetic-archive", "synthetic-object", SCOPE,
            "SYNTHETIC", ROOT_ID, "READY", contract_digest(self.evidence), None,
            "synthetic-operation")
        self.facts = dict(current_root_id=ROOT_ID, current_fork_receipt_digest=D1,
            current_baseline_digest=D2, recorded_inventory=inventory(),
            current_inventory=inventory(), current_union_digest=D3,
            current_manifest_digest=D4, current_data_generation=1,
            current_counts=self.counts, current_integrity=IntegrityFacts(SCOPE,
                "synthetic-archive", 1, D3, D4, True, True, True, True),
            current_code_digest=D5, current_contract_version="track2-v1",
            current_configuration_digest=D1)

    def denies(self, callback, code="CANDIDATE_NOT_READY", error=Track2Error):
        with self.assertRaises(error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def accepted(self, callback):
        try:
            result = callback()
        except Track2Error as error:
            self.fail('Expected accepted independent metadata, got ' + error.code)
        self.assertIsNone(result)

    def test_each_state_admits_only_named_metadata_requests(self):
        allowed = {"BUILDING": ("build",), "READY": ("verify", "request_activation"),
            "ACTIVATION_PENDING": ("recover", "complete_activation"),
            "ACTIVE": ("verify", "inspect_evidence"),
            "FAILED": ("inspect_evidence", "request_fresh_candidate"),
            "RECOVERY_REQUIRED": ("recover",)}
        all_actions = ("build", "verify", "request_activation", "recover",
            "complete_activation", "inspect_evidence", "request_fresh_candidate",
            "normal_save", "retry", "activate")
        for state, admitted in allowed.items():
            candidate = replace(self.candidate, state=state)
            for action in all_actions:
                with self.subTest(state=state, action=action):
                    call = lambda: lifecycle.admit_candidate_action(candidate, action,
                        evidence=self.evidence, intent_operation_id="synthetic-operation",
                        intent_digest=D1, expected_intent_digest=D1)
                    if action in admitted:
                        self.assertIsNone(call())
                    else:
                        code = ("RUNTIME_BINDING_UNVERIFIED" if state == "ACTIVE" and
                            action == "normal_save" else "TRACK2_RECOVERY_REQUIRED" if
                            state in ("ACTIVATION_PENDING", "RECOVERY_REQUIRED") else
                            "CANDIDATE_NOT_READY")
                        self.denies(call, code)

    def test_missing_wrong_and_forged_state_fail_closed(self):
        wrong = copy.copy(self.candidate)
        object.__setattr__(wrong, "state", "VERIFIED")
        for candidate in (None, {}, wrong):
            self.denies(lambda: lifecycle.admit_candidate_action(candidate, "verify"))
        for action in (None, True, [], "unknown"):
            self.denies(lambda: lifecycle.admit_candidate_action(self.candidate, action))

    def test_ready_activation_request_requires_exact_clean_evidence(self):
        for evidence in (None, {}, replace(self.evidence, integrity_verified=False),
                         replace(self.evidence, source_digest=D5)):
            self.denies(lambda: lifecycle.admit_candidate_action(self.candidate,
                "request_activation", evidence=evidence))
        missing = replace(self.candidate, evidence_digest=None)
        self.denies(lambda: lifecycle.admit_candidate_action(missing, "verify"))

    def test_ready_activation_request_has_stable_error_for_unencodable_evidence(self):
        evidence = replace(self.evidence, counts=((chr(0xD800), 1),))
        # Deliberately do not digest malformed metadata in fixture setup.
        with self.assertRaises(Exception) as caught:
            lifecycle.admit_candidate_action(self.candidate,
                'request_activation', evidence=evidence)
        self.assertIs(type(caught.exception), Track2Error)
        self.assertEqual('CANDIDATE_NOT_READY', getattr(caught.exception, 'code', None))
        self.assertEqual('CANDIDATE_NOT_READY', str(caught.exception))

    def test_candidate_evidence_comparison_has_stable_error_for_unencodable_evidence(self):
        evidence = replace(self.evidence, counts=((chr(0xD800), 1),))
        with self.assertRaises(Exception) as caught:
            lifecycle.compare_candidate_evidence(self.candidate, evidence,
                **{**self.facts, 'current_counts': evidence.counts})
        self.assertIs(type(caught.exception), Track2Error)
        self.assertEqual('CANDIDATE_NOT_READY', getattr(caught.exception, 'code', None))
        self.assertEqual('CANDIDATE_NOT_READY', str(caught.exception))

    def test_valid_unicode_count_names_preserve_existing_policy_compatibility(self):
        counts = (('原始来源🦉', 2), ('解码来源', 2), ('最终并集', 2))
        evidence = replace(self.evidence, counts=counts)
        candidate = replace(self.candidate, evidence_digest=contract_digest(evidence))
        self.assertIsNone(lifecycle.admit_candidate_action(candidate,
            'request_activation', evidence=evidence))
        self.accepted(lambda: lifecycle.compare_candidate_evidence(candidate,
            evidence, **{**self.facts, 'current_counts': counts}))

    def test_pending_recovers_or_completes_only_exact_intent(self):
        candidate = replace(self.candidate, state="ACTIVATION_PENDING")
        for action in ("recover", "complete_activation"):
            for changes in ({}, {"intent_operation_id": "wrong", "intent_digest": D1,
                 "expected_intent_digest": D1}, {"intent_operation_id": "synthetic-operation",
                 "intent_digest": D2, "expected_intent_digest": D1},
                 {"intent_operation_id": "synthetic-operation", "intent_digest": True,
                  "expected_intent_digest": True}):
                self.denies(lambda: lifecycle.admit_candidate_action(candidate, action,
                    **changes), "TRACK2_RECOVERY_REQUIRED")

    def test_exact_current_evidence_compares_without_authority_or_mutation(self):
        before = contract_digest(self.candidate)
        self.accepted(lambda: lifecycle.compare_candidate_evidence(self.candidate,
            self.evidence, **self.facts))
        self.assertEqual(before, contract_digest(self.candidate))
        self.denies(lambda: lifecycle.admit_candidate_action(
            replace(self.candidate, state="ACTIVE"), "normal_save"),
            "RUNTIME_BINDING_UNVERIFIED")

    def test_each_independently_supplied_identity_drift_is_denied(self):
        changes = {"current_root_id": (FileIdentity(7, "bc" * 16, 1), "BINDING_CONFLICT"),
            "current_fork_receipt_digest": (D5, "FORK_INCOMPLETE"),
            "current_baseline_digest": (D5, "BASELINE_CHANGED"),
            "current_union_digest": (D5, "CANDIDATE_NOT_READY"),
            "current_manifest_digest": (D5, "CANDIDATE_NOT_READY"),
            "current_data_generation": (2, "CANDIDATE_NOT_READY"),
            "current_counts": ((("raw_source_count", 99),), "CANDIDATE_NOT_READY"),
            "current_code_digest": (D4, "CANDIDATE_NOT_READY"),
            "current_contract_version": ("obsolete", "CANDIDATE_NOT_READY"),
            "current_configuration_digest": (D5, "CANDIDATE_NOT_READY")}
        for field, (value, code) in changes.items():
            with self.subTest(field=field):
                facts = {**self.facts, field: value}
                self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
                    self.evidence, **facts), code)

    def test_missing_forged_or_dirty_evidence_cannot_compare(self):
        forged = copy.copy(self.evidence)
        object.__setattr__(forged, "data_generation", True)
        for evidence in (None, {}, forged, replace(self.evidence, integrity_verified=False)):
            self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
                evidence, **self.facts))
        for candidate in (None, replace(self.candidate, evidence_digest=D5),
                replace(self.candidate, last_error="SOURCE_READ_TIMEOUT"),
                replace(self.candidate, state="BUILDING")):
            self.denies(lambda: lifecycle.compare_candidate_evidence(candidate,
                self.evidence, **self.facts))

    def test_complete_source_material_is_hashed_not_cached_digest_or_count(self):
        self.assertEqual(independent_source_digest(self.inv),
                         lifecycle.source_inventory_digest(self.inv))
        row = self.inv.rows[0]
        changes = [replace(self.inv, rows=(replace(row,
            message=replace(row.message, content="synthetic-altered")), self.inv.rows[1])),
            replace(self.inv, rows=(replace(row, server_id="999"), self.inv.rows[1])),
            replace(self.inv, rows=tuple(reversed(self.inv.rows))),
            replace(self.inv, sources=(replace(self.inv.sources[0], schema_fingerprint="changed"),)),
            replace(self.inv, digests=replace(self.inv.digests, source_rows_digest=D5))]
        for changed in changes:
            with self.subTest(kind="complete-source-material"):
                self.assertNotEqual(independent_source_digest(self.inv),
                                    lifecycle.source_inventory_digest(changed))
                self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
                    self.evidence, **{**self.facts, "current_inventory": changed}), "SOURCE_CHANGED")

    def test_cached_source_digests_cannot_hide_descriptor_count_or_scope_change(self):
        for changed in (replace(self.inv, sources=()), replace(self.inv, rows=()),
            replace(self.inv, counts=(replace(self.inv.counts[0], raw_total=99),)),
            replace(self.inv, scope=OTHER_SCOPE), replace(self.inv, mode="quick")):
            self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
                self.evidence, **{**self.facts, "current_inventory": changed}), "SOURCE_CHANGED")
        self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
            self.evidence, **{**self.facts, "recorded_inventory": None}), "SOURCE_CHANGED")

    def test_current_integrity_requires_each_exact_scope_identity_and_health_fact(self):
        integrity = self.facts["current_integrity"]
        changes = {"scope": OTHER_SCOPE, "archive_id": "wrong", "data_generation": 9,
            "union_digest": D5, "manifest_digest": D5, "current_ok": False,
            "history_ok": False, "asset_closure_ok": False, "transaction_clean": False}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.denies(lambda: lifecycle.compare_candidate_evidence(self.candidate,
                    self.evidence, **{**self.facts,
                        "current_integrity": replace(integrity, **{field: value})}))

    def test_duplicate_negative_bool_and_empty_counts_are_not_valid_evidence(self):
        for counts in ((('raw_source_count', 2), ('raw_source_count', 2)),
                       (('raw_source_count', -1),), (('raw_source_count', True),), ()):
            changed = copy.copy(self.evidence)
            object.__setattr__(changed, 'counts', counts)
            candidate = replace(self.candidate, evidence_digest=contract_digest(changed))
            self.denies(lambda: lifecycle.compare_candidate_evidence(candidate,
                changed, **{**self.facts, 'current_counts': counts}))
            self.denies(lambda: lifecycle.admit_candidate_action(candidate,
                'request_activation', evidence=changed))

    def test_wrong_row_scope_or_malformed_inventory_fails_without_processing(self):
        row = self.inv.rows[0]
        wrong = replace(self.inv, rows=(replace(row,
            locator=replace(row.locator, scope=OTHER_SCOPE)), self.inv.rows[1]))
        bad_shape = replace(self.inv, rows=list(self.inv.rows))
        bad_digest = replace(self.inv, digests=replace(self.inv.digests, source_set_digest='invalid'))
        for inv in (None, {}, wrong, bad_shape, bad_digest):
            self.denies(lambda: lifecycle.source_inventory_digest(inv), 'SOURCE_CHANGED')

    def test_fresh_session_row_tokens_do_not_invalidate_same_complete_source(self):
        changed = replace(self.inv, rows=tuple(replace(row,
            raw_row_token='synthetic-fresh-' + str(index)) for index, row in enumerate(self.inv.rows)))
        self.assertEqual(independent_source_digest(self.inv), lifecycle.source_inventory_digest(changed))
        self.accepted(lambda: lifecycle.compare_candidate_evidence(self.candidate,
            self.evidence, **{**self.facts, 'current_inventory': changed}))

    def test_actual_core_fresh_inventory_excludes_lookup_tokens_but_binds_lossless_rows(self):
        # Real Core reader and spawned snapshot workers; all SQLite/cache roots
        # are temporary synthetics. No verifier or archive-write route is used.
        with synthetic_history() as fixture, tempfile.TemporaryDirectory(
                prefix='synthetic-task6-reader-') as work:
            reader = ConversationReader(SyntheticReaderDB(fixture.operation_root),
                fixture.scope, Path(work), deadline_seconds=10, worker_grace_seconds=2)
            with reader.open_inventory(mode='full') as session:
                first = session.inventory
            with reader.open_inventory(mode='full') as session:
                fresh = session.inventory
            self.assertNotEqual(tuple(row.raw_row_token for row in first.rows),
                                tuple(row.raw_row_token for row in fresh.rows))
            self.assertEqual(first.digests, fresh.digests)
            self.assertEqual(lifecycle.source_inventory_digest(first),
                             lifecycle.source_inventory_digest(fresh))
            connection = sqlite3.connect(fixture.operation_root / 'message/message_0.db')
            try:
                connection.execute('UPDATE Msg_synthetic SET source=? WHERE local_id=1',
                                   (b'synthetic-opaque-raw-change',))
                connection.commit()
            finally:
                connection.close()
            with reader.open_inventory(mode='full') as session:
                changed = session.inventory
            self.assertEqual(tuple(row.message for row in first.rows),
                             tuple(row.message for row in changed.rows))
            self.assertEqual(first.counts, changed.counts)
            self.assertNotEqual(lifecycle.source_inventory_digest(first),
                                lifecycle.source_inventory_digest(changed))

    def test_pure_comparison_and_requests_do_no_io(self):
        events, enabled = [], [False]
        def audit(event, args):
            if enabled[0] and (event == "open" or event.startswith(("os.", "subprocess.", "socket."))):
                events.append(event)
        sys.addaudithook(audit)
        enabled[0] = True
        try:
            lifecycle.source_inventory_digest(self.inv)
            self.accepted(lambda: lifecycle.compare_candidate_evidence(self.candidate, self.evidence, **self.facts))
            lifecycle.admit_candidate_action(self.candidate, "request_activation", evidence=self.evidence)
        finally:
            enabled[0] = False
        self.assertEqual([], events)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-task6-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.profile = V3Profile(Path(self.temp.name), "synthetic-profile", "SYNTHETIC")
        self.proof = RuntimeProof(SCOPE, "synthetic-provider", D1, True)
        self.base = object()

    def denies(self, callback):
        with self.assertRaises(Track2Error) as caught:
            callback()
        self.assertEqual("RUNTIME_BINDING_UNVERIFIED", caught.exception.code)

    def test_explicit_synthetic_verifier_gets_exact_inputs_and_returns_same_proof(self):
        verifier = SyntheticVerifier(self.proof)
        self.assertIs(self.proof, runtime.verify_synthetic_runtime(self.profile,
            self.base, SCOPE, verifier))
        self.assertEqual([(self.base, SCOPE)], verifier.calls)

    def test_non_synthetic_purpose_rejects_before_any_verifier_call(self):
        for purpose in ("FORMAL", "REHEARSAL"):
            verifier = SyntheticVerifier(self.proof)
            self.denies(lambda: runtime.verify_synthetic_runtime(
                replace(self.profile, purpose=purpose), self.base, SCOPE, verifier))
            self.assertEqual([], verifier.calls)

    def test_missing_or_invalid_verifier_and_profile_are_denied(self):
        for verifier in (None, object(), {"verify": lambda: self.proof}):
            self.denies(lambda: runtime.verify_synthetic_runtime(self.profile,
                self.base, SCOPE, verifier))
        bad_profile = copy.copy(self.profile)
        object.__setattr__(bad_profile, "purpose", True)
        for profile, scope in ((None, SCOPE), (bad_profile, SCOPE), (self.profile, None)):
            verifier = SyntheticVerifier(self.proof)
            self.denies(lambda: runtime.verify_synthetic_runtime(profile, self.base, scope, verifier))
            self.assertEqual([], verifier.calls)

    def test_runtime_proof_requires_exact_type_scope_synthetic_and_contract(self):
        class Subclass(RuntimeProof):
            pass
        forged = copy.copy(self.proof)
        object.__setattr__(forged, "binding_evidence_digest", "invalid")
        forged_bool = copy.copy(self.proof)
        object.__setattr__(forged_bool, "synthetic", 1)
        for proof in (None, {}, replace(self.proof, scope=OTHER_SCOPE),
            replace(self.proof, synthetic=False), Subclass(SCOPE, "synthetic", D1, True),
            forged, forged_bool):
            self.denies(lambda: runtime.verify_synthetic_runtime(self.profile,
                self.base, SCOPE, SyntheticVerifier(proof)))

    def test_authenticated_adapter_construction_does_not_replace_verifier(self):
        caller_asserted = AuthenticatedHistoryDB(self.base, SCOPE.account_id)
        self.denies(lambda: runtime.verify_synthetic_runtime(self.profile,
            caller_asserted, SCOPE, None))

    def test_verifier_exception_is_bounded_and_private_details_are_discarded(self):
        class BrokenVerifier:
            def verify(self, base_db, scope):
                raise RuntimeError("synthetic-private-error")
        self.denies(lambda: runtime.verify_synthetic_runtime(self.profile,
            self.base, SCOPE, BrokenVerifier()))

    def test_deadline_rejects_equal_or_late_return_and_invalid_clock_values(self):
        self.assertIsNone(runtime.ensure_returned_before_deadline(10.0, 9.999))
        for deadline, returned in ((10., 10.), (10., 11.), (True, 1.),
            (10., True), (math.inf, 1.), (10., math.nan), (0., -1.), ("10", 1.)):
            with self.subTest(kind="returned-deadline"):
                with self.assertRaises(HistoryError) as caught:
                    runtime.ensure_returned_before_deadline(deadline, returned)
                self.assertEqual("SOURCE_READ_TIMEOUT", caught.exception.code)

    def test_retry_once_only_before_both_durable_boundaries(self):
        for used, prepared, installed, want in ((0, False, False, True),
            (1, False, False, False), (2, False, False, False),
            (0, True, False, False), (0, False, True, False), (0, True, True, False)):
            with self.subTest(used=used, prepared=prepared, installed=installed):
                self.assertIs(want, runtime.source_retry_allowed(SourceSetChanged(),
                    retries_used=used, t7_prepared=prepared, durable_vault_installed=installed))

    def test_out_of_range_clock_observation_has_stable_timeout_error(self):
        with self.assertRaises(Exception) as caught:
            runtime.ensure_returned_before_deadline(10 ** 1000, 1.)
        self.assertIs(type(caught.exception), HistoryError)
        self.assertEqual('SOURCE_READ_TIMEOUT', str(caught.exception))

    def test_retry_policy_does_not_recast_other_core_errors_or_malformed_inputs(self):
        errors = (HistoryError("TRANSACTION_RECOVERY_REQUIRED"),
            HistoryError("SOURCE_READ_TIMEOUT"), HistoryError("SOURCE_SET_CHANGED"),
            RuntimeError("SOURCE_SET_CHANGED"), None)
        for error in errors:
            self.assertIs(False, runtime.source_retry_allowed(error, retries_used=0,
                t7_prepared=False, durable_vault_installed=False))
        for used, prepared, installed in ((True, False, False), (-1, False, False),
            (0., False, False), (math.inf, False, False), (0, 0, False), (0, False, 0)):
            self.assertIs(False, runtime.source_retry_allowed(SourceSetChanged(),
                retries_used=used, t7_prepared=prepared, durable_vault_installed=installed))
        forged = SourceSetChanged()
        forged.code = "TRANSACTION_RECOVERY_REQUIRED"
        self.assertIs(False, runtime.source_retry_allowed(forged, retries_used=0,
            t7_prepared=False, durable_vault_installed=False))

    def test_origin_metadata_is_independent_of_source_verification(self):
        # Pure hand-built NONAUTHORITATIVE metadata, no root/confirmation issuer.
        root = RootHandleProof(Path(self.temp.name) / "input", ROOT_ID,
            str(Path(self.temp.name) / "input"), (), ())
        entries = (TreeEntry(".", "directory", None, None, ROOT_ID),)
        content = contract_digest(("track2-v1", "content-tree", ((".", "directory", None, None),)))
        physical = contract_digest(("track2-v1", "physical-tree", root, entries))
        baseline = TreeSnapshot(content, physical, 1, 0,
            "2026-10-09T00:00:00+00:00", root, entries)
        origin = ArchiveOriginConfirmation("synthetic-origin", SCOPE, ROOT_ID, content, physical, True)
        approval = ForkApproval("synthetic-metadata-only", "SYNTHETIC", "synthetic-object",
            SCOPE, baseline, contract_digest(origin), (), D1, False, D2, CONTRACT_VERSION, None, D1)
        self.assertEqual(ScopeAuthorization(SCOPE, True), runtime.authorize_archive_origin(
            origin, approval, baseline, SCOPE))
        for wrong in (None, self.proof, replace(origin, explicit_origin_confirmed=False),
            replace(origin, scope=OTHER_SCOPE), replace(origin, input_content_digest=D3),
            replace(origin, input_physical_digest=D3),
            replace(origin, input_root_id=FileIdentity(7, "bc" * 16, 1))):
            updated = (approval if wrong is None or wrong is self.proof else
                replace(approval, archive_origin_confirmation_digest=contract_digest(wrong)))
            with self.assertRaises(HistoryError) as caught:
                runtime.authorize_archive_origin(wrong, updated, baseline, SCOPE)
            self.assertEqual("LEGACY_SCOPE_UNVERIFIED", caught.exception.code)
        with self.assertRaises(HistoryError) as caught:
            runtime.authorize_archive_origin(origin,
                replace(approval, archive_origin_confirmation_digest=D3), baseline, SCOPE)
        self.assertEqual("LEGACY_SCOPE_UNVERIFIED", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
