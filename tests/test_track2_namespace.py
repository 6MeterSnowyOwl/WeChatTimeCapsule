"""Behavior tests: explicit synthetic namespaces, genuine leases, frozen startup."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from wechat_exporter.physical_paths import prove_existing_root, snapshot_tree
from wechat_exporter.track2_contracts import (AuthorityBinding, CandidateRecord, LegacyNamespaceProof,
    LockLease, Track2Error, canonical_bytes, contract_digest, dto_document)
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.v3_profile import open_v3_profile, read_legacy_namespace
from wechat_exporter.namespace_lock import authority_lock, require_live_lease
from wechat_exporter.v3_registry import (read_binding, register_candidate, read_candidates,
    register_preserved_v2, _binding_matches)
from track2_fixtures import junction

PROJECT = Path(__file__).resolve().parents[1]
PROBE = PROJECT / "tests" / "_track2_namespace_probe.py"
SCOPE = ArchiveScope("synthetic-account", "synthetic-peer")


class NamespaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-track2-namespace-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "old"
        self.old.mkdir()
        self.settings = self.old / "settings.json"
        self.registry = self.old / "archive_registry.json"
        self.settings.write_bytes(b'{"known_archive_roots":[]}')
        self.registry.write_bytes(b'{"archives":[],"bootstrap_completed":false}')
        self.legacy = LegacyNamespaceProof((self.old,), (),
            hashlib.sha256(self.settings.read_bytes()).hexdigest(),
            hashlib.sha256(self.registry.read_bytes()).hexdigest(),
            ((self.old, prove_existing_root(self.old).identity),))
        self.root = self.base / "WeChatTimeCapsule-v3"
        self.root.mkdir()
        self.candidate = self.base / "candidate"
        self.candidate.mkdir()
        self.before = snapshot_tree(self.old)

    def profile(self):
        return open_v3_profile(self.root, "SYNTHETIC", self.legacy)

    def rejects(self, callback, code="TRACK2_RECOVERY_REQUIRED"):
        with self.assertRaises(Track2Error) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def record(self, state="BUILDING"):
        return CandidateRecord("candidate-1", "object-1", SCOPE, "SYNTHETIC",
            prove_existing_root(self.candidate).identity, state, None, None, "build-1")

    def test_explicit_profile_creates_only_v3_files_and_retains_exact_legacy_proof(self):
        profile = self.profile()
        self.assertTrue((self.root / "settings.json").is_file(), "v3 settings must exist")
        self.assertTrue((self.root / "registry.json").is_file(), "v3 registry must exist")
        self.assertEqual(self.legacy, read_legacy_namespace(profile))
        self.assertEqual(self.before, snapshot_tree(self.old))
        self.assertIsNone(read_binding(profile, "object-1"))
        self.assertEqual(profile, self.profile())

    def test_new_destination_is_parent_proved_before_creation(self):
        absent = self.base / "selected" / "WeChatTimeCapsule-v3"
        profile = open_v3_profile(absent, "SYNTHETIC", self.legacy)
        self.assertTrue((absent / "settings.json").is_file())
        self.assertEqual(absent, profile.namespace_root)
        self.assertEqual(self.before, snapshot_tree(self.old))

    def test_legacy_overlap_refused_without_a_new_file(self):
        for root in (self.old, self.old / "nested", self.base):
            with self.subTest(kind="existing" if root.exists() else "absent"):
                self.rejects(lambda: open_v3_profile(root, "SYNTHETIC", self.legacy),
                             "LEGACY_NAMESPACE_OVERLAP")
        self.assertEqual(self.before, snapshot_tree(self.old))

    def test_malformed_legacy_proof_fails_before_namespace_mutation(self):
        bad = replace(self.legacy, physical_root_ids=())
        self.rejects(lambda: open_v3_profile(self.root, "SYNTHETIC", bad), "ROOT_PROOF_FAILED")
        self.assertEqual([], list(self.root.iterdir()))

    def test_corrupt_old_metadata_is_never_repaired_or_used_as_fallback(self):
        self.registry.write_bytes(b"synthetic-corrupt-old")
        self.settings.write_bytes(b"synthetic-corrupt-old-settings")
        before = snapshot_tree(self.old)
        profile = self.profile()
        self.assertTrue((self.root / "settings.json").is_file())
        self.assertEqual(before, snapshot_tree(self.old))
        self.assertEqual((), read_candidates(profile, "object-1"))

    def test_corrupt_or_missing_v3_persistence_is_not_repaired(self):
        for name in ("settings.json", "registry.json"):
            with self.subTest(leaf=name):
                with tempfile.TemporaryDirectory(prefix="synthetic-v3-corruption-") as tmp:
                    root = Path(tmp)
                    profile = open_v3_profile(root, "SYNTHETIC", self.legacy)
                    self.assertTrue((root / name).is_file())
                    (root / name).write_bytes(b"bad")
                    old = snapshot_tree(root)
                    self.rejects(lambda: open_v3_profile(root, "SYNTHETIC", self.legacy))
                    self.assertEqual(old, snapshot_tree(root))
                    (root / name).unlink()
                    self.rejects(lambda: read_binding(profile, "object-1"))
                    self.assertFalse((root / name).exists())

    def test_profile_purpose_or_legacy_proof_change_fails_closed(self):
        self.profile()
        self.rejects(lambda: open_v3_profile(self.root, "FORMAL", self.legacy))
        self.rejects(lambda: open_v3_profile(self.root, "SYNTHETIC",
            replace(self.legacy, settings_digest="f" * 64)))

    def test_hardlinked_profile_file_refused_before_read_or_mutation(self):
        self.profile()
        self.assertTrue((self.root / "settings.json").is_file())
        os.link(self.root / "settings.json", self.base / "shared-settings")
        self.rejects(lambda: read_binding(self.profile(), "object-1"), "ROOT_PROOF_FAILED")

    def test_candidate_registration_never_creates_active_binding(self):
        profile = self.profile()
        record = self.record()
        with authority_lock(profile, "register") as lease:
            register_candidate(profile, record, self.candidate, lease)
            register_candidate(profile, record, self.candidate, lease)
        self.assertEqual((record,), read_candidates(profile, "object-1"))
        self.assertIsNone(read_binding(profile, "object-1"))
        self.assertFalse((self.root / "objects" / "object-1" / "binding.json").exists())
        self.assertEqual(self.before, snapshot_tree(self.old))

    def test_candidate_active_or_legacy_domain_root_refused(self):
        profile = self.profile()
        with authority_lock(profile, "register") as lease:
            self.rejects(lambda: register_candidate(profile, self.record("ACTIVE"), self.candidate, lease),
                         "BINDING_CONFLICT")
            self.rejects(lambda: register_candidate(profile,
                replace(self.record(), root_id=prove_existing_root(self.old).identity), self.old, lease),
                "LEGACY_NAMESPACE_OVERLAP")
        self.assertEqual((), read_candidates(profile, "object-1"))

    def test_preserved_v2_reference_is_readonly_and_scope_bound(self):
        profile = self.profile()
        binding = AuthorityBinding("object-1", SCOPE, 0, "PRESERVED_V2_READONLY", "v2-1",
            prove_existing_root(self.old).identity, None, None, None)
        with authority_lock(profile, "preserve") as lease:
            register_preserved_v2(profile, binding, self.old, lease)
            self.rejects(lambda: register_preserved_v2(profile,
                replace(binding, kind="ACTIVE_V3"), self.old, lease), "BINDING_CONFLICT")
            self.rejects(lambda: register_candidate(profile,
                replace(self.record(), scope=ArchiveScope("other-account", "synthetic-peer")),
                self.candidate, lease), "BINDING_CONFLICT")
        self.assertEqual(binding, read_binding(profile, "object-1"))
        self.assertTrue(_binding_matches(profile, "object-1", contract_digest(binding), 0))
        self.assertFalse(_binding_matches(profile, "object-1", "f" * 64, 0))
        self.assertFalse(_binding_matches(profile, "object-1", contract_digest(binding), 1))
        self.assertEqual(self.before, snapshot_tree(self.old))

    def test_forged_expired_and_cross_thread_leases_cannot_mutate(self):
        profile = self.profile()
        with authority_lock(profile, "genuine") as lease:
            require_live_lease(profile, lease)
            self.rejects(lambda: register_candidate(profile, self.record(), self.candidate,
                LockLease(lease.namespace_id, lease.owner_operation_id)), "WRITER_BUSY")
            seen = []
            def other_thread():
                try:
                    require_live_lease(profile, lease)
                except Track2Error as error:
                    seen.append(error.code)
            worker = threading.Thread(target=other_thread)
            worker.start(); worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(["WRITER_BUSY"], seen)
        self.rejects(lambda: register_candidate(profile, self.record(), self.candidate, lease), "WRITER_BUSY")

    def test_invalid_timeout_and_operation_id_do_not_create_a_lock(self):
        profile = self.profile()
        before = snapshot_tree(self.root)
        absent = self.base / "not-created"
        # These real refusal paths distinguish timeout validation from _settings
        # type validation and physical root access, without replacing either.
        profiles = (("valid", profile), ("invalid-profile", None),
                    ("absent-root", replace(profile, namespace_root=absent)))
        timeouts = (("negative", -1), ("over-bound", 30.1), ("bool", True),
                    ("nan", float("nan")), ("infinity", float("inf")),
                    ("negative-infinity", float("-inf")), ("text", "30"),
                    ("none", None), ("huge-positive-int", 10**1000),
                    ("huge-negative-int", -(10**1000)))
        for label, timeout in timeouts:
            for profile_label, selected in profiles:
                with self.subTest(timeout_case=label, profile_case=profile_label):
                    code = None
                    try:
                        self._enter(selected, "valid", timeout)
                    except Exception as error:
                        code = getattr(error, "code", None)
                    self.assertEqual("WRITER_BUSY", code)
                    self.assertFalse((self.root / "authority.lock").exists())
                    self.assertFalse(absent.exists())
        self.rejects(lambda: self._enter(profile, "../escape", 0), "WRITER_BUSY")
        self.assertFalse((self.root / "authority.lock").exists())
        self.assertEqual(before, snapshot_tree(self.root))

    def _enter(self, profile, operation, timeout):
        with authority_lock(profile, operation, timeout):
            pass

    def test_real_cross_process_timeout_and_process_death_release_same_lock_file(self):
        profile = self.profile()
        child = subprocess.Popen([sys.executable, "-I", "-B", "-S", str(PROBE), "lock",
            str(PROJECT), str(self.root), canonical_bytes(self.legacy).decode("utf8")],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf8")
        reader = None
        try:
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
            reader.start()
            try:
                line = lines.get(timeout=5)
            except queue.Empty:
                self.fail("bounded child lease handshake timed out")
            self.assertTrue(line.strip(), "child must acquire lease before contention")
            self.assertTrue(json.loads(line)["lease_acquired"])
            start = time.monotonic()
            self.rejects(lambda: self._enter(profile, "contender", .15), "WRITER_BUSY")
            self.assertLess(time.monotonic() - start, 3)
            self.assertTrue((self.root / "authority.lock").exists())
            before = (self.root / "authority.lock").stat().st_ino
            child.kill(); child.wait(5)
            self.assertIsNotNone(child.returncode)
            with authority_lock(profile, "after-death", timeout_seconds=2) as lease:
                require_live_lease(profile, lease)
            self.assertEqual(before, (self.root / "authority.lock").stat().st_ino)
            self.assertFalse((self.old / "authority.lock").exists())
            print("SYNTHETIC_OS_LOCK=" + json.dumps({"contention": "WRITER_BUSY",
                "child_reaped": True, "released_after_death": True, "same_lock_file": True}))
        finally:
            if child.poll() is None:
                child.kill(); child.wait(5)
            if reader is not None:
                reader.join(3)
                self.assertFalse(reader.is_alive(), "child handshake reader must be reclaimed")
            child.stdout.close(); child.stderr.close()

    def test_corrupt_binding_and_unsafe_object_identifier_fail_closed(self):
        profile = self.profile()
        self.rejects(lambda: read_binding(profile, "../escape"))
        location = self.root / "objects" / "object-1"
        location.mkdir(parents=True)
        (location / "binding.json").write_bytes(b"bad")
        before = snapshot_tree(self.root)
        self.rejects(lambda: read_binding(profile, "object-1"))
        self.assertEqual(before, snapshot_tree(self.root))

    def test_actual_frozen_normal_bootstrap_does_not_access_v3(self):
        frozen = PROJECT / "tests" / "fixtures" / "legacy_rc6" / "wechat_exporter"
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen.glob("*.py")}
        result = subprocess.run([sys.executable, "-I", "-B", "-S", str(PROBE), "bootstrap", str(PROJECT)],
            cwd=PROJECT, capture_output=True, text=True, encoding="utf8", timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual((True, 3, 0, 0), (facts["bootstrap"], facts["legacy_targets"],
            facts["v3_accesses"], facts["provider_calls"]))
        self.assertTrue(facts["new_profile_initialized"])
        self.assertTrue(facts["candidate_registered"])
        self.assertTrue(facts["new_profile_oldbytes_preserved"])
        self.assertTrue(facts["frozen_manifest_verified"])
        self.assertEqual(before, facts["frozen_source_digests"])
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen.glob("*.py")}
        self.assertEqual(before, after)
        print("SYNTHETIC_FROZEN_BOOTSTRAP=" + json.dumps(facts, sort_keys=True))

    def test_missing_profile_lock_returns_stable_error_without_creating_lock(self):
        profile = self.profile()
        (self.root / "settings.json").unlink()
        code = None
        try:
            self._enter(profile, "missing", 0)
        except Exception as error:
            code = getattr(error, "code", None)
        self.assertEqual("TRACK2_RECOVERY_REQUIRED", code)
        self.assertFalse((self.root / "authority.lock").exists())

    def test_real_junction_namespace_is_refused_before_mutation(self):
        link = self.base / "junction"
        junction(link, self.root)
        self.rejects(lambda: open_v3_profile(link, "SYNTHETIC", self.legacy), "ROOT_PROOF_FAILED")
        self.assertEqual([], list(self.root.iterdir()))

    def test_profile_root_replacement_and_duplicate_wire_keys_are_refused(self):
        profile = self.profile()
        document = (self.root / "settings.json").read_text("utf8")
        (self.root / "settings.json").write_text(document.replace("{", '{"schema_version":3,', 1), "utf8")
        self.rejects(lambda: read_binding(profile, "object-1"))
        (self.root / "settings.json").write_text(document, "utf8")
        moved = self.base / "preserved-namespace"
        self.root.rename(moved)
        self.root.mkdir()
        import shutil
        shutil.copytree(moved, self.root, dirs_exist_ok=True)
        self.rejects(lambda: self._enter(profile, "replaced", 0), "ROOT_PROOF_FAILED")
        self.assertFalse((self.root / "authority.lock").exists())

    def test_exception_releases_lease_and_candidate_wrong_id_is_rejected(self):
        profile = self.profile()
        with self.assertRaisesRegex(RuntimeError, "synthetic-interruption"):
            with authority_lock(profile, "failure") as expired:
                raise RuntimeError("synthetic-interruption")
        with authority_lock(profile, "after-failure", 0) as lease:
            self.rejects(lambda: require_live_lease(profile, expired), "WRITER_BUSY")
            self.rejects(lambda: register_candidate(profile,
                replace(self.record(), root_id=prove_existing_root(self.old).identity),
                self.candidate, lease), "ROOT_PROOF_FAILED")
        self.assertEqual((), read_candidates(profile, "object-1"))

    def test_candidate_hardlinked_descendant_cannot_enter_index(self):
        profile = self.profile()
        payload = self.candidate / "synthetic.bin"
        payload.write_bytes(b"synthetic")
        os.link(payload, self.base / "shared-payload")
        with authority_lock(profile, "unsafe-candidate") as lease:
            self.rejects(lambda: register_candidate(profile, self.record(), self.candidate, lease),
                         "ROOT_PROOF_FAILED")
        self.assertEqual((), read_candidates(profile, "object-1"))

    def test_unknown_namespace_leaf_is_not_adopted_as_owned_persistence(self):
        profile = self.profile()
        (self.root / "unowned.json").write_bytes(b"synthetic")
        before = snapshot_tree(self.root)
        self.rejects(lambda: read_binding(profile, "object-1"))
        self.assertEqual(before, snapshot_tree(self.root))

    def test_case_alias_and_shared_candidate_root_cannot_create_second_index_identity(self):
        profile = self.profile()
        record = self.record()
        other = self.base / "other-candidate"
        other.mkdir()
        with authority_lock(profile, "index-collision") as lease:
            register_candidate(profile, record, self.candidate, lease)
            before = (self.root / "registry.json").read_bytes()
            self.rejects(lambda: register_candidate(profile,
                replace(record, archive_id="candidate-2", object_id="OBJECT-1",
                        root_id=prove_existing_root(other).identity), other, lease), "BINDING_CONFLICT")
            self.rejects(lambda: register_candidate(profile,
                replace(record, archive_id="candidate-2"), self.candidate, lease), "BINDING_CONFLICT")
            self.assertEqual(before, (self.root / "registry.json").read_bytes())

    def test_restart_ignores_uncommitted_registry_leaf_without_deleting_evidence(self):
        profile = self.profile()
        before = (self.root / "registry.json").read_bytes()
        pending = self.root / ("registry.json.pending-" + "f" * 32)
        pending.write_bytes(b"synthetic-uncommitted-and-invalid")
        self.assertEqual((), read_candidates(self.profile(), "object-1"))
        self.assertIsNone(read_binding(profile, "object-1"))
        self.assertEqual(before, (self.root / "registry.json").read_bytes())
        self.assertEqual(b"synthetic-uncommitted-and-invalid", pending.read_bytes())

    def test_candidate_parent_child_overlap_is_rejected_in_both_registration_orders(self):
        for order in ("parent-first", "child-first"):
            with self.subTest(order=order):
                case = self.base / order
                case.mkdir()
                namespace = case / "namespace"
                namespace.mkdir()
                parent = case / "parent"
                child = parent / "child"
                child.mkdir(parents=True)
                profile = open_v3_profile(namespace, "SYNTHETIC", self.legacy)
                parent_record = replace(self.record(), archive_id="candidate-parent", object_id="object-parent",
                    root_id=prove_existing_root(parent).identity)
                child_record = replace(self.record(), archive_id="candidate-child", object_id="object-child",
                    root_id=prove_existing_root(child).identity)
                first, second = ((parent_record, parent), (child_record, child))
                if order == "child-first":
                    first, second = second, first
                with authority_lock(profile, "index-overlap") as lease:
                    register_candidate(profile, *first, lease)
                    before = (namespace / "registry.json").read_bytes()
                    self.rejects(lambda: register_candidate(profile, *second, lease), "BINDING_CONFLICT")
                    self.assertEqual(before, (namespace / "registry.json").read_bytes())
                    register_candidate(profile, *first, lease)  # Exact repeat remains idempotent.
                    self.assertEqual(before, (namespace / "registry.json").read_bytes())

    def test_preserved_v2_candidate_relationships_are_disjoint_in_both_registration_orders(self):
        for relation in ("same-root", "candidate-child", "preserved-child"):
            for order in ("candidate-first", "preserved-first"):
                with self.subTest(relation=relation, order=order):
                    case = self.base / (relation + "-" + order)
                    case.mkdir()
                    namespace = case / "namespace"
                    namespace.mkdir()
                    outer = case / "payload"
                    outer.mkdir()
                    inner = outer / "child"
                    inner.mkdir()
                    candidate_root = inner if relation == "candidate-child" else outer
                    preserved_root = inner if relation == "preserved-child" else outer
                    profile = open_v3_profile(namespace, "SYNTHETIC", self.legacy)
                    candidate = replace(self.record(), root_id=prove_existing_root(candidate_root).identity)
                    preserved = AuthorityBinding("object-1", SCOPE, 0, "PRESERVED_V2_READONLY", "preserved-1",
                        prove_existing_root(preserved_root).identity, None, None, None)
                    baseline = snapshot_tree(outer)
                    with authority_lock(profile, "role-overlap") as lease:
                        if order == "candidate-first":
                            register_candidate(profile, candidate, candidate_root, lease)
                            before = (namespace / "registry.json").read_bytes()
                            self.rejects(lambda: register_preserved_v2(profile, preserved, preserved_root, lease),
                                         "BINDING_CONFLICT")
                            register_candidate(profile, candidate, candidate_root, lease)
                        else:
                            register_preserved_v2(profile, preserved, preserved_root, lease)
                            before = (namespace / "registry.json").read_bytes()
                            self.rejects(lambda: register_candidate(profile, candidate, candidate_root, lease),
                                         "BINDING_CONFLICT")
                            register_preserved_v2(profile, preserved, preserved_root, lease)
                        self.assertEqual(before, (namespace / "registry.json").read_bytes())
                    self.assertEqual(baseline, snapshot_tree(outer))

    def test_replaced_indexed_candidate_or_preserved_root_fails_closed_before_mutation(self):
        for role in ("candidate", "preserved"):
            with self.subTest(role=role):
                case = self.base / ("stale-" + role)
                case.mkdir()
                namespace = case / "namespace"
                namespace.mkdir()
                indexed = case / "indexed"
                indexed.mkdir()
                other = case / "other"
                other.mkdir()
                profile = open_v3_profile(namespace, "SYNTHETIC", self.legacy)
                with authority_lock(profile, "stale-index") as lease:
                    if role == "candidate":
                        record = replace(self.record(), root_id=prove_existing_root(indexed).identity)
                        register_candidate(profile, record, indexed, lease)
                    else:
                        record = AuthorityBinding("object-1", SCOPE, 0, "PRESERVED_V2_READONLY", "preserved-1",
                            prove_existing_root(indexed).identity, None, None, None)
                        register_preserved_v2(profile, record, indexed, lease)
                    indexed.rename(case / "preserved-indexed-root")
                    indexed.mkdir()
                    fresh = replace(self.record(), archive_id="candidate-2", object_id="object-2",
                                    root_id=prove_existing_root(other).identity)
                    before = (namespace / "registry.json").read_bytes()
                    self.rejects(lambda: register_candidate(profile, fresh, other, lease), "ROOT_PROOF_FAILED")
                    self.rejects(lambda: read_candidates(profile, "object-2"), "ROOT_PROOF_FAILED")
                    self.assertEqual(before, (namespace / "registry.json").read_bytes())
