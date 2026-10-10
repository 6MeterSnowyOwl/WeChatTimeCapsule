"""Owned synthetic P1 behavior: missing authority must reject before IO."""
import importlib.util
from pathlib import Path
import sys
import unittest
from dataclasses import replace
import copy
import os
import threading
import time
import gc
import shutil
import subprocess
import json
import hashlib
from unittest.mock import patch
from operation_root_fixtures import (operation_fixture, prepare, mutation_audit, SCOPE,
    existing_configurations, existing_namespace)

from wechat_exporter import track2_contracts as dto
from wechat_exporter import physical_paths as physical
from wechat_exporter.history_contracts import ArchiveScope
from track2_fixtures import synthetic_roots, legacy_proof, symlink


class WireAndAuthorityTests(unittest.TestCase):
    def old_approval(self, roots):
        baseline = physical.snapshot_tree(roots.input_v2)
        arguments = dict(approval_id="synthetic-old", purpose="SYNTHETIC",
            object_id="synthetic-object", scope=ArchiveScope("synthetic", "conversation"),
            input_baseline=baseline, archive_origin_confirmation_digest="1" * 64,
            destination_parent_proofs=(), legacy_namespace_digest="2" * 64,
            safety_backup_required=False, code_digest="3" * 64,
            contract_version="track2-v1", b_receipt_digest=None)
        if "root_context_digest" in dto.ForkApproval.__dataclass_fields__:
            arguments["root_context_digest"] = "4" * 64
        return dto.ForkApproval(**arguments)

    def test_old_approval_wire_cannot_silently_upgrade_missing_context(self):
        # Removing per-class version/mandatory field enforcement admits old facts.
        with synthetic_roots() as (_, roots, _, _):
            document = dto.dto_document(self.old_approval(roots))
            document["contract_version"] = "track2-v1"
            document["fields"].pop("root_context_digest", None)
            observed = None
            try:
                dto.decode_dto(dto.ForkApproval, document)
            except ValueError:
                observed = "rejected"
            self.assertEqual("rejected", observed)

    def test_caller_dto_cannot_admit_write_and_attempts_zero_mutations(self):
        # The absence of issuer validation is reached through a no-op seam only.
        spec = importlib.util.find_spec("wechat_exporter.operation_roots")
        admission = (getattr(__import__("wechat_exporter.operation_roots", fromlist=["x"]),
            "require_operation_admission", lambda *a, **k: None) if spec
            else lambda *a, **k: None)
        events, enabled = [], [False]
        def audit(event, args):
            if enabled[0] and (event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir")
                    or event == "open" and (args[1] and any(c in args[1] for c in "wax+")
                        or args[2] & (0x1 | 0x2 | 0x40 | 0x200))):
                events.append(event)
        sys.addaudithook(audit)
        with synthetic_roots() as (_, roots, _, _):
            approval = self.old_approval(roots)
            enabled[0] = True
            observed = None
            try:
                admission({}, approval, action="WORK_CREATE", role="reader_work", target=roots.reader_work)
            except Exception as error:
                observed = getattr(error, "code", None)
            finally:
                enabled[0] = False
            self.assertEqual("SOURCE_AUTHORIZATION_REQUIRED", observed)
            self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()


