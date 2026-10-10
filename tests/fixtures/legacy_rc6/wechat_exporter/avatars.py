from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import AvatarSnapshot


DIRECT_FORMATS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
}


class AvatarExporter:
    """Create one portable avatar snapshot per participant, without networking."""

    def __init__(self, db, avatar_dir: Path, logger: logging.Logger):
        self.db = db
        self.avatar_dir = avatar_dir
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self.archive_root = avatar_dir.parent.parent
        self.logger = logger

    def export(
        self,
        role: str,
        username: str,
        label: str,
        input_func: Callable[[str], str] = input,
        print_func: Callable[..., None] = print,
    ) -> AvatarSnapshot:
        metadata = self._contact_avatar_metadata(username)
        remote_available = bool(metadata["big_url"] or metadata["small_url"])

        local_cache = self._find_proven_local_cache(metadata["md5"])
        if local_cache:
            try:
                snapshot = self._save_bytes(local_cache.read_bytes(), role, "local_cache", remote_available)
                print_func(f"{label}头像：已从本地头像缓存复制。")
                return snapshot
            except Exception as exc:
                self.logger.warning("本地头像缓存无效 role=%s error_type=%s", role, type(exc).__name__)

        database_image = self._head_image_blob(username)
        if database_image:
            try:
                snapshot = self._save_bytes(database_image, role, "local_database", remote_available)
                print_func(f"{label}头像：已从本地 head_image.db 恢复。")
                return snapshot
            except Exception as exc:
                self.logger.warning("头像数据库内容无效 role=%s error_type=%s", role, type(exc).__name__)

        print_func(f"未能从微信本地数据恢复{label}头像。")
        if remote_available:
            print_func("本地数据库存在远程头像地址，但依据 no-network 策略未获取。")
        while True:
            answer = input_func(f"输入{label}本地头像文件路径（直接回车跳过）：\n> ").strip()
            if not answer:
                status = "remote_available_not_fetched" if remote_available else "unavailable"
                self.logger.info("头像不可用 role=%s status=%s", role, status)
                return AvatarSnapshot(status=status, remote_available=remote_available)
            source = Path(os.path.expandvars(answer.strip('"'))).expanduser()
            if not source.is_file():
                print_func("文件不存在，请重新输入或直接回车跳过。")
                continue
            try:
                snapshot = self._save_bytes(source.read_bytes(), role, "manual", remote_available)
                print_func(f"{label}头像：已复制手动指定的本地文件。")
                return snapshot
            except Exception:
                print_func("该文件不是可识别的静态图片，请重新输入或直接回车跳过。")

    def _contact_avatar_metadata(self, username: str) -> dict[str, str]:
        relative_path = self._find_database("contact.db")
        if not relative_path:
            return {"md5": "", "big_url": "", "small_url": ""}
        connection = self.db._open(relative_path)
        try:
            row = connection.execute(
                "SELECT head_img_md5, big_head_url, small_head_url FROM contact "
                "WHERE username=? LIMIT 1",
                (username,),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {"md5": "", "big_url": "", "small_url": ""}
        return {
            "md5": str(row[0] or "").lower(),
            "big_url": str(row[1] or ""),
            "small_url": str(row[2] or ""),
        }

    def _head_image_blob(self, username: str) -> bytes | None:
        relative_path = self._find_database("head_image.db")
        if not relative_path:
            return None
        connection = self.db._open(relative_path)
        try:
            row = connection.execute(
                "SELECT image_buffer FROM head_image WHERE username=? "
                "ORDER BY update_time DESC LIMIT 1",
                (username,),
            ).fetchone()
        finally:
            connection.close()
        return bytes(row[0]) if row and row[0] else None

    def _find_database(self, basename: str) -> str | None:
        for relative_path, source_path, _ in self.db._db_files:
            if os.path.basename(source_path) == basename:
                return relative_path
        return None

    def _find_proven_local_cache(self, expected_md5: str) -> Path | None:
        if len(expected_md5) != 32:
            return None
        roots = [
            Path(self.db.account_dir) / "head_imgs",
            Path(self.db.account_dir) / "temp" / "head_image",
            Path(self.db.db_dir) / "all_users" / "head_imgs",
        ]
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.name.lower() == expected_md5 or path.stem.lower() == expected_md5:
                    return path
                try:
                    if hashlib.md5(path.read_bytes()).hexdigest() == expected_md5:
                        return path
                except OSError:
                    continue
        return None

    def _save_bytes(
        self,
        data: bytes,
        role: str,
        status: str,
        remote_available: bool,
    ) -> AvatarSnapshot:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            image.verify()

        if image_format in DIRECT_FORMATS:
            extension = DIRECT_FORMATS[image_format]
            destination = self.avatar_dir / (("me" if role == "self" else "contact") + extension)
            destination.write_bytes(data)
        else:
            destination = self.avatar_dir / (("me" if role == "self" else "contact") + ".png")
            with Image.open(io.BytesIO(data)) as image:
                frame = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                frame.save(destination, format="PNG")

        relative_path = destination.relative_to(self.archive_root).as_posix()
        self.logger.info(
            "头像已保存 role=%s status=%s bytes=%d format=%s",
            role,
            status,
            destination.stat().st_size,
            image_format,
        )
        return AvatarSnapshot(
            status=status,
            relative_path=relative_path,
            remote_available=remote_available,
        )
