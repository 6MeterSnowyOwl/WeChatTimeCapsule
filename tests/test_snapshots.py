from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_exporter.archive import (
    ArchiveTarget,
    allocate_snapshot_id,
    clone_immutable_asset,
    create_snapshot,
    create_target,
    discover_targets,
    load_snapshot_index,
    load_message_store,
    snapshot_hashes,
)
from wechat_exporter.deletion import (
    JOURNAL_NAME,
    TOMBSTONE_NAME,
    delete_snapshot_transaction,
    recover_pending_snapshot_deletion,
)
from wechat_exporter.integrity import verify_snapshot_offline
from wechat_exporter.models import ContactChoice, ImageResult
from wechat_exporter.migrations import (
    CURRENT_MESSAGE_SCHEMA_VERSION,
    CURRENT_MUTABLE_SCHEMA_VERSION,
)
from wechat_exporter.utils import write_json_atomic


class IncrementalFakeDB:
    _db_files = []
    account_dir = "unused"
    db_dir = "unused"

    def __init__(self, messages):
        self.messages = list(messages)
        self.full_scan_calls = 0
        self.incremental_calls: list[tuple[int, int]] = []

    def get_messages(self, username, limit, offset):
        self.full_scan_calls += 1
        rows = sorted(self.messages, key=lambda item: item["sort_seq"], reverse=True)
        return rows[offset:offset + limit]

    def get_new_messages(self, username, since_seq, limit):
        self.incremental_calls.append((since_seq, limit))
        rows = sorted(
            (item for item in self.messages if item["sort_seq"] > since_seq),
            key=lambda item: item["sort_seq"],
        )
        return rows[:limit]


class CountingImageExporter:
    calls: list[str] = []

    def __init__(self, db, username, image_dir, logger):
        self.image_dir = Path(image_dir)

    def export(self, message):
        self.calls.append(message.local_id)
        destination = self.image_dir / f"image_{message.local_id}.jpg"
        destination.write_bytes(("image:" + message.local_id).encode("ascii"))
        return ImageResult("success", f"assets/images/{destination.name}")


class CountingVideoExporter:
    calls: list[str] = []

    def __init__(self, db, username, video_dir, logger):
        self.video_dir = Path(video_dir)

    def export(self, message):
        self.calls.append(message.local_id)
        destination = self.video_dir / f"video_{message.local_id}.mp4"
        destination.write_bytes(("video:" + message.local_id).encode("ascii"))
        return ImageResult("success", f"assets/videos/{destination.name}")