class ProducerAdmissionTests(unittest.TestCase):
    def rejected(self, function, code):
        observed = None
        with mutation_audit() as events:
            try:
                function()
            except Exception as error:
                observed = (type(error), getattr(error, "code", None))
        self.assertEqual((dto.Track2Error, code), observed)
        self.assertEqual([], events)

    def test_wrong_missing_provider_scope_binding_source_refuses(self):
        # A caller-created domain must never substitute for the factory binding.
        with operation_fixture() as f:
            for field, value in (("provider_id", "wrong"),
                    ("scope", ArchiveScope("wrong", "conversation")),
                    ("binding_evidence_digest", "2" * 64)):
                domain = f.domains[0]
                changed = (replace(domain, **{field: value}) if hasattr(domain, "__dataclass_fields__")
                    else type(domain)(**{**vars(domain), field: value}))
                self.rejected(lambda: f.producer.prepare(f.selection, f.legacy_proof,
                    (changed,), **f.arguments), "OPERATION_CONTEXT_MISMATCH")
            self.rejected(lambda: f.producer.prepare(f.selection, f.legacy_proof,
                (), **f.arguments), "OPERATION_CONTEXT_MISMATCH")

    def test_missing_content_enumeration_and_ancestor_metadata_refuse(self):
        # Removing a required explicit footprint must reject before its read.
        with operation_fixture() as f:
            for access, path in (("CONTENT", f.source), ("ENUMERATE", f.selection.input_v2),
                    ("METADATA", f.base.parent)):
                reads = tuple(r for r in f.arguments["read_footprints"]
                    if not (r.access == access and r.path == path))
                with patch.object(physical, "_open", wraps=physical._open) as opened, \
                        patch.object(physical.os, "scandir", wraps=physical.os.scandir) as enumerated:
                    self.rejected(lambda: prepare(f, read_footprints=reads), "FOOTPRINT_NOT_APPROVED")
                    self.assertEqual([], opened.call_args_list)
                    self.assertEqual([], enumerated.call_args_list)

    def test_missing_configuration_inventory_is_not_an_implied_grant(self):
        with operation_fixture() as f:
            self.rejected(lambda: prepare(f, configuration_objects=()), "OPERATION_CONTEXT_MISMATCH")

    def test_outside_owner_request_refuses_before_target_reads(self):
        with operation_fixture() as f:
            outside = replace(f.selection, candidate_v3=f.base.parent / "not-owned-p1")
            with patch.object(physical, "_open", wraps=physical._open) as opened, \
                    patch.object(physical.os, "scandir", wraps=physical.os.scandir) as enumerated:
                self.rejected(lambda: f.producer.prepare(outside, f.legacy_proof,
                    f.domains, **f.arguments), "SOURCE_AUTHORIZATION_REQUIRED")
                self.assertEqual([], opened.call_args_list)
                self.assertEqual([], enumerated.call_args_list)

    def test_genuine_missing_siblings_admit_with_real_handles_zero_mutations(self):
        # Removing physical producer preparation or sibling admission breaks this.
        with operation_fixture() as f:
            before = (physical.snapshot_tree(f.selection.input_v2), physical.snapshot_tree(f.source))
            with mutation_audit() as events:
                result = prepare(f)
            self.assertEqual([], events)
            self.assertIs(type(result), tuple)
            self.assertEqual(2, len(result))
            context, approval = result
            self.assertEqual("operation-root-v1", context.context_version)
            self.assertEqual(dto.contract_digest(context), approval.root_context_digest)
            with mutation_audit() as events:
                operation = __import__("wechat_exporter.operation_roots", fromlist=["x"])
                self.assertIsNone(operation.require_operation_admission(context, approval,
                    action="WORK_CREATE", role="reader_work", target=f.selection.reader_work))
            self.assertEqual([], events)
            self.assertEqual(before, (physical.snapshot_tree(f.selection.input_v2), physical.snapshot_tree(f.source)))

    def test_same_account_wrong_conversation_refuses(self):
        with operation_fixture() as f:
            domain = replace(f.domains[0], scope=ArchiveScope(SCOPE.account_id, "wrong-conversation"))
            self.rejected(lambda: f.producer.prepare(f.selection, f.legacy_proof, (domain,),
                **f.arguments), "OPERATION_CONTEXT_MISMATCH")


