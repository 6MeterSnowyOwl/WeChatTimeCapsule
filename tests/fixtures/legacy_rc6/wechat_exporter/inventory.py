from __future__ import annotations

import hashlib
import json
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

TYPE_LABELS = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "sticker",
    48: "location",
    49: "app_message",
    10000: "system",
}


def canonical_type(raw_type: int) -> int:
    return raw_type & 0xFF if raw_type > 0xFFFF else raw_type


def encoded_subtype(raw_type: int) -> str:
    return str(raw_type >> 32) if raw_type > 0xFFFFFFFF else ""


def _decoded_content(db, row: Any, canonical: int) -> str:
    message_type = db._msg_type_name(canonical)
    for field in ("message_content", "compress_content"):
        value = row[field]
        if not value:
            continue
        if isinstance(value, bytes):
            value = db._friendly_content(value, message_type)
        text = str(value).strip()
        if text and text != f"[{message_type}]":
            return text
    return ""


def _app_subtype(content: str, raw_type: int) -> tuple[str, bool]:
    text = content.lstrip("\ufeff\x00 \t\r\n")
    start = text.find("<")
    if start >= 0:
        try:
            root = ET.fromstring(text[start:])
            value = str(root.findtext(".//appmsg/type") or "").strip()
            if value:
                return value, True
        except ET.ParseError:
            pass
    return encoded_subtype(raw_type), False


def _classification(canonical: int, subtype: str) -> tuple[str, str, str]:
    if canonical == 49 and subtype == "57":
        return "quote", "fully_supported", "A"
    if canonical == 49 and subtype == "62":
        return "nudge", "placeholder", "A"
    if canonical in {1, 10000}:
        return TYPE_LABELS[canonical], "fully_supported", "A"
    if canonical == 42:
        return "contact_card", "fully_supported", "A"
    if canonical == 48:
        return "location", "fully_supported", "A"
    if canonical == 3:
        return "image", "fully_supported", "B"
    if canonical == 43:
        return "video", "fully_supported", "B"
    if canonical == 49 and subtype in {"4", "5"}:
        return "link_card", "partial", "A"
    if canonical == 49 and subtype == "6":
        return "file", "partial", "B"
    if canonical == 49 and subtype == "33":
        return "mini_program", "partial", "B"
    if canonical in {34, 47}:
        return TYPE_LABELS[canonical], "placeholder", "B"
    return TYPE_LABELS.get(canonical, "unknown"), "placeholder", "C"


def build_message_coverage_inventory(db, username: str) -> dict[str, Any]:
    found = db._msg_conn(username)
    if found is None:
        raise RuntimeError("conversation table not found")
    connection, table = found
    try:
        rows = connection.execute(
            "SELECT local_id, local_type, message_content, compress_content, "
            f"packed_info_data FROM {table} ORDER BY sort_seq ASC"
        ).fetchall()
    finally:
        connection.close()

    chat_hash = hashlib.md5(username.encode("utf-8")).hexdigest()
    image_root = Path(db.account_dir) / "msg" / "attach" / chat_hash
    image_names = {
        path.name.casefold() for path in image_root.rglob("*.dat") if path.is_file()
    } if image_root.is_dir() else set()

    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    from wechatauto.media import MediaDownloader

    media = MediaDownloader(db)
    with tempfile.TemporaryDirectory(prefix="wechat-inventory-") as temporary:
        for row in rows:
            raw_type = int(row["local_type"] or 0)
            canonical = canonical_type(raw_type)
            content = _decoded_content(db, row, canonical)
            subtype, xml_ok = _app_subtype(content, raw_type) if canonical == 49 else ("", True)
            kind, support, tier = _classification(canonical, subtype)
            key = (canonical, subtype)
            entry = grouped.setdefault(
                key,
                {
                "message_type": canonical,
                "subtype": subtype or None,
                "kind": kind,
                "count": 0,
                "raw_type_variants": set(),
                "current_support": support,
                "tier": tier,
                "parser_can_read_structure": True,
                "local_payload_available_count": 0,
                "local_media_available_count": 0,
                "local_media_candidate_count": 0,
                "requires_external_network": False,
                "requires_extra_decryption": (
                    True if canonical == 3 else None if canonical == 47 else False
                ),
                },
            )
            entry["count"] += 1
            entry["raw_type_variants"].add(raw_type)
            if content:
                entry["local_payload_available_count"] += 1
            if canonical == 49 and not xml_ok:
                entry["parser_can_read_structure"] = False
            if canonical == 3:
                probe = {
                    "packed_info": row["packed_info_data"],
                    "content": row["message_content"],
                }
                digest = media._img_md5(probe)
                if digest and (
                    digest.casefold() + ".dat" in image_names
                    or digest.casefold() + "_t.dat" in image_names
                ):
                    entry["local_media_candidate_count"] += 1
            method = None
            if canonical == 34:
                method = media.download_voice
            elif canonical == 43:
                method = media.download_video
            elif canonical == 49 and subtype == "6":
                method = media.download_file
            if method is not None:
                try:
                    restored = method(username, int(row["local_id"]), temporary)
                except Exception:
                    restored = None
                if restored and Path(restored).is_file() and Path(restored).stat().st_size > 0:
                    entry["local_media_available_count"] += 1

    result = []
    for key in sorted(grouped):
        entry = grouped[key]
        entry["raw_type_variants"] = sorted(entry["raw_type_variants"])
        entry["local_payload_available"] = (
            entry["local_payload_available_count"] == entry["count"]
        )
        media_relevant = (
            entry["message_type"] in {3, 34, 43, 47}
            or (entry["message_type"] == 49 and entry["subtype"] == "6")
        )
        entry["local_media_available"] = (
            entry["local_media_available_count"] == entry["count"]
            if media_relevant
            else None
        )
        result.append(entry)

    return {
        "schema_version": 1,
        "privacy": "aggregate counts only; no content or identity fields",
        "total_messages": len(rows),
        "types": result,
        "tier_b_capability_research": {
            "video": {
                "present_in_sample": any(item["kind"] == "video" for item in result),
                "upstream_local_api": "download_video",
                "local_format": "mp4 when already cached",
                "network_required": False,
                "extra_decryption": False,
                "codec_required": False,
                "v0_2_1_status": "fully supported for the validated locally cached MP4 sample",
            },
            "voice": {
                "present_in_sample": any(item["kind"] == "voice" for item in result),
                "upstream_local_api": "download_voice",
                "local_format": "silk",
                "network_required": False,
                "extra_decryption": False,
                "codec_required": True,
                "v0_2_1_status": "placeholder; local output is SILK and no archival codec is bundled",
            },
            "sticker": {
                "present_in_sample": any(item["kind"] == "sticker" for item in result),
                "upstream_local_api": None,
                "local_format": None,
                "network_required": False,
                "extra_decryption": None,
                "codec_required": None,
                "v0_2_1_status": "deferred_ui_dependent; upstream only exposes a WeChat UI screenshot path",
            },
            "file": {
                "present_in_sample": any(item["kind"] == "file" for item in result),
                "upstream_local_api": "download_file",
                "local_format": "original file when already cached",
                "network_required": False,
                "extra_decryption": False,
                "codec_required": False,
                "v0_2_1_status": "partial metadata only; 0 real file bodies restored locally",
            },
        },
    }


def write_inventory(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
