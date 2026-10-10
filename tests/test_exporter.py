from __future__ import annotations

import io
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from wechat_exporter.avatars import AvatarExporter
from wechat_exporter.contacts import contact_choice_from_search_result
from wechat_exporter.markdown import MarkdownArchiveWriter, sender_resolver
from wechat_exporter.messages import (
    MESSAGE_REQUIRED_COLUMNS,
    iter_oldest_first,
    spool_all_messages,
    verify_public_pagination_scope,
)
from wechat_exporter.models import (
    AvatarSnapshot,
    ChatMessage,
    ContactChoice,
    ExportStats,
    ImageResult,
    resolve_contact_display_name,
)
from wechat_exporter.utils import escape_markdown, safe_windows_name


class FakeDB:
    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda item: item["sort_seq"], reverse=True)

    def get_messages(self, username, limit, offset):
        return self.messages[offset:offset + limit]

    def get_nickname(self, username):
        return {"internal_a": "小雪"}.get(username, username)


class FakeConnection:
    def __init__(self, found):
        self.found = found

    def execute(self, query, parameters):
        self.query = query
        return self

    def fetchone(self):
        return (1,) if self.found else None

    def fetchall(self):
        if "table_info" in self.query:
            return [(index, column) for index, column in enumerate(MESSAGE_REQUIRED_COLUMNS)]
        return []

    def close(self):
        pass


class FakeShardedDB:
    def __init__(self, matches):
        self.paths = ["message/message_0.db", "message/message_1.db"]
        self.matches = dict(zip(self.paths, matches))

    def _message_dbs(self):
        return self.paths

    def _open(self, relative_path):
        return FakeConnection(self.matches[relative_path])


class FakeAvatarDB:
    _db_files = []
    account_dir = "unused"
    db_dir = "unused"


