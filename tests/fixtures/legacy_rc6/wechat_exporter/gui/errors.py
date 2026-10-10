from __future__ import annotations

import errno
from dataclasses import dataclass

from wechat_exporter import __version__
from wechat_exporter.locks import TargetBusyError
from wechat_exporter.migrations import UnsupportedFutureSchema
from wechat_exporter.service import RuntimeWorkDirectoryError


@dataclass(frozen=True)
class UserFacingError:
    title: str
    message: str
    code: str
    technical_summary: str


def map_exception(error: BaseException) -> UserFacingError:
    technical = (
        f"error={type(error).__name__}; module={type(error).__module__}; "
        f"app_version={__version__}"
    )
    if isinstance(error, TargetBusyError):
        return UserFacingError(
            "操作正在进行",
            "这个备份对象正在执行另一项操作。请等待完成后再试。",
            "TARGET_BUSY",
            technical,
        )
    if isinstance(error, UnsupportedFutureSchema) or "由更新版本创建" in str(error):
        return UserFacingError(
            "需要更新软件",
            "这个备份对象由更新版本创建。当前版本不会冒险读取或修改它。",
            "FUTURE_SCHEMA",
            technical,
        )
    if isinstance(error, RuntimeWorkDirectoryError):
        return UserFacingError(
            "本地临时空间不可用",
            "无法准备本次保存所需的本地临时空间。请检查当前 Windows 账号的本地应用数据目录后重试。",
            "LOCAL_WORKSPACE_UNAVAILABLE",
            technical,
        )
    if isinstance(error, OSError) and getattr(error, "errno", None) == errno.ENOSPC:
        return UserFacingError(
            "保存空间不足",
            "时间胶囊保存位置空间不足。请释放空间或在设置中选择其他位置。",
            "DISK_FULL",
            technical,
        )
    text = str(error)
    if ("多个" in text and "微信账号" in text) or "已变化的微信账号" in text:
        return UserFacingError(
            "需要重新选择微信账号",
            "检测到多个账号，或此前选择的账号已经变化。请返回主窗口重新选择后再试。",
            "WECHAT_ACCOUNT_SELECTION_REQUIRED",
            technical,
        )
    if "不受支持" in text and "微信版本" in text:
        return UserFacingError(
            "当前微信版本不受支持",
            "为了避免保存不完整，本次操作没有继续。离线完整性验证仍可使用。",
            "WECHAT_VERSION_UNSUPPORTED",
            technical,
        )
    if "未发现可读取的微信账号" in text or "微信" in text and "账号" in text:
        return UserFacingError(
            "没有检测到已登录微信",
            "请先启动并登录 Windows 微信，然后重新尝试。离线验证仍然可以使用。",
            "WECHAT_NOT_AVAILABLE",
            technical,
        )
    if "版本" in text and "验证" in text:
        return UserFacingError(
            "暂时无法读取微信",
            "当前微信的基础读取检查没有通过。可以返回主窗口重新检测。",
            "WECHAT_VERSION_UNVERIFIED",
            technical,
        )
    if "不是完整的微信时间胶囊备份对象" in text or "身份或时间胶囊索引无效" in text:
        return UserFacingError(
            "无法添加这个备份对象",
            "所选目录缺少必需文件，或内部数据无效。原目录没有被复制或修改。",
            "INVALID_ARCHIVE_IMPORT",
            technical,
        )
    if "诊断报告隐私自检失败" in text:
        return UserFacingError(
            "诊断报告未生成",
            "隐私自检发现报告中可能含有敏感信息，因此已安全停止且没有保留 ZIP。",
            "DIAGNOSTICS_PRIVACY_CHECK_FAILED",
            technical,
        )
    return UserFacingError(
        "操作没有完成",
        "没有创建或修改其他已完成的时间胶囊。可以打开日志文件夹查看进一步信息。",
        "OPERATION_FAILED",
        technical,
    )
