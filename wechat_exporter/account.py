from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError, version
import io
import logging
import threading
from typing import Callable


EXPECTED_UPSTREAM_VERSION = "1.2.1"


class _DiscardedOutput(io.TextIOBase):
    def write(self, text):
        return len(text)


_UPSTREAM_OUTPUT = _DiscardedOutput()
_UPSTREAM_PRIVACY_LOCK = threading.RLock()


@contextmanager
def quiet_upstream():
    """Silence private upstream diagnostics in this process, including cold spawn."""
    with _UPSTREAM_PRIVACY_LOCK, redirect_stdout(_UPSTREAM_OUTPUT), redirect_stderr(_UPSTREAM_OUTPUT):
        root_logger = logging.getLogger()
        handlers, level = list(root_logger.handlers), root_logger.level
        # The pinned package configures the root logger during its first import.
        # Keep its named logger quiet without disabling application logging.
        logging.getLogger('wechatauto').disabled = True
        try:
            from wechatauto.param import WxParam
            WxParam.ENABLE_FILE_LOGGER = False
        finally:
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)
        # The process-lifetime sink has no .buffer for upstream to rewrap/close.
        # Reentrant calls preserve the outer redirected streams on return.
        yield


class UpstreamUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendInfo:
    package_version: str
    account: str
    wxid: str


@dataclass(frozen=True)
class AccountChoice:
    stable_id: str
    account_locator: str
    display_label: str
    masked_identity: str
    last_activity: float


def _mask_identity(value: str) -> str:
    suffix = value[-4:] if len(value) >= 4 else "••••"
    return "••••" + suffix


def list_account_choices() -> list[AccountChoice]:
    try:
        with quiet_upstream():
            from wechatauto.db import list_accounts
    except Exception as exc:
        raise UpstreamUnavailable(
            f"无法加载 wechatauto-replica：{type(exc).__name__}: {exc}"
        ) from exc
    with quiet_upstream():
        accounts = list_accounts()
    result = []
    for index, item in enumerate(accounts, 1):
        locator = str(item.get("account") or "").strip()
        stable_id = str(item.get("wxid") or locator).strip()
        if not locator or not stable_id:
            continue
        result.append(
            AccountChoice(
                stable_id=stable_id,
                account_locator=locator,
                display_label=f"微信账号 {index}",
                masked_identity=_mask_identity(stable_id),
                last_activity=float(item.get("last_activity") or 0),
            )
        )
    return result


def create_database_for_account(stable_id: str):
    try:
        with quiet_upstream():
            from wechatauto.db import WeChatDB
    except Exception as exc:
        raise UpstreamUnavailable(
            f"无法加载 wechatauto-replica：{type(exc).__name__}: {exc}"
        ) from exc
    selected = next(
        (item for item in list_account_choices() if item.stable_id == stable_id),
        None,
    )
    if selected is None:
        raise RuntimeError("此前选择的微信账号当前不存在，请重新选择。")
    with quiet_upstream():
        db = WeChatDB(account=selected.account_locator)
    return db, BackendInfo(
        package_version=upstream_version(),
        account=selected.account_locator,
        wxid=selected.stable_id,
    )


def upstream_version() -> str:
    try:
        return version("wechatauto-replica")
    except PackageNotFoundError as exc:
        raise UpstreamUnavailable(
            "未安装 wechatauto-replica。请先运行：python -m pip install -r requirements.txt"
        ) from exc


def create_database(
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
):
    try:
        with quiet_upstream():
            from wechatauto.db import WeChatDB, list_accounts
    except Exception as exc:
        raise UpstreamUnavailable(
            f"无法加载 wechatauto-replica：{type(exc).__name__}: {exc}"
        ) from exc

    with quiet_upstream():
        accounts = list_accounts()
    if not accounts:
        raise RuntimeError("未发现可读取的微信账号。请确认 Windows 微信 4.x 已启动并登录。")

    if len(accounts) == 1:
        selected = accounts[0]
        print_func(f"检测到微信账号：{selected.get('wxid') or selected['account']}")
    else:
        print_func("检测到多个微信账号：")
        for index, item in enumerate(accounts, 1):
            print_func(f"[{index}] {item.get('wxid') or item['account']}")
        while True:
            answer = input_func("请选择账号编号：").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(accounts):
                selected = accounts[int(answer) - 1]
                break
            print_func("请输入列表中的有效编号。")

    with quiet_upstream():
        db = WeChatDB(account=selected["account"])
    return db, BackendInfo(
        package_version=upstream_version(),
        account=selected["account"],
        wxid=selected.get("wxid") or getattr(db, "wxid", ""),
    )
