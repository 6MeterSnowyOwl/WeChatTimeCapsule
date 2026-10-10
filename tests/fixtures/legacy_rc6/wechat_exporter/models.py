from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .message_types import parse_message_content


@dataclass(frozen=True)
class ChatMessage:
    local_id: str
    message_type: str
    sender_id: int | str | None
    sender_username: str
    create_time: int
    content: str
    sort_seq: int
    subtype: str = ""
    payload: dict[str, Any] | None = None

    @classmethod
    def from_upstream(cls, raw: dict[str, Any]) -> "ChatMessage":
        create_time = int(raw.get("create_time") or 0)
        local_id = raw.get("local_id")
        sort_seq = int(raw.get("sort_seq") or 0)
        stable_id = str(local_id if local_id is not None else sort_seq)
        content = raw.get("content", "")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        elif content is None:
            content = ""
        else:
            content = str(content)
        parsed = parse_message_content(str(raw.get("type") or "未知"), content)
        return cls(
            local_id=stable_id,
            message_type=parsed.message_type,
            sender_id=raw.get("sender_id"),
            sender_username=str(raw.get("sender_username") or ""),
            create_time=create_time,
            content=parsed.content,
            sort_seq=sort_seq,
            subtype=parsed.subtype,
            payload=parsed.payload,
        )

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.create_time)

    @property
    def stable_key(self) -> tuple[int, int, str]:
        return (self.sort_seq, self.create_time, self.local_id)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "local_id": self.local_id,
            "message_type": self.message_type,
            "sender_id": self.sender_id,
            "sender_username": self.sender_username,
            "create_time": self.create_time,
            "content": self.content,
            "sort_seq": self.sort_seq,
            "subtype": self.subtype,
            "payload": self.payload,
        }

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> "ChatMessage":
        if "subtype" in raw and "payload" in raw:
            return cls(**raw)
        return cls.from_upstream(
            {
                "local_id": raw.get("local_id"),
                "type": raw.get("message_type"),
                "sender_id": raw.get("sender_id"),
                "sender_username": raw.get("sender_username"),
                "create_time": raw.get("create_time"),
                "content": raw.get("content"),
                "sort_seq": raw.get("sort_seq"),
            }
        )


@dataclass(frozen=True)
class ContactChoice:
    username: str
    display_name: str
    nickname: str = ""
    remark: str = ""
    alias: str = ""

    @classmethod
    def from_fields(
        cls,
        *,
        username: object,
        nickname: object = "",
        remark: object = "",
        alias: object = "",
    ) -> "ContactChoice":
        normalized_username = str(username or "").strip()
        normalized_nickname = str(nickname or "").strip()
        normalized_remark = str(remark or "").strip()
        normalized_alias = str(alias or "").strip()
        return cls(
            username=normalized_username,
            display_name=resolve_contact_display_name(
                remark=normalized_remark,
                nickname=normalized_nickname,
                alias=normalized_alias,
                username=normalized_username,
            ),
            nickname=normalized_nickname,
            remark=normalized_remark,
            alias=normalized_alias,
        )


def resolve_contact_display_name(
    *,
    remark: object = "",
    nickname: object = "",
    alias: object = "",
    username: object = "",
) -> str:
    """Return the canonical user-visible name for a one-to-one contact."""
    for value in (remark, nickname, alias, username):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return "未知联系人"


@dataclass(frozen=True)
class MediaResult:
    status: str
    relative_path: str | None = None
    reason: str = ""


# Kept as a compatibility alias for callers and older tests.
ImageResult = MediaResult


@dataclass(frozen=True)
class AvatarSnapshot:
    status: str
    relative_path: str | None = None
    remote_available: bool = False


@dataclass
class ExportStats:
    message_count: int = 0
    image_count: int = 0
    thumbnail_count: int = 0
    failed_image_count: int = 0
    unsupported_message_count: int = 0
    duplicate_message_id_count: int = 0
    text_message_count: int = 0
    image_message_count: int = 0
    message_type_counts: dict[str, int] = field(default_factory=dict)
    fully_supported_counts: dict[str, int] = field(default_factory=dict)
    partial_support_counts: dict[str, int] = field(default_factory=dict)
    placeholder_counts: dict[str, int] = field(default_factory=dict)
    sender_counts: dict[str, int] = field(
        default_factory=lambda: {"self": 0, "contact": 0, "unknown": 0}
    )
    markdown_order_verified: bool = True
    first_message_time: int | None = None
    last_message_time: int | None = None
    export_dir: Path | None = None
