from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import AvatarSnapshot, ChatMessage, ContactChoice, ExportStats, MediaResult
from .utils import escape_markdown


PLACEHOLDER_LABELS = {
    "语音": "语音消息",
    "视频": "视频消息",
    "动画表情": "动画表情",
    "文件/链接/卡片": "卡片消息",
    "拍一拍": "拍一拍消息",
    "位置": "位置消息",
    "联系人名片": "联系人名片",
    "链接卡片": "链接卡片",
    "小程序卡片": "小程序卡片",
    "文件": "文件消息",
}


def _safe_web_url(value: object) -> str:
    url = str(value or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return ""
    if any(character in url for character in "\r\n\x00<>"):
        return ""
    return url


def _human_size(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "未知"


class MarkdownArchiveWriter:
    def __init__(
        self,
        path: Path,
        chat_name: str,
        exported_at: datetime,
        first_time: int | None,
        last_time: int | None,
        avatars: dict[str, AvatarSnapshot] | None = None,
    ):
        self.path = path
        self.chat_name = chat_name
        self.exported_at = exported_at
        self.first_time = first_time
        self.last_time = last_time
        self.avatars = avatars or {}
        self._handle = None
        self._date = None

    def __enter__(self) -> "MarkdownArchiveWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        first = datetime.fromtimestamp(self.first_time).strftime("%Y-%m-%d") if self.first_time else "无"
        last = datetime.fromtimestamp(self.last_time).strftime("%Y-%m-%d") if self.last_time else "无"
        self._handle.write(f"# 与 {escape_markdown(self.chat_name)} 的聊天记录\n\n")
        self._handle.write(f"> 导出时间：{self.exported_at.strftime('%Y-%m-%d %H:%M')}\n")
        self._handle.write(f"> 消息范围：{first} 至 {last}\n\n---\n")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle:
            self._handle.close()

    def write_message(
        self,
        message: ChatMessage,
        sender_name: str,
        media_result: MediaResult | None,
        stats: ExportStats,
    ) -> None:
        assert self._handle is not None
        stamp = message.timestamp
        date = stamp.strftime("%Y-%m-%d")
        if date != self._date:
            self._handle.write(f"\n## {date}\n")
            self._date = date
        self._handle.write("\n" + self._render_sender(message, sender_name, stamp) + "\n\n")

        if message.message_type == "文本":
            self._handle.write(escape_markdown(message.content) or "[空文本消息]")
            self._handle.write("\n")
            self._count(stats.fully_supported_counts, "文字")
        elif message.message_type == "图片":
            if media_result and media_result.relative_path:
                self._handle.write(f"![图片]({media_result.relative_path})\n")
                self._count(stats.fully_supported_counts, "图片")
            else:
                self._handle.write("> ⚠ 图片未能导出\n")
                self._count(stats.partial_support_counts, "图片")
        elif message.message_type == "系统消息":
            content = escape_markdown(message.content) or "[空系统消息]"
            self._handle.write("\n".join("> " + line for line in content.splitlines()) + "\n")
            self._count(stats.fully_supported_counts, "系统消息")
        elif message.message_type == "引用消息":
            self._write_quote(message)
            self._count(stats.fully_supported_counts, "引用/回复")
        elif message.message_type == "视频":
            if media_result and media_result.relative_path:
                self._handle.write(f"[▶ 播放视频]({media_result.relative_path})\n")
                self._count(stats.fully_supported_counts, "视频")
            else:
                self._handle.write("> ⚠ 视频文件未能归档\n")
                self._count(stats.partial_support_counts, "视频")
        elif message.message_type == "联系人名片":
            self._write_contact_card(message)
            bucket = (
                stats.fully_supported_counts
                if (message.payload or {}).get("parse_status") != "malformed"
                else stats.placeholder_counts
            )
            self._count(bucket, "联系人名片")
            if bucket is stats.placeholder_counts:
                stats.unsupported_message_count += 1
        elif message.message_type == "位置":
            self._write_location(message)
            bucket = (
                stats.fully_supported_counts
                if (message.payload or {}).get("parse_status") != "malformed"
                else stats.placeholder_counts
            )
            self._count(bucket, "位置")
            if bucket is stats.placeholder_counts:
                stats.unsupported_message_count += 1
        elif message.message_type == "链接卡片":
            self._write_link_card(message)
            self._count(stats.partial_support_counts, "链接卡片")
        elif message.message_type == "文件":
            self._write_file(message)
            self._count(stats.partial_support_counts, "文件")
        elif message.message_type == "小程序卡片":
            self._write_mini_program(message)
            self._count(stats.partial_support_counts, "小程序卡片")
        else:
            label = PLACEHOLDER_LABELS.get(message.message_type, "消息类型")
            self._handle.write(f"> [暂未支持的{label}]\n")
            stats.unsupported_message_count += 1
            self._count(stats.placeholder_counts, label)

    def _write_contact_card(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        if payload.get("parse_status") == "malformed":
            self._handle.write("> [无法解析的联系人名片]\n")
            return
        nickname = escape_markdown(str(payload.get("nickname") or "").strip())
        self._handle.write("> 👤 联系人名片\n")
        self._handle.write(f"> 昵称：{nickname or '未提供'}\n")

    def _write_location(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        if payload.get("parse_status") == "malformed":
            self._handle.write("> [无法解析的位置消息]\n")
            return
        name = escape_markdown(str(payload.get("name") or "").strip())
        address = escape_markdown(str(payload.get("address") or "").strip())
        x = escape_markdown(str(payload.get("x") or "").strip())
        y = escape_markdown(str(payload.get("y") or "").strip())
        self._handle.write(f"> 📍 {name or '位置'}\n")
        if address:
            self._handle.write(f"> 地址：{address}\n")
        if x and y:
            self._handle.write(f"> 坐标：{x}, {y}\n")

    def _write_link_card(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        title = escape_markdown(str(payload.get("title") or "").strip()) or "未命名链接"
        source = escape_markdown(str(payload.get("source_name") or "").strip())
        description = escape_markdown(str(payload.get("description") or "").strip())
        url = _safe_web_url(payload.get("url"))
        self._handle.write(f"> 🔗 {title}\n")
        if source:
            self._handle.write(f"> 来源：{source}\n")
        if description:
            self._handle.write(f"> {description}\n")
        if url:
            self._handle.write(f"> <{url}>\n")

    def _write_file(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        filename = escape_markdown(str(payload.get("filename") or "").strip()) or "未命名文件"
        size = _human_size(payload.get("size_bytes"))
        self._handle.write(f"> 📎 文件：{filename}\n")
        self._handle.write(f"> 大小：{size}\n")
        self._handle.write("> ⚠ 文件正文未归档，仅保留卡片元数据\n")

    def _write_mini_program(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        title = escape_markdown(str(payload.get("title") or "").strip()) or "未命名小程序"
        app_name = escape_markdown(str(payload.get("app_name") or "").strip())
        description = escape_markdown(str(payload.get("description") or "").strip())
        self._handle.write(f"> ◈ 小程序：{title}\n")
        if app_name:
            self._handle.write(f"> 来源：{app_name}\n")
        if description:
            self._handle.write(f"> {description}\n")
        self._handle.write("> ⚠ 缩略图未归档\n")

    @staticmethod
    def _count(bucket: dict[str, int], label: str) -> None:
        bucket[label] = bucket.get(label, 0) + 1

    def _write_quote(self, message: ChatMessage) -> None:
        assert self._handle is not None
        payload = message.payload or {}
        reference = payload.get("reference") or {}
        sender = escape_markdown(str(reference.get("sender_name") or "").strip())
        heading = f"引用 {sender}：" if sender else "引用消息："
        self._handle.write(f"> {heading}\n")
        kind = str(reference.get("kind") or "unknown")
        if kind == "text":
            quoted = escape_markdown(str(reference.get("text") or "")) or "[空文本消息]"
        else:
            quoted = {
                "image": "[图片]",
                "voice": "[语音消息]",
                "video": "[视频消息]",
                "sticker": "[动画表情]",
                "location": "[位置消息]",
                "card": "[卡片消息]",
            }.get(kind, "[原消息类型暂未识别]")
        for line in quoted.splitlines() or [quoted]:
            self._handle.write(f"> {line}\n")
        self._handle.write("\n")
        self._handle.write(escape_markdown(message.content) or "[空回复消息]")
        self._handle.write("\n")

    def _render_sender(self, message: ChatMessage, sender_name: str, stamp: datetime) -> str:
        role = "self" if message.sender_id == 2 or str(message.sender_id) == "2" else "contact"
        avatar = self.avatars.get(role)
        time_text = stamp.strftime("%H:%M")
        if avatar and avatar.relative_path:
            source = html.escape(avatar.relative_path, quote=True)
            name = html.escape(sender_name, quote=False)
            return (
                f'<img src="{source}" width="28" height="28" alt="avatar"> '
                f"<b>{name}</b>　<sub>{time_text}</sub>"
            )
        return f"👤 **{escape_markdown(sender_name)}**　{time_text}"


def sender_resolver(contact: ContactChoice) -> Callable[[ChatMessage], str]:
    """Resolve participants for a confirmed one-to-one conversation.

    Upstream ``sender_username`` can contain an unresolved numeric
    ``real_sender_id``.  The selected contact is the canonical identity for
    every non-self message in this one-to-one archive.
    """
    def resolve(message: ChatMessage) -> str:
        if message.sender_id == 2 or str(message.sender_id) == "2":
            return "我"
        return contact.display_name

    return resolve
