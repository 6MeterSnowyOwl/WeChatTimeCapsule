"""Synthetic filesystem tests: no archive, DB, settings, or product writer.

Each test names the safety break it detects. Subprocess faults replace only the
OS mutation primitive to exit AFTER its real effect, preserving real I/O.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from wechat_exporter import capsule_transaction as tx
from wechat_exporter.history_contracts import HistoryError


META = ".synthetic-transaction"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def accept(root):
    return None


class SyntheticTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-t7-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "target"
        self.stage = self.base / "stage"
        self.root.mkdir()
        self.stage.mkdir()
        self.names = ("a.jsonl", "b.json", "new.txt")
        self.old = {"a.jsonl": b"old-a", "b.json": b"old-b"}
        self.new = {"a.jsonl": b"", "b.json": b"new-b", "new.txt": b"new"}
        for name, data in self.old.items():
            (self.root / name).write_bytes(data)
        for name, data in self.new.items():
            (self.stage / name).write_bytes(data)
        self.hashes = {name: sha(data) for name, data in self.new.items()}

    def prepare(self, verifier=accept):
        result = tx.prepare_synthetic_transaction(
            self.root, self.stage, self.names, self.hashes,
            verify_postimage=verifier)
        self.assertIsNotNone(result, "prepare must produce durable intent")
        return result

    def journal(self):
        return json.loads((self.root / META / "journal.json").read_text())

    def save_journal(self, value):
        (self.root / META / "journal.json").write_text(json.dumps(value))

    def assert_old(self):
        for name, data in self.old.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse((self.root / "new.txt").exists())

    def assert_new(self):
        for name, data in self.new.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def error(self, operation):
        with self.assertRaises(HistoryError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "TRANSACTION_RECOVERY_REQUIRED")
        self.assertEqual(str(caught.exception), "TRANSACTION_RECOVERY_REQUIRED")

    def test_prepare_records_complete_independent_preimages_before_install(self):
        prepared = self.prepare()
        j = self.journal()
        self.assertEqual(j["state"], "PREPARED")
        self.assertEqual([e["path"] for e in j["files"]], list(self.names))
        for index, name in enumerate(self.names):
            entry = j["files"][index]
            self.assertIs(entry["existed"], name in self.old)
            self.assertEqual(entry["pre_hash"], sha(self.old[name]) if name in self.old else None)
            if name in self.old:
                backup = self.root / META / f"backup-{index}"
                self.assertEqual(backup.read_bytes(), self.old[name])
                self.assertNotEqual(backup.stat().st_ino, (self.root / name).stat().st_ino)
        self.assertEqual(prepared.root, self.root)
        self.assert_old()

    def test_empty_jsonl_commits_only_after_hashes_and_semantics(self):
        states = []
        def verify(root):
            self.assertEqual(root, self.root)
            self.assert_new()
            states.append(self.journal()["state"])
        prepared = self.prepare(verify)
        tx.commit_synthetic_transaction(prepared, before_install=lambda: None)
        self.assertEqual(states, ["PREPARED"])
        self.assertEqual(self.journal()["state"], "COMMITTED")
        self.assert_new()
        self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=verify))
        self.assertEqual(states, ["PREPARED", "COMMITTED"])
        self.assertFalse((self.root / META).exists())
        self.assertFalse(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))

    def test_before_install_failure_installs_nothing_and_preserves_staging(self):
        prepared = self.prepare()
        stage_before = snapshot(self.stage)
        def fail():
            raise RuntimeError("synthetic-only-detail")
        self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=fail))
        self.assert_old()
        self.assertEqual(snapshot(self.stage), stage_before)
        self.assertFalse((self.root / META).exists())

    def test_partial_install_failure_rolls_back_all_managed_existence(self):
        prepared = self.prepare()
        real = os.replace
        def fault(src, dst):
            real(src, dst)
            if Path(dst) == self.root / "new.txt":
                raise OSError("synthetic fault after all managed replacements")
        with patch.object(tx.os, "replace", side_effect=fault):
            self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=lambda: None))
        self.assertEqual(self.journal()["state"], "PREPARED")
        self.assert_new()
        self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assert_old()
        self.assertFalse(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))

    def test_semantic_failure_never_commits_and_is_recoverable(self):
        def fail(root):
            raise ValueError("synthetic semantic rejection")
        prepared = self.prepare(fail)
        self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=lambda: None))
        self.assertEqual(self.journal()["state"], "PREPARED")
        self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assert_old()

    def test_postimage_hash_tampering_never_reaches_verifier_or_commit(self):
        seen = []
        prepared = self.prepare(lambda root: seen.append(True))
        real = os.replace
        def tamper(src, dst):
            real(src, dst)
            if Path(dst) == self.root / "new.txt":
                (self.root / "a.jsonl").write_bytes(b"damaged")
        with patch.object(tx.os, "replace", side_effect=tamper):
            self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=lambda: None))
        self.assertEqual(seen, [])
        self.assertEqual(self.journal()["state"], "PREPARED")

    def test_corrupt_or_missing_later_backup_prevents_any_restore(self):
        for action in ("missing", "corrupt"):
            with self.subTest(action=action):
                if (self.root / META).exists():
                    # Restore known synthetic evidence between independent cases.
                    (self.root / META / "backup-1").write_bytes(b"old-b")
                    tx.recover_synthetic_transaction(self.root, verify_postimage=accept)
                self.prepare()
                for name, data in self.new.items():
                    (self.root / name).write_bytes(data)
                backup = self.root / META / "backup-1"
                backup.unlink() if action == "missing" else backup.write_bytes(b"corrupt")
                before = snapshot(self.root)
                self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
                self.assertEqual(snapshot(self.root), before)

    def test_interrupted_recovery_keeps_all_backups_for_second_recovery(self):
        self.prepare()
        for name, data in self.new.items():
            (self.root / name).write_bytes(data)
        real = os.replace
        def fault(src, dst):
            real(src, dst)
            if Path(dst) == self.root / "a.jsonl":
                raise OSError("interrupt recovery after first restore")
        with patch.object(tx.os, "replace", side_effect=fault):
            self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual((self.root / META / "backup-0").read_bytes(), b"old-a")
        self.assertEqual((self.root / META / "backup-1").read_bytes(), b"old-b")
        self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assert_old()

    def test_unknown_malformed_or_missing_journal_preserves_evidence(self):
        self.prepare()
        journal = self.root / META / "journal.json"
        original = journal.read_bytes()
        for value in (b"{}", b"not-json", original.replace(b"PREPARED", b"UNKNOWN"), None):
            with self.subTest(kind="missing" if value is None else "malformed"):
                if value is None:
                    journal.unlink()
                else:
                    journal.write_bytes(value)
                before = snapshot(self.root)
                self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
                self.assertEqual(snapshot(self.root), before)
                journal.write_bytes(original)

    def test_journal_path_hash_existence_and_duplicate_corruption_fail_closed(self):
        self.prepare()
        original = self.journal()
        for mutation in ("path", "hash", "existed", "duplicate", "extra"):
            j = json.loads(json.dumps(original))
            if mutation == "path": j["files"][0]["path"] = "../escape"
            if mutation == "hash": j["files"][0]["pre_hash"] = "bad"
            if mutation == "existed": j["files"][0]["existed"] = 1
            if mutation == "duplicate": j["files"][1]["path"] = "A.JSONL"
            if mutation == "extra": j["unknown"] = True
            self.save_journal(j)
            before = snapshot(self.root)
            self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
            self.assertEqual(snapshot(self.root), before)

    def test_committed_recovery_rechecks_hashes_and_semantics_before_cleanup(self):
        prepared = self.prepare()
        tx.commit_synthetic_transaction(prepared, before_install=lambda: None)
        (self.root / "b.json").write_bytes(b"damaged")
        before = snapshot(self.root)
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual(snapshot(self.root), before)
        (self.root / "b.json").write_bytes(b"new-b")
        def fail(root): raise RuntimeError("synthetic verifier failure")
        before = snapshot(self.root)
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=fail))
        self.assertEqual(snapshot(self.root), before)

    def test_no_root_creation_no_nested_or_shared_roots(self):
        absent = self.base / "absent"
        for target, staging in ((absent, self.stage), (self.root, absent),
                                (self.root, self.root), (self.base, self.stage)):
            self.error(lambda: tx.prepare_synthetic_transaction(
                target, staging, self.names, self.hashes, verify_postimage=accept))
        self.assertFalse(absent.exists())
        self.assert_old()

    def test_windows_alias_case_ancestor_and_metadata_paths_rejected_before_writes(self):
        invalid = [("../x",), ("/x",), ("C:/x",), ("a\\b",), ("a//b",),
                   ("a/./b",), ("a:b",), ("name.",), ("name ",), ("CON",),
                   ("nul.txt",), ("COM1.txt",), ("LPT9",), ("x~1",),
                   ("A", "a"), ("a", "a/b"), (META,), (META.upper()+"/x",)]
        before = snapshot(self.root)
        for names in invalid:
            with self.subTest(case=names):
                self.error(lambda: tx.prepare_synthetic_transaction(
                    self.root, self.stage, names, {n: sha(b"") for n in names},
                    verify_postimage=accept))
                self.assertEqual(snapshot(self.root), before)

    def test_staged_hash_mismatch_and_wrong_existing_case_rejected(self):
        for names, hashes in ((self.names, {**self.hashes, "b.json": sha(b"bad")}),
                              (("A.JSONL",), {"A.JSONL": sha(b"")})):
            self.error(lambda: tx.prepare_synthetic_transaction(
                self.root, self.stage, names, hashes, verify_postimage=accept))
        self.assertFalse((self.root / META).exists())

    def test_prepared_does_not_overwrite_orphan_or_other_operation(self):
        meta = self.root / META
        meta.mkdir()
        (meta / "orphan").write_bytes(b"evidence")
        before = snapshot(self.root)
        self.error(lambda: tx.prepare_synthetic_transaction(
            self.root, self.stage, self.names, self.hashes, verify_postimage=accept))
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual(snapshot(self.root), before)

    def test_reparse_and_hardlink_files_rejected_without_following(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"outside")
        link = self.stage / "linked"
        if os.name == "nt":
            # A real directory junction needs no symlink privilege; scoped temp paths only.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, "synthetic junction creation failed")
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.addCleanup(lambda: link.rmdir() if os.name == "nt" and link.exists()
                        else link.unlink(missing_ok=True))
        self.error(lambda: tx.prepare_synthetic_transaction(
            self.root, self.stage, ("linked/file",), {"linked/file": sha(b"outside")},
            verify_postimage=accept))
        os.link(outside / "file", self.stage / "hard")
        self.error(lambda: tx.prepare_synthetic_transaction(
            self.root, self.stage, ("hard",), {"hard": sha(b"outside")},
            verify_postimage=accept))
        self.assertEqual((outside / "file").read_bytes(), b"outside")

    def test_backup_hardlink_tampering_fails_closed(self):
        self.prepare()
        outside = self.base / "outside-file"
        outside.write_bytes(b"old-b")
        backup = self.root / META / "backup-1"
        backup.unlink()
        os.link(outside, backup)
        before = snapshot(self.root)
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual(snapshot(self.root), before)

    def test_before_install_backup_tampering_cannot_start_install(self):
        prepared = self.prepare()
        def tamper():
            (self.root / META / "backup-1").write_bytes(b"corrupt")
        self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=tamper))
        self.assert_old()
        self.assertEqual(self.journal()["state"], "PREPARED")

    def test_before_install_late_payload_tampering_cannot_partially_install(self):
        prepared = self.prepare()
        def tamper():
            (self.root / META / "payload-2").write_bytes(b"corrupt")
        self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=tamper))
        self.assert_old()

    def test_prepared_payload_is_independent_of_later_staging_changes(self):
        prepared = self.prepare()
        (self.stage / "b.json").write_bytes(b"later staging change")
        tx.commit_synthetic_transaction(prepared, before_install=lambda: None)
        self.assert_new()

    def test_changed_preimage_is_not_installed_over(self):
        prepared = self.prepare()
        (self.root / "b.json").write_bytes(b"another writer")
        before = snapshot(self.root)
        self.error(lambda: tx.commit_synthetic_transaction(prepared, before_install=lambda: None))
        self.assertEqual(snapshot(self.root), before)

    def test_failed_prepared_journal_flush_installs_nothing_and_preserves_evidence(self):
        real_fsync = os.fsync
        real_replace = os.replace
        journal_replaced = []
        def replace(src, dst):
            real_replace(src, dst)
            if Path(dst).name == "journal.json": journal_replaced.append(True)
        def fail_flush(fd):
            if journal_replaced: raise OSError("synthetic journal flush failure")
            real_fsync(fd)
        with patch.object(tx.os, "replace", side_effect=replace), patch.object(tx.os, "fsync", side_effect=fail_flush):
            self.error(lambda: tx.prepare_synthetic_transaction(
                self.root, self.stage, self.names, self.hashes, verify_postimage=accept))
        self.assert_old()
        self.assertTrue((self.root / META / "backup-0").exists())

    def test_terminal_cleanup_interruption_can_resume_without_removed_backup(self):
        self.prepare()
        real = Path.unlink
        def fault(path, *args, **kwargs):
            real(path, *args, **kwargs)
            if path == self.root / META / "backup-0":
                raise OSError("synthetic cleanup interruption")
        with patch.object(Path, "unlink", new=fault):
            self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual(self.journal()["state"], "ROLLED_BACK")
        self.assertFalse((self.root / META / "backup-0").exists())
        self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assert_old()

    def test_unknown_extra_evidence_is_not_cleaned_from_committed_operation(self):
        prepared = self.prepare()
        tx.commit_synthetic_transaction(prepared, before_install=lambda: None)
        (self.root / META / "unknown-evidence").write_bytes(b"evidence")
        before = snapshot(self.root)
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual(snapshot(self.root), before)

    def test_actual_junction_in_recovery_destination_is_not_followed(self):
        (self.root / "nested").mkdir()
        (self.stage / "nested").mkdir()
        (self.root / "nested" / "file").write_bytes(b"old")
        (self.stage / "nested" / "file").write_bytes(b"new")
        tx.prepare_synthetic_transaction(self.root, self.stage, ("nested/file",),
                                         {"nested/file": sha(b"new")}, verify_postimage=accept)
        outside = self.base / "elsewhere"
        (self.root / "nested").rename(outside)
        link = self.root / "nested"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.addCleanup(lambda: link.rmdir() if os.name == "nt" and link.exists()
                        else link.unlink(missing_ok=True))
        before = snapshot(self.root / META)
        self.error(lambda: tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
        self.assertEqual((outside / "file").read_bytes(), b"old")
        self.assertEqual(snapshot(self.root / META), before)

    def test_literal_subprocess_death_before_during_after_install_and_recovery(self):
        for point in ("before", "during", "after", "committed", "recovery"):
            with self.subTest(point=point):
                prepared = self.prepare()
                code = r'''
import os, sys
from pathlib import Path
from wechat_exporter import capsule_transaction as tx
root = Path(sys.argv[1]); point = sys.argv[2]
real = os.replace
def fault(src, dst):
    real(src, dst)
    dest = Path(dst)
    if point in ('during', 'recovery') and dest == root / 'a.jsonl': os._exit(73)
    if point == 'after' and dest == root / 'new.txt': os._exit(73)
tx.os.replace = fault
if point == 'recovery':
    tx.recover_synthetic_transaction(root, verify_postimage=lambda root: None)
else:
    import json
    j = json.loads((root / '.synthetic-transaction' / 'journal.json').read_text())
    prepared = tx.PreparedSyntheticTransaction(j['operation_id'], root,
        root / '.synthetic-transaction' / 'journal.json',
        {e['path']: e['post_hash'] for e in j['files']}, lambda root: None)
    tx.commit_synthetic_transaction(prepared,
        before_install=lambda: os._exit(73) if point == 'before' else None)
    os._exit(73)
'''
                if point == "recovery":
                    for name, data in self.new.items():
                        (self.root / name).write_bytes(data)
                result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code,
                                         str(self.root), point], capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 73, result.stderr.decode("utf8"))
                self.assertTrue(tx.recover_synthetic_transaction(self.root, verify_postimage=accept))
                if point == "committed":
                    self.assert_new()
                    for name, data in self.old.items(): (self.root / name).write_bytes(data)
                    (self.root / "new.txt").unlink()
                else:
                    self.assert_old()


if __name__ == "__main__":
    unittest.main()
