from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_exporter.archive import (
    ArchiveTarget,
    ArchivedMessage,
    create_snapshot,
    snapshot_hashes,
    write_message_store_atomic,
)
from wechat_exporter.integrity import (
    generate_manifest,
    manifest_path,
    verify_snapshot_offline,
)
from wechat_exporter.migrations import (
    CURRENT_MESSAGE_SCHEMA_VERSION,
    CURRENT_MUTABLE_SCHEMA_VERSION,
    UnsupportedFutureSchema,
    migrate_mutable_state,
)
from wechat_exporter.models import ChatMessage
from wechat_exporter.reconciliation import (
    collect_reconciliation,
    reconcile_and_create_snapshot,
)
from wechat_exporter.utils import write_json_atomic


def raw(local_id: int, sort_seq: int, content: str = "body") -> dict:
    return {
        "local_id": local_id,
        "type": "文本",
        "sender_id": 380,
        "sender_username": "380",
        "create_time": 1_700_000_000 + sort_seq,
        "content": content,
        "sort_seq": sort_seq,
    }


def archived(item: dict) -> ArchivedMessage:
    return ArchivedMessage(ChatMessage.from_upstream(item))


class ReconciliationDB:
    _db_files = []
    account_dir = "unused"
    db_dir = "unused"

    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self, username, limit, offset):
        rows = sorted(self.messages, key=lambda item: item["sort_seq"], reverse=True)
        return rows[offset:offset + limit]

    def get_new_messages(self, username, since_seq, limit):
        rows = sorted(
            (item for item in self.messages if item["sort_seq"] > since_seq),
            key=lambda item: item["sort_seq"],
        )
        return rows[:limit]