class SealedCurrentAdmissionTests(unittest.TestCase):
    rejected = ProducerAdmissionTests.rejected
    def admission(self, context, approval, **changes):
        operation = __import__("wechat_exporter.operation_roots", fromlist=["x"])
        return operation.require_operation_admission(context, approval, **{
            "action": "WORK_CREATE", "role": "reader_work", "target": context.selection.reader_work,
            **changes})

    def test_context_changes_and_freshness_are_not_authority(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            for field, value in (("object_id", "wrong"), ("operation_id", "wrong"),
                    ("scope", ArchiveScope("wrong", "conversation")), ("provider_id", "wrong")):
                forged = copy.copy(context)
                object.__setattr__(forged, field, value)
                self.rejected(lambda: self.admission(forged, approval), "OPERATION_CONTEXT_MISMATCH")
            self.rejected(lambda: self.admission(replace(context, freshness=replace(context.freshness,
                issued_at_ns=context.freshness.issued_at_ns + 1)), approval), "OPERATION_CONTEXT_MISMATCH")

    def test_copy_replace_decode_and_seal_transplant_refuse_before_handles(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            forged = [copy.copy(approval), replace(approval), dto.decode_dto(dto.ForkApproval,
                dto.dto_document(approval))]
            for other in forged:
                object.__setattr__(other, "_operation_issuer", getattr(approval, "_operation_issuer"))
                with patch.object(physical, "_open", wraps=physical._open) as observed:
                    self.rejected(lambda: self.admission(context, other), "SOURCE_AUTHORIZATION_REQUIRED")
                    self.assertEqual([], observed.call_args_list)

    def test_approval_bytes_cannot_change_on_original_object(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            object.__setattr__(approval, "object_id", "wrong")
            with patch.object(physical, "_open", wraps=physical._open) as observed:
                self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_MISMATCH")
                self.assertEqual([], observed.call_args_list)

    def test_monotonic_expiry_cannot_be_extended_by_wallclock_rollback(self):
        with operation_fixture() as f:
            context, approval = prepare(f, lifetime_seconds=.02)
            time.sleep(.03)
            with patch("wechat_exporter.operation_roots.time.time_ns", return_value=1):
                self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_EXPIRED")

    def test_thread_and_closed_issuer_cannot_resurrect_permission(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            errors = []
            def other_thread():
                try:
                    self.admission(context, approval)
                except Exception as error:
                    errors.append(getattr(error, "code", None))
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join()
            self.assertEqual(["SOURCE_AUTHORIZATION_REQUIRED"], errors)
            f.producer.close()
            self.rejected(lambda: self.admission(context, approval), "SOURCE_AUTHORIZATION_REQUIRED")

    def test_unknown_action_role_leaf_and_real_purpose_refuse_before_handles(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            for changes in ({"action": "ARBITRARY"}, {"role": "input_v2"},
                    {"target": context.selection.reader_work / "arbitrary"}):
                with patch.object(physical, "_open", wraps=physical._open) as observed:
                    self.rejected(lambda: self.admission(context, approval, **changes), "OPERATION_CONTEXT_MISMATCH")
                    self.assertEqual([], observed.call_args_list)
            for purpose in ("FORMAL", "REHEARSAL"):
                forged = copy.copy(approval)
                object.__setattr__(forged, "purpose", purpose)
                self.rejected(lambda: self.admission(context, forged), "SOURCE_AUTHORIZATION_REQUIRED")

    def test_missing_suffix_external_creation_is_not_silently_blessed(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            context.selection.reader_work.mkdir()
            self.rejected(lambda: self.admission(context, approval), "ROOT_PROOF_FAILED")

    def test_parent_source_input_and_protected_replacements_refuse(self):
        for role in ("source", "input_v2", "protected"):
            with self.subTest(role=role), operation_fixture() as f:
                context, approval = prepare(f)
                target = f.source if role == "source" else f.legacy if role == "protected" else f.selection.input_v2
                target.rename(target.with_name(target.name + "-old"))
                target.mkdir()
                self.rejected(lambda: self.admission(context, approval), "ROOT_PROOF_FAILED")

    def test_input_bytes_and_shared_hardlink_are_rechecked(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            (f.selection.input_v2 / "archive.bin").write_bytes(b"changed")
            self.rejected(lambda: self.admission(context, approval, action="FORK_ROOT_CREATE",
                role="candidate_v3", target=f.selection.candidate_v3), "BASELINE_CHANGED")
        with operation_fixture() as f:
            context, approval = prepare(f)
            os.link(f.source / "source.bin", f.source / "shared-hardlink.bin")
            self.rejected(lambda: self.admission(context, approval), "ROOT_PROOF_FAILED")

    def test_producer_admission_does_not_read_program_files_outside_owned_tree(self):
        with operation_fixture() as f:
            observed, enabled = [], [True]
            def audit(event, args):
                if enabled[0] and event == "open" and isinstance(args[0], str):
                    if f.base not in Path(args[0]).parents and Path(args[0]) != f.base:
                        observed.append(args[0])
            sys.addaudithook(audit)
            try:
                prepare(f)
            finally:
                enabled[0] = False
            self.assertEqual([], observed)


class FileConfigurationAdmissionTests(unittest.TestCase):
    rejected = ProducerAdmissionTests.rejected

    def test_ambiguous_config_and_selected_paths_refuse_before_any_target_io(self):
        for selected in (False, True):
            with self.subTest(selected=selected), operation_fixture() as f:
                ambiguous = f.base / "unapproved-parent" / ".." / "ambiguous-leaf"
                if selected:
                    f.selection = replace(f.selection, reader_work=ambiguous)
                    f.arguments["write_footprints"] = tuple(replace(w, path=ambiguous)
                        if w.role == "reader_work" else w for w in f.arguments["write_footprints"])
                else:
                    configs = tuple(replace(c, path=ambiguous) if c.kind == "LEGACY_SETTINGS" else c
                        for c in f.arguments["configuration_objects"])
                    f.arguments["configuration_objects"] = configs
                    f.legacy_proof = replace(f.legacy_proof, settings_digest=dto.contract_digest(
                        tuple((c.kind, c.path, c.expected_digest) for c in configs if c.kind == "LEGACY_SETTINGS")))
                reads = list(f.arguments["read_footprints"])
                for path in (ambiguous, *ambiguous.parents):
                    if not any(r.path == path and r.access == "METADATA" for r in reads):
                        reads.append(dto.ReadFootprint(path, "METADATA", False, None))
                f.arguments["read_footprints"] = tuple(reads)
                with patch.object(physical, "_open", wraps=physical._open) as opened, \
                        patch.object(physical.os, "lstat", wraps=physical.os.lstat) as queried, \
                        patch.object(physical.os, "scandir", wraps=physical.os.scandir) as enumerated:
                    from wechat_exporter.operation_roots import _capture_missing
                    self.rejected(lambda: _capture_missing(f.base, ambiguous), "ROOT_PROOF_FAILED")
                    self.assertEqual([], opened.call_args_list)
                    self.assertEqual([], queried.call_args_list)
                    self.assertEqual([], enumerated.call_args_list)
                    self.rejected(lambda: prepare(f), "ROOT_PROOF_FAILED")
                    self.assertEqual([], opened.call_args_list)
                    self.assertEqual([], queried.call_args_list)
                    self.assertEqual([], enumerated.call_args_list)

    def test_context_schema_requires_exact_absent_configuration_parent_witnesses(self):
        with operation_fixture() as f:
            context, _ = prepare(f)
            witnesses = context.protected_objects
            first = witnesses[0]
            malformed = ((), (replace(first, owner="CONFIGURATION_PARENT:UNKNOWN:" + "1" * 64),),
                (replace(first, proof=replace(first.proof, directory=False)), *witnesses[1:]),
                (replace(first, proof=replace(first.proof, path=f.base / "unassociated-parent")), *witnesses[1:]))
            for proofs in malformed:
                with self.subTest(count=len(proofs)):
                    observed = None
                    try:
                        replace(context, protected_objects=tuple(sorted(proofs,
                            key=lambda p: (p.owner, str(p.proof.path).casefold()))))
                    except ValueError:
                        observed = "rejected"
                    self.assertEqual("rejected", observed)

    def absent_nested_configuration(self, f):
        parent = f.base / "configuration-parent"
        parent.mkdir()
        target = parent / "first-missing" / "settings.json"
        configs = tuple(replace(c, path=target) if c.kind == "LEGACY_SETTINGS" else c
            for c in f.arguments["configuration_objects"])
        f.arguments["configuration_objects"] = configs
        f.legacy_proof = replace(f.legacy_proof, settings_digest=dto.contract_digest(
            tuple((c.kind, c.path, c.expected_digest) for c in configs if c.kind == "LEGACY_SETTINGS")))
        reads = list(f.arguments["read_footprints"])
        for path in (target, *target.parents):
            if not any(r.path == path and r.access == "METADATA" for r in reads):
                reads.append(dto.ReadFootprint(path, "METADATA", False, None))
        f.arguments["read_footprints"] = tuple(reads)
        return parent, target

    def test_absent_configuration_capture_stops_at_first_missing_without_leaf_query(self):
        with operation_fixture() as f:
            parent, target = self.absent_nested_configuration(f)
            with patch.object(physical.os, "lstat", wraps=physical.os.lstat) as observed:
                context, approval = prepare(f)
            self.assertFalse(any(call.args[0] == target for call in observed.call_args_list))
            owner = "CONFIGURATION_PARENT:LEGACY_SETTINGS:" + dto.contract_digest(target)
            witnesses = [p.proof for p in context.protected_objects if p.owner == owner]
            self.assertEqual(1, len(witnesses))
            self.assertTrue(witnesses[0].directory)
            self.assertEqual(parent, witnesses[0].path)
            SealedCurrentAdmissionTests.admission(self, context, approval)

    def test_absent_configuration_parent_replacement_or_reparse_refuses_before_leaf_query(self):
        for alias in (False, True):
            with self.subTest(alias=alias), operation_fixture() as f:
                parent, target = self.absent_nested_configuration(f)
                context, approval = prepare(f)
                old = parent.with_name("original-configuration-parent")
                parent.rename(old)
                if alias:
                    symlink(parent, old, directory=True)
                else:
                    parent.mkdir()
                try:
                    with patch.object(physical.os, "lstat", wraps=physical.os.lstat) as observed:
                        self.rejected(lambda: SealedCurrentAdmissionTests.admission(self, context, approval),
                            "ROOT_PROOF_FAILED")
                        self.assertFalse(any(call.args[0] == target for call in observed.call_args_list))
                finally:
                    if alias:
                        parent.unlink()

    def test_absent_configuration_first_missing_creation_refuses_before_leaf_query(self):
        with operation_fixture() as f:
            parent, target = self.absent_nested_configuration(f)
            context, approval = prepare(f)
            target.parent.mkdir()
            with patch.object(physical.os, "lstat", wraps=physical.os.lstat) as observed:
                self.rejected(lambda: SealedCurrentAdmissionTests.admission(self, context, approval),
                    "ROOT_PROOF_FAILED")
                self.assertFalse(any(call.args[0] == target for call in observed.call_args_list))

    def test_selected_missing_parent_is_validated_before_destination_query(self):
        with operation_fixture() as f:
            parent = f.base / "destination-parent"
            parent.mkdir()
            target = parent / "first-missing" / "reader"
            f.selection = replace(f.selection, reader_work=target)
            f.arguments["write_footprints"] = tuple(replace(w, path=target)
                if w.role == "reader_work" else w for w in f.arguments["write_footprints"])
            reads = list(f.arguments["read_footprints"])
            for path in (target, *target.parents):
                if not any(r.path == path and r.access == "METADATA" for r in reads):
                    reads.append(dto.ReadFootprint(path, "METADATA", False, None))
            f.arguments["read_footprints"] = tuple(reads)
            context, approval = prepare(f)
            parent.rename(parent.with_name("original-destination-parent"))
            parent.mkdir()
            with patch.object(physical.os, "lstat", wraps=physical.os.lstat) as observed:
                self.rejected(lambda: SealedCurrentAdmissionTests.admission(self, context, approval),
                    "ROOT_PROOF_FAILED")
                self.assertFalse(any(call.args[0] == target for call in observed.call_args_list))

    def test_absent_configuration_path_is_reserved_against_planned_writes(self):
        with operation_fixture() as f:
            configs = tuple(replace(c, path=f.selection.reader_work)
                if c.kind == "LEGACY_SETTINGS" else c for c in f.arguments["configuration_objects"])
            f.arguments["configuration_objects"] = configs
            f.legacy_proof = replace(f.legacy_proof, settings_digest=dto.contract_digest(
                tuple((c.kind, c.path, c.expected_digest) for c in configs if c.kind == "LEGACY_SETTINGS")))
            self.rejected(lambda: prepare(f), "ROOT_PROOF_FAILED")

    def test_explicit_existing_configuration_files_are_real_file_proofs(self):
        # Fabricating directory proofs or refusing all existing files breaks this.
        with operation_fixture() as f:
            existing_configurations(f)
            with mutation_audit() as events:
                context, approval = prepare(f)
            self.assertEqual([], events)
            objects = {p.proof.path: p.proof for p in context.protected_objects}
            for config in f.arguments["configuration_objects"]:
                self.assertIn(config.path, objects)
                self.assertIs(type(objects[config.path]), dto.ObjectHandleProof)
                self.assertIs(objects[config.path].directory, False)
                self.assertEqual(len(config.path.read_bytes()), objects[config.path].size)
            self.assertEqual(dto.contract_digest(context), approval.root_context_digest)

    def test_configuration_exact_read_coverage_checked_before_content(self):
        with operation_fixture() as f:
            existing_configurations(f)
            selected = f.arguments["configuration_objects"][0]
            reads = tuple(r for r in f.arguments["read_footprints"]
                if not (r.path == selected.path and r.access == "CONTENT"))
            with patch.object(physical, "_open", wraps=physical._open) as observed:
                self.rejected(lambda: prepare(f, read_footprints=reads), "FOOTPRINT_NOT_APPROVED")
                self.assertFalse(any(call.args[0] == selected.path for call in observed.call_args_list))

    def test_stale_configuration_hash_and_absent_file_claim_refuse(self):
        with operation_fixture() as f:
            existing_configurations(f)
            configs = f.arguments["configuration_objects"]
            configs[0].path.write_bytes(b"changed")
            self.rejected(lambda: prepare(f), "OPERATION_CONTEXT_MISMATCH")
        with operation_fixture() as f:
            f.arguments["configuration_objects"][0].path.write_bytes(b"unexpected-present")
            self.rejected(lambda: prepare(f), "OPERATION_CONTEXT_MISMATCH")

    def test_configuration_file_replacement_and_aliases_fail_current_proof(self):
        with operation_fixture() as f:
            existing_configurations(f)
            context, approval = prepare(f)
            path = f.arguments["configuration_objects"][0].path
            original = path.read_bytes()
            path.rename(path.with_name("replaced-settings.json"))
            path.write_bytes(original)
            self.rejected(lambda: SealedCurrentAdmissionTests.admission(self, context, approval), "ROOT_PROOF_FAILED")
        with operation_fixture() as f:
            existing_configurations(f)
            first, second = f.arguments["configuration_objects"]
            second.path.unlink()
            os.link(first.path, second.path)
            self.rejected(lambda: prepare(f), "ROOT_PROOF_FAILED")

    def test_existing_namespace_empty_index_has_no_omitted_reference_reads(self):
        with operation_fixture() as f:
            root, _, _ = existing_namespace(f)
            with mutation_audit() as events:
                context, approval = prepare(f)
            self.assertEqual([], events)
            self.assertIn("namespace", dict(context.existing_role_proofs))
            self.assertNotEqual(dto.contract_digest(None), context.freshness.registry_digest)

    def test_registry_reference_omitted_from_protection_refuses_before_follow(self):
        # Registry facts are parsed first; a reference is never a new read grant.
        with operation_fixture() as f:
            hidden = f.base / "unapproved-reference"
            hidden.mkdir()
            (hidden / "private-synthetic.bin").write_bytes(b"unapproved")
            existing_namespace(f, references=(hidden,))
            with patch.object(physical, "_open", wraps=physical._open) as observed:
                self.rejected(lambda: prepare(f), "FOOTPRINT_NOT_APPROVED")
                self.assertFalse(any(call.args[0] == hidden for call in observed.call_args_list))

    def test_source_input_interchange_and_nested_missing_suffix_refuse(self):
        with operation_fixture() as f:
            source = replace(f.domains[0], root=f.selection.input_v2)
            reads = f.arguments["read_footprints"] + tuple(dto.ReadFootprint(f.selection.input_v2,
                access, True, source.domain_id) for access in ("ENUMERATE", "CONTENT"))
            self.rejected(lambda: f.producer.prepare(f.selection, f.legacy_proof, (source,),
                **{**f.arguments, "read_footprints": reads}), "ROOT_PROOF_FAILED")
        with operation_fixture() as f:
            roots = replace(f.selection, staging=f.selection.reader_work / "nested")
            writes = tuple(replace(w, path=roots.staging) if w.role == "staging" else w
                for w in f.arguments["write_footprints"])
            reads = f.arguments["read_footprints"] + (dto.ReadFootprint(roots.staging, "METADATA", False, None),)
            self.rejected(lambda: f.producer.prepare(roots, f.legacy_proof, f.domains,
                **{**f.arguments, "read_footprints": reads, "write_footprints": writes}), "ROOT_PROOF_FAILED")

    def test_namespace_fixed_directory_and_zero_byte_lock_types_refuse(self):
        for leaf in ("objects", "operations", "authority.lock"):
            with self.subTest(leaf=leaf), operation_fixture() as f:
                root, _, _ = existing_namespace(f)
                path = root / leaf
                if leaf == "authority.lock":
                    path.mkdir()
                else:
                    path.rmdir()
                    path.write_bytes(b"ordinary-wrong-layout-file")
                self.rejected(lambda: prepare(f), "ROOT_PROOF_FAILED")


class LoadedCodeAndLifetimeTests(unittest.TestCase):
    rejected = ProducerAdmissionTests.rejected
    admission = SealedCurrentAdmissionTests.admission

    def test_replaced_config_physical_or_codec_logic_invalidates_freshness(self):
        # Omitting actual helper/adapter/codec executable facts leaves stale grants usable.
        import wechat_exporter.operation_roots as operation
        for module, name in ((operation, "_exact_content"), (physical, "snapshot_tree"), (dto, "_matches")):
            with self.subTest(function=name), operation_fixture() as f:
                context, approval = prepare(f)
                original = getattr(module, name)
                def replaced(*args, **kwargs):
                    return original(*args, **kwargs)
                with patch.object(module, name, replaced):
                    self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_MISMATCH")

    def test_gc_cannot_remove_replacement_base_or_follow_changed_owner_name(self):
        # Default tempfile finalization must never bypass the physical owner gate.
        from wechat_exporter.operation_roots import SyntheticApprovalProducer
        from operation_root_fixtures import PROVIDER, BINDING
        producer = SyntheticApprovalProducer(SCOPE, PROVIDER, BINDING)
        original = producer.base
        saved = original.with_name(original.name + "-retained-original")
        original_id = physical.prove_existing_root(original).identity
        original.rename(saved)
        original.mkdir()
        replacement_id = physical.prove_existing_root(original).identity
        marker = original / "must-survive.bin"
        marker.write_bytes(b"synthetic replacement evidence")
        del producer
        gc.collect()
        self.assertTrue(original.exists())
        self.assertEqual(b"synthetic replacement evidence", marker.read_bytes())
        # Cleanup targets are two literal test-owned roots, with actual IDs checked.
        self.assertEqual(original_id, physical.prove_existing_root(saved).identity)
        self.assertEqual(replacement_id, physical.prove_existing_root(original).identity)
        shutil.rmtree(saved)
        shutil.rmtree(original)

    def test_expiry_during_real_revalidation_refuses_before_return(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            deadline = f.producer._records[id(approval)]["deadline"]
            ticks = iter((deadline - 1, deadline + 1))
            with patch("wechat_exporter.operation_roots.time.monotonic_ns", side_effect=lambda: next(ticks)):
                self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_EXPIRED")

    def test_changed_loaded_default_closure_or_property_invalidates_freshness(self):
        import wechat_exporter.operation_roots as operation
        for kind in ("defaults", "property"):
            with self.subTest(kind=kind), operation_fixture() as f:
                context, approval = prepare(f)
                if kind == "defaults":
                    target = operation.SyntheticApprovalProducer.prepare.__wrapped__
                    prior = target.__kwdefaults__
                    try:
                        target.__kwdefaults__ = {**prior, "lifetime_seconds": 299}
                        self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_MISMATCH")
                    finally:
                        target.__kwdefaults__ = prior
                else:
                    previous = operation.SyntheticApprovalProducer.base
                    replacement = property(lambda issuer: previous.fget(issuer))
                    with patch.object(operation.SyntheticApprovalProducer, "base", replacement):
                        self.rejected(lambda: self.admission(context, approval), "OPERATION_CONTEXT_MISMATCH")

    def test_loaded_code_digest_is_stable_across_real_fresh_process_hashseeds(self):
        # Compiled frozenset constants must not let repr/hash ordering change code anchors.
        digests = []
        for seed in ("1", "7", "42"):
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c",
                "from wechat_exporter.operation_roots import _code_digest; print(_code_digest())"],
                cwd=Path(__file__).parent.parent, env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode)
            digests.append(result.stdout.strip())
        self.assertEqual([digests[0]] * 3, digests)


class PureContextAndPhysicalMatrixTests(unittest.TestCase):
    rejected = ProducerAdmissionTests.rejected
    admission = SealedCurrentAdmissionTests.admission

    def test_authorize_fork_requires_keyword_context_before_body(self):
        from wechat_exporter.fork_authorization import authorize_fork
        with operation_fixture() as f:
            _, approval = prepare(f)
            observed = None
            try:
                authorize_fork(approval, approval.input_baseline, f.selection)
            except Exception as error:
                observed = type(error)
            self.assertIs(TypeError, observed)

    def test_pure_context_digest_match_accepts_without_issuing_filesystem_authority(self):
        from wechat_exporter.fork_authorization import authorize_fork
        with operation_fixture() as f:
            context, approval = prepare(f)
            decoded = dto.decode_dto(dto.ForkApproval, dto.dto_document(approval))
            result = None
            with mutation_audit() as events:
                try:
                    result = authorize_fork(decoded, decoded.input_baseline, f.selection, context=context)
                except Exception as error:
                    result = type(error)
            self.assertIs(decoded, result)
            self.assertEqual([], events)
            self.rejected(lambda: self.admission(context, decoded), "SOURCE_AUTHORIZATION_REQUIRED")

    def test_authorize_fork_wrong_context_digest_is_stable_pure_mismatch(self):
        from wechat_exporter.fork_authorization import authorize_fork
        with operation_fixture() as f:
            context, approval = prepare(f)
            wrong = replace(approval, root_context_digest="9" * 64)
            self.rejected(lambda: authorize_fork(wrong, wrong.input_baseline, f.selection,
                context=context), "OPERATION_CONTEXT_MISMATCH")

    def test_receipt_old_wire_or_missing_context_field_cannot_decode(self):
        with synthetic_roots() as (_, roots, legacy, source):
            proof = physical.prove_roots(roots, legacy_proof(legacy,
                physical.prove_existing_root(legacy).identity), (source,))
            snapshots = dict(proof.snapshots)
            receipt = dto.ForkReceipt("synthetic-operation", "synthetic-archive", "synthetic-object",
                SCOPE, "SYNTHETIC", proof, snapshots["input_v2"], snapshots["candidate_v3"],
                snapshots["safety_backup"], "1" * 64, "2" * 64)
            for old in (True, False):
                document = dto.dto_document(receipt)
                if old:
                    document["contract_version"] = "track2-v1"
                else:
                    document["fields"].pop("root_context_digest")
                with self.assertRaises(ValueError):
                    dto.decode_dto(dto.ForkReceipt, document)

    def test_duplicate_read_footprints_refuse_before_handles(self):
        with operation_fixture() as f:
            reads = f.arguments["read_footprints"]
            with patch.object(physical, "_open", wraps=physical._open) as observed:
                self.rejected(lambda: prepare(f, read_footprints=reads + (reads[0],)), "OPERATION_CONTEXT_MISMATCH")
                self.assertEqual([], observed.call_args_list)

    def test_cyclic_context_has_stable_private_error_before_reads(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            malformed = copy.copy(context)
            object.__setattr__(malformed, "read_footprints", (malformed,))
            self.rejected(lambda: self.admission(malformed, approval), "OPERATION_CONTEXT_MISMATCH")

    def test_real_source_directory_and_configuration_file_symlinks_refuse(self):
        with operation_fixture() as f:
            context, approval = prepare(f)
            old = f.source.with_name("source-real-original")
            f.source.rename(old)
            symlink(f.source, old, directory=True)
            self.rejected(lambda: self.admission(context, approval), "ROOT_PROOF_FAILED")

    def test_existing_writable_role_cannot_contain_or_enter_protected_archive(self):
        for role in ("candidate_v3", "staging"):
            with self.subTest(role=role), operation_fixture() as f:
                writable = getattr(f.selection, role)
                writable.mkdir()
                archive = writable / "other-preserved-archive"
                archive.mkdir()
                (archive / "ordinary.bin").write_bytes(b"preserved-synthetic")
                reads = f.arguments["read_footprints"] + tuple(dto.ReadFootprint(writable, access,
                    True, None) for access in ("ENUMERATE", "CONTENT"))
                reads += (dto.ReadFootprint(archive, "METADATA", False, None),)
                self.rejected(lambda: prepare(f, read_footprints=reads,
                    protected_roots=(f.legacy, archive)), "ROOT_PROOF_FAILED")

    def test_missing_writable_target_inside_other_v3_namespace_refuses(self):
        for role in ("candidate_v3", "reader_work"):
            with self.subTest(role=role), operation_fixture() as f:
                selected = f.selection
                other = f.base / "other-owned-namespace"
                f.selection = replace(selected, namespace=other)
                existing_namespace(f)
                f.selection = replace(selected, **{role: other / "operations" / "unapproved-new-root"})
                configs = list(f.arguments["configuration_objects"])
                for kind, name in (("OTHER_V3_SETTINGS", "settings.json"), ("OTHER_V3_REGISTRY", "registry.json")):
                    path = other / name
                    configs.append(dto.ConfigurationSelection(kind, path,
                        hashlib.sha256(path.read_bytes()).hexdigest()))
                f.arguments["configuration_objects"] = tuple(configs)
                f.arguments["write_footprints"] = tuple(replace(w, path=getattr(f.selection, role))
                    if w.role == role else w for w in f.arguments["write_footprints"])
                reads = list(f.arguments["read_footprints"])
                reads.append(dto.ReadFootprint(other, "CONTENT", True, None))
                for path in (getattr(f.selection, role), *getattr(f.selection, role).parents):
                    if not any(r.path == path and r.access == "METADATA" for r in reads):
                        reads.append(dto.ReadFootprint(path, "METADATA", False, None))
                f.arguments["read_footprints"] = tuple(reads)
                self.rejected(lambda: prepare(f, protected_roots=(f.legacy, other)), "ROOT_PROOF_FAILED")

    def test_external_nonancestor_metadata_and_recursive_metadata_refuse(self):
        with operation_fixture() as f:
            outside = f.base.parent / "unapproved-nonancestor-metadata"
            reads = f.arguments["read_footprints"] + (dto.ReadFootprint(outside, "METADATA", False, None),)
            self.rejected(lambda: prepare(f, read_footprints=reads), "SOURCE_AUTHORIZATION_REQUIRED")
        with operation_fixture() as f:
            reads = f.arguments["read_footprints"] + (dto.ReadFootprint(f.base / "broad-unrequested-tree",
                "METADATA", True, None),)
            self.rejected(lambda: prepare(f, read_footprints=reads), "OPERATION_CONTEXT_MISMATCH")
        with operation_fixture() as f:
            existing_configurations(f)
            path = f.arguments["configuration_objects"][0].path
            old = path.with_name("real-original-settings.json")
            path.rename(old)
            symlink(path, old, directory=False)
            self.rejected(lambda: prepare(f), "ROOT_PROOF_FAILED")

    def test_actual_destination_parent_identity_replacement_refuses(self):
        with operation_fixture() as f:
            parent = f.base / "owned-output-parent"
            parent.mkdir()
            f.selection = replace(f.selection, reader_work=parent / "reader")
            f.arguments["write_footprints"] = tuple(replace(w, path=f.selection.reader_work)
                if w.role == "reader_work" else w for w in f.arguments["write_footprints"])
            f.arguments["read_footprints"] += (dto.ReadFootprint(parent, "METADATA", False, None),
                dto.ReadFootprint(f.selection.reader_work, "METADATA", False, None))
            context, approval = prepare(f)
            parent.rename(parent.with_name("retained-old-parent"))
            parent.mkdir()
            self.rejected(lambda: self.admission(context, approval), "ROOT_PROOF_FAILED")
