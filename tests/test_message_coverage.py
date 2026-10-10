from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_exporter.archive import (
    create_snapshot,
    create_target,
    load_message_store,
    snapshot_hashes,
)
from wechat_exporter.deletion import delete_snapshot_transaction
from wechat_exporter.integrity import verify_snapshot_offline
from wechat_exporter.inventory import canonical_type, encoded_subtype
from wechat_exporter.markdown import MarkdownArchiveWriter
from wechat_exporter.message_types import parse_message_content
from wechat_exporter.migrations import CURRENT_MESSAGE_SCHEMA_VERSION, migrate_mutable_state
from wechat_exporter.models import ChatMessage, ContactChoice, ExportStats, MediaResult
from wechat_exporter.utils import write_json_atomic


def quote_xml(
    *, reply: str = "当前回复", quoted: str = "原消息", sender: str = "Synthetic Contact", quoted_type: str = "1"
) -> str:
    return (
        "<msg><appmsg><title>" + reply + "</title><type>57</type>"
        "<refermsg><type>" + quoted_type + "</type><displayname>" + sender
        + "</displayname><content>" + quoted + "</content></refermsg>"
        "</appmsg></msg>"
    )


class FakeDB:
    def __init__(self, rows):
        self.rows = list(rows)

    def get_messages(self, username, limit=1000, offset=0):
        ordered = sorted(self.rows, key=lambda row: row["sort_seq"], reverse=True)
        return ordered[offset:offset + limit]

    def get_new_messages(self, username, since_seq=0, limit=200):
        rows = [row for row in self.rows if row["sort_seq"] > since_seq]
        return sorted(rows, key=lambda row: row["sort_seq"])[:limit]


