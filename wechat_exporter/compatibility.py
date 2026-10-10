from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import Enum


TESTED_WECHAT_VERSIONS = frozenset({"4.1.13.63", "4.1.13.65"})
UPSTREAM_TARGET_RANGE = "WeChat 4.x"


class VersionVerification(str, Enum):
    TESTED = "TESTED"
    UNTESTED = "UNTESTED"
    UNSUPPORTED = "UNSUPPORTED"


CompatibilityStatus = VersionVerification


class RuntimeCapability(str, Enum):
    READY = "READY"
    WECHAT_NOT_RUNNING = "WECHAT_NOT_RUNNING"
    NOT_LOGGED_IN = "NOT_LOGGED_IN"
    ACCOUNT_SELECTION_REQUIRED = "ACCOUNT_SELECTION_REQUIRED"
    PROBE_FAILED = "PROBE_FAILED"
    NOT_PROBED = "NOT_PROBED"


class ProbeErrorCode(str, Enum):
    NONE = "NONE"
    PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
    ACCOUNT_NOT_AVAILABLE = "ACCOUNT_NOT_AVAILABLE"
    ACCOUNT_SELECTION_REQUIRED = "ACCOUNT_SELECTION_REQUIRED"
    DATABASE_DISCOVERY_FAILED = "DATABASE_DISCOVERY_FAILED"
    DATABASE_OPEN_FAILED = "DATABASE_OPEN_FAILED"
    CONTACT_SCHEMA_UNSUPPORTED = "CONTACT_SCHEMA_UNSUPPORTED"
    CONTACT_QUERY_FAILED = "CONTACT_QUERY_FAILED"
    MESSAGE_SCHEMA_UNSUPPORTED = "MESSAGE_SCHEMA_UNSUPPORTED"
    TIMEOUT = "TIMEOUT"
    INTERNAL_PROBE_ERROR = "INTERNAL_PROBE_ERROR"


@dataclass(frozen=True)
class CapabilityProbeResult:
    runtime_capability: RuntimeCapability
    error_code: ProbeErrorCode = ProbeErrorCode.NONE
    detail: str = ""
    account_display_name: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "runtime_capability": self.runtime_capability.value,
            "error_code": self.error_code.value,
            "detail": self.detail,
            "account_display_name": self.account_display_name,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "CapabilityProbeResult":
        if not isinstance(payload, dict):
            raise ValueError("invalid capability probe payload")
        return cls(
            RuntimeCapability(str(payload["runtime_capability"])),
            ProbeErrorCode(str(payload["error_code"])),
            str(payload.get("detail") or ""),
            str(payload.get("account_display_name") or ""),
        )


def operations_allowed(
    version_verification: VersionVerification,
    runtime_capability: RuntimeCapability,
) -> bool:
    return (
        version_verification
        in (VersionVerification.TESTED, VersionVerification.UNTESTED)
        and runtime_capability == RuntimeCapability.READY
    )


@dataclass(frozen=True)
class WeChatCompatibility:
    detected: bool
    version: str
    status: VersionVerification
    label: str


def _running_wechat_paths() -> list[str]:
    if platform.system() != "Windows":
        return []
    try:
        import psutil
    except Exception:
        return []
    paths: set[str] = set()
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").casefold() == "weixin.exe":
                path = process.info.get("exe")
                if path:
                    paths.add(str(path))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(paths)


def _file_version(path: str) -> str:
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        ms = info["FileVersionMS"]
        ls = info["FileVersionLS"]
        return ".".join(
            str(value)
            for value in (
                win32api.HIWORD(ms),
                win32api.LOWORD(ms),
                win32api.HIWORD(ls),
                win32api.LOWORD(ls),
            )
        )
    except Exception:
        return ""


def classify_wechat_version(version: str) -> VersionVerification:
    if version in TESTED_WECHAT_VERSIONS:
        return VersionVerification.TESTED
    if version.split(".", 1)[0] == "4":
        return VersionVerification.UNTESTED
    return VersionVerification.UNSUPPORTED


def detect_wechat_compatibility() -> WeChatCompatibility:
    paths = _running_wechat_paths()
    if not paths:
        return WeChatCompatibility(
            False, "", VersionVerification.UNTESTED, "○ 未检测到"
        )
    version = _file_version(paths[0])
    if not version:
        return WeChatCompatibility(
            True, "未知", VersionVerification.UNSUPPORTED, "✕ 版本无法确认"
        )
    status = classify_wechat_version(version)
    labels = {
        VersionVerification.TESTED: "✓ 已验证",
        VersionVerification.UNTESTED: "⚠ 尚未验证",
        VersionVerification.UNSUPPORTED: "✕ 当前不支持",
    }
    return WeChatCompatibility(True, version, status, labels[status])
