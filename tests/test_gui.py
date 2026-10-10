from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QStackedWidget,
)

from wechat_exporter.account import AccountChoice, BackendInfo
from wechat_exporter.archive import ArchiveTarget
from wechat_exporter.current_capsule import AvatarHistoryEntry, CapsuleResult
from wechat_exporter.gui import dialogs as dialogs_module
from wechat_exporter.gui.dialogs import AboutDialog, AddTargetDialog, AvatarHistoryDialog
from wechat_exporter.gui.errors import map_exception
from wechat_exporter.gui.main_window import (
    MainWindow,
    TargetCard,
    backup_mode_label,
    display_time,
    status_presentation,
)
from wechat_exporter.gui.theme import SemanticRole
from wechat_exporter.gui.workers import TaskWorker
from wechat_exporter.locks import TargetBusyError, TargetOperationLock
from wechat_exporter.migrations import UnsupportedFutureSchema
from wechat_exporter.models import ContactChoice
from wechat_exporter.compatibility import (
    CapabilityProbeResult,
    CompatibilityStatus,
    ProbeErrorCode,
    RuntimeCapability,
    VersionVerification,
    WeChatCompatibility,
)
from wechat_exporter.service import (
    AppSettings,
    ExporterService,
    TargetSummary,
    WeChatStatus,
)
from wechat_exporter.utils import write_json_atomic


def make_target(root: Path, *, with_snapshot: bool = False) -> Path:
    target = root / "exports" / "Synthetic Contact"
    target.mkdir(parents=True)
    write_json_atomic(
        target / "target.json",
        {"schema_version": 2, "username": "internal_target", "display_name": "Synthetic Contact"},
    )
    snapshots = []
    if with_snapshot:
        snapshot_id = "20260912_190000"
        snapshot = target / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True)
        (snapshot / "chat.md").write_text("# archive\n", encoding="utf-8")
        write_json_atomic(
            snapshot / "export_meta.json",
            {
                "status": "COMPLETE",
                "snapshot_id": snapshot_id,
                "message_count": 4,
                "new_message_count": 1,
                "created_at": "2026-09-12T19:00:00+09:00",
                "backup_mode": "reconciliation",
            },
        )
        snapshots.append(
            {
                "id": snapshot_id,
                "created_at": "2026-09-12T19:00:00+09:00",
                "message_count": 4,
                "new_message_count": 1,
                "status": "COMPLETE",
            }
        )
    write_json_atomic(target / "snapshot_index.json", {"schema_version": 2, "snapshots": snapshots})
    write_json_atomic(
        target / "archive_state.json",
        {"schema_version": 2, "message_count": 4 if with_snapshot else 0},
    )
    (target / "state").mkdir()
    write_json_atomic(target / "state" / "schema.json", {"schema_version": 1})
    (target / "state" / "messages.jsonl").write_text("", encoding="utf-8")
    return target


def make_service(root: Path, target: Path, database=(object(), BackendInfo("1.2.1", "a", "me"))):
    settings = AppSettings(root / "settings.json", root / "exports")
    settings.data["archive_root"] = str(root / "exports")
    settings.data["selected_account_identity"] = "account-1"
    account = AccountChoice("account-1", "locator-1", "微信账号 1", "••••0001", 1)
    tested = WeChatCompatibility(
        True, "4.1.13.63", CompatibilityStatus.TESTED, "✓ 已验证"
    )
    with patch("wechat_exporter.service.configure_logging", return_value=Mock()):
        service = ExporterService(
            root,
            settings=settings,
            account_lister=Mock(return_value=[account]),
            database_factory=Mock(return_value=database),
            compatibility_provider=Mock(return_value=tested),
            probe_runner=Mock(
                return_value=CapabilityProbeResult(RuntimeCapability.READY)
            ),
        )
    return service


class FakeWindowService:
    def __init__(self, root: Path):
        self.project_root = root
        self.settings = AppSettings(root / "settings.json", root / "exports")
        self.recovery_notices = ()

    def list_targets(self):
        return []

    def detect_wechat(self):
        return WeChatStatus(message="未检测到")