class MessageCoverageTests(unittest.TestCase):
    def message(self, content: str, *, quoted_type: str = "1") -> ChatMessage:
        return ChatMessage.from_upstream(
            {
                "local_id": 1,
                "type": "文件/链接/卡片",
                "sender_id": 3,
                "create_time": 1700000000,
                "content": content,
                "sort_seq": 1,
            }
        )

    def render(self, message: ChatMessage) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chat.md"
            stats = ExportStats(message_count=1)
            with MarkdownArchiveWriter(
                path, "Synthetic Contact", datetime.now().astimezone(),
                message.create_time, message.create_time,
            ) as writer:
                writer.write_message(message, "Synthetic Contact", None, stats)
            return path.read_text(encoding="utf-8")

    def render_with_media(self, message: ChatMessage, path: str) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "chat.md"
            with MarkdownArchiveWriter(
                target, "Synthetic Contact", datetime.now().astimezone(),
                message.create_time, message.create_time,
            ) as writer:
                writer.write_message(
                    message, "Synthetic Contact", MediaResult("success", path), ExportStats()
                )
            return target.read_text(encoding="utf-8")

    def test_quote_parser_builds_structured_normalized_payload(self):
        message = self.message(quote_xml())
        self.assertEqual(message.message_type, "引用消息")
        self.assertEqual(message.subtype, "57")
        self.assertEqual(message.content, "当前回复")
        self.assertEqual(message.payload["reference"]["text"], "原消息")
        self.assertNotIn("<appmsg>", json.dumps(message.to_json_dict(), ensure_ascii=False))

    def test_quote_renderer_writes_sender_quote_and_reply(self):
        text = self.render(self.message(quote_xml()))
        self.assertIn("> 引用 Synthetic Contact：", text)
        self.assertIn("> 原消息", text)
        self.assertIn("\n当前回复\n", text)

    def test_quoted_image_has_stable_placeholder_and_keeps_reply(self):
        text = self.render(self.message(quote_xml(quoted_type="3")))
        self.assertIn("> [图片]", text)
        self.assertIn("当前回复", text)

    def test_quote_special_characters_are_markdown_escaped(self):
        text = self.render(
            self.message(quote_xml(reply="*回复*", quoted="[原文](x)", sender="Synthetic Contact *"))
        )
        self.assertIn("引用 Synthetic Contact \\*：", text)
        self.assertIn("\\[原文\\](x)", text)
        self.assertIn("\\*回复\\*", text)

    def test_quote_missing_optional_sender_remains_readable(self):
        text = self.render(self.message(quote_xml(sender="")))
        self.assertIn("> 引用消息：", text)
        self.assertIn("> 原消息", text)

    def test_malformed_appmsg_is_safe_placeholder_and_does_not_leak(self):
        secret = "SECRET_RAW_PAYLOAD"
        message = self.message("<msg><appmsg>" + secret)
        text = self.render(message)
        self.assertEqual(message.content, "")
        self.assertIn("暂未支持的卡片消息", text)
        self.assertNotIn(secret, text)

    def test_nudge_is_recognized_but_remains_placeholder(self):
        parsed = parse_message_content(
            "文件/链接/卡片", "<msg><appmsg><type>62</type><patinfo /></appmsg></msg>"
        )
        message = ChatMessage("1", parsed.message_type, 3, "", 1, parsed.content, 1,
                              parsed.subtype, parsed.payload)
        text = self.render(message)
        self.assertEqual(message.message_type, "拍一拍")
        self.assertIn("暂未支持的拍一拍消息", text)

    def test_contact_card_uses_only_display_fields(self):
        message = ChatMessage.from_upstream({
            "local_id": 2, "type": 42, "sender_id": 3, "create_time": 1700000001,
            "sort_seq": 2,
            "content": '<msg nickname="Snow" alias="public-id" username="internal_private" '
                       'smallheadimgurl="https://remote.invalid/avatar" />',
        })
        text = self.render(message)
        self.assertEqual(message.message_type, "联系人名片")
        self.assertEqual(message.payload["nickname"], "Snow")
        self.assertNotIn("username", message.payload)
        self.assertIn("昵称：Snow", text)
        self.assertNotIn("remote.invalid", text)
        self.assertNotIn("internal_private", text)

    def test_malformed_contact_card_is_safe_placeholder(self):
        message = ChatMessage.from_upstream({
            "local_id": 20, "type": 42, "sender_id": 3, "create_time": 1700000020,
            "sort_seq": 20, "content": "<msg SECRET_CONTACT",
        })
        text = self.render(message)
        self.assertIn("无法解析的联系人名片", text)
        self.assertNotIn("SECRET_CONTACT", text)

    def test_location_parser_and_renderer_keep_name_address_and_coordinates(self):
        message = ChatMessage.from_upstream({
            "local_id": 3, "type": "位置", "sender_id": 3,
            "create_time": 1700000002, "sort_seq": 3,
            "content": '<msg><location x="35.1" y="139.2" scale="16" '
                       'poiname="Snow Park" label="North Gate" /></msg>',
        })
        text = self.render(message)
        self.assertEqual(message.payload["x"], "35.1")
        self.assertIn("📍 Snow Park", text)
        self.assertIn("地址：North Gate", text)
        self.assertIn("坐标：35.1, 139.2", text)

    def test_malformed_location_is_safe_placeholder(self):
        message = ChatMessage.from_upstream({
            "local_id": 21, "type": "位置", "sender_id": 3,
            "create_time": 1700000021, "sort_seq": 21, "content": "<msg SECRET_LOCATION",
        })
        text = self.render(message)
        self.assertIn("无法解析的位置消息", text)
        self.assertNotIn("SECRET_LOCATION", text)

    def test_link_card_renders_readable_fields_without_raw_xml(self):
        raw = (
            "<msg><appmsg><type>5</type><title>Example</title><des>Summary</des>"
            "<url>https://example.invalid/path</url><sourcedisplayname>Publisher"
            "</sourcedisplayname></appmsg></msg>"
        )
        message = self.message(raw)
        text = self.render(message)
        self.assertEqual(message.message_type, "链接卡片")
        self.assertIn("🔗 Example", text)
        self.assertIn("来源：Publisher", text)
        self.assertIn("<https://example.invalid/path>", text)
        self.assertNotIn("<appmsg>", text)

    def test_link_card_missing_optional_fields_stays_readable(self):
        message = self.message(
            "<msg><appmsg><type>5</type><title></title><des></des><url></url>"
            "</appmsg></msg>"
        )
        self.assertIn("未命名链接", self.render(message))

    def test_unsafe_link_scheme_is_not_emitted_as_clickable_link(self):
        message = self.message(
            "<msg><appmsg><type>5</type><title>Example</title>"
            "<url>javascript:alert(1)</url></appmsg></msg>"
        )
        self.assertNotIn("javascript:", self.render(message))

    def test_file_card_is_metadata_only_and_never_links_remote_body(self):
        message = self.message(
            "<msg><appmsg><type>6</type><title>sample.pdf</title><appattach>"
            "<totallen>1536</totallen><fileext>pdf</fileext>"
            "<cdnattachurl>https://remote.invalid/file</cdnattachurl>"
            "</appattach></appmsg></msg>"
        )
        text = self.render(message)
        self.assertEqual(message.message_type, "文件")
        self.assertIn("文件：sample.pdf", text)
        self.assertIn("大小：1.5 KB", text)
        self.assertIn("文件正文未归档", text)
        self.assertNotIn("remote.invalid", json.dumps(message.to_json_dict()))

    def test_file_card_missing_fields_has_safe_fallback(self):
        message = self.message("<msg><appmsg><type>6</type><appattach /></appmsg></msg>")
        text = self.render(message)
        self.assertIn("未命名文件", text)
        self.assertIn("大小：未知", text)

    def test_mini_program_card_preserves_structure_but_not_remote_thumbnail(self):
        message = self.message(
            "<msg><appmsg><type>33</type><title>Mini Snow</title>"
            "<url>https://example.invalid/mini</url><thumburl>https://remote.invalid/t.jpg</thumburl>"
            "<weappinfo><appid>app-id</appid><pagepath>pages/home</pagepath></weappinfo>"
            "</appmsg></msg>"
        )
        text = self.render(message)
        self.assertEqual(message.message_type, "小程序卡片")
        self.assertEqual(message.payload["page_path"], "pages/home")
        self.assertIn("小程序：Mini Snow", text)
        self.assertIn("缩略图未归档", text)
        self.assertNotIn("remote.invalid", json.dumps(message.to_json_dict()))

    def test_mini_program_missing_fields_has_safe_fallback(self):
        message = self.message("<msg><appmsg><type>33</type><weappinfo /></appmsg></msg>")
        self.assertIn("未命名小程序", self.render(message))

    def test_real_video_shape_renders_local_playback_reference(self):
        message = ChatMessage.from_upstream({
            "local_id": 4, "type": "视频", "sender_id": 3,
            "create_time": 1700000003, "sort_seq": 4, "content": "<msg><videomsg /></msg>",
        })
        text = self.render_with_media(message, "assets/videos/video.mp4")
        self.assertIn("[▶ 播放视频](assets/videos/video.mp4)", text)

    def test_video_without_local_media_has_explicit_fallback(self):
        message = ChatMessage.from_upstream({
            "local_id": 5, "type": "视频", "sender_id": 3,
            "create_time": 1700000004, "sort_seq": 5, "content": "<msg><videomsg /></msg>",
        })
        self.assertIn("视频文件未能归档", self.render(message))

    def test_new_structured_message_types_round_trip(self):
        rows = [
            {"local_id": 30, "type": 42, "sender_id": 3, "create_time": 1,
             "sort_seq": 30, "content": '<msg nickname="Snow" alias="id" />'},
            {"local_id": 31, "type": "位置", "sender_id": 3, "create_time": 2,
             "sort_seq": 31,
             "content": '<msg><location x="35" y="139" poiname="Park" /></msg>'},
            {"local_id": 32, "type": "文件/链接/卡片", "sender_id": 3,
             "create_time": 3, "sort_seq": 32,
             "content": '<msg><appmsg><type>5</type><title>Link</title></appmsg></msg>'},
            {"local_id": 33, "type": "文件/链接/卡片", "sender_id": 3,
             "create_time": 4, "sort_seq": 33,
             "content": '<msg><appmsg><type>6</type><title>File</title></appmsg></msg>'},
            {"local_id": 34, "type": "文件/链接/卡片", "sender_id": 3,
             "create_time": 5, "sort_seq": 34,
             "content": '<msg><appmsg><type>33</type><title>Mini</title></appmsg></msg>'},
        ]
        for row in rows:
            original = ChatMessage.from_upstream(row)
            self.assertEqual(ChatMessage.from_json_dict(original.to_json_dict()), original)

    def test_normalized_round_trip_preserves_quote_structure(self):
        original = self.message(quote_xml())
        restored = ChatMessage.from_json_dict(original.to_json_dict())
        self.assertEqual(restored, original)

    def test_encoded_appmsg_type_is_derived_not_guessed(self):
        raw = (57 << 32) | 49
        self.assertEqual(canonical_type(raw), 49)
        self.assertEqual(encoded_subtype(raw), "57")

    def test_v1_message_state_migrates_offline_without_snapshot_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "target"
            (root / "state").mkdir(parents=True)
            (root / "snapshots" / "old").mkdir(parents=True)
            (root / "snapshots" / "old" / "chat.md").write_text("immutable", encoding="utf-8")
            for name, payload in (
                ("target.json", {"schema_version": 2, "username": "wxid", "display_name": "Synthetic Contact"}),
                ("archive_state.json", {"schema_version": 2, "message_count": 1,
                                        "last_sort_seq": 1, "last_message_identity": "local:1"}),
                ("snapshot_index.json", {"schema_version": 2, "snapshots": []}),
            ):
                write_json_atomic(root / name, payload)
            write_json_atomic(root / "state" / "schema.json", {"schema_version": 1})
            old = self.message(quote_xml()).to_json_dict()
            old["message_type"] = "文件/链接/卡片"
            old["content"] = quote_xml()
            old.pop("subtype")
            old.pop("payload")
            (root / "state" / "messages.jsonl").write_text(
                json.dumps({"identity": "local:1", **old, "media": None}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            before = snapshot_hashes(root / "snapshots" / "old")
            result = migrate_mutable_state(root)
            records = load_message_store(root / "state" / "messages.jsonl")
            self.assertIn("message_v1_to_v2", result.applied)
            self.assertEqual(records[0].message.message_type, "引用消息")
            self.assertEqual(snapshot_hashes(root / "snapshots" / "old"), before)
            schema = json.loads((root / "state" / "schema.json").read_text(encoding="utf-8"))
            self.assertEqual(schema["schema_version"], CURRENT_MESSAGE_SCHEMA_VERSION)

    def test_quote_snapshot_integrity_delete_and_rebuild(self):
        row = {
            "local_id": 1, "type": "文件/链接/卡片", "sender_id": 3,
            "create_time": 1700000000, "content": quote_xml(), "sort_seq": 1,
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "work").mkdir()
            target = create_target(
                base / "exports", ContactChoice.from_fields(username="wxid", remark="Synthetic Contact")
            )
            db = FakeDB([row])
            with patch(
                "wechat_exporter.archive._capture_avatars",
                return_value={},
            ):
                first = create_snapshot(db, target, "me", base / "work", logging.getLogger())
                self.assertTrue(verify_snapshot_offline(target.root, first.snapshot_id).ok)
                self.assertIn("引用 Synthetic Contact", (first.path / "chat.md").read_text(encoding="utf-8"))
                delete_snapshot_transaction(target, first.snapshot_id)
                second = create_snapshot(db, target, "me", base / "work", logging.getLogger())
            self.assertTrue(verify_snapshot_offline(target.root, second.snapshot_id).ok)
            self.assertIn("当前回复", (second.path / "chat.md").read_text(encoding="utf-8"))
            self.assertIn("> 原消息", (second.path / "chat.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
