from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wechat_exporter.archive import ArchiveTarget, load_snapshot_index, snapshot_hashes
from wechat_exporter.current_capsule import (
    CURRENT_JOURNAL,
    AvatarHistoryEntry,
    create_current_capsule,
    load_avatar_history,
    recover_current_capsule_transaction,
)
from wechat_exporter.integrity import generate_current_manifest, sha256_file, verify_current_capsule
from wechat_exporter.models import AvatarSnapshot, ImageResult
from wechat_exporter.reconciliation import reconcile_and_update_capsule
from wechat_exporter.utils import write_json_atomic


class FakeDB:
    _db_files = []
    account_dir = "unused"
    db_dir = "unused"

    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self, username, limit, offset):
        rows = sorted(self.messages, key=lambda item: item["sort_seq"], reverse=True)
        return rows[offset:offset + limit]

    def get_new_messages(self, username, since_seq, limit):
        return [item for item in sorted(self.messages, key=lambda x: x["sort_seq"])
                if item["sort_seq"] > since_seq][:limit]


class FakeImageExporter:
    calls = []

    def __init__(self, db, username, image_dir, logger):
        self.image_dir = Path(image_dir)

    def export(self, message):
        self.calls.append(message.local_id)
        destination = self.image_dir / f"image_{message.local_id}.jpg"
        destination.write_bytes(("image-" + message.local_id).encode())
        return ImageResult("success", f"assets/images/{destination.name}")


class FakeAvatarExporter:
    contact_bytes = b"contact-a"

    def __init__(self, db, avatar_dir, logger):
        self.avatar_dir = Path(avatar_dir)

    def export(self, role, username, label, input_func, print_func):
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        name = "me.jpg" if role == "self" else "contact.jpg"
        destination = self.avatar_dir / name
        destination.write_bytes(b"self-avatar" if role == "self" else self.contact_bytes)
        return AvatarSnapshot("success", f"assets/avatars/{name}")


def messages(count=3, image_ids=frozenset({2})):
    return [{
        "local_id": number,
        "type": "图片" if number in image_ids else "文本",
        "sender_id": 2 if number % 2 else 1,
        "sender_username": "internal_contact" if number % 2 else "internal_self",
        "create_time": 1_700_000_000 + number,
        "content": "" if number in image_ids else f"message {number}",
        "sort_seq": number,
    } for number in range(1, count + 1)]


def make_target(base: Path) -> ArchiveTarget:
    target = ArchiveTarget(base / "exports" / "Synthetic Contact", "internal_contact", "Synthetic Contact")
    target.root.mkdir(parents=True)
    write_json_atomic(target.root / "target.json", {
        "schema_version": 2, "username": target.username, "display_name": target.display_name,
    })
    write_json_atomic(target.root / "snapshot_index.json", {"schema_version": 2, "snapshots": []})
    write_json_atomic(target.root / "archive_state.json", {
        "schema_version": 2, "last_snapshot_id": None, "last_sort_seq": 0,
        "last_message_identity": None, "last_sort_key": [0, None], "message_count": 0,
    })
    write_json_atomic(target.state_dir / "schema.json", {
        "schema_version": 2, "format": "wechat-exporter-normalized-message-jsonl",
        "identity": "local_id", "sort_key": ["sort_seq", "create_time", "local_id"],
        "content_model": "structured-v2",
    })
    target.message_store.write_text("", encoding="utf-8")
    return target