class FakeCloseEvent:
    def __init__(self):
        self.ignored = False
        self.accepted = False

    def ignore(self):
        self.ignored = True

    def accept(self):
        self.accepted = True


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_quick_backup_service_dispatches_existing_core(self):
        from tests.full_history_fixtures import make_fixture, make_service as original_service, close_test_logs
        from tests.one_shot_provider_fixtures import PlaintextDescriptor
        from wechat_exporter import one_shot_provider
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_fixture(root)
            service = original_service(root, fixture)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    first = service.quick_backup(fixture.archive)
                    outcome = service.quick_backup(fixture.archive)
                self.assertEqual(first.capsule.save_mode, 'reconciliation')
                self.assertEqual(outcome.snapshot.path, fixture.archive)
                self.assertEqual(outcome.capsule.save_mode, 'quick')
                self.assertEqual((outcome.capsule.message_count, outcome.capsule.new_message_count), (6, 0))
                self.assertTrue(service.verify_capsule(fixture.archive).ok)
            finally:
                close_test_logs(root)

    def test_reconciliation_service_dispatches_existing_core(self):
        from tests.full_history_fixtures import make_fixture, make_service as original_service, close_test_logs
        from tests.one_shot_provider_fixtures import PlaintextDescriptor
        from wechat_exporter import one_shot_provider
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_fixture(root)
            service = original_service(root, fixture)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    outcome = service.reconciliation_backup(fixture.archive)
                self.assertEqual(outcome.snapshot.path, fixture.archive)
                self.assertEqual(outcome.capsule.save_mode, 'reconciliation')
                self.assertEqual(outcome.capsule.message_count, 6)
                self.assertIsNotNone(outcome.reconciliation)
                self.assertTrue(outcome.report_path.is_file())
                metadata = json.loads((fixture.archive / 'capsule_meta.json').read_text(encoding='utf8'))
                self.assertEqual(metadata['counts']['raw_source_count'], 6)
                self.assertEqual(metadata['counts']['archive_only_count'], 1)
                self.assertTrue(service.verify_capsule(fixture.archive).ok)
            finally:
                close_test_logs(root)

    def test_offline_verify_does_not_initialize_wechat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_target(root, with_snapshot=True)
            factory = Mock(side_effect=AssertionError("database must remain offline"))
            settings = AppSettings(root / "settings.json", root / "exports")
            settings.data["archive_root"] = str(root / "exports")
            with patch("wechat_exporter.service.configure_logging", return_value=Mock()):
                service = ExporterService(root, settings=settings, database_factory=factory)
            results = service.verify_snapshots(target)
            self.assertEqual(len(results), 1)
            factory.assert_not_called()

    def test_delete_snapshot_does_not_initialize_wechat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_target(root, with_snapshot=True)
            factory = Mock(side_effect=AssertionError("database must remain offline"))
            settings = AppSettings(root / "settings.json", root / "exports")
            settings.data["archive_root"] = str(root / "exports")
            with patch("wechat_exporter.service.configure_logging", return_value=Mock()):
                service = ExporterService(root, settings=settings, database_factory=factory)
            result = service.delete_snapshot(target, "20260912_190000")
            self.assertEqual(result.remaining_snapshot_count, 0)
            summary = service.list_targets()[0]
            self.assertEqual(summary.legacy_snapshot_count, 0)
            self.assertIsNone(summary.last_backup_at)
            self.assertTrue(target.is_dir())
            self.assertEqual(
                json.loads((target / "archive_state.json").read_text(encoding="utf-8"))["message_count"],
                4,
            )
            factory.assert_not_called()

    def test_operation_lock_blocks_second_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = TargetOperationLock(root, "first")
            first.acquire()
            try:
                with self.assertRaises(TargetBusyError):
                    TargetOperationLock(root, "second").acquire()
            finally:
                first.release()

    def test_operation_lock_recovers_only_dead_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json_atomic(
                root / ".operation.lock",
                {"pid": 999999, "operation_id": "old", "created_at": "old"},
            )
            lock = TargetOperationLock(root, "new", process_checker=lambda pid: False)
            lock.acquire()
            try:
                self.assertTrue(lock.path.is_file())
                self.assertNotEqual(json.loads(lock.path.read_text())["operation_id"], "old")
            finally:
                lock.release()

    def test_worker_success_is_emitted_only_after_task_completion(self):
        events = []

        def task():
            events.append("snapshot-complete")
            return "ok"

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda result: events.append("succeeded:" + result))
        worker.signals.finished.connect(lambda: events.append("finished"))
        worker.run()
        self.assertEqual(events, ["snapshot-complete", "succeeded:ok", "finished"])

    def test_worker_exception_maps_to_human_error_without_private_text(self):
        private = "private-chat-body"
        mapped = map_exception(RuntimeError(private))
        self.assertEqual(mapped.code, "OPERATION_FAILED")
        self.assertNotIn(private, mapped.message)
        self.assertNotIn(private, mapped.technical_summary)

    def test_add_dialog_disables_second_dispatch_until_add_finishes(self):
        callbacks = {}

        def run_task(label, task, on_success, *, on_finished=None):
            del label, task, on_success
            callbacks["finished"] = on_finished

        dialog = AddTargetDialog(Mock(), run_task)
        dialog.selected_contact = ContactChoice.from_fields(
            username="stable-contact-1", remark="Synthetic Contact"
        )
        dialog.buttons.button(QDialogButtonBox.Ok).setEnabled(True)

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.Ok
        ):
            dialog.confirm_add()

        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        self.assertFalse(dialog.search_button.isEnabled())
        callbacks["finished"]()
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        self.assertTrue(dialog.search_button.isEnabled())
        dialog.close()

    def test_future_schema_maps_to_fail_closed_gui_message(self):
        mapped = map_exception(UnsupportedFutureSchema("由更新版本创建"))
        self.assertEqual(mapped.code, "FUTURE_SCHEMA")
        self.assertIn("不会冒险", mapped.message)

    def test_snapshot_list_uses_index_metadata_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_target(root, with_snapshot=True)
            service = make_service(root, target)
            rows = service.list_snapshots(target)
            self.assertEqual(rows[0].message_count, 4)
            self.assertEqual(rows[0].new_message_count, 1)
            self.assertEqual(backup_mode_label(rows[0].backup_mode), "完整核对")

    def test_missing_avatar_uses_local_fallback(self):
        summary = TargetSummary(
            Path("missing"), "Synthetic Contact", 4, None, "✓ 完整", True, None,
            True, 0, 1,
        )
        card = TargetCard(summary)
        self.assertFalse(card.avatar.pixmap().isNull())

    def test_collapsed_card_is_within_72_to_84_logical_pixels(self):
        summary = TargetSummary(Path("target"), "Example", 1385, "2026-09-12T20:00:00+09:00",
                                "✓ 时间胶囊完整", True, None, True, 2, 4)
        card = TargetCard(summary)
        self.assertGreaterEqual(card.height(), 72)
        self.assertLessEqual(card.height(), 84)

    def test_status_icon_is_inline_with_name_and_has_tooltip(self):
        summary = TargetSummary(Path("target"), "Example", 1, "2026-09-12T20:00:00+09:00",
                                "✓ 时间胶囊完整", True, None, True, 1, 4)
        card = TargetCard(summary)
        self.assertEqual(card.integrity_icon.text(), "✓")
        self.assertEqual(card.integrity_icon.toolTip(), summary.integrity_label)
        self.assertEqual(card.integrity_icon.parentWidget(), card.header)

    def test_unsaved_target_has_circle_and_no_fake_count(self):
        summary = TargetSummary(Path("target"), "New", 0, None, "○ 尚未保存", None,
                                None, False, 0, 0)
        card = TargetCard(summary)
        self.assertEqual(card.integrity_icon.text(), "○")
        self.assertIn("尚未生成", card.summary_label.text())
        self.assertNotIn("0 条", card.summary_label.text())

    def test_runtime_gate_disables_only_online_card_actions(self):
        summary = TargetSummary(Path("target"), "Example", 1, None,
                                "✓ 时间胶囊完整", True, None, True, 1, 0)
        card = TargetCard(summary)
        card.set_online_actions_enabled(False)
        self.assertFalse(card.quick_button.isEnabled())
        self.assertFalse(card.reconcile_button.isEnabled())
        self.assertTrue(card.open_chat_button.isEnabled())
        self.assertTrue(card.open_folder_button.isEnabled())
        self.assertTrue(card.history_button.isEnabled())
        self.assertTrue(card.verify_button.isEnabled())

    def test_card_metadata_uses_secondary_text_not_disabled_text(self):
        """Catches the RC3 palette(mid) metadata rule and disabled-role regressions."""
        summary = TargetSummary(
            Path("target"), "Example", 1385, "2026-09-12T20:00:00+09:00",
            "✓ 时间胶囊完整", True, None, True, 1, 0,
        )
        card = TargetCard(summary)
        self.assertTrue(card.summary_label.isEnabled())
        self.assertEqual(
            card.summary_label.property("semanticRole"),
            SemanticRole.TEXT_SECONDARY.value,
        )

    def test_remove_action_exists_only_inside_expanded_actions_panel(self):
        """Catches exposing the removal action on the compact card face."""
        summary = TargetSummary(
            Path("target"), "Synthetic Contact", 12, None, "✓ 时间胶囊完整", True,
            None, True, 0, 0, archive_id="archive-a",
        )
        card = TargetCard(summary)
        self.assertTrue(hasattr(card, "remove_button"))
        self.assertIs(card.remove_button.parentWidget(), card.actions_panel)
        self.assertTrue(card.actions_panel.isHidden())
        self.assertFalse(card.remove_button.isVisible())
        card.show()
        card.set_expanded(True)
        self.app.processEvents()
        self.assertTrue(card.remove_button.isVisible())
        card.close()

    def test_remove_confirmation_copy_is_registry_only_and_cancel_is_default(self):
        """Catches deletion wording or a destructive default action."""
        dialog_type = getattr(dialogs_module, "RemoveBackupTargetDialog", None)
        self.assertIsNotNone(dialog_type, "RC3 has no remove-target confirmation dialog")
        dialog = dialog_type("Synthetic Contact")
        visible = " ".join(label.text() for label in dialog.findChildren(QLabel))
        buttons = dialog.findChild(QDialogButtonBox)
        remove = buttons.button(QDialogButtonBox.Ok)
        cancel = buttons.button(QDialogButtonBox.Cancel)
        self.assertEqual(dialog.windowTitle(), "从备份对象一览中移除 Synthetic Contact？")
        self.assertIn("只会把 Synthetic Contact 从微信时间胶囊的列表中移除", visible)
        self.assertIn("聊天、图片、视频、历史头像和其他文件不会被删除", visible)
        self.assertIn("添加已有备份对象", visible)
        self.assertNotIn("永久删除", visible)
        self.assertEqual(remove.text(), "移除")
        self.assertEqual(cancel.text(), "取消")
        self.assertFalse(remove.isDefault())
        self.assertTrue(cancel.isDefault())
        dialog.close()

    def test_remove_cancel_keeps_registry_and_card_unchanged(self):
        """Catches Cancel dispatching a registry mutation."""
        dialog_type = getattr(dialogs_module, "RemoveBackupTargetDialog", None)
        self.assertIsNotNone(dialog_type, "RC3 has no remove-target confirmation dialog")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_target(root)
            service = make_service(root, target)
            summary = service.list_targets()[0]
            window = MainWindow(service)
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            before = service.registry.entries()
            self.assertTrue(hasattr(window, "remove_backup_target"))
            with (
                patch.object(dialog_type, "exec", return_value=QDialog.Rejected),
                patch.object(service, "remove_backup_target") as remove,
            ):
                window.remove_backup_target(summary)
            remove.assert_not_called()
            self.assertEqual(service.registry.entries(), before)
            self.assertEqual(len(window.target_cards), 1)
            window.close()

    def test_remove_confirm_refreshes_cards_without_restart(self):
        """Catches a successful removal leaving a stale card until app restart."""
        dialog_type = getattr(dialogs_module, "RemoveBackupTargetDialog", None)
        self.assertIsNotNone(dialog_type, "RC3 has no remove-target confirmation dialog")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_target(root)
            service = make_service(root, target)
            summary = service.list_targets()[0]
            window = MainWindow(service)
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            with patch.object(dialog_type, "exec", return_value=QDialog.Accepted):
                window.remove_backup_target(summary)
                window.thread_pool.waitForDone(3000)
                self.app.processEvents()
            self.assertEqual(service.registry.entries(), [])
            self.assertEqual(window.target_cards, [])
            self.assertTrue(target.is_dir())
            window.close()

    def test_top_account_and_version_use_readable_secondary_role(self):
        """Catches account/version text inheriting a border or disabled color."""
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            self.assertTrue(window.wechat_account.isEnabled())
            self.assertEqual(
                window.wechat_account.property("semanticRole"),
                SemanticRole.TEXT_SECONDARY.value,
            )
            window.close()

    def test_manual_refresh_always_restores_button(self):
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window.refresh_wechat_status(manual=True)
            self.assertFalse(window.refresh_button.isEnabled())
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            self.assertTrue(window.refresh_button.isEnabled())
            self.assertIn(window.refresh_button.text(), ("✓ 重新检测完成", "重新检测失败", "重新检测"))
            window.close()

    def test_manual_refresh_failure_also_restores_button(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = FakeWindowService(Path(temporary))
            window = MainWindow(service)
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            service.detect_wechat = Mock(side_effect=RuntimeError("private failure"))
            with patch.object(QMessageBox, "exec", return_value=QMessageBox.Ok):
                window.refresh_wechat_status(manual=True)
                window.thread_pool.waitForDone(3000)
                self.app.processEvents()
            self.assertTrue(window.refresh_button.isEnabled())
            self.assertIn(window.refresh_button.text(), ("重新检测失败", "重新检测"))
            window.close()

    def test_untested_ready_hides_internal_verification_from_main_gui(self):
        """Catches exposing the internal UNTESTED maintenance state to users."""
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window._show_wechat_status(
                WeChatStatus(
                    version_verification=VersionVerification.UNTESTED,
                    runtime_capability=RuntimeCapability.READY,
                    probe_error_code=ProbeErrorCode.NONE,
                    account_name="微信账号 1",
                    version="4.1.13.65",
                    account_count=1,
                )
            )
            visible = " ".join((
                window.wechat_status.text(),
                window.wechat_account.text(),
                window.wechat_verification.text(),
            ))
            self.assertIn("● 已连接", visible)
            self.assertIn("微信账号 1", visible)
            self.assertIn("WeChat 4.1.13.65", visible)
            for forbidden in ("⚠", "尚未验证", "尚未正式验证", "已验证", "基础兼容性检查已通过"):
                self.assertNotIn(forbidden, visible)
            self.assertFalse(window.wechat_verification.isVisible())
            window.close()

    def test_untested_ready_uses_success_connection_without_warning_component(self):
        """Catches routing READY through the warning component because it is UNTESTED."""
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window._show_wechat_status(
                WeChatStatus(
                    version_verification=VersionVerification.UNTESTED,
                    runtime_capability=RuntimeCapability.READY,
                    probe_error_code=ProbeErrorCode.NONE,
                    account_name="微信账号 1",
                    version="4.1.13.65",
                    account_count=1,
                )
            )
            self.assertEqual(
                window.wechat_status.property("semanticRole"),
                SemanticRole.STATUS_SUCCESS.value,
            )
            self.assertEqual(window.wechat_verification.text(), "")
            self.assertFalse(window.wechat_verification.isVisible())
            self.assertEqual(
                window.wechat_account.property("semanticRole"),
                SemanticRole.TEXT_SECONDARY.value,
            )
            window.close()

    def test_tested_ready_hides_internal_verification_from_main_gui(self):
        """Catches exposing the internal TESTED maintenance state as a badge."""
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window._show_wechat_status(
                WeChatStatus(
                    version_verification=VersionVerification.TESTED,
                    runtime_capability=RuntimeCapability.READY,
                    account_name="微信账号 1",
                    version="4.1.13.65",
                    account_count=1,
                )
            )
            self.assertEqual(
                window.wechat_status.property("semanticRole"),
                SemanticRole.STATUS_SUCCESS.value,
            )
            self.assertEqual(window.wechat_account.text(), "微信账号 1 · WeChat 4.1.13.65")
            self.assertEqual(window.wechat_verification.text(), "")
            self.assertFalse(window.wechat_verification.isVisible())
            window.close()

    def test_tested_and_untested_ready_have_equivalent_user_status_semantics(self):
        """Catches making exact-version verification alter the READY UI structure."""
        presentations = [
            status_presentation(
                WeChatStatus(
                    version_verification=verification,
                    runtime_capability=RuntimeCapability.READY,
                    account_name="微信账号 1",
                    version=version,
                )
            )
            for verification, version in (
                (VersionVerification.TESTED, "4.1.13.65"),
                (VersionVerification.UNTESTED, "4.1.99.1"),
            )
        ]
        for presentation in presentations:
            self.assertEqual(presentation.connection_text, "● 已连接")
            self.assertEqual(presentation.connection_role, SemanticRole.STATUS_SUCCESS)
            self.assertEqual(presentation.verification_text, "")
        self.assertEqual(
            presentations[0].account_text.replace("4.1.13.65", "<version>"),
            presentations[1].account_text.replace("4.1.99.1", "<version>"),
        )

    def test_probe_failed_uses_error_status_while_archive_metadata_stays_readable(self):
        """Catches probe failure borrowing success/warning colors or disabling archive text."""
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window._show_targets([
                TargetSummary(Path(temporary) / "archive", "Synthetic Contact", 12, None,
                              "✓ 时间胶囊完整", True, None, True, 0, 0)
            ])
            window._show_wechat_status(
                WeChatStatus(
                    version_verification=VersionVerification.UNTESTED,
                    runtime_capability=RuntimeCapability.PROBE_FAILED,
                    probe_error_code=ProbeErrorCode.TIMEOUT,
                    version="4.1.13.65",
                    message="检测到微信，但本地数据库只读检查失败。",
                    account_count=1,
                )
            )
            self.assertEqual(window.wechat_status.text(), "✕ 暂时无法读取微信")
            self.assertIn("基础读取检查没有通过", window.wechat_account.text())
            self.assertNotIn("尚未验证", window.wechat_account.text())
            self.assertEqual(
                window.wechat_status.property("semanticRole"),
                SemanticRole.STATUS_ERROR.value,
            )
            self.assertTrue(window.target_cards[0].summary_label.isEnabled())
            self.assertEqual(
                window.target_cards[0].summary_label.property("semanticRole"),
                SemanticRole.TEXT_SECONDARY.value,
            )
            window.close()

    def test_not_running_and_account_selection_have_distinct_neutral_warning_roles(self):
        """Catches treating all non-READY states as the same warning color."""
        not_running = status_presentation(
            WeChatStatus(runtime_capability=RuntimeCapability.WECHAT_NOT_RUNNING)
        )
        selection = status_presentation(
            WeChatStatus(
                runtime_capability=RuntimeCapability.ACCOUNT_SELECTION_REQUIRED,
                selection_required=True,
            )
        )
        self.assertEqual(not_running.connection_text, "○ 未检测到微信")
        self.assertEqual(not_running.connection_role, SemanticRole.STATUS_NEUTRAL)
        self.assertEqual(not_running.account_text, "请启动并登录 Windows 微信。")
        self.assertEqual(selection.connection_text, "● 检测到多个微信账号")
        self.assertEqual(selection.connection_role, SemanticRole.STATUS_WARNING)
        self.assertEqual(selection.account_text, "选择要使用的账号。")

    def test_not_logged_in_uses_login_guidance_without_compatibility_warning(self):
        """Catches translating an account state into a compatibility warning."""
        presentation = status_presentation(
            WeChatStatus(runtime_capability=RuntimeCapability.NOT_LOGGED_IN)
        )
        self.assertEqual(presentation.connection_text, "○ 微信尚未登录")
        self.assertEqual(presentation.connection_role, SemanticRole.STATUS_NEUTRAL)
        self.assertEqual(presentation.account_text, "登录 Windows 微信后重新检测。")

    def test_unsupported_version_remains_a_user_visible_error(self):
        """Catches hiding a genuinely blocked unsupported major version."""
        presentation = status_presentation(
            WeChatStatus(
                version_verification=VersionVerification.UNSUPPORTED,
                runtime_capability=RuntimeCapability.NOT_PROBED,
                version="5.0.0",
            )
        )
        self.assertEqual(presentation.connection_text, "✕ 当前微信版本暂不支持")
        self.assertEqual(presentation.connection_role, SemanticRole.STATUS_ERROR)
        self.assertEqual(presentation.account_text, "WeChat 5.0.0")
        self.assertEqual(presentation.verification_text, "")

    def test_about_explains_runtime_capability_without_verification_jargon(self):
        """Catches reintroducing TESTED/UNTESTED language in ordinary About copy."""
        with tempfile.TemporaryDirectory() as temporary:
            service = FakeWindowService(Path(temporary))
            dialog = AboutDialog(service)
            visible = " ".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn("运行时检查当前 Windows 微信是否具备所需的本地读取能力", visible)
            for forbidden in ("TESTED", "UNTESTED", "尚未验证", "已验证"):
                self.assertNotIn(forbidden, visible)
            dialog.close()

    def test_unverified_internal_error_is_not_shown_as_a_gui_warning(self):
        """Catches translating an internal verification detail into user-facing jargon."""
        mapped = map_exception(RuntimeError("当前微信版本尚未验证"))
        visible = f"{mapped.title} {mapped.message}"
        self.assertIn("暂时无法读取微信", visible)
        self.assertNotIn("尚未验证", visible)

    def test_integrity_error_has_red_cross(self):
        summary = TargetSummary(Path("target"), "Broken", 10, "2026-09-12T20:00:00+09:00",
                                "✕ 时间胶囊完整性异常", False, None, True, 1, 2)
        card = TargetCard(summary)
        self.assertEqual(card.integrity_icon.text(), "✕")
        self.assertEqual(card.integrity_icon.objectName(), "integrityBad")

    def test_long_name_retains_full_tooltip_and_large_count_formats(self):
        name = "这是一个非常非常非常长的联系人显示名称用于验证截断行为"
        summary = TargetSummary(Path("target"), name, 1234567, "2026-09-12T20:00:00+09:00",
                                "✓ 时间胶囊完整", True, None, True, 1, 0)
        card = TargetCard(summary)
        self.assertEqual(card.name_label.toolTip(), name)
        self.assertIn("1,234,567", card.summary_label.text())

    def test_compact_display_time_today_yesterday_and_old_year(self):
        now = datetime.fromisoformat("2026-09-12T20:00:00+09:00")
        self.assertEqual(display_time("2026-09-12T09:30:00+09:00", now=now), "今天 09:30")
        self.assertEqual(display_time("2026-09-11T09:30:00+09:00", now=now), "昨天 09:30")
        self.assertEqual(display_time("2025-09-11T09:30:00+09:00", now=now), "2025-09-11 09:30")

    def test_expanding_second_card_collapses_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000); self.app.processEvents()
            rows = [TargetSummary(Path(temporary) / str(i), f"Target {i}", i, None,
                                  "○ 尚未保存", None, None, False, 0, 0) for i in range(2)]
            window._show_targets(rows)
            window.target_cards[0].set_expanded(True)
            window.target_cards[1].set_expanded(True)
            self.assertFalse(window.target_cards[0].is_expanded())
            self.assertTrue(window.target_cards[1].is_expanded())
            window.close()

    def test_main_window_has_no_detail_page_or_stacked_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            window = MainWindow(FakeWindowService(Path(temporary)))
            window.thread_pool.waitForDone(3000); self.app.processEvents()
            self.assertFalse(hasattr(window, "detail_page"))
            self.assertEqual(window.findChildren(QStackedWidget), [])
            window.close()

    def test_card_copy_never_mentions_saved_copy_count(self):
        summary = TargetSummary(Path("target"), "Example", 42, "2026-09-12T20:00:00+09:00",
                                "✓ 时间胶囊完整", True, None, True, 1, 99)
        card = TargetCard(summary)
        visible = " ".join(label.text() for label in card.findChildren(type(card.name_label)))
        self.assertNotIn("已保存", visible)
        self.assertNotIn("99", visible)

    def test_five_collapsed_cards_fit_in_compact_height_budget(self):
        rows = [TargetCard(TargetSummary(Path(str(i)), f"Target {i}", i, None,
                                         "○ 尚未保存", None, None, False, 0, 0))
                for i in range(5)]
        self.assertLessEqual(sum(card.height() for card in rows) + 4 * 7, 440)

    def test_avatar_history_dialog_is_modal_content_not_navigation_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "assets/avatars/history/contact/avatar.jpg"
            image.parent.mkdir(parents=True); image.write_bytes(b"not-a-real-image")
            summary = TargetSummary(root, "Example", 1, None, "✓ 时间胶囊完整", True,
                                    None, True, 1, 0)
            dialog = AvatarHistoryDialog(summary, [AvatarHistoryEntry("a" * 64,
                "2026-09-12T20:00:00+09:00", "assets/avatars/history/contact/avatar.jpg", True)])
            self.assertIsInstance(dialog, QDialog)
            self.assertIn("首次观察", " ".join(label.text() for label in dialog.findChildren(QLabel)))
            dialog.close()

    def test_open_chat_points_to_root_chat_md(self):
        root = Path("target").resolve()
        summary = TargetSummary(root, "Example", 1, None, "✓ 时间胶囊完整", True,
                                None, True, 1, 0)
        card = TargetCard(summary)
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            card.open_chat_button.click()
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), root / "chat.md")

    def test_open_folder_points_to_target_root(self):
        root = Path("target").resolve()
        summary = TargetSummary(root, "Example", 1, None, "✓ 时间胶囊完整", True,
                                None, True, 1, 0)
        card = TargetCard(summary)
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            card.open_folder_button.click()
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), root)

    def test_changing_archive_root_keeps_previous_root_discoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_root = root / "old"
            new_root = root / "new"
            settings = AppSettings(root / "settings.json", old_root)
            settings.data["archive_root"] = str(old_root)
            settings.set_archive_root(new_root)
            self.assertIn(old_root.resolve(), [item.resolve() for item in settings.archive_roots()])

    def test_close_during_mutation_is_refused_and_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "old-snapshot.txt"
            archive.write_bytes(b"immutable")
            window = MainWindow(FakeWindowService(root))
            window.thread_pool.waitForDone(3000)
            self.app.processEvents()
            window.active_mutation = True
            event = FakeCloseEvent()
            with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
                window.closeEvent(event)
            self.assertTrue(event.ignored)
            self.assertEqual(archive.read_bytes(), b"immutable")
            window.active_mutation = False
            window.close()


if __name__ == "__main__":
    unittest.main()
