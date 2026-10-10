from __future__ import annotations

from typing import Callable

import os

from .models import ContactChoice


CONTACT_REQUIRED_COLUMNS = ("username", "nick_name", "remark", "alias")


def contact_choice_from_search_result(raw: dict, alias: str = "") -> ContactChoice:
    return ContactChoice.from_fields(
        username=raw.get("username"),
        nickname=raw.get("nick_name") or raw.get("nickname"),
        remark=raw.get("remark"),
        alias=alias,
    )


def _aliases_for_matches(db, matches: list[dict]) -> dict[str, str]:
    """Read aliases only for the current search result set, never all chats."""
    usernames = [str(item.get("username") or "") for item in matches]
    usernames = [item for item in usernames if item]
    if not usernames or not hasattr(db, "_open") or not hasattr(db, "_db_files"):
        return {}
    relative_path = next(
        (
            relative for relative, source, _ in db._db_files
            if os.path.basename(source) == "contact.db"
        ),
        None,
    )
    if not relative_path:
        return {}
    placeholders = ",".join("?" for _ in usernames)
    connection = db._open(relative_path)
    try:
        rows = connection.execute(
            f"SELECT username, alias FROM contact WHERE username IN ({placeholders})",
            tuple(usernames),
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): str(row[1] or "") for row in rows}


def contact_by_username(db, username: str) -> ContactChoice | None:
    """Load one exact contact identity without enumerating other contacts."""
    if not hasattr(db, "_open") or not hasattr(db, "_db_files"):
        return None
    relative_path = next(
        (
            relative for relative, source, _ in db._db_files
            if os.path.basename(source) == "contact.db"
        ),
        None,
    )
    if not relative_path:
        return None
    connection = db._open(relative_path)
    try:
        row = connection.execute(
            f"SELECT {', '.join(CONTACT_REQUIRED_COLUMNS)} FROM contact "
            "WHERE username=? LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return ContactChoice.from_fields(
        username=row["username"],
        nickname=row["nick_name"],
        remark=row["remark"],
        alias=row["alias"],
    )


def choose_contact(
    db,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> ContactChoice:
    while True:
        keyword = input_func("\n请输入联系人或群聊名称：").strip()
        if not keyword:
            print_func("名称不能为空。")
            continue
        raw_matches = db.search_contact(keyword)
        aliases = _aliases_for_matches(db, raw_matches)
        matches = [
            contact_choice_from_search_result(
                item, aliases.get(str(item.get("username") or ""), "")
            )
            for item in raw_matches
        ]
        if not matches:
            print_func("没有找到匹配项，请换一个名称或输入内部 username/wxid。")
            continue
        matches.sort(
            key=lambda item: (
                0 if keyword in (item.remark, item.nickname, item.username) else 1,
                item.display_name.casefold(),
                item.username,
            )
        )
        print_func("匹配结果：")
        for index, item in enumerate(matches, 1):
            detail = f"备注：{item.remark} / " if item.remark else ""
            nick = item.nickname or item.display_name
            alias = f"微信号：{item.alias} / " if item.alias else ""
            print_func(
                f"[{index}] {item.display_name}（{detail}昵称：{nick} / "
                f"{alias}内部 ID：{item.username}）"
            )
        while True:
            answer = input_func("请选择编号：").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(matches):
                return matches[int(answer) - 1]
            print_func("请输入列表中的有效编号。")
