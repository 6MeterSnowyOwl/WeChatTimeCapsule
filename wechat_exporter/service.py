from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QStandardPaths

from . import PRODUCT_SLUG, __version__
from .account import (
    AccountChoice,
    BackendInfo,
    create_database_for_account,
    list_account_choices,
)
from .archive import (
    ArchiveTarget,
    create_target,
    discover_targets,
    load_snapshot_index,
    migrate_legacy_export,
    save_target,
)
from .contacts import contact_by_username
from .current_capsule import (
    AvatarHistoryEntry,
    CapsuleResult,
    create_current_capsule,
    load_avatar_history,
    recover_current_capsule_transaction,
)
from .deletion import (
    JOURNAL_NAME,
    SnapshotDeletionResult,
    delete_snapshot_transaction,
    recover_pending_snapshot_deletion,
)
from .compatibility import (
    CapabilityProbeResult,
    CompatibilityStatus,
    ProbeErrorCode,
    RuntimeCapability,
    VersionVerification,
    WeChatCompatibility,
    detect_wechat_compatibility,
    operations_allowed as compatibility_operations_allowed,
)
from .capability_probe import probe_account_with_timeout
from .integrity import (
    IntegrityResult,
    verify_all_snapshots_offline,
    verify_current_capsule,
    verify_snapshot_offline,
)
from .locks import TargetBusyError, TargetOperationLock
from .migrations import MigrationResult, migrate_mutable_state
from .models import ContactChoice
from .reconciliation import ReconciliationResult, reconcile_and_update_capsule
from .registry import ArchiveRegistry
from .utils import configure_logging, write_json_atomic


@dataclass(frozen=True)
class TargetSummary:
    root: Path
    display_name: str
    message_count: int
    last_saved_at: str | None
    integrity_label: str
    integrity_ok: bool | None
    avatar_path: Path | None
    capsule_exists: bool
    avatar_history_count: int
    legacy_snapshot_count: int
    archive_id: str = ""

    @property
    def last_backup_at(self) -> str | None:
        return self.last_saved_at


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_id: str
    path: Path
    created_at: str | None
    message_count: int
    new_message_count: int
    backup_mode: str
    integrity_label: str
    integrity_ok: bool | None


@dataclass(frozen=True)
class WeChatStatus:
    version_verification: VersionVerification = VersionVerification.UNTESTED
    runtime_capability: RuntimeCapability = RuntimeCapability.WECHAT_NOT_RUNNING
    probe_error_code: ProbeErrorCode = ProbeErrorCode.PROCESS_NOT_FOUND
    account_name: str = ""
    version: str = ""
    version_label: str = ""
    message: str = ""
    account_count: int = 0
    selection_required: bool = False
    accounts: tuple[AccountChoice, ...] = ()
    remembered_missing: bool = False

    @property
    def connected(self) -> bool:
        return self.runtime_capability == RuntimeCapability.READY

    @property
    def operations_allowed(self) -> bool:
        return compatibility_operations_allowed(
            self.version_verification, self.runtime_capability
        )

    @property
    def compatibility_status(self) -> str:
        """Compatibility alias for diagnostics consumers predating RC2."""
        return self.version_verification.value


@dataclass(frozen=True)
class BackupOutcome:
    capsule: CapsuleResult
    reconciliation: ReconciliationResult | None = None
    report_path: Path | None = None

    @property
    def snapshot(self) -> CapsuleResult:
        """Compatibility alias for legacy non-GUI callers."""
        return self.capsule


@dataclass(frozen=True)
class BackupTargetRemovalResult:
    archive_id: str
    removed: bool
    message: str


class RuntimeWorkDirectoryError(RuntimeError):
    """The private per-user scratch directory cannot be prepared safely."""

    def __init__(self):
        super().__init__("应用临时工作目录不可用。")


def documents_archive_root() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    base = Path(location) if location else Path.home() / "Documents"
    return base / "微信时间胶囊"