def make_messages(
    count: int,
    image_ids: set[int] | None = None,
    video_ids: set[int] | None = None,
) -> list[dict]:
    image_ids = image_ids or set()
    video_ids = video_ids or set()
    return [
        {
            "local_id": index,
            "type": (
                "图片" if index in image_ids else "视频" if index in video_ids else "文本"
            ),
            "sender_id": 2 if index % 2 else 380,
            "sender_username": "" if index % 2 else "380",
            "create_time": 1_700_000_000 + index,
            "content": "" if index in image_ids | video_ids else f"message {index}",
            "sort_seq": index,
        }
        for index in range(1, count + 1)
    ]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        CountingImageExporter.calls = []
        CountingVideoExporter.calls = []
        self.logger = logging.getLogger("snapshot-test")

    def make_target(self, root: Path) -> ArchiveTarget:
        target = ArchiveTarget(root / "exports" / "Synthetic Contact", "internal_target", "Synthetic Contact")
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
            target.root / "state" / "schema.json",
            {
                "schema_version": CURRENT_MESSAGE_SCHEMA_VERSION,
                "format": "wechat-exporter-normalized-message-jsonl",
                "identity": "local_id",
                "sort_key": ["sort_seq", "create_time", "local_id"],
            },
        )
        write_json_atomic(
            target.root / "archive_state.json",
            {
                "schema_version": CURRENT_MUTABLE_SCHEMA_VERSION,
                "last_snapshot_id": None,
                "last_sort_seq": 0,
                "last_message_identity": None,
                "last_sort_key": [0, None],
                "message_count": 0,
            },
        )
        return target

    def create(self, db, target, work, now):
        with patch("wechat_exporter.archive.ImageExporter", CountingImageExporter), patch(
            "wechat_exporter.archive.VideoExporter", CountingVideoExporter
        ):
            return create_snapshot(
                db, target, "internal_self", work, self.logger, now=now,
                input_func=lambda prompt: "", print_func=lambda *args: None,
            )

    def test_same_second_snapshot_ids_are_collision_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshots = Path(temporary)
            now = datetime(2026, 9, 12, 17, 30, 0)
            first = allocate_snapshot_id(snapshots, now)
            (snapshots / first).mkdir()
            second = allocate_snapshot_id(snapshots, now)
        self.assertEqual(first, "20260912_173000")
        self.assertEqual(second, "20260912_173000_01")

    def test_legacy_export_is_registered_only_after_explicit_target_add(self):
        with tempfile.TemporaryDirectory() as temporary:
            exports = Path(temporary) / "exports"
            legacy = exports / "Synthetic Contact"
            legacy.mkdir(parents=True)
            (legacy / "chat.md").write_text("legacy", encoding="utf-8")
            write_json_atomic(
                legacy / "export_meta.json",
                {"username": "internal_target", "chat_name": "Synthetic Contact"},
            )
            chat_before = (legacy / "chat.md").read_bytes()
            meta_before = (legacy / "export_meta.json").read_bytes()

            self.assertEqual(discover_targets(exports), [])
            target = create_target(
                exports,
                ContactChoice.from_fields(
                    username="internal_target", nickname="Synthetic Contact", alias="wechat-id"
                ),
            )

            self.assertEqual(target.root, legacy)
            self.assertEqual(len(discover_targets(exports)), 1)
            self.assertEqual((legacy / "chat.md").read_bytes(), chat_before)
            self.assertEqual((legacy / "export_meta.json").read_bytes(), meta_before)

    def test_two_backups_without_new_messages_create_immutable_full_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(100, {10}))
            now = datetime(2026, 9, 12, 17, 30, 0).astimezone()

            first = self.create(db, target, work, now)
            first_hashes = snapshot_hashes(first.path)
            second = self.create(db, target, work, now)

            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(first.message_count, 100)
            self.assertEqual(second.message_count, 100)
            self.assertEqual(second.new_message_count, 0)
            self.assertEqual(snapshot_hashes(first.path), first_hashes)
            self.assertEqual(CountingImageExporter.calls, ["10"])
            self.assertEqual(second.asset_counts.reused, 1)
            self.assertEqual(second.asset_counts.hardlinked + second.asset_counts.copied, 1)
            self.assertEqual(second.duplicate_count, 0)
            self.assertEqual(len(load_message_store(target.message_store)), 100)

    def test_incremental_adds_five_and_decodes_only_new_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(100, {10}))
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )
            full_scan_calls = db.full_scan_calls
            db.messages = make_messages(105, {10, 103})
            second = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 35, 0).astimezone()
            )

            self.assertEqual(first.message_count, 100)
            self.assertEqual(second.message_count, 105)
            self.assertEqual(second.new_message_count, 5)
            self.assertEqual(second.new_image_count, 1)
            self.assertEqual(CountingImageExporter.calls, ["10", "103"])
            self.assertEqual(db.full_scan_calls, full_scan_calls)
            self.assertTrue(db.incremental_calls)
            self.assertEqual(len(load_message_store(target.message_store)), 105)
            self.assertEqual(second.duplicate_count, 0)

    def test_copy_fallback_when_hardlink_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jpg"
            destination = root / "snapshot" / "asset.jpg"
            source.write_bytes(b"immutable")
            from wechat_exporter.archive import AssetCounts
            counts = AssetCounts()
            with patch("wechat_exporter.archive.os.link", side_effect=OSError("unsupported")):
                mode = clone_immutable_asset(source, destination, counts)
            self.assertEqual(mode, "copy")
            self.assertEqual(destination.read_bytes(), b"immutable")
            self.assertEqual(counts.copied, 1)
            self.assertEqual(counts.hardlinked, 0)

    def test_corrupt_state_uses_full_recovery_without_changing_old_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(10))
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )
            before = snapshot_hashes(first.path)
            state_path = target.root / "archive_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["message_count"] = 999
            write_json_atomic(state_path, state)

            second = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 31, 0).astimezone()
            )
            meta = json.loads((second.path / "export_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["input_mode"], "full_recovery")
            self.assertEqual(second.message_count, 10)
            self.assertEqual(second.new_message_count, 0)
            self.assertEqual(snapshot_hashes(first.path), before)

    def test_delete_latest_preserves_unique_media_for_offline_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(100, {10}))
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )
            db.messages = make_messages(110, {10, 105})
            second = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 31, 0).astimezone()
            )
            state_before = target.message_store.read_bytes()
            unique_bytes = (second.path / "assets" / "images" / "image_105.jpg").read_bytes()

            result = delete_snapshot_transaction(target, second.snapshot_id)

            self.assertEqual(result.remaining_snapshot_count, 1)
            self.assertEqual(target.message_store.read_bytes(), state_before)
            self.assertEqual(
                (target.state_dir / "assets" / "images" / "image_105.jpg").read_bytes(),
                unique_bytes,
            )
            calls_before_rebuild = list(CountingImageExporter.calls)
            third = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 32, 0).astimezone()
            )
            self.assertEqual(third.message_count, 110)
            self.assertEqual(CountingImageExporter.calls, calls_before_rebuild)
            self.assertEqual(
                (third.path / "assets" / "images" / "image_105.jpg").read_bytes(),
                unique_bytes,
            )
            self.assertTrue(verify_snapshot_offline(target.root, third.snapshot_id).ok)
            self.assertTrue(first.path.is_dir())

    def test_video_uses_generic_vault_and_survives_delete_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            rows = make_messages(8, video_ids={7})
            rows[0].update({
                "type": 42,
                "content": '<msg nickname="Snow" alias="public-id" />',
            })
            rows[1].update({
                "type": "位置",
                "content": '<msg><location x="35.1" y="139.2" poiname="Snow Park" /></msg>',
            })
            db = IncrementalFakeDB(rows)
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 20, 30, 0).astimezone()
            )
            video = first.path / "assets" / "videos" / "video_7.mp4"
            video_bytes = video.read_bytes()
            self.assertEqual(first.video_count, 1)
            self.assertEqual(first.new_video_count, 1)
            self.assertIn(
                "[▶ 播放视频](assets/videos/video_7.mp4)",
                (first.path / "chat.md").read_text(encoding="utf-8"),
            )
            first_markdown = (first.path / "chat.md").read_text(encoding="utf-8")
            self.assertIn("联系人名片", first_markdown)
            self.assertIn("📍 Snow Park", first_markdown)

            delete_snapshot_transaction(target, first.snapshot_id)
            durable = target.state_dir / "assets" / "videos" / "video_7.mp4"
            self.assertEqual(durable.read_bytes(), video_bytes)
            calls_before = list(CountingVideoExporter.calls)

            rebuilt = self.create(
                db, target, work, datetime(2026, 9, 12, 20, 31, 0).astimezone()
            )
            self.assertEqual(CountingVideoExporter.calls, calls_before)
            self.assertEqual(
                (rebuilt.path / "assets" / "videos" / "video_7.mp4").read_bytes(),
                video_bytes,
            )
            rebuilt_markdown = (rebuilt.path / "chat.md").read_text(encoding="utf-8")
            self.assertIn("联系人名片", rebuilt_markdown)
            self.assertIn("📍 Snow Park", rebuilt_markdown)
            self.assertTrue(verify_snapshot_offline(target.root, rebuilt.snapshot_id).ok)

    def test_deleting_hardlinked_snapshot_keeps_other_link_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(10, {5}))
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )
            second = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 31, 0).astimezone()
            )
            first_asset = first.path / "assets" / "images" / "image_5.jpg"
            second_asset = second.path / "assets" / "images" / "image_5.jpg"
            before = first_asset.read_bytes()
            self.assertTrue(os.path.samefile(first_asset, second_asset))

            delete_snapshot_transaction(target, second.snapshot_id)

            self.assertTrue(first_asset.is_file())
            self.assertEqual(first_asset.read_bytes(), before)

    def test_delete_middle_snapshot_keeps_lineage_hint_and_other_hashes_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(10, {5}))
            first = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )
            middle = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 31, 0).astimezone()
            )
            last = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 32, 0).astimezone()
            )
            first_before = snapshot_hashes(first.path)
            last_before = snapshot_hashes(last.path)

            delete_snapshot_transaction(target, middle.snapshot_id)

            self.assertEqual(snapshot_hashes(first.path), first_before)
            self.assertEqual(snapshot_hashes(last.path), last_before)
            self.assertFalse((target.root / "integrity" / f"{middle.snapshot_id}.json").exists())
            self.assertTrue((target.root / "integrity" / f"{first.snapshot_id}.json").is_file())
            self.assertTrue((target.root / "integrity" / f"{last.snapshot_id}.json").is_file())
            self.assertTrue(verify_snapshot_offline(target.root, first.snapshot_id).ok)
            self.assertTrue(verify_snapshot_offline(target.root, last.snapshot_id).ok)
            tombstones = json.loads((target.root / TOMBSTONE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(tombstones["deleted_snapshots"][0]["snapshot_id"], middle.snapshot_id)

    def test_interrupted_deletion_finishes_on_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.make_target(root)
            work = root / "work"
            work.mkdir()
            db = IncrementalFakeDB(make_messages(10, {5}))
            snapshot = self.create(
                db, target, work, datetime(2026, 9, 12, 17, 30, 0).astimezone()
            )

            def fail_after_rename(phase):
                if phase == "snapshot_renamed":
                    raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                delete_snapshot_transaction(
                    target, snapshot.snapshot_id, phase_hook=fail_after_rename
                )
            self.assertTrue((target.root / JOURNAL_NAME).is_file())

            recovered = recover_pending_snapshot_deletion(target.root)

            self.assertEqual(recovered.snapshot_id, snapshot.snapshot_id)
            self.assertFalse((target.root / JOURNAL_NAME).exists())
            self.assertEqual(load_snapshot_index(target)["snapshots"], [])
            self.assertTrue(target.root.is_dir())
            self.assertGreater(len(load_message_store(target.message_store)), 0)


if __name__ == "__main__":
    unittest.main()