class ExporterTests(unittest.TestCase):
    def render(self, messages, avatars=None, image_result=None):
        db = FakeDB(messages)
        contact = ContactChoice.from_fields(username="internal_a", nickname="小雪")
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        spool = spool_all_messages(db, "chat", root / "spool", page_size=1)
        stats = ExportStats(message_count=spool.count)
        with MarkdownArchiveWriter(
            root / "chat.md",
            "测试",
            datetime.now().astimezone(),
            spool.first_time,
            spool.last_time,
            avatars=avatars,
        ) as writer:
            resolve = sender_resolver(contact)
            ordered = list(iter_oldest_first(spool))
            for message in ordered:
                result = image_result if message.message_type == "图片" else None
                writer.write_message(message, resolve(message), result, stats)
        text = (root / "chat.md").read_text(encoding="utf-8")
        temporary.cleanup()
        return text

    def test_pagination_and_chronological_iteration(self):
        messages = [
            {"local_id": i, "type": "文本", "sender_id": 2, "create_time": 1700000000 + i,
             "content": f"m{i}", "sort_seq": i}
            for i in range(1, 8)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spool = spool_all_messages(FakeDB(messages), "wxid", Path(tmp), page_size=3)
            ordered = list(iter_oldest_first(spool))
        self.assertEqual(spool.count, 7)
        self.assertEqual([item.sort_seq for item in ordered], list(range(1, 8)))

    def test_markdown_escapes_content_and_keeps_relative_image(self):
        messages = [
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
             "content": "# 标题\n[链接](x)\n---", "sort_seq": 1},
            {"local_id": 2, "type": "图片", "sender_id": 3, "sender_username": "internal_a",
             "create_time": 1700000002, "content": "", "sort_seq": 2},
        ]
        db = FakeDB(messages)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = spool_all_messages(db, "chat", root / "spool", page_size=1)
            stats = ExportStats(message_count=2)
            with MarkdownArchiveWriter(root / "chat.md", "测试", datetime.now().astimezone(),
                                       spool.first_time, spool.last_time) as writer:
                resolve = sender_resolver(
                    ContactChoice.from_fields(username="internal_a", nickname="小雪")
                )
                ordered = list(iter_oldest_first(spool))
                writer.write_message(ordered[0], resolve(ordered[0]), None, stats)
                writer.write_message(
                    ordered[1], resolve(ordered[1]),
                    ImageResult("success", "assets/images/a.jpg"), stats,
                )
            text = (root / "chat.md").read_text(encoding="utf-8")
        self.assertIn("\\# 标题", text)
        self.assertIn("\\[链接\\](x)", text)
        self.assertIn("![图片](assets/images/a.jpg)", text)
        self.assertIn("👤 **小雪**", text)
        self.assertNotIn("C:\\", text)

    def test_unsupported_message_has_human_placeholder_without_private_trace(self):
        raw = {"local_id": 99, "type": "语音", "sender_id": 2, "create_time": 1700000000,
               "content": "secret", "sort_seq": 99}
        db = FakeDB([raw])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = spool_all_messages(db, "chat", root / "spool")
            stats = ExportStats(message_count=1)
            with MarkdownArchiveWriter(root / "chat.md", "测试", datetime.now().astimezone(),
                                       spool.first_time, spool.last_time) as writer:
                message = next(iter_oldest_first(spool))
                writer.write_message(message, "我", None, stats)
            text = (root / "chat.md").read_text(encoding="utf-8")
        self.assertIn("暂未支持的语音消息", text)
        self.assertNotIn("message_id", text)
        self.assertNotIn("secret", text)
        self.assertEqual(stats.unsupported_message_count, 1)

    def test_windows_names_and_escape(self):
        self.assertEqual(safe_windows_name('A:B/C*D?'), "A_B_C_D_")
        self.assertEqual(safe_windows_name("CON"), "_CON")
        self.assertEqual(escape_markdown("*x*"), "\\*x\\*")

    def test_multi_shard_chat_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "多个消息分片"):
            verify_public_pagination_scope(FakeShardedDB([True, True]), "filehelper")
        self.assertEqual(verify_public_pagination_scope(FakeShardedDB([True, False]), "filehelper"), 1)

    def test_both_participants_have_avatars(self):
        messages = [
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
             "content": "我方", "sort_seq": 1},
            {"local_id": 2, "type": "文本", "sender_id": 3, "sender_username": "internal_a",
             "create_time": 1700000002, "content": "对方", "sort_seq": 2},
        ]
        avatars = {
            "self": AvatarSnapshot("local_database", "assets/avatars/me.jpg"),
            "contact": AvatarSnapshot("manual", "assets/avatars/contact.png"),
        }
        text = self.render(messages, avatars)
        self.assertEqual(text.count("assets/avatars/me.jpg"), 1)
        self.assertEqual(text.count("assets/avatars/contact.png"), 1)
        self.assertIn("<b>我</b>", text)
        self.assertIn("<b>小雪</b>", text)

    def test_missing_self_avatar_has_no_broken_image(self):
        raw = {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
               "content": "hello", "sort_seq": 1}
        text = self.render([raw], {"self": AvatarSnapshot("unavailable")})
        self.assertNotIn("assets/avatars/me", text)
        self.assertIn("👤 **我**", text)

    def test_missing_contact_avatar_has_no_broken_image(self):
        raw = {"local_id": 1, "type": "文本", "sender_id": 3, "sender_username": "internal_a",
               "create_time": 1700000001, "content": "hello", "sort_seq": 1}
        text = self.render([raw], {"contact": AvatarSnapshot("remote_available_not_fetched")})
        self.assertNotIn("assets/avatars/contact", text)
        self.assertIn("👤 **小雪**", text)

    def test_both_avatars_missing(self):
        messages = [
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
             "content": "a", "sort_seq": 1},
            {"local_id": 2, "type": "文本", "sender_id": 3, "sender_username": "internal_a",
             "create_time": 1700000002, "content": "b", "sort_seq": 2},
        ]
        text = self.render(messages, {
            "self": AvatarSnapshot("unavailable"),
            "contact": AvatarSnapshot("unavailable"),
        })
        self.assertNotIn("<img", text)
        self.assertEqual(text.count("👤"), 2)

    def test_chinese_and_special_character_names(self):
        raw = {"local_id": 1, "type": "文本", "sender_id": 3, "sender_username": "A<&",
               "create_time": 1700000001, "content": "正文", "sort_seq": 1}
        db = FakeDB([raw])
        db.get_nickname = lambda username: "雪鸮 <&>"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = spool_all_messages(db, "chat", root / "spool")
            message = next(iter_oldest_first(spool))
            with MarkdownArchiveWriter(
                root / "chat.md", "测试", datetime.now().astimezone(),
                spool.first_time, spool.last_time,
                avatars={"contact": AvatarSnapshot("manual", "assets/avatars/contact.jpg")},
            ) as writer:
                contact = ContactChoice.from_fields(
                    username="A<&", nickname="雪鸮 <&>"
                )
                writer.write_message(
                    message, sender_resolver(contact)(message), None, ExportStats()
                )
            text = (root / "chat.md").read_text(encoding="utf-8")
        self.assertIn("雪鸮 &lt;&amp;&gt;", text)
        self.assertNotIn("<b>雪鸮 <&></b>", text)

    def test_multiline_body_and_image_relative_path(self):
        messages = [
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
             "content": "第一行\n第二行", "sort_seq": 1},
            {"local_id": 2, "type": "图片", "sender_id": 3, "sender_username": "internal_a",
             "create_time": 1700000002, "content": "", "sort_seq": 2},
        ]
        text = self.render(messages, image_result=ImageResult("success", "assets/images/photo.jpg"))
        self.assertIn("第一行\n第二行", text)
        self.assertIn("![图片](assets/images/photo.jpg)", text)
        self.assertNotIn("C:\\", text)

    def test_duplicate_message_ids_are_counted_and_removed(self):
        messages = [
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000001,
             "content": "a", "sort_seq": 1},
            {"local_id": 1, "type": "文本", "sender_id": 2, "create_time": 1700000002,
             "content": "b", "sort_seq": 2},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spool = spool_all_messages(FakeDB(messages), "chat", Path(tmp), page_size=1)
            ordered = list(iter_oldest_first(spool))
        self.assertEqual(spool.duplicate_message_id_count, 1)
        self.assertEqual(len(ordered), 1)

    def test_manual_avatar_is_copied_into_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "outside-name.dat"
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), (240, 240, 255)).save(buffer, format="PNG")
            source.write_bytes(buffer.getvalue())
            exporter = AvatarExporter(
                FakeAvatarDB(), root / "archive" / "assets" / "avatars",
                logging.getLogger("avatar-test"),
            )
            answers = iter([str(source)])
            snapshot = exporter.export(
                "contact", "internal_test", "对方",
                input_func=lambda prompt: next(answers),
                print_func=lambda *args: None,
            )
            destination = root / "archive" / snapshot.relative_path
            self.assertEqual(snapshot.status, "manual")
            self.assertEqual(snapshot.relative_path, "assets/avatars/contact.png")
            self.assertTrue(destination.is_file())
            self.assertGreater(destination.stat().st_size, 0)

    def test_display_name_prefers_remark_over_nickname(self):
        self.assertEqual(
            resolve_contact_display_name(
                remark="Synthetic Contact", nickname="380", alias="fixture_public_id_001",
                username="internal_fixture_internal_contact",
            ),
            "Synthetic Contact",
        )

    def test_display_name_uses_nickname_when_remark_empty(self):
        self.assertEqual(
            resolve_contact_display_name(
                remark="", nickname="380", alias="fixture_public_id_001",
                username="internal_fixture_internal_contact",
            ),
            "380",
        )

    def test_display_name_uses_alias_when_remark_and_nickname_empty(self):
        self.assertEqual(
            resolve_contact_display_name(
                remark="", nickname="", alias="fixture_public_id_001",
                username="internal_fixture_internal_contact",
            ),
            "fixture_public_id_001",
        )

    def test_display_name_falls_back_to_internal_username(self):
        self.assertEqual(
            resolve_contact_display_name(
                remark="", nickname="", alias="",
                username="internal_fixture_internal_contact",
            ),
            "internal_fixture_internal_contact",
        )

    def test_selection_and_markdown_share_canonical_display_name(self):
        contact = contact_choice_from_search_result(
            {
                "username": "internal_fixture_internal_contact",
                "nick_name": "380",
                "remark": "Synthetic Contact",
            },
            alias="fixture_public_id_001",
        )
        message = ChatMessage.from_upstream(
            {
                "local_id": 1,
                "type": "文本",
                "sender_id": 380,
                "sender_username": "380",
                "create_time": 1700000001,
                "content": "body",
                "sort_seq": 1,
            }
        )
        self.assertEqual(contact.display_name, "Synthetic Contact")
        self.assertEqual(sender_resolver(contact)(message), contact.display_name)


if __name__ == "__main__":
    unittest.main()