class AppSettings:
    def __init__(self, path: Path, project_exports: Path):
        self.path = path
        self.project_exports = project_exports
        self.recovery_notice = ""
        self.data = self._load()
        if not self.path.is_file():
            self.save()

    @classmethod
    def default(cls, project_root: Path) -> "AppSettings":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        current = base / PRODUCT_SLUG / "settings.json"
        legacy = base / "WeChatMarkdownExporter" / "settings.json"
        # Existing installations stay on the validated legacy location. New
        # installations use the product slug; no config or archive is moved.
        path = legacy if legacy.is_file() and not current.is_file() else current
        return cls(path, project_root / "exports")

    def _load(self) -> dict:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return self._normalize(raw)
                raise ValueError("settings must be an object")
            except (OSError, ValueError, TypeError):
                backup = self.path.with_name(
                    f"{self.path.name}.corrupt.{datetime.now():%Y%m%d_%H%M%S}"
                )
                try:
                    self.path.replace(backup)
                    self.recovery_notice = (
                        "应用设置损坏，已恢复默认设置。你的时间胶囊没有被修改。"
                    )
                except OSError:
                    self.recovery_notice = (
                        "应用设置损坏，已使用临时默认设置。你的时间胶囊没有被修改。"
                    )
        return self._defaults()

    def _defaults(self) -> dict:
        return {
            "schema_version": 2,
            "archive_root": str(documents_archive_root()),
            "known_archive_roots": [],
            "onboarding_completed": False,
            "window_size": [900, 650],
            "last_selected_target": "",
            "selected_account_identity": "",
            "suppressed_first_backup_notices": [],
        }

    def _normalize(self, raw: dict) -> dict:
        defaults = self._defaults()
        defaults.update(raw)
        defaults["schema_version"] = 2
        defaults.setdefault("selected_account_identity", "")
        defaults.setdefault("suppressed_first_backup_notices", [])
        return defaults

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    @property
    def archive_root(self) -> Path:
        return Path(self.data["archive_root"])

    @property
    def onboarding_completed(self) -> bool:
        return bool(self.data.get("onboarding_completed"))

    def set_onboarding_completed(self) -> None:
        self.data["onboarding_completed"] = True
        self.save()

    @property
    def selected_account_identity(self) -> str:
        return str(self.data.get("selected_account_identity") or "")

    def set_selected_account_identity(self, identity: str) -> None:
        self.data["selected_account_identity"] = identity
        self.save()

    def first_backup_notice_suppressed(self, archive_id: str) -> bool:
        return archive_id in self.data.get("suppressed_first_backup_notices", [])

    def suppress_first_backup_notice(self, archive_id: str) -> None:
        values = list(self.data.get("suppressed_first_backup_notices", []))
        if archive_id not in values:
            values.append(archive_id)
            self.data["suppressed_first_backup_notices"] = values
            self.save()

    @property
    def app_data_dir(self) -> Path:
        return self.path.parent

    def set_archive_root(self, path: Path) -> None:
        previous = str(self.archive_root.resolve())
        known = list(self.data.get("known_archive_roots", []))
        if previous not in known:
            known.append(previous)
        self.data["known_archive_roots"] = known
        self.data["archive_root"] = str(path.resolve())
        self.save()

    def archive_roots(self) -> list[Path]:
        values = [self.archive_root]
        values.extend(Path(item) for item in self.data.get("known_archive_roots", []))
        result: list[Path] = []
        seen: set[str] = set()
        for value in values:
            key = str(value.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result


class ExporterService:
    """Thin application adapter shared by GUI workflows; core stays authoritative."""

    def __init__(
        self,
        project_root: Path,
        *,
        settings: AppSettings | None = None,
        registry: ArchiveRegistry | None = None,
        account_lister: Callable[[], list[AccountChoice]] = list_account_choices,
        database_factory: Callable[[str], tuple[object, BackendInfo]] = create_database_for_account,
        compatibility_provider: Callable[[], WeChatCompatibility] = detect_wechat_compatibility,
        probe_runner: Callable[[str], CapabilityProbeResult] = probe_account_with_timeout,
    ):
        self.project_root = project_root
        from wechatauto.param import WxParam
        WxParam.ENABLE_FILE_LOGGER = False
        self.settings = settings or AppSettings.default(project_root)
        self.registry = registry or ArchiveRegistry(
            self.settings.app_data_dir / "archive_registry.json"
        )
        self.account_lister = account_lister
        self.database_factory = database_factory
        self.compatibility_provider = compatibility_provider
        self.probe_runner = probe_runner
        self.logger = configure_logging(self.settings.app_data_dir / "logs" / "gui.log")
        self._database: tuple[object, BackendInfo] | None = None
        self._database_lock = threading.RLock()
        self._target_registry_lock = threading.RLock()
        self._selected_account_identity = ""
        self._bootstrap_registry()
        self._recover_pending_deletions()
        self._recover_pending_current_capsules()

    @property
    def recovery_notices(self) -> tuple[str, ...]:
        return tuple(
            notice
            for notice in (self.settings.recovery_notice, self.registry.recovery_notice)
            if notice
        )

    def _bootstrap_registry(self) -> None:
        if self.registry.bootstrap_completed:
            return
        known_identities: set[str] = set()
        for entry in self.registry.entries():
            target_path = entry.path / "target.json"
            if not target_path.is_file():
                continue
            try:
                raw = json.loads(target_path.read_text(encoding="utf-8"))
                username = str(raw.get("username") or "").strip()
                if username:
                    known_identities.add(username)
            except (OSError, ValueError, TypeError):
                continue
        roots = self.settings.archive_roots()
        # One-time compatibility discovery for archives created by 0.4.0 in source mode.
        if discover_targets(self.settings.project_exports):
            roots.append(self.settings.project_exports)
        for root in roots:
            for target in discover_targets(root):
                if target.username in known_identities:
                    continue
                self.registry.register(target.root)
                known_identities.add(target.username)
        self.registry.mark_bootstrap_completed()

    def _recover_pending_deletions(self) -> None:
        for target in self._targets():
            if not (target.root / JOURNAL_NAME).is_file():
                continue
            try:
                with TargetOperationLock(target.root, "recover_snapshot_deletion"):
                    recover_pending_snapshot_deletion(target.root)
            except TargetBusyError:
                continue

    def _recover_pending_current_capsules(self) -> None:
        for target in self._targets():
            if not (target.root / "current_capsule_journal.json").is_file():
                continue
            try:
                with TargetOperationLock(target.root, "recover_current_capsule"):
                    recover_current_capsule_transaction(target.root)
            except TargetBusyError:
                continue

    def account_options(self) -> tuple[AccountChoice, ...]:
        return tuple(self.account_lister())

    def select_account(self, stable_id: str) -> AccountChoice:
        selected = next(
            (item for item in self.account_options() if item.stable_id == stable_id),
            None,
        )
        if selected is None:
            raise RuntimeError("选择的微信账号当前不存在，请重新检测。")
        with self._database_lock:
            if stable_id != self._selected_account_identity:
                self._database = None
            self._selected_account_identity = stable_id
        if self.settings.selected_account_identity != stable_id:
            self.settings.set_selected_account_identity(stable_id)
        return selected

    def _resolve_account(self) -> tuple[tuple[AccountChoice, ...], bool, bool]:
        accounts = self.account_options()
        if not accounts:
            return accounts, False, False
        if self._selected_account_identity and any(
            item.stable_id == self._selected_account_identity for item in accounts
        ):
            return accounts, False, False
        remembered = self.settings.selected_account_identity
        remembered_exists = any(item.stable_id == remembered for item in accounts)
        if len(accounts) == 1 and (not remembered or remembered_exists):
            self.select_account(accounts[0].stable_id)
            return accounts, False, False
        if len(accounts) == 1 and remembered and not remembered_exists:
            return accounts, True, True
        return accounts, True, bool(remembered and not remembered_exists)

    def _ensure_backup_compatible(self) -> WeChatStatus:
        status = self.detect_wechat()
        if not status.operations_allowed:
            raise RuntimeError(
                "微信在线读取能力检查未通过；本次操作没有继续。"
                f" error code: {status.runtime_capability.value}/"
                f"{status.probe_error_code.value}"
            )
        return status

    def _db(self):
        with self._database_lock:
            if self._database is None:
                if not self._selected_account_identity:
                    accounts, selection_required, _ = self._resolve_account()
                    if not accounts:
                        raise RuntimeError("未发现可读取的微信账号。")
                    if selection_required:
                        raise RuntimeError("检测到多个或已变化的微信账号，请先明确选择。")
                self._database = self.database_factory(self._selected_account_identity)
        return self._database

    def runtime_work_root(self) -> Path:
        work_root = self.settings.app_data_dir / "work"
        try:
            work_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeWorkDirectoryError() from exc
        if not work_root.is_dir():
            raise RuntimeWorkDirectoryError()
        return work_root

    def _targets(self) -> list[ArchiveTarget]:
        with self._target_registry_lock:
            seen_paths: set[str] = set()
            by_identity: dict[str, ArchiveTarget] = {}
            candidates: list[ArchiveTarget] = []
            for entry in self.registry.entries():
                target_path = entry.path / "target.json"
                if not target_path.is_file():
                    continue
                try:
                    raw = json.loads(target_path.read_text(encoding="utf-8"))
                    username = str(raw.get("username") or "").strip()
                    display_name = str(raw.get("display_name") or username).strip()
                    if username:
                        candidates.append(ArchiveTarget(entry.path, username, display_name))
                except (OSError, ValueError, TypeError):
                    continue
            for target in candidates:
                path_key = str(target.root.resolve()).casefold()
                identity_key = target.username
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                existing = by_identity.get(identity_key)
                if existing is None or self._target_preference(target) > self._target_preference(existing):
                    by_identity[identity_key] = target
            return sorted(
                by_identity.values(), key=lambda item: item.display_name.casefold()
            )

    @staticmethod
    def _target_preference(target: ArchiveTarget) -> tuple[int, int]:
        has_current_capsule = (
            (target.root / "chat.md").is_file()
            and (target.root / "capsule_meta.json").is_file()
        )
        return (int(has_current_capsule), int(target.message_store.is_file()))

    def find_target(self, root: Path) -> ArchiveTarget:
        resolved = root.resolve()
        for target in self._targets():
            if target.root.resolve() == resolved:
                return target
        raise FileNotFoundError("备份对象不存在或尚未登记。")

    def list_targets(self) -> list[TargetSummary]:
        summaries: list[TargetSummary] = []
        for target in self._targets():
            index = load_snapshot_index(target)["snapshots"]
            integrity_ok = None
            integrity_label = "○ 尚未保存"
            avatar = None
            message_count = 0
            last_saved_at = None
            avatar_history_count = 0
            metadata_path = target.root / "capsule_meta.json"
            capsule_exists = (target.root / "chat.md").is_file() and metadata_path.is_file()
            if capsule_exists:
                result = verify_current_capsule(target.root)
                integrity_ok = True if result.ok and result.manifest_present else None if result.ok else False
                integrity_label = (
                    "✓ 时间胶囊完整"
                    if integrity_ok is True
                    else "⚠ 未建立校验基线"
                    if result.ok
                    else "✕ 时间胶囊完整性异常"
                )
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    message_count = int(metadata.get("message_count", 0))
                    last_saved_at = str(metadata.get("last_saved_at") or "") or None
                    avatar_history_count = int(metadata.get("avatar_history_count", 0))
                    relative = str(
                        ((metadata.get("avatars") or {}).get("contact") or {}).get("path") or ""
                    )
                    candidate = target.root / relative
                    if relative and candidate.is_file():
                        avatar = candidate
                except (OSError, ValueError, TypeError):
                    pass
            summaries.append(
                # Every listed target comes from the registry after bootstrap.
                # Keep the defensive empty fallback for damaged in-memory state.
                TargetSummary(
                    target.root,
                    target.display_name,
                    message_count,
                    last_saved_at,
                    integrity_label,
                    integrity_ok,
                    avatar,
                    capsule_exists,
                    avatar_history_count,
                    len(index),
                    (
                        entry.archive_id
                        if (entry := self.registry.entry_for_path(target.root))
                        else ""
                    ),
                )
            )
        return summaries

    def remove_backup_target(self, archive_id: str) -> BackupTargetRemovalResult:
        """Remove one list entry without reading or mutating its archive directory."""
        with self._target_registry_lock:
            removed = self.registry.remove(archive_id)
        return BackupTargetRemovalResult(
            archive_id,
            removed,
            "已从备份对象一览中移除。"
            if removed
            else "该备份对象已经不在列表中。",
        )

    def list_snapshots(self, target_root: Path) -> list[SnapshotSummary]:
        target = self.find_target(target_root)
        result: list[SnapshotSummary] = []
        for entry in reversed(load_snapshot_index(target)["snapshots"]):
            snapshot_path = target.snapshots_dir / entry["id"]
            metadata = {}
            try:
                metadata = json.loads(
                    (snapshot_path / "export_meta.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass
            integrity = verify_snapshot_offline(target.root, entry["id"])
            ok = integrity.ok and integrity.manifest_present
            label = "✓ 完整" if ok else "⚠ 未建立校验基线" if integrity.ok else "✕ 完整性异常"
            result.append(
                SnapshotSummary(
                    entry["id"], snapshot_path, entry.get("created_at"),
                    int(entry.get("message_count", 0)),
                    int(entry.get("new_message_count", 0)),
                    str(metadata.get("backup_mode") or (
                        "legacy" if entry.get("legacy_import") else "quick"
                    )),
                    label, ok,
                )
            )
        return result

    def detect_wechat(self) -> WeChatStatus:
        compatibility = self.compatibility_provider()
        version_label = (
            f"WeChat {compatibility.version} · {compatibility.label}"
            if compatibility.detected
            else "WeChat 未检测到"
        )
        if not compatibility.detected:
            status = WeChatStatus(
                version_verification=compatibility.status,
                runtime_capability=RuntimeCapability.WECHAT_NOT_RUNNING,
                probe_error_code=ProbeErrorCode.PROCESS_NOT_FOUND,
                version=compatibility.version,
                version_label=version_label,
                message="没有检测到正在运行的 Windows 微信。",
            )
            self._log_compatibility(status)
            return status
        if compatibility.status == VersionVerification.UNSUPPORTED:
            status = WeChatStatus(
                version_verification=compatibility.status,
                runtime_capability=RuntimeCapability.NOT_PROBED,
                probe_error_code=ProbeErrorCode.NONE,
                version=compatibility.version,
                version_label=version_label,
                message="当前微信主版本不在支持范围内。",
            )
            self._log_compatibility(status)
            return status
        try:
            accounts, selection_required, remembered_missing = self._resolve_account()
        except Exception:
            status = WeChatStatus(
                version_verification=compatibility.status,
                runtime_capability=RuntimeCapability.PROBE_FAILED,
                probe_error_code=ProbeErrorCode.DATABASE_DISCOVERY_FAILED,
                version=compatibility.version,
                version_label=version_label,
                message="微信账号检测失败，请重新检测。",
            )
            self._log_compatibility(status)
            return status
        if not accounts:
            status = WeChatStatus(
                version_verification=compatibility.status,
                runtime_capability=RuntimeCapability.NOT_LOGGED_IN,
                probe_error_code=ProbeErrorCode.ACCOUNT_NOT_AVAILABLE,
                version=compatibility.version,
                version_label=version_label,
                message="没有检测到可读取的已登录微信。",
            )
            self._log_compatibility(status)
            return status
        if selection_required:
            message = (
                "此前选择的账号当前不存在，请重新选择。"
                if remembered_missing
                else "请选择要使用的微信账号。"
            )
            status = WeChatStatus(
                version_verification=compatibility.status,
                runtime_capability=RuntimeCapability.ACCOUNT_SELECTION_REQUIRED,
                probe_error_code=ProbeErrorCode.ACCOUNT_SELECTION_REQUIRED,
                version=compatibility.version,
                version_label=version_label, message=message,
                account_count=len(accounts), selection_required=True,
                accounts=accounts,
                remembered_missing=remembered_missing,
            )
            self._log_compatibility(status)
            return status
        selected = next(
            item for item in accounts if item.stable_id == self._selected_account_identity
        )
        try:
            probe = self.probe_runner(selected.stable_id)
        except Exception:
            probe = CapabilityProbeResult(
                RuntimeCapability.PROBE_FAILED, ProbeErrorCode.INTERNAL_PROBE_ERROR
            )
        messages = {
            RuntimeCapability.READY: "本地数据库只读检查通过。",
            RuntimeCapability.PROBE_FAILED: (
                "检测到微信，但基础兼容性检查超时。可以稍后重新检测。"
                if probe.error_code == ProbeErrorCode.TIMEOUT
                else "检测到微信，但本地数据库只读检查失败。可以稍后重新检测。"
            ),
        }
        status = WeChatStatus(
            version_verification=compatibility.status,
            runtime_capability=probe.runtime_capability,
            probe_error_code=probe.error_code,
            account_name=probe.account_display_name or selected.display_label,
            version=compatibility.version,
            version_label=version_label,
            message=messages.get(probe.runtime_capability, "微信当前不可读取。"),
            account_count=len(accounts),
            accounts=accounts,
        )
        self._log_compatibility(status)
        return status

    def _log_compatibility(self, status: WeChatStatus) -> None:
        self.logger.info(
            "compat.result version=%s verification=%s runtime=%s error=%s",
            status.version or "unknown",
            status.version_verification.value,
            status.runtime_capability.value,
            status.probe_error_code.value,
        )

    def search_contacts(self, keyword: str) -> list[ContactChoice]:
        keyword = keyword.strip()
        if not keyword:
            return []
        self._ensure_backup_compatible()
        db, _ = self._db()
        from .contacts import _aliases_for_matches, contact_choice_from_search_result

        raw = db.search_contact(keyword)
        aliases = _aliases_for_matches(db, raw)
        return [
            contact_choice_from_search_result(
                item, aliases.get(str(item.get("username") or ""), "")
            )
            for item in raw
        ]

    def add_target(self, contact: ContactChoice) -> TargetSummary:
        with self._target_registry_lock:
            existing = next(
                (
                    target
                    for target in self._targets()
                    if target.username == contact.username
                ),
                None,
            )
            if existing is not None:
                return next(
                    item
                    for item in self.list_targets()
                    if item.root.resolve() == existing.root.resolve()
                )
            target = create_target(self.settings.archive_root, contact)
            self.registry.register(target.root)
            return next(
                item
                for item in self.list_targets()
                if item.root.resolve() == target.root.resolve()
            )

    def import_existing_archive(self, target_root: Path) -> TargetSummary:
        root = target_root.resolve()
        required = (
            root / "target.json",
            root / "archive_state.json",
            root / "snapshot_index.json",
            root / "state" / "schema.json",
            root / "state" / "messages.jsonl",
        )
        if not all(path.is_file() for path in required):
            raise ValueError(f"所选目录不是完整的{PRODUCT_NAME}备份对象。")
        target_raw = json.loads(required[0].read_text(encoding="utf-8"))
        index_raw = json.loads(required[2].read_text(encoding="utf-8"))
        state_schema = json.loads(required[3].read_text(encoding="utf-8"))
        if state_schema.get('schema_version') == 3:
            from .history_contracts import ArchiveScope
            from .product_history import read_product
            from .track2_contracts import decode_dto
            # Import is an offline read/registration, not permission to save as
            # this account. Save independently validates the selected account.
            read_product(root, decode_dto(ArchiveScope, state_schema['scope']))
        elif int(state_schema.get('schema_version', 0)) > 2:
            raise RuntimeError("该归档由更新版本创建；当前版本不会登记或修改它。")
        if int(target_raw.get("schema_version", 0)) > 2 or int(index_raw.get("schema_version", 0)) > 2:
            raise RuntimeError("该归档由更新版本创建；当前版本不会登记或修改它。")
        if not str(target_raw.get("username") or "").strip() or not isinstance(index_raw.get("snapshots"), list):
            raise ValueError("所选备份对象的身份或时间胶囊索引无效。")
        self.registry.register(root)
        return next(item for item in self.list_targets() if item.root.resolve() == root)

    def _prepare_mutation(self, target: ArchiveTarget) -> MigrationResult:
        return migrate_mutable_state(target.root)

    def quick_backup(self, target_root: Path) -> BackupOutcome:
        from .history_service import save_history
        return save_history(self, target_root, 'quick')

    def _legacy_quick_backup(self, target_root: Path) -> BackupOutcome:
        self._ensure_backup_compatible()
        target = self.find_target(target_root)
        with TargetOperationLock(target.root, "quick_backup"):
            self._prepare_mutation(target)
            db, backend = self._db()
            refreshed = contact_by_username(db, target.username)
            if refreshed and refreshed.display_name != target.display_name:
                target = ArchiveTarget(target.root, target.username, refreshed.display_name)
                save_target(target)
            work_root = self.runtime_work_root()
            migrate_legacy_export(db, target, work_root, self.logger)
            capsule = create_current_capsule(
                db, target, backend.wxid, work_root, self.logger,
                input_func=lambda prompt: "", print_func=lambda *args: None,
            )
            return BackupOutcome(capsule)

    def reconciliation_backup(self, target_root: Path) -> BackupOutcome:
        from .history_service import save_history
        return save_history(self, target_root, 'full')

    def _legacy_reconciliation_backup(self, target_root: Path) -> BackupOutcome:
        self._ensure_backup_compatible()
        target = self.find_target(target_root)
        with TargetOperationLock(target.root, "reconciliation_backup"):
            self._prepare_mutation(target)
            db, backend = self._db()
            refreshed = contact_by_username(db, target.username)
            if refreshed and refreshed.display_name != target.display_name:
                target = ArchiveTarget(target.root, target.username, refreshed.display_name)
                save_target(target)
            work_root = self.runtime_work_root()
            migrate_legacy_export(db, target, work_root, self.logger)
            capsule, reconciliation, report = reconcile_and_update_capsule(
                db, target, backend.wxid, work_root, self.logger,
                input_func=lambda prompt: "", print_func=lambda *args: None,
            )
            return BackupOutcome(capsule, reconciliation, report)

    def delete_snapshot(
        self, target_root: Path, snapshot_id: str
    ) -> SnapshotDeletionResult:
        target = self.find_target(target_root)
        with TargetOperationLock(target.root, "delete_snapshot"):
            recover_pending_snapshot_deletion(target.root)
            return delete_snapshot_transaction(target, snapshot_id)

    def verify_snapshots(self, target_root: Path, latest_only: bool = False) -> list[IntegrityResult]:
        target = self.find_target(target_root)
        if latest_only:
            index = load_snapshot_index(target)["snapshots"]
            return [verify_snapshot_offline(target.root, index[-1]["id"])] if index else []
        return verify_all_snapshots_offline(target.root)

    def verify_capsule(self, target_root: Path) -> IntegrityResult:
        target = self.find_target(target_root)
        return verify_current_capsule(target.root)

    def avatar_history(self, target_root: Path) -> list[AvatarHistoryEntry]:
        target = self.find_target(target_root)
        return load_avatar_history(target.root)

    def technical_info(self) -> dict[str, str]:
        return {
            "product_version": __version__,
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "target_count": str(len(self._targets())),
        }

    def generate_diagnostics(self):
        from .diagnostics import generate_diagnostics

        return generate_diagnostics(self)

    @staticmethod
    def target_log_id(target_root: Path) -> str:
        return hashlib.sha256(str(target_root.resolve()).encode("utf-8")).hexdigest()[:12]
