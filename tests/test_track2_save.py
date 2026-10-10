"""Handmade NONAUTHORITATIVE metadata; never exercise a save writer.

Every fixture is an in-memory policy input, not a committed active binding,
live lease, READY receipt, origin authentication or runtime proof.
"""
from copy import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from test_track2_state import fixture
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.track2_contracts import (
    AuthorityBinding, CandidateRecord, FileIdentity, StateBundle, Track2Error,
    canonical_bytes, contract_digest,
)
from wechat_exporter.v3_save import compare_active_save


ROOT = FileIdentity(91, "a" * 32, 1)
OTHER_ROOT = FileIdentity(91, "b" * 32, 1)


def inputs():
    state = fixture()
    binding = AuthorityBinding("synthetic-object", state.scope, 3, "ACTIVE_V3",
        state.archive_id, ROOT, "1" * 64, "2" * 64, 5)
    return binding, dict(requested_object_id="synthetic-object",
        requested_scope=state.scope, requested_root_id=ROOT,
        requested_archive_id=state.archive_id, state=state, mode="full",
        expected_binding_generation=3, expected_binding_digest=contract_digest(binding))


def malformed(value, **changes):
    """Simulate unchecked DTO input without changing an accepted fixture."""
    result = copy(value)
    for name, change in changes.items():
        object.__setattr__(result, name, change)
    return result