class Phase2BTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("phase2b-test")

    def make_target(self, base: Path, records: list[ArchivedMessage]) -> ArchiveTarget:
        target = ArchiveTarget(base / "Synthetic Contact", "internal_target", "Synthetic Contact")
        target.root.mkdir(parents=True)
        write_json_atomic(
            target.root / "target.json",
            {
                "schema_version": CURRENT_MUTABLE_SCHEMA_VERSION,
                "username": target.username,
                "display_name": target.display_name,
            },
        )
        write_json_atomic(
            target.root / "snapshot_index.json",
            {"schema_version": CURRENT_MUTABLE_SCHEMA_VERSION, "snapshots": []},
        )
        write_json_atomic(
            target.state_dir / "schema.json",
            {
                "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
                "format": "wechat-exporter-normalized-message-jsonl",
                "identity": "local_id",
                "sort_key": ["sort_seq", "create_time", "local_id"],
            },
        )
        write_message_store_atomic(target.message_store, records)
        last = records[-1] if records else None
        write_json_atomic(
            target.root / "archive_state.json",
            {
                "schema_version": CURRENT_MUTABLE_SCHEMA_VERSION,
                "last_snapshot_id": None,
                "last_sort_seq": last.message.sort_seq if last else 0,
                "last_message_identity": last.identity if last else None,
                "last_sort_key": [
                    last.message.sort_seq if last else 0,
                    last.message.local_id if last else None,
                ],
                "message_count": len(records),
            },
        )
        return target

    def test_reconciliation_finds_delayed_old_sort_seq_and_restores_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = [archived(raw(1, 100)), archived(raw(2, 200)), archived(raw(4, 400))]
            target = self.make_target(root, existing)
            db = ReconciliationDB([raw(1, 100), raw(2, 200), raw(3, 300), raw(4, 400)])
            work = root / "work"
            work.mkdir()

            result = collect_reconciliation(db, target, work, self.logger)

            self.assertEqual(result.newly_discovered, 1)
            self.assertEqual(
                [record.message.local_id for record in result.collection.records],
                ["1", "2", "3", "4"],
            )

    def test_reconciliation_preserves_archive_only_messages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = [archived(raw(index, index * 100)) for index in range(1, 5)]
            target = self.make_target(root, existing)
            db = ReconciliationDB([raw(3, 300), raw(4, 400)])
            work = root / "work"
            work.mkdir()

            result = collect_reconciliation(db, target, work, self.logger)

            self.assertEqual(result.newly_discovered, 0)
            self.assertEqual(
                [record.message.local_id for record in result.collection.records],
                ["1", "2", "3", "4"],
            )

    def test_identity_conflict_is_reported_without_body_and_archive_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            target = self.make_target(root, [])
            db = ReconciliationDB([raw(100, 100, "archived-private-A")])
            first = create_snapshot(
                db, target, "internal_self", work, self.logger,
                now=datetime(2026, 9, 12, 19, 0, 0).astimezone(),
                input_func=lambda prompt: "", print_func=lambda *args: None,
            )
            before = snapshot_hashes(first.path)
            db.messages = [raw(100, 100, "database-private-B")]

            second, reconciliation, report = reconcile_and_create_snapshot(
                db, target, "internal_self", work, self.logger,
                now=datetime(2026, 9, 12, 19, 1, 0).astimezone(),
                input_func=lambda prompt: "", print_func=lambda *args: None,
            )

            report_text = report.read_text(encoding="utf-8")
            self.assertEqual(len(reconciliation.conflicts), 1)
            self.assertIn("content", reconciliation.conflicts[0].fields)
            self.assertNotIn("archived-private-A", report_text)
            self.assertNotIn("database-private-B", report_text)
            self.assertIn("archived-private-A", (second.path / "chat.md").read_text(encoding="utf-8"))
            self.assertNotIn("database-private-B", (second.path / "chat.md").read_text(encoding="utf-8"))
            self.assertEqual(snapshot_hashes(first.path), before)

    def test_sort_key_conflict_fails_safe_and_writes_sanitized_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            target = self.make_target(root, [archived(raw(1, 100, "private-A"))])
            db = ReconciliationDB([raw(1, 101, "private-B")])

            with self.assertRaisesRegex(RuntimeError, "fail safe"):
                reconcile_and_create_snapshot(
                    db, target, "internal_self", work, self.logger,
                    now=datetime(2026, 9, 12, 19, 2, 0).astimezone(),
                    input_func=lambda prompt: "", print_func=lambda *args: None,
                )

            reports = list((target.root / "reports").glob("*.json"))
            self.assertEqual(len(reports), 1)
            report_text = reports[0].read_text(encoding="utf-8")
            self.assertNotIn("private-A", report_text)
            self.assertNotIn("private-B", report_text)
            self.assertIn("sort_seq", report_text)
            self.assertEqual(list(target.snapshots_dir.glob("*")), [])

    def make_integrity_fixture(self, root: Path) -> tuple[Path, str, Path]:
        target = root / "Synthetic Contact"
        snapshot_id = "20260912_190000"
        snapshot = target / "snapshots" / snapshot_id
        asset = snapshot / "assets" / "images" / "a.jpg"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"valid-image-bytes")
        (snapshot / "chat.md").write_text(
            '# 与 Synthetic Contact 的聊天记录\n\n![图片](assets/images/a.jpg)\n', encoding="utf-8"
        )
        write_json_atomic(
            snapshot / "export_meta.json",
            {
                "status": "COMPLETE",
                "snapshot_id": snapshot_id,
                "parent_snapshot_id": None,
                "message_count": 1,
                "first_message_time": "2026-09-12T19:00:00+09:00",
                "last_message_time": "2026-09-12T19:00:00+09:00",
            },
        )
        write_json_atomic(
            target / "snapshot_index.json",
            {
                "schema_version": CURRENT_MUTABLE_SCHEMA_VERSION,
                "snapshots": [{
                    "id": snapshot_id,
                    "status": "COMPLETE",
                    "message_count": 1,
                    "created_at": "2026-09-12T19:00:00+09:00",
                }],
            },
        )
        generate_manifest(target, snapshot_id)
        return target, snapshot_id, asset

    def test_integrity_detects_hash_mismatch_missing_asset_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, snapshot_id, asset = self.make_integrity_fixture(Path(temporary))
            self.assertTrue(verify_snapshot_offline(target, snapshot_id).ok)

            asset.write_bytes(b"tampered-image")
            result = verify_snapshot_offline(target, snapshot_id)
            self.assertGreater(result.count("HASH_MISMATCH"), 0)

            asset.unlink()
            result = verify_snapshot_offline(target, snapshot_id)
            self.assertGreater(result.count("MISSING_ASSET"), 0)

            chat = target / "snapshots" / snapshot_id / "chat.md"
            chat.write_text(chat.read_text(encoding="utf-8") + "![x](../../evil.jpg)\n", encoding="utf-8")
            result = verify_snapshot_offline(target, snapshot_id)
            self.assertEqual(result.count("PATH_ESCAPE"), 1)

    def test_integrity_verification_is_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, snapshot_id, _ = self.make_integrity_fixture(Path(temporary))
            with patch(
                "wechat_exporter.account.create_database",
                side_effect=AssertionError("WeChat must not be initialized"),
            ):
                result = verify_snapshot_offline(target, snapshot_id)
            self.assertTrue(result.ok)

    def test_missing_manifest_performs_structure_check_without_creating_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, snapshot_id, _ = self.make_integrity_fixture(Path(temporary))
            manifest_path(target, snapshot_id).unlink()

            result = verify_snapshot_offline(target, snapshot_id)

            self.assertTrue(result.ok)
            self.assertFalse(result.manifest_present)
            self.assertFalse(manifest_path(target, snapshot_id).exists())

    def make_v0_mutable_state(self, root: Path) -> Path:
        target = root / "Synthetic Contact"
        (target / "state").mkdir(parents=True)
        write_json_atomic(
            target / "target.json", {"username": "internal_target", "display_name": "Synthetic Contact"}
        )
        write_json_atomic(
            target / "archive_state.json",
            {
                "last_snapshot_id": None,
                "last_sort_seq": 0,
                "last_message_identity": None,
                "message_count": 0,
            },
        )
        write_json_atomic(target / "snapshot_index.json", {"snapshots": []})
        (target / "state" / "messages.jsonl").write_text("", encoding="utf-8")
        snapshot = target / "snapshots" / "legacy"
        snapshot.mkdir(parents=True)
        (snapshot / "chat.md").write_text("immutable", encoding="utf-8")
        return target

    def test_v0_migration_current_noop_and_snapshot_immutability(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.make_v0_mutable_state(Path(temporary))
            before = snapshot_hashes(target / "snapshots" / "legacy")

            result = migrate_mutable_state(
                target, now=datetime(2026, 9, 12, 19, 0, 0).astimezone()
            )
            noop = migrate_mutable_state(target)

            self.assertEqual(result.from_version, 0)
            self.assertEqual(result.to_version, CURRENT_MUTABLE_SCHEMA_VERSION)
            self.assertEqual(
                result.applied,
                ("v0_to_v1", "v1_to_v2", "message_v0_to_v2"),
            )
            self.assertTrue(result.backup_path.is_dir())
            self.assertEqual(noop.applied, ())
            self.assertIsNone(noop.backup_path)
            self.assertEqual(snapshot_hashes(target / "snapshots" / "legacy"), before)
            schema = json.loads((target / "state" / "schema.json").read_text(encoding="utf-8"))
            self.assertEqual(schema["schema_version"], CURRENT_MESSAGE_SCHEMA_VERSION)

    def test_future_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "Synthetic Contact"
            target.mkdir()
            for name, payload in (
                ("target.json", {"schema_version": 999}),
                ("archive_state.json", {"schema_version": 999}),
                ("snapshot_index.json", {"schema_version": 999, "snapshots": []}),
            ):
                write_json_atomic(target / name, payload)
            with self.assertRaisesRegex(UnsupportedFutureSchema, "更新版本"):
                migrate_mutable_state(target)

    def test_interrupted_migration_leaves_live_state_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.make_v0_mutable_state(Path(temporary))
            original = {
                name: (target / name).read_bytes()
                for name in ("target.json", "archive_state.json", "snapshot_index.json")
            }
            calls = 0

            def failing_write(path: Path, payload: dict) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                write_json_atomic(path, payload)

            with self.assertRaises(OSError):
                migrate_mutable_state(target, write_func=failing_write)
            self.assertEqual(
                original,
                {
                    name: (target / name).read_bytes()
                    for name in original
                },
            )
            self.assertTrue(any((target / "state_backups").iterdir()))


if __name__ == "__main__":
    unittest.main()
