from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import ChatMessage, MediaResult


DISPLAYABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class ImageExporter:
    def __init__(self, db, username: str, image_dir: Path, logger: logging.Logger):
        try:
            from wechatauto.media import MediaDownloader
        except Exception as exc:
            raise RuntimeError(f"无法加载上游 MediaDownloader：{exc}") from exc
        self.username = username
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.downloader = MediaDownloader(db, save_dir=str(image_dir))
        self._terminal_reason = ""

    def export(self, message: ChatMessage) -> MediaResult:
        if self._terminal_reason:
            return MediaResult("failed", reason=self._terminal_reason)
        try:
            raw_output = self.downloader.download_image(
                self.username, int(message.local_id), save_dir=str(self.image_dir)
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            if "detect_image_key" in str(exc):
                self._terminal_reason = reason
            self.logger.warning("图片恢复异常 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)
        if not raw_output:
            reason = "上游未找到可恢复的本地图片或解密失败"
            self.logger.warning("图片恢复失败 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)

        source = Path(raw_output).resolve()
        image_root = self.image_dir.resolve()
        try:
            source.relative_to(image_root)
        except ValueError:
            reason = "上游返回了导出目录之外的路径，已拒绝引用"
            self.logger.warning("图片路径校验失败 local_id=%s", message.local_id)
            return MediaResult("failed", reason=reason)

        extension = source.suffix.lower()
        if extension not in DISPLAYABLE_EXTENSIONS:
            reason = f"恢复结果格式 {extension or '(无扩展名)'} 不能在 Markdown 中可靠显示"
            self.logger.warning("图片格式不可显示 local_id=%s ext=%s", message.local_id, extension)
            return MediaResult("failed", reason=reason)

        thumb = "_thumb" in source.stem.lower()
        stable_id = re.sub(r"[^0-9A-Za-z_-]", "_", message.local_id)
        base = message.timestamp.strftime("%Y%m%d_%H%M%S") + "_" + stable_id
        if thumb:
            base += "_thumb"
        destination = self.image_dir / (base + extension)
        if source != destination:
            if destination.exists():
                destination.unlink()
            source.replace(destination)
        relative = destination.relative_to(self.image_dir.parent.parent).as_posix()
        return MediaResult("thumbnail" if thumb else "success", relative_path=relative)


class VideoExporter:
    """Restore an already-cached local video without network or UI access."""

    def __init__(self, db, username: str, video_dir: Path, logger: logging.Logger):
        try:
            from wechatauto.media import MediaDownloader
        except Exception as exc:
            raise RuntimeError(f"无法加载上游 MediaDownloader：{exc}") from exc
        self.username = username
        self.video_dir = video_dir
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.downloader = MediaDownloader(db, save_dir=str(video_dir))

    def export(self, message: ChatMessage) -> MediaResult:
        try:
            raw_output = self.downloader.download_video(
                self.username, int(message.local_id), save_dir=str(self.video_dir)
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.logger.warning("视频恢复异常 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)
        if not raw_output:
            reason = "上游未找到已缓存的本地视频"
            self.logger.warning("视频恢复失败 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)

        source = Path(raw_output).resolve()
        video_root = self.video_dir.resolve()
        try:
            source.relative_to(video_root)
        except ValueError:
            reason = "上游返回了导出目录之外的路径，已拒绝引用"
            self.logger.warning("视频路径校验失败 local_id=%s", message.local_id)
            return MediaResult("failed", reason=reason)
        if source.suffix.casefold() != ".mp4":
            reason = f"恢复结果格式 {source.suffix or '(无扩展名)'} 不是已验证的 MP4"
            self.logger.warning("视频格式未验证 local_id=%s ext=%s", message.local_id, source.suffix)
            return MediaResult("failed", reason=reason)

        stable_id = re.sub(r"[^0-9A-Za-z_-]", "_", message.local_id)
        destination = self.video_dir / (
            message.timestamp.strftime("%Y%m%d_%H%M%S") + "_" + stable_id + ".mp4"
        )
        if source != destination:
            if destination.exists():
                destination.unlink()
            source.replace(destination)
        relative = destination.relative_to(self.video_dir.parent.parent).as_posix()
        return MediaResult("success", relative_path=relative)