class Track2SaveGuardTests(unittest.TestCase):
    def refuses(self, binding, arguments, code):
        with self.assertRaises(Track2Error) as caught:
            compare_active_save(binding, **arguments)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_exact_active_binding_returns_same_input_for_real_modes(self):
        # Break: empty return or renamed incremental/reconciliation modes.
        for mode in ("quick", "full"):
            binding, arguments = inputs()
            arguments["mode"] = mode
            self.assertIs(binding, compare_active_save(binding, **arguments))

    def test_absent_and_preserved_binding_do_not_admit_save(self):
        # Break: read-only v2/absent binding becomes active admission.
        binding, arguments = inputs()
        preserved = replace(binding, kind="PRESERVED_V2_READONLY", generation=0,
            activation_approval_digest=None, activation_manifest_digest=None,
            activation_data_generation=None)
        for value in (None, preserved):
            self.refuses(value, arguments, "RUNTIME_BINDING_UNVERIFIED")

    def test_candidate_status_cannot_substitute_for_authority(self):
        # Break: a READY/ACTIVE status flag is treated as an authority binding.
        binding, arguments = inputs()
        for status in ("BUILDING", "READY", "ACTIVATION_PENDING", "ACTIVE", "FAILED", "RECOVERY_REQUIRED"):
            candidate = CandidateRecord(binding.archive_id, binding.object_id, binding.scope,
                "SYNTHETIC", ROOT, status, "3" * 64, None, "operation")
            self.refuses(candidate, arguments, "RUNTIME_BINDING_UNVERIFIED")

    def test_all_activation_anchors_and_positive_authority_generation_required(self):
        # Break: partial/inactive/suspect binding is accepted.
        binding, arguments = inputs()
        for changes in ({"activation_approval_digest": None},
                {"activation_manifest_digest": None}, {"activation_data_generation": None},
                {"generation": 0}, {"generation": True}, {"kind": "INACTIVE"},
                {"activation_approval_digest": "bad"}, {"activation_data_generation": True}):
            with self.subTest(changes=changes):
                self.refuses(malformed(binding, **changes), arguments, "RUNTIME_BINDING_UNVERIFIED")

    def test_request_object_scope_root_and_archive_must_match(self):
        # Break: cached arbitrary destination/object differs from current binding.
        binding, arguments = inputs()
        for key, value in (("requested_object_id", "other-object"),
                ("requested_scope", ArchiveScope("other-account", "other-conversation")),
                ("requested_root_id", OTHER_ROOT), ("requested_archive_id", "other-archive")):
            with self.subTest(key=key):
                self.refuses(binding, dict(arguments, **{key: value}), "BINDING_CONFLICT")

    def test_cached_digest_and_generation_are_independent_required_comparisons(self):
        # Break: only matching IDs are checked, admitting a superseded request.
        binding, arguments = inputs()
        for key, value in (("expected_binding_generation", 2), ("expected_binding_generation", 4),
                ("expected_binding_generation", True), ("expected_binding_generation", None),
                ("expected_binding_digest", "9" * 64), ("expected_binding_digest", "bad"),
                ("expected_binding_digest", None)):
            with self.subTest(key=key, value=value):
                self.refuses(binding, dict(arguments, **{key: value}), "BINDING_CONFLICT")

    def test_same_generation_changed_binding_is_detected_by_cached_digest(self):
        # Break: generation equality alone blesses an altered activation anchor.
        binding, arguments = inputs()
        self.refuses(replace(binding, activation_manifest_digest="4" * 64), arguments, "BINDING_CONFLICT")

    def test_superseded_binding_requires_separately_refreshed_request(self):
        # Break: old root/archive/generation request admitted against new binding.
        binding, arguments = inputs()
        switched = replace(binding, generation=4, root_id=OTHER_ROOT, archive_id="new-archive")
        self.refuses(switched, arguments, "BINDING_CONFLICT")
        fresh = dict(arguments, requested_root_id=OTHER_ROOT, requested_archive_id="new-archive",
            expected_binding_generation=4, expected_binding_digest=contract_digest(switched),
            state=replace(arguments["state"], archive_id="new-archive"))
        self.assertIs(switched, compare_active_save(switched, **fresh))

    def test_data_generation_can_advance_without_changing_authority_anchors(self):
        # Break: data generation confused with authority generation/anchor.
        binding, arguments = inputs()
        for generation in (5, 6, 100):
            state = replace(arguments["state"], data_generation=generation)
            self.assertIs(binding, compare_active_save(binding, **dict(arguments, state=state)))
        self.assertEqual(3, binding.generation)
        self.assertEqual(5, binding.activation_data_generation)
        self.assertEqual("2" * 64, binding.activation_manifest_digest)

    def test_state_older_than_activation_is_recovery_required(self):
        # Break: rolled-back capsule can receive admission.
        binding, arguments = inputs()
        self.refuses(binding, dict(arguments, state=replace(arguments["state"], data_generation=4)),
            "TRACK2_RECOVERY_REQUIRED")

    def test_state_scope_and_archive_must_match_binding(self):
        # Break: wrong capsule is accepted because request IDs match.
        binding, arguments = inputs()
        for changes in ({"scope": ArchiveScope("other", "other")}, {"archive_id": "other-archive"}):
            with self.subTest(changes=changes):
                self.refuses(binding, dict(arguments, state=replace(arguments["state"], **changes)),
                    "BINDING_CONFLICT")

    def test_modes_fail_closed_without_coercion(self):
        # Break: unsupported modes fall back to full/v2 or truthiness.
        binding, arguments = inputs()
        for value in ("incremental", "full_reconciliation", "FULL", "", None, True, b"full", "full\ud800"):
            with self.subTest(value=value):
                self.refuses(binding, dict(arguments, mode=value), "RUNTIME_BINDING_UNVERIFIED")

    def test_malformed_requested_ids_scope_and_root_refuse_stably(self):
        # Break: malformed input leaks exception/data or bypasses exact comparison.
        binding, arguments = inputs()
        for key, value in (("requested_object_id", True), ("requested_object_id", ""),
                ("requested_object_id", "private\ud800"), ("requested_archive_id", None),
                ("requested_scope", None), ("requested_scope", ArchiveScope("private\ud800", "x")),
                ("requested_root_id", None), ("requested_root_id", malformed(ROOT, volume_serial=True))):
            with self.subTest(key=key):
                self.refuses(binding, dict(arguments, **{key: value}), "BINDING_CONFLICT")

    def test_malformed_binding_unicode_and_nested_dto_refuse_stably(self):
        # Break: canonical UTF8 encoding failure escapes boundary as an error.
        binding, arguments = inputs()
        for changes in ({"object_id": "private\ud800"}, {"archive_id": "private\ud800"},
                {"scope": ArchiveScope("private\ud800", "x")}, {"root_id": malformed(ROOT, link_count=0)}):
            with self.subTest(changes=changes):
                self.refuses(malformed(binding, **changes), arguments, "RUNTIME_BINDING_UNVERIFIED")

    def test_semantically_invalid_constructible_state_is_refused(self):
        # Break: StateBundle structural validation mistaken for T4 semantics.
        binding, arguments = inputs()
        state = arguments["state"]
        bad_record = replace(state.records[0], canonical_identity="server:not-a-number")
        for changed in (replace(state, records=tuple(reversed(state.records))),
                replace(state, records=(bad_record,) + state.records[1:]),
                replace(state, metadata=(("message_count", 999),)),
                replace(state, metadata=(("mode", "incremental"),)),
                replace(state, metadata=(("unknown", 0),))):
            self.refuses(binding, dict(arguments, state=changed), "TRACK2_RECOVERY_REQUIRED")

    def test_malformed_state_and_utf8_fail_without_raw_errors(self):
        # Break: bad metadata/record UTF8 escapes as ValueError/UnicodeError.
        binding, arguments = inputs()
        state = arguments["state"]
        record = replace(state.records[0], message=replace(state.records[0].message, content="private\ud800"))
        for changed in (None, {}, malformed(state, data_generation=True),
                replace(state, records=(record,) + state.records[1:]),
                replace(state, metadata=(("integrity_result", "private\ud800"),))):
            self.refuses(binding, dict(arguments, state=changed), "TRACK2_RECOVERY_REQUIRED")

    def test_valid_cjk_emoji_binding_and_message_are_accepted(self):
        # Break: strict UTF8 validation accidentally rejects valid non-ASCII text.
        binding, arguments = inputs()
        state = arguments["state"]
        scope = ArchiveScope("账号🦉", "会话雪鸮")
        records = []
        for record in state.to_core_records():
            records.append(replace(record, scope=scope,
                message=replace(record.message, content=record.message.content + "🦉😀"),
                source_refs=tuple(replace(ref, scope=scope) for ref in record.source_refs),
                legacy_aliases=tuple(dict(alias, scope=scope) for alias in record.legacy_aliases)))
        state = StateBundle(scope, "存档🦉", 6, tuple(records), state.metadata)
        binding = replace(binding, scope=scope, object_id="对象雪鸮🦉", archive_id=state.archive_id)
        arguments.update(requested_scope=scope, requested_object_id=binding.object_id,
            requested_archive_id=state.archive_id, state=state,
            expected_binding_digest=contract_digest(binding))
        self.assertIs(binding, compare_active_save(binding, **arguments))

    def test_empty_semantically_valid_state_is_admitted(self):
        # Break: guard creates dummy records or rejects empty valid capsule.
        binding, arguments = inputs()
        state = StateBundle(binding.scope, binding.archive_id, 5, (), (("message_count", 0),))
        self.assertIs(binding, compare_active_save(binding, **dict(arguments, state=state)))

    def test_input_data_unchanged_on_admission_and_refusal_without_io(self):
        # Break: guard writes/opens a file or mutates archive-only/body/media data.
        binding, arguments = inputs()
        before = canonical_bytes((binding, arguments["state"]))
        with patch("builtins.open", side_effect=AssertionError("unexpected I/O")), \
                patch("pathlib.Path.open", side_effect=AssertionError("unexpected I/O")):
            self.assertIs(binding, compare_active_save(binding, **arguments))
            self.refuses(binding, dict(arguments, requested_root_id=OTHER_ROOT), "BINDING_CONFLICT")
        self.assertEqual(before, canonical_bytes((binding, arguments["state"])))
        self.assertEqual("old-image.png", arguments["state"].records[0].media_asset)
        self.assertEqual("archive_only", arguments["state"].records[-1].binding_status)


if __name__ == "__main__":
    unittest.main()
