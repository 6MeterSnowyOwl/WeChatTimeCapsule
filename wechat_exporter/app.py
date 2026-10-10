from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import PRODUCT_NAME, __version__
from .account import EXPECTED_UPSTREAM_VERSION, UpstreamUnavailable, create_database
from .archive import (
    ArchiveTarget,
    create_target,
    discover_targets,
    load_snapshot_index,
    migrate_legacy_export,
    save_target,
)
from .contacts import choose_contact, contact_by_username
from .current_capsule import create_current_capsule
from .integrity import (
    generate_missing_manifests,
    manifest_path,
    verify_all_snapshots_offline,
    verify_snapshot_offline,
    verify_current_capsule,
)
from .migrations import MigrationResult, migrate_mutable_state
from .reconciliation import reconcile_and_update_capsule
from .utils import configure_logging


def _display_time(value: str | None) -> str:
    if not value:
        return "无"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _show_targets(targets: list[ArchiveTarget], print_func: Callable[..., None]) -> None:
    print_func("\n备份对象一览：\n")
    if not targets:
        print_func("  （暂无）\n")
    for index, target in enumerate(targets, 1):
        try:
            snapshots = load_snapshot_index(target)["snapshots"]
        except Exception:
            snapshots = []
        metadata = {}
        try:
            metadata = json.loads(
                (target.root / "capsule_meta.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            pass
        print_func(f"[{index}] {target.display_name}")
        print_func(f"    上次更新：{_display_time(metadata.get('last_saved_at'))}")
        print_func(f"    当前时间胶囊：{'有' if metadata else '尚未生成'}\n")
    print_func("[A] 添加新的备份对象")
    print_func("[Q] 退出")


def _show_snapshots(target: ArchiveTarget, print_func: Callable[..., None]) -> None:
    snapshots = load_snapshot_index(target)["snapshots"]
    print_func(f"\n{target.display_name} 的时间胶囊\n")
    if not snapshots:
        print_func("  （还没有时间胶囊）")
        return
    for index, item in enumerate(snapshots, 1):
        added = int(item.get("new_message_count") or 0)
        suffix = f" (+{added})" if index > 1 else ""
        integrity = item.get("integrity_status") or (
            "BASELINE_PRESENT"
            if manifest_path(target.root, str(item.get("id") or "")).is_file()
            else "NO_MANIFEST"
        )
        print_func(f"[{index}] {_display_time(item.get('created_at'))}")
        print_func(f"    {item.get('message_count', 0)} messages{suffix}")
        print_func(f"    integrity: {integrity}\n")


def _refresh_target_identity(db, target: ArchiveTarget) -> ArchiveTarget:
    contact = contact_by_username(db, target.username)
    if contact is None or contact.display_name == target.display_name:
        return target
    refreshed = ArchiveTarget(target.root, target.username, contact.display_name)
    save_target(refreshed)
    return refreshed


def _print_integrity_results(results, print_func: Callable[..., None]) -> None:
    checked = sum(result.checked_files for result in results)
    missing = sum(result.count("MISSING_ASSET", "MISSING_FILE", "MISSING_REQUIRED_FILE") for result in results)
    mismatches = sum(result.count("HASH_MISMATCH") for result in results)
    escapes = sum(result.count("PATH_ESCAPE", "MANIFEST_PATH_ESCAPE") for result in results)
    invalid = sum(len(result.issues) for result in results) - missing - mismatches - escapes
    print_func("\n验证完成\n")
    for result in results:
        marker = "✅" if result.ok and result.manifest_present else "⚠" if result.ok else "❌"
        print_func(f"{marker} {result.snapshot_id}")
        for issue in result.issues:
            print_func(f"    {issue.code}: {issue.path or '-'}")
        if not result.manifest_present:
            print_func("    NO_MANIFEST：仅完成结构检查，无法证明字节未变化。")
    print_func(f"\n检查文件/引用：{checked}")
    print_func(f"缺失：{missing}")
    print_func(f"Hash mismatch：{mismatches}")
    print_func(f"Path escape：{escapes}")
    print_func(f"其他无效项：{max(0, invalid)}")


def _integrity_menu(
    target: ArchiveTarget,
    input_func: Callable[[str], str],
    print_func: Callable[..., None],
) -> None:
    while True:
        print_func(f"\n离线验证：{target.display_name}")
        print_func("[1] 验证最新时间胶囊")
        print_func("[2] 验证全部时间胶囊")
        print_func("[3] 为缺少 manifest 的时间胶囊建立当前基线")
        print_func("[B] 返回")
        answer = input_func("> ").strip().lower()
        if answer == "b":
            return
        snapshots = load_snapshot_index(target)["snapshots"]
        if answer == "1":
            if not snapshots:
                print_func("没有可验证的时间胶囊。")
                continue
            _print_integrity_results(
                [verify_snapshot_offline(target.root, snapshots[-1]["id"])], print_func
            )
        elif answer == "2":
            _print_integrity_results(verify_all_snapshots_offline(target.root), print_func)
        elif answer == "3":
            confirmation = input_func(
                "这会把当前文件 hash 登记为基线，但不会修改时间胶囊。确认？[y/N] "
            ).strip().lower()
            if confirmation not in {"y", "yes"}:
                print_func("已取消。")
                continue
            created = generate_missing_manifests(target.root)
            print_func(f"已建立 {len(created)} 份 manifest。")
        else:
            print_func("请输入 1、2、3 或 B。")


def _report_migration(result: MigrationResult, print_func: Callable[..., None]) -> None:
    if not result.applied:
        return
    print_func(
        f"已安全迁移 mutable state：schema {result.from_version} → {result.to_version}"
    )
    print_func(f"迁移前保护副本：{result.backup_path}")


def _target_menu(
    get_database,
    target: ArchiveTarget,
    work_root: Path,
    logger,
    input_func: Callable[[str], str],
    print_func: Callable[..., None],
) -> None:
    migration = migrate_mutable_state(target.root)
    _report_migration(migration, print_func)
    while True:
        metadata = {}
        try:
            metadata = json.loads(
                (target.root / "capsule_meta.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            pass
        print_func(f"\n{target.display_name}\n")
        print_func(f"上次更新：{_display_time(metadata.get('last_saved_at'))}")
        print_func(f"消息：{metadata.get('message_count', 0)} 条")
        print_func("\n[1] 快速更新 — 只检查上次之后的新消息")
        print_func("[2] 完整核对并更新 — 重新检查全部聊天")
        print_func("[3] 验证当前时间胶囊（离线）")
        print_func("[B] 返回")
        answer = input_func("> ").strip().lower()
        if answer == "b":
            return
        if answer == "3":
            result = verify_current_capsule(target.root)
            _print_integrity_results([result], print_func)
            continue
        if answer not in {"1", "2"}:
            print_func("请输入 1、2、3 或 B。")
            continue

        db, backend = get_database()
        target = _refresh_target_identity(db, target)
        migrated = migrate_legacy_export(db, target, work_root, logger)
        if migrated:
            print_func("已将旧版数据非破坏性登记为首个时间胶囊。")
        if answer == "1":
            print_func("正在读取新消息并更新当前时间胶囊……")
            result = create_current_capsule(
                db, target, backend.wxid, work_root, logger,
                input_func=input_func, print_func=print_func,
            )
            reconciliation = None
        else:
            print_func("正在重新检查全部聊天并与内部历史核对……")
            result, reconciliation, report = reconcile_and_update_capsule(
                db, target, backend.wxid, work_root, logger,
                input_func=input_func, print_func=print_func,
            )
            print_func(f"数据库消息：{reconciliation.database_message_count}")
            print_func(f"内部历史已有：{reconciliation.archive_before_count}")
            print_func(f"新增发现：{reconciliation.newly_discovered}")
            print_func(f"冲突：{len(reconciliation.conflicts)}")
            print_func(f"复核报告：{report}")
        print_func(f"\n当前时间胶囊已更新：{target.display_name}")
        print_func(f"总消息：{result.message_count}")
        print_func(f"新增消息：{result.new_message_count}")
        print_func(f"图片总计：{result.image_count}")
        print_func(f"新增图片：{result.new_image_count}")
        print_func(f"复用资源：{result.asset_counts.reused}")
        print_func(f"Hardlink：{result.asset_counts.hardlinked}")
        print_func(f"Copy：{result.asset_counts.copied}")
        print_func(f"路径：{result.path}")


def run(
    project_root: Path,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> int:
    logs_root = project_root / "logs"
    exports_root = project_root / "exports"
    work_root = project_root / "work"
    exports_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    logger = configure_logging(logs_root / "export.log")
    cached = None

    def get_database():
        nonlocal cached
        if cached is None:
            print_func("正在识别微信账号并初始化只读数据访问层……")
            cached = create_database(input_func=input_func, print_func=print_func)
            if cached[1].package_version != EXPECTED_UPSTREAM_VERSION:
                print_func(
                    f"警告：当前上游版本为 {cached[1].package_version}，"
                    f"本工具按 {EXPECTED_UPSTREAM_VERSION} 验证。"
                )
        return cached

    print_func(f"{PRODUCT_NAME} v{__version__}")
    print_func("快速保存、完整核对与离线完整性验证。\n")
    try:
        while True:
            targets = discover_targets(exports_root)
            _show_targets(targets, print_func)
            answer = input_func("> ").strip().lower()
            if answer == "q":
                print_func("已退出。")
                return 0
            if answer == "a":
                db, _ = get_database()
                contact = choose_contact(db, input_func=input_func, print_func=print_func)
                print_func(
                    f"\n即将添加备份对象：{contact.display_name} "
                    f"({contact.alias or contact.username})"
                )
                confirmed = input_func("确认添加？[y/N] ").strip().lower()
                if confirmed not in {"y", "yes"}:
                    print_func("已取消。")
                    continue
                target = create_target(exports_root, contact)
                print_func(f"已添加：{target.display_name}")
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(targets):
                _target_menu(
                    get_database, targets[int(answer) - 1], work_root,
                    logger, input_func, print_func,
                )
                continue
            print_func("请输入备份对象编号、A 或 Q。")
    except (KeyboardInterrupt, EOFError):
        print_func("\n已取消。")
        return 130
    except UpstreamUnavailable as exc:
        print_func(f"\n初始化失败：{exc}")
        return 2
    except Exception as exc:
        print_func(f"\n操作失败：{type(exc).__name__}: {exc}")
        logger.exception("归档异常（日志不记录消息正文或密钥）")
        return 1
