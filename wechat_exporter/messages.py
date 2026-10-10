from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .models import ChatMessage


MESSAGE_REQUIRED_COLUMNS = (
    "local_id",
    "local_type",
    "real_sender_id",
    "create_time",
    "message_content",
    "source",
    "packed_info_data",
    "compress_content",
    "sort_seq",
)

MESSAGE_PUBLIC_FIELDS = (
    "local_id",
    "type",
    "sender_id",
    "sender_username",
    "create_time",
    "content",
    "sort_seq",
)


@dataclass(frozen=True)
class MessageSpool:
    pages: tuple[Path, ...]
    count: int
    first_time: int | None
    last_time: int | None
    duplicate_message_id_count: int = 0


def verify_public_pagination_scope(db, username: str) -> int | None:
    """Fail closed if upstream 1.2.1 stores one chat table in multiple shards.

    ``get_messages`` is the required public API and currently selects the first
    shard containing the chat table.  The check reads schema names only through
    upstream's already-decrypted, read-only connections; it never reads raw
    WeChat files or handles database keys here.
    """
    message_dbs = getattr(db, "_message_dbs", None)
    open_db = getattr(db, "_open", None)
    if not callable(message_dbs) or not callable(open_db):
        return None
    table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
    matching_paths: list[str] = []
    for relative_path in message_dbs():
        connection = open_db(relative_path)
        try:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
        finally:
            connection.close()
        if found:
            matching_paths.append(relative_path)
    if len(matching_paths) > 1:
        raise RuntimeError(
            "该会话表存在于多个消息分片，而上游 1.2.1 的公开 get_messages() "
            "不能证明已合并全部分片；为避免生成不完整归档，本次导出已停止。"
        )
    if matching_paths:
        connection = open_db(matching_paths[0])
        try:
            escaped = table.replace('"', '""')
            columns = {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{escaped}")', ()
                ).fetchall()
            }
        finally:
            connection.close()
        if not set(MESSAGE_REQUIRED_COLUMNS).issubset(columns):
            raise RuntimeError(
                "微信消息表字段结构与当前读取器不兼容；本次导出已停止。"
            )
    return len(matching_paths)


def spool_all_messages(
    db,
    username: str,
    spool_dir: Path,
    page_size: int = 1000,
    progress: Callable[[int], None] | None = None,
) -> MessageSpool:
    """Page newest-to-oldest into JSONL files, keeping only one page in memory."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    page_paths: list[Path] = []
    seen: set[tuple[int, int, str]] = set()
    seen_message_ids: set[str] = set()
    duplicate_message_id_count = 0
    count = 0
    first_time: int | None = None
    last_time: int | None = None
    offset = 0
    previous_signature: tuple[tuple[int, int, str], ...] | None = None

    while True:
        raw_page = db.get_messages(username, limit=page_size, offset=offset)
        if not raw_page:
            break
        messages = [ChatMessage.from_upstream(item) for item in raw_page]
        messages.sort(key=lambda item: item.stable_key, reverse=True)
        signature = tuple(item.stable_key for item in messages)
        if signature == previous_signature:
            raise RuntimeError("上游分页接口重复返回同一页，已停止以避免无限循环。")
        previous_signature = signature

        unique = []
        for message in messages:
            if message.local_id in seen_message_ids:
                duplicate_message_id_count += 1
                continue
            seen_message_ids.add(message.local_id)
            if message.stable_key in seen:
                continue
            seen.add(message.stable_key)
            unique.append(message)
            count += 1
            if first_time is None or message.create_time < first_time:
                first_time = message.create_time
            if last_time is None or message.create_time > last_time:
                last_time = message.create_time

        if unique:
            page_path = spool_dir / f"page-{len(page_paths):06d}.jsonl"
            with page_path.open("w", encoding="utf-8", newline="\n") as handle:
                for message in unique:
                    handle.write(json.dumps(message.to_json_dict(), ensure_ascii=False) + "\n")
            page_paths.append(page_path)

        offset += len(raw_page)
        if progress:
            progress(count)
        if len(raw_page) < page_size:
            break

    return MessageSpool(
        tuple(page_paths), count, first_time, last_time, duplicate_message_id_count
    )


def iter_oldest_first(spool: MessageSpool) -> Iterator[ChatMessage]:
    for page_path in reversed(spool.pages):
        with page_path.open("r", encoding="utf-8") as handle:
            page = [ChatMessage.from_json_dict(json.loads(line)) for line in handle if line.strip()]
        page.sort(key=lambda item: item.stable_key)
        yield from page