class CurrentCapsuleTests(unittest.TestCase):
    def setUp(self):
        FakeImageExporter.calls = []
        FakeAvatarExporter.contact_bytes = b"contact-a"
        self.logger = logging.getLogger("current-capsule-test")

    def save(self, db, target, work, now=None, mode="quick"):
        with patch("wechat_exporter.current_capsule.ImageExporter", FakeImageExporter), \
             patch("wechat_exporter.current_capsule.AvatarExporter", FakeAvatarExporter):
            return create_current_capsule(
                db, target, "internal_self", work, self.logger,
                now=now or datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc),
                input_func=lambda prompt: "", print_func=lambda *args: None, save_mode=mode,
            )

    def test_current_capsule_uses_target_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); result = self.save(FakeDB(messages()), target, root / "work")
            self.assertEqual(result.path, target.root)
            self.assertTrue((target.root / "chat.md").is_file())
            self.assertTrue((target.root / "capsule_meta.json").is_file())

    def test_save_does_not_append_snapshot_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); before = (target.root / "snapshot_index.json").read_bytes()
            self.save(FakeDB(messages()), target, root / "work")
            self.assertEqual((target.root / "snapshot_index.json").read_bytes(), before)

    def test_reconciliation_does_not_append_snapshot_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); before = (target.root / "snapshot_index.json").read_bytes()
            (root / "work").mkdir()
            with patch("wechat_exporter.current_capsule.ImageExporter", FakeImageExporter), \
                 patch("wechat_exporter.current_capsule.AvatarExporter", FakeAvatarExporter):
                result, reconciliation, report = reconcile_and_update_capsule(
                    FakeDB(messages()), target, "internal_self", root / "work", self.logger,
                    now=datetime(2026, 9, 12, 20, tzinfo=timezone.utc),
                    input_func=lambda prompt: "", print_func=lambda *args: None,
                )
            self.assertEqual(result.save_mode, "reconciliation")
            self.assertEqual((target.root / "snapshot_index.json").read_bytes(), before)

    def test_two_saves_replace_same_chat_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            first = self.save(db, target, root / "work")
            first_path = first.path / "chat.md"
            db.messages.append(messages(4, frozenset())[3])
            second = self.save(db, target, root / "work", datetime(2026, 9, 12, 21, tzinfo=timezone.utc))
            self.assertEqual(first_path, second.path / "chat.md")
            self.assertEqual(second.message_count, 4)

    def test_existing_media_is_not_redecoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            self.save(db, target, root / "work", datetime(2026, 9, 12, 21, tzinfo=timezone.utc))
            self.assertEqual(FakeImageExporter.calls, ["2"])

    def test_current_assets_include_rendered_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); self.save(FakeDB(messages()), target, root / "work")
            self.assertEqual((target.root / "assets/images/image_2.jpg").read_bytes(), b"image-2")

    def test_durable_media_can_rebuild_current_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            (target.root / "assets/images/image_2.jpg").unlink()
            self.save(db, target, root / "work", datetime(2026, 9, 12, 21, tzinfo=timezone.utc))
            self.assertTrue((target.root / "assets/images/image_2.jpg").is_file())

    def test_manifest_verifies_complete_capsule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); self.save(FakeDB(messages()), target, root / "work")
            result = verify_current_capsule(target.root)
            self.assertTrue(result.ok)
            self.assertTrue(result.manifest_present)

    def test_manifest_detects_chat_hash_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); self.save(FakeDB(messages()), target, root / "work")
            (target.root / "chat.md").write_text("changed", encoding="utf-8")
            self.assertEqual(verify_current_capsule(target.root).count("HASH_MISMATCH"), 1)

    def test_manifest_detects_untracked_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); self.save(FakeDB(messages()), target, root / "work")
            (target.root / "assets/extra.bin").write_bytes(b"extra")
            self.assertEqual(verify_current_capsule(target.root).count("UNTRACKED_FILE"), 1)

    def test_avatar_history_contains_contact_not_self(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); self.save(FakeDB(messages()), target, root / "work")
            history = load_avatar_history(target.root)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].sha256, sha256_file(target.root / "assets/avatars/contact.jpg"))
            self.assertNotEqual(history[0].sha256, sha256_file(target.root / "assets/avatars/me.jpg"))

    def test_unchanged_avatar_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            self.save(db, target, root / "work", datetime(2026, 9, 13, 20, tzinfo=timezone.utc))
            self.assertEqual(len(load_avatar_history(target.root)), 1)

    def test_new_avatar_appends_one_history_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            FakeAvatarExporter.contact_bytes = b"contact-b"
            self.save(db, target, root / "work", datetime(2026, 9, 13, 20, tzinfo=timezone.utc))
            history = load_avatar_history(target.root)
            self.assertEqual(len(history), 2)
            self.assertEqual(sum(item.current for item in history), 1)

    def test_first_observed_at_survives_unchanged_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            first = load_avatar_history(target.root)[0].first_observed_at
            self.save(db, target, root / "work", datetime(2026, 9, 14, 20, tzinfo=timezone.utc))
            self.assertEqual(load_avatar_history(target.root)[0].first_observed_at, first)

    def test_legacy_contact_avatars_import_by_unique_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root)
            entries = []
            for stamp, data in (("20260910_100000", b"legacy-a"), ("20260911_100000", b"legacy-a"),
                                ("20260912_100000", b"legacy-b")):
                path = target.snapshots_dir / stamp / "assets/avatars/contact.jpg"
                path.parent.mkdir(parents=True); path.write_bytes(data)
                write_json_atomic(path.parents[2] / "export_meta.json", {"created_at": datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).isoformat()})
                entries.append({"id": stamp, "status": "COMPLETE"})
            write_json_atomic(target.root / "snapshot_index.json", {"schema_version": 2, "snapshots": entries})
            self.save(FakeDB(messages()), target, root / "work")
            self.assertEqual(len(load_avatar_history(target.root)), 3)

    def test_legacy_snapshot_bytes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root)
            snap = target.snapshots_dir / "20260910_100000"; snap.mkdir(parents=True)
            (snap / "chat.md").write_bytes(b"immutable legacy")
            before = snapshot_hashes(snap)
            self.save(FakeDB(messages()), target, root / "work")
            self.assertEqual(snapshot_hashes(snap), before)

    def test_failed_commit_restores_previous_readable_capsule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root); db = FakeDB(messages())
            self.save(db, target, root / "work")
            before = (target.root / "chat.md").read_bytes()
            from wechat_exporter import current_capsule as module
            original = module._copy_atomic
            def fail_chat(source, destination):
                if destination.name == "chat.md":
                    raise OSError("simulated")
                return original(source, destination)
            with patch("wechat_exporter.current_capsule._copy_atomic", side_effect=fail_chat):
                with self.assertRaises(OSError):
                    self.save(db, target, root / "work", datetime(2026, 9, 13, 20, tzinfo=timezone.utc))
            self.assertEqual((target.root / "chat.md").read_bytes(), before)

    def test_recovery_restores_interrupted_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root)
            (target.root / "chat.md").write_bytes(b"new")
            previous = target.root / ".current_transaction/previous/chat.md"
            previous.parent.mkdir(parents=True); previous.write_bytes(b"old")
            write_json_atomic(target.root / CURRENT_JOURNAL, {
                "phase": "prepared", "managed_files": ["chat.md"],
            })
            self.assertTrue(recover_current_capsule_transaction(target.root))
            self.assertEqual((target.root / "chat.md").read_bytes(), b"old")

    def test_recovery_committed_transaction_keeps_new_capsule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root)
            (target.root / "chat.md").write_bytes(b"new")
            previous = target.root / ".current_transaction/previous/chat.md"
            previous.parent.mkdir(parents=True); previous.write_bytes(b"old")
            write_json_atomic(target.root / CURRENT_JOURNAL, {
                "phase": "committed", "managed_files": ["chat.md"],
            })
            recover_current_capsule_transaction(target.root)
            self.assertEqual((target.root / "chat.md").read_bytes(), b"new")

    def test_metadata_records_save_mode_and_history_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = make_target(root)
            self.save(FakeDB(messages()), target, root / "work", mode="reconciliation")
            metadata = json.loads((target.root / "capsule_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["last_save_mode"], "reconciliation")
            self.assertEqual(metadata["avatar_history_count"], 1)

    def test_manifest_generation_requires_core_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                generate_current_manifest(Path(tmp))


if __name__ == "__main__":
    unittest.main()
