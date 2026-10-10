from __future__ import annotations

import logging
import re
import shutil
from tempfile import TemporaryDirectory
from pathlib import Path

from .history_contracts import HistoryError
from .models import ChatMessage, MediaResult


DISPLAYABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _canonical_component(identity: str) -> str:
    if not isinstance(identity, str) or not re.fullmatch(r"server:-?[0-9]+|archive:[0-9a-f]{32}", identity):
        raise HistoryError("SOURCE_IDENTITY_MISMATCH")
    if identity.startswith("server:"):
        number = identity.split(":", 1)[1]
        if number == "0" or str(int(number)) != number:
            raise HistoryError("SOURCE_IDENTITY_MISMATCH")
    return identity.replace(":", "_")


def _install_new_asset(source: Path, directory: Path, base: str) -> Path:
    """Exclusive creation: never replace an already referenced or concurrent file."""
    index = 0
    while True:
        destination = directory / (base + (f"_{index}" if index else "") + source.suffix.lower())
        try:
            handle = destination.open("xb")
        except FileExistsError:
            index += 1
            continue
        with handle, source.open("rb") as reader:
            shutil.copyfileobj(reader, handle)
        return destination


class ImageExporter:
    def __init__(self, db, username: str, image_dir: Path, logger: logging.Logger, *, downloader_factory=None):
        try:
            from wechatauto.media import MediaDownloader
        except Exception as exc:
            raise RuntimeError(f"无法加载上游 MediaDownloader：{exc}") from exc
        self.username = username
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.downloader = (downloader_factory or MediaDownloader)(db, save_dir=str(image_dir))
        self._terminal_reason = ""

    def export(self, message: ChatMessage, *, canonical_identity: str | None = None) -> MediaResult:
        if canonical_identity is not None:
            component = _canonical_component(canonical_identity)
            try:
                with TemporaryDirectory(prefix=component + "-", dir=self.image_dir) as temporary:
                    return self._export(message, Path(temporary), component)
            except OSError:
                # Validated media could not be materialized; never a placeholder.
                raise HistoryError("SOURCE_READ_FAILED") from None
        return self._export(message, self.image_dir, None)

    def _export(self, message: ChatMessage, download_dir: Path, component: str | None) -> MediaResult:
        if self._terminal_reason:
            return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE" if component else self._terminal_reason)
        try:
            raw_output = self.downloader.download_image(
                self.username, int(message.local_id), save_dir=str(download_dir)
            )
        except HistoryError:
            raise
        except Exception as exc:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            if "detect_image_key" in str(exc):
                self._terminal_reason = reason
            self.logger.warning("图片恢复异常 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)
        if not raw_output:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = "上游未找到可恢复的本地图片或解密失败"
            self.logger.warning("图片恢复失败 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)

        source = Path(raw_output).resolve()
        image_root = download_dir.resolve()
        try:
            source.relative_to(image_root)
        except ValueError:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = "上游返回了导出目录之外的路径，已拒绝引用"
            self.logger.warning("图片路径校验失败 local_id=%s", message.local_id)
            return MediaResult("failed", reason=reason)

        extension = source.suffix.lower()
        if extension not in DISPLAYABLE_EXTENSIONS:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = f"恢复结果格式 {extension or '(无扩展名)'} 不能在 Markdown 中可靠显示"
            self.logger.warning("图片格式不可显示 local_id=%s ext=%s", message.local_id, extension)
            return MediaResult("failed", reason=reason)

        thumb = "_thumb" in source.stem.lower()
        if component is not None:
            destination = _install_new_asset(source, self.image_dir, component + "_image" + ("_thumb" if thumb else ""))
            relative = destination.relative_to(self.image_dir.parent.parent).as_posix()
            return MediaResult("thumbnail" if thumb else "success", relative_path=relative)
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

    def __init__(self, db, username: str, video_dir: Path, logger: logging.Logger, *, downloader_factory=None):
        try:
            from wechatauto.media import MediaDownloader
        except Exception as exc:
            raise RuntimeError(f"无法加载上游 MediaDownloader：{exc}") from exc
        self.username = username
        self.video_dir = video_dir
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.downloader = (downloader_factory or MediaDownloader)(db, save_dir=str(video_dir))

    def export(self, message: ChatMessage, *, canonical_identity: str | None = None) -> MediaResult:
        if canonical_identity is not None:
            component = _canonical_component(canonical_identity)
            try:
                with TemporaryDirectory(prefix=component + "-", dir=self.video_dir) as temporary:
                    return self._export(message, Path(temporary), component)
            except OSError:
                raise HistoryError("SOURCE_READ_FAILED") from None
        return self._export(message, self.video_dir, None)

    def _export(self, message: ChatMessage, download_dir: Path, component: str | None) -> MediaResult:
        try:
            raw_output = self.downloader.download_video(
                self.username, int(message.local_id), save_dir=str(download_dir)
            )
        except HistoryError:
            raise
        except Exception as exc:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.logger.warning("视频恢复异常 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)
        if not raw_output:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = "上游未找到已缓存的本地视频"
            self.logger.warning("视频恢复失败 local_id=%s reason=%s", message.local_id, reason)
            return MediaResult("failed", reason=reason)

        source = Path(raw_output).resolve()
        video_root = download_dir.resolve()
        try:
            source.relative_to(video_root)
        except ValueError:
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = "上游返回了导出目录之外的路径，已拒绝引用"
            self.logger.warning("视频路径校验失败 local_id=%s", message.local_id)
            return MediaResult("failed", reason=reason)
        if source.suffix.casefold() != ".mp4":
            if component is not None:
                return MediaResult("failed", reason="LOCAL_MEDIA_UNAVAILABLE")
            reason = f"恢复结果格式 {source.suffix or '(无扩展名)'} 不是已验证的 MP4"
            self.logger.warning("视频格式未验证 local_id=%s ext=%s", message.local_id, source.suffix)
            return MediaResult("failed", reason=reason)

        if component is not None:
            destination = _install_new_asset(source, self.video_dir, component + "_video")
            return MediaResult("success", relative_path=destination.relative_to(self.video_dir.parent.parent).as_posix())
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
