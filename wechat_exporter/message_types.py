from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


APP_MESSAGE_LABEL = "文件/链接/卡片"


@dataclass(frozen=True)
class ParsedMessageContent:
    message_type: str
    content: str
    subtype: str = ""
    payload: dict[str, Any] | None = None


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    value = node.findtext(path)
    return str(value or "").strip()


def _attribute(node: ET.Element | None, name: str) -> str:
    if node is None:
        return ""
    return str(node.attrib.get(name) or "").strip()


def _integer(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _xml_root(content: str) -> ET.Element | None:
    text = content.lstrip("\ufeff\x00 \t\r\n")
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except (ET.ParseError, ValueError):
        return None


def _quoted_kind(value: str) -> str:
    return {
        "1": "text",
        "3": "image",
        "34": "voice",
        "43": "video",
        "47": "sticker",
        "48": "location",
        "49": "card",
    }.get(value, "unknown")


def parse_message_content(message_type: str, content: str) -> ParsedMessageContent:
    """Convert supported WeChat payloads into a stable, display-oriented shape.

    Raw app-message XML is deliberately not copied into the normalized display
    record. Unknown subtypes retain their stable subtype only and remain a
    placeholder until a real local payload is validated.
    """
    if message_type == "42":
        root = _xml_root(content)
        if root is None:
            return ParsedMessageContent(
                "联系人名片", "", payload={"kind": "contact_card", "parse_status": "malformed"}
            )
        return ParsedMessageContent(
            "联系人名片",
            "",
            payload={
                "kind": "contact_card",
                "nickname": _attribute(root, "nickname"),
                "alias": _attribute(root, "alias"),
            },
        )

    if message_type == "位置":
        root = _xml_root(content)
        location = None if root is None else root.find(".//location")
        if root is not None and root.tag == "location":
            location = root
        if location is None:
            return ParsedMessageContent(
                "位置", "", payload={"kind": "location", "parse_status": "malformed"}
            )
        return ParsedMessageContent(
            "位置",
            "",
            payload={
                "kind": "location",
                "name": _attribute(location, "poiname"),
                "address": _attribute(location, "label"),
                # The controlled sample confirms x is latitude-range and y is
                # longitude-range. Keep the source names in the normalized data.
                "x": _attribute(location, "x"),
                "y": _attribute(location, "y"),
                "scale": _attribute(location, "scale"),
            },
        )

    if message_type != APP_MESSAGE_LABEL:
        return ParsedMessageContent(message_type, content)

    root = _xml_root(content)
    if root is None:
        return ParsedMessageContent(
            APP_MESSAGE_LABEL,
            "",
            payload={"kind": "unsupported_app_message", "parse_status": "malformed"},
        )
    appmsg = root.find(".//appmsg")
    if appmsg is None and root.tag == "appmsg":
        appmsg = root
    subtype = _text(appmsg, "type")

    if subtype in {"4", "5"}:
        return ParsedMessageContent(
            "链接卡片",
            "",
            subtype,
            {
                "kind": "link_card",
                "title": _text(appmsg, "title"),
                "description": _text(appmsg, "des"),
                "url": _text(appmsg, "url"),
                "source_name": _text(appmsg, "sourcedisplayname")
                or _text(appmsg, "appinfo/appname"),
            },
        )

    if subtype == "6":
        size = _integer(_text(appmsg, "appattach/totallen"))
        return ParsedMessageContent(
            "文件",
            "",
            subtype,
            {
                "kind": "file",
                "filename": _text(appmsg, "title"),
                "extension": _text(appmsg, "appattach/fileext"),
                "size_bytes": size,
                "body_status": "not_archived",
            },
        )

    if subtype == "33":
        return ParsedMessageContent(
            "小程序卡片",
            "",
            subtype,
            {
                "kind": "mini_program",
                "title": _text(appmsg, "title"),
                "description": _text(appmsg, "des"),
                "url": _text(appmsg, "url"),
                "app_name": _text(appmsg, "appinfo/appname"),
                "app_id": _text(appmsg, "weappinfo/appid"),
                "page_path": _text(appmsg, "weappinfo/pagepath"),
                "thumbnail_status": "not_archived",
            },
        )

    if subtype == "57":
        refer = appmsg.find("refermsg") if appmsg is not None else None
        reference_type = _text(refer, "type")
        reference_kind = _quoted_kind(reference_type)
        reference: dict[str, Any] = {
            "kind": reference_kind,
            "sender_name": _text(refer, "displayname"),
        }
        if reference_kind == "text":
            reference["text"] = _text(refer, "content")
        reply = _text(appmsg, "title")
        return ParsedMessageContent(
            "引用消息",
            reply,
            subtype,
            {"kind": "quote", "reference": reference},
        )

    if subtype == "62":
        return ParsedMessageContent(
            "拍一拍",
            "",
            subtype,
            {"kind": "nudge", "support": "placeholder"},
        )

    return ParsedMessageContent(
        APP_MESSAGE_LABEL,
        "",
        subtype,
        {"kind": "unsupported_app_message", "parse_status": "parsed"},
    )


def coverage_bucket(message_type: str, payload: dict[str, Any] | None = None) -> str:
    if message_type in {
        "文本", "图片", "系统消息", "引用消息", "视频", "位置", "联系人名片"
    }:
        return "fully_supported"
    if message_type in {"链接卡片", "文件", "小程序卡片"}:
        return "partial"
    if payload and payload.get("kind") == "nudge":
        return "placeholder"
    return "placeholder"
