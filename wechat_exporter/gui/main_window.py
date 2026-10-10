from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
    QSizePolicy, QStyle, QVBoxLayout, QWidget,
)

from wechat_exporter import DISPLAY_VERSION, PRODUCT_NAME
from wechat_exporter.compatibility import RuntimeCapability, VersionVerification
from wechat_exporter.service import BackupOutcome, TargetSummary
from .dialogs import (
    AboutDialog, AccountSelectorDialog, AddTargetDialog, AvatarHistoryDialog,
    DiagnosticsCompletedDialog, FirstBackupDialog, OnboardingDialog, SettingsDialog,
    RemoveBackupTargetDialog,
)
from .errors import map_exception
from .theme import (
    SemanticRole,
    application_style_sheet,
    apply_widget_theme,
    semantic_role,
)
from .workers import TaskWorker


def display_time(value: str | None, *, now: datetime | None = None) -> str:
    if not value:
        return "尚未保存"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    reference = now or datetime.now(moment.tzinfo)
    if moment.date() == reference.date():
        return f"今天 {moment:%H:%M}"
    if (reference.date() - moment.date()).days == 1:
        return f"昨天 {moment:%H:%M}"
    if moment.year == reference.year:
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def backup_mode_label(mode: str) -> str:
    return {
        "reconciliation": "完整核对", "full_reconciliation": "完整核对",
        "migration": "结构迁移", "quick": "快速更新", "incremental": "快速更新",
    }.get(mode, "更新")


@dataclass(frozen=True)
class StatusPresentation:
    connection_text: str
    connection_role: SemanticRole
    account_text: str
    verification_text: str = ""
    verification_role: SemanticRole = SemanticRole.STATUS_NEUTRAL


def status_presentation(status) -> StatusPresentation:
    if status.runtime_capability == RuntimeCapability.READY:
        return StatusPresentation(
            "● 已连接",
            SemanticRole.STATUS_SUCCESS,
            " · ".join(
                item for item in (status.account_name, f"WeChat {status.version}" if status.version else "")
                if item
            ),
        )
    if status.version_verification == VersionVerification.UNSUPPORTED:
        return StatusPresentation(
            "✕ 当前微信版本暂不支持",
            SemanticRole.STATUS_ERROR,
            f"WeChat {status.version}" if status.version else "",
        )
    if status.runtime_capability == RuntimeCapability.WECHAT_NOT_RUNNING:
        return StatusPresentation(
            "○ 未检测到微信",
            SemanticRole.STATUS_NEUTRAL,
            "请启动并登录 Windows 微信。",
        )
    if status.runtime_capability == RuntimeCapability.PROBE_FAILED:
        return StatusPresentation(
            "✕ 暂时无法读取微信",
            SemanticRole.STATUS_ERROR,
            "聊天数据库的基础读取检查没有通过。可以尝试重新检测。",
        )
    if status.runtime_capability == RuntimeCapability.ACCOUNT_SELECTION_REQUIRED:
        return StatusPresentation(
            "● 检测到多个微信账号",
            SemanticRole.STATUS_WARNING,
            "选择要使用的账号。",
        )
    if status.runtime_capability == RuntimeCapability.NOT_LOGGED_IN:
        return StatusPresentation(
            "○ 微信尚未登录",
            SemanticRole.STATUS_NEUTRAL,
            "登录 Windows 微信后重新检测。",
        )
    detail = " · ".join(item for item in (status.message, status.version_label) if item)
    return StatusPresentation("⚠ 暂停更新", SemanticRole.STATUS_WARNING, detail)


class ElidedLabel(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.full_text = text
        self.setToolTip(text)
        estimated = 20 + sum(20 if ord(character) > 127 else 12 for character in text)
        self.setFixedWidth(min(max(68, estimated), 520))

    def resizeEvent(self, event):
        width = max(0, event.size().width() - 2)
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.ElideRight, width))
        super().resizeEvent(event)


class TargetCard(QFrame):
    expanded_changed = Signal(object, bool)
    quick_requested = Signal(object)
    reconciliation_requested = Signal(object)
    history_requested = Signal(object)
    verify_requested = Signal(object)
    remove_requested = Signal(object)
    COLLAPSED_HEIGHT = 80

    def __init__(self, summary: TargetSummary, parent=None):
        super().__init__(parent)
        self.summary = summary
        self._expanded = False
        self.setObjectName("targetCard")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"{summary.display_name} 当前时间胶囊")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 7, 10, 7)
        outer.setSpacing(8)

        self.header = QWidget()
        self.header.setObjectName("cardHeader")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self.avatar = QLabel()
        self.avatar.setObjectName("cardAvatar")
        self.avatar.setFixedSize(50, 50)
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setPixmap(self.avatar_pixmap(summary.avatar_path, 48))
        header.addWidget(self.avatar)

        text = QVBoxLayout()
        text.setContentsMargins(0, 1, 0, 1)
        text.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(7)
        self.name_label = ElidedLabel(summary.display_name)
        self.name_label.setObjectName("cardName")
        semantic_role(self.name_label, SemanticRole.TEXT_PRIMARY)
        self.name_label.setToolTip(summary.display_name)
        self.name_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        name_row.addWidget(self.name_label)
        self.integrity_icon = QLabel(self._status_icon(summary))
        self.integrity_icon.setObjectName(self._status_object_name(summary))
        semantic_role(self.integrity_icon, self._status_role(summary))
        self.integrity_icon.setAccessibleName(summary.integrity_label)
        self.integrity_icon.setToolTip(summary.integrity_label)
        name_row.addWidget(self.integrity_icon)
        name_row.addStretch()
        text.addLayout(name_row)
        second_line = (
            f"{summary.message_count:,} 条消息 · {display_time(summary.last_saved_at)}"
            if summary.capsule_exists else "尚未生成当前时间胶囊"
        )
        self.summary_label = QLabel(second_line)
        self.summary_label.setObjectName("cardSummary")
        semantic_role(self.summary_label, SemanticRole.TEXT_SECONDARY)
        text.addWidget(self.summary_label)
        header.addLayout(text, 1)
        self.arrow = QPushButton("⌄")
        self.arrow.setObjectName("cardArrow")
        semantic_role(self.arrow, SemanticRole.STATUS_NEUTRAL)
        self.arrow.setFlat(True)
        self.arrow.setFixedSize(36, 36)
        self.arrow.setToolTip("展开操作")
        self.arrow.setAccessibleName(f"展开 {summary.display_name}")
        self.arrow.clicked.connect(self.toggle_expanded)
        header.addWidget(self.arrow)
        outer.addWidget(self.header)

        self.actions_panel = QWidget()
        self.actions_panel.setObjectName("cardActions")
        panel = QVBoxLayout(self.actions_panel)
        panel.setContentsMargins(60, 4, 4, 8)
        panel.setSpacing(8)
        save_row = QHBoxLayout()
        self.quick_button = QPushButton("快速保存")
        self.quick_button.setObjectName("primaryButton")
        self.quick_button.setToolTip("只检查上次之后的新消息")
        self.quick_button.clicked.connect(lambda: self.quick_requested.emit(self.summary))
        self.reconcile_button = QPushButton("完整核对并保存")
        self.reconcile_button.setToolTip("重新检查全部聊天并补找可能遗漏的旧消息")
        self.reconcile_button.clicked.connect(lambda: self.reconciliation_requested.emit(self.summary))
        save_row.addWidget(self.quick_button)
        save_row.addWidget(self.reconcile_button)
        save_row.addStretch()
        panel.addLayout(save_row)
        note = QLabel("快速保存：只检查新消息，速度更快，适合平时使用。\n完整核对并保存：重新检查全部聊天，补找可能遗漏的旧消息。")
        note.setObjectName("cardNote")
        semantic_role(note, SemanticRole.TEXT_MUTED)
        note.setWordWrap(True)
        panel.addWidget(note)
        tools = QHBoxLayout()
        self.open_chat_button = QPushButton("打开聊天")
        self.open_chat_button.setEnabled(summary.capsule_exists)
        self.open_chat_button.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str((summary.root / "chat.md").resolve()))))
        self.open_folder_button = QPushButton("打开文件夹")
        self.open_folder_button.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(summary.root.resolve()))))
        self.history_button = QPushButton(f"头像历史 ({summary.avatar_history_count})")
        self.history_button.clicked.connect(lambda: self.history_requested.emit(self.summary))
        self.verify_button = QPushButton("验证完整性")
        self.verify_button.setEnabled(summary.capsule_exists)
        self.verify_button.clicked.connect(lambda: self.verify_requested.emit(self.summary))
        self.remove_button = QPushButton("移除备份对象")
        self.remove_button.setObjectName("removeTargetButton")
        semantic_role(self.remove_button, SemanticRole.STATUS_WARNING)
        self.remove_button.setToolTip("只从备份对象一览移除；不会删除任何已保存文件")
        self.remove_button.setEnabled(bool(summary.archive_id))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.summary))
        for button in (self.open_chat_button, self.open_folder_button,
                       self.history_button, self.verify_button, self.remove_button):
            tools.addWidget(button)
        tools.addStretch()
        panel.addLayout(tools)
        outer.addWidget(self.actions_panel)
        self.actions_panel.hide()
        self.setFixedHeight(self.COLLAPSED_HEIGHT)

    @staticmethod
    def _status_icon(summary: TargetSummary) -> str:
        if not summary.capsule_exists:
            return "○"
        if summary.integrity_ok is True:
            return "✓"
        if summary.integrity_ok is False:
            return "✕"
        return "⚠"

    @staticmethod
    def _status_object_name(summary: TargetSummary) -> str:
        if not summary.capsule_exists:
            return "integrityEmpty"
        if summary.integrity_ok is True:
            return "integrityGood"
        if summary.integrity_ok is False:
            return "integrityBad"
        return "integrityWarn"

    @staticmethod
    def _status_role(summary: TargetSummary) -> SemanticRole:
        if not summary.capsule_exists:
            return SemanticRole.STATUS_NEUTRAL
        if summary.integrity_ok is True:
            return SemanticRole.STATUS_SUCCESS
        if summary.integrity_ok is False:
            return SemanticRole.STATUS_ERROR
        return SemanticRole.STATUS_WARNING

    def avatar_pixmap(self, path: Path | None, size: int) -> QPixmap:
        pixmap = QPixmap(str(path)) if path and path.is_file() else QPixmap()
        if pixmap.isNull():
            pixmap = self.style().standardIcon(QStyle.SP_FileIcon).pixmap(size, size)
        return pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, emit_signal: bool = True) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.actions_panel.setVisible(expanded)
        self.arrow.setText("⌃" if expanded else "⌄")
        self.arrow.setToolTip("收起操作" if expanded else "展开操作")
        self.setFixedHeight(218 if expanded else self.COLLAPSED_HEIGHT)
        if emit_signal:
            self.expanded_changed.emit(self.summary, expanded)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_online_actions_enabled(self, enabled: bool) -> None:
        """Gate WeChat reads without affecting any local archive operation."""
        self.quick_button.setEnabled(enabled)
        self.reconcile_button.setEnabled(enabled)
        reason = "" if enabled else "微信只读能力检查通过后可用"
        self.quick_button.setToolTip(reason or "只检查上次之后的新消息")
        self.reconcile_button.setToolTip(
            reason or "重新检查全部聊天并补找可能遗漏的旧消息"
        )

    def set_local_actions_enabled(self, enabled: bool) -> None:
        self.remove_button.setEnabled(enabled and bool(self.summary.archive_id))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.header.geometry().contains(event.position().toPoint()):
            self.toggle_expanded()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.toggle_expanded()
            event.accept()
            return
        super().keyPressEvent(event)


class BackupCompletedDialog(QDialog):
    def __init__(self, target_name: str, outcome: BackupOutcome, parent=None):
        super().__init__(parent)
        self.outcome = outcome
        self.setWindowTitle("当前时间胶囊已更新")
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        heading = QLabel("当前时间胶囊已更新")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        result = outcome.capsule
        coverage_lines = []
        for heading_text, key in (("完整保存", "fully_supported"),
                                  ("部分保存", "partial"),
                                  ("暂未完整支持", "placeholder")):
            values = result.coverage_summary.get(key, {})
            if values:
                coverage_lines.append(heading_text + "：")
                coverage_lines.extend(f"{name} {count}" for name, count in values.items())
        summary = (
            f"{target_name}\n\n总消息：{result.message_count:,}\n"
            f"新增消息：{result.new_message_count:,}\n新增图片：{result.new_image_count:,}\n"
            f"更新方式：{backup_mode_label(result.save_mode)}\n保存时间：{display_time(result.saved_at)}"
        )
        if coverage_lines:
            summary += "\n\n" + "\n".join(coverage_lines)
        layout.addWidget(QLabel(summary))
        actions = QHBoxLayout()
        chat = QPushButton("打开聊天")
        chat.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str((result.path / "chat.md").resolve()))))
        folder = QPushButton("打开文件夹")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(result.path.resolve()))))
        actions.addWidget(chat)
        actions.addWidget(folder)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("完成")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.thread_pool = QThreadPool(self)
        self.workers: set[TaskWorker] = set()
        self.progress_dialogs: set[QProgressDialog] = set()
        self.active_mutation = False
        self.current_target: TargetSummary | None = None
        self.last_wechat_status = None
        self.target_cards: list[TargetCard] = []
        self.expanded_root: Path | None = None
        self.setWindowTitle(f"{PRODUCT_NAME} v{DISPLAY_VERSION}")
        width, height = service.settings.data.get("window_size", [900, 650])
        self.resize(max(760, int(width)), max(560, int(height)))
        self.setMinimumSize(760, 560)
        self.main_page = self._build_main_page()
        self.setCentralWidget(self.main_page)
        apply_widget_theme(self)
        self.refresh_all()

    def show_onboarding_if_needed(self):
        if not self.service.settings.onboarding_completed:
            dialog = OnboardingDialog(self.service.settings, self)
            if dialog.exec() == QDialog.Accepted:
                if dialog.selected_root.resolve() != self.service.settings.archive_root.resolve():
                    self.service.settings.set_archive_root(dialog.selected_root)
                self.service.settings.set_onboarding_completed()
        if self.service.recovery_notices:
            QMessageBox.warning(self, "应用设置已恢复", "\n\n".join(self.service.recovery_notices))

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("mainPage")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(10)
        title_row = QHBoxLayout()
        title = QLabel(f"{PRODUCT_NAME} v{DISPLAY_VERSION}")
        title.setObjectName("mainTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        outer.addLayout(title_row)
        status = QFrame()
        status.setObjectName("statusPanel")
        status.setFixedHeight(58)
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(10, 7, 10, 7)
        self.wechat_status = QLabel("○ 正在检测……")
        self.wechat_status.setObjectName("wechatStatus")
        semantic_role(self.wechat_status, SemanticRole.STATUS_NEUTRAL)
        self.wechat_account = QLabel("")
        self.wechat_account.setObjectName("wechatAccount")
        semantic_role(self.wechat_account, SemanticRole.TEXT_SECONDARY)
        self.wechat_verification = QLabel("")
        self.wechat_verification.setObjectName("wechatVerification")
        semantic_role(self.wechat_verification, SemanticRole.STATUS_NEUTRAL)
        self.wechat_verification.hide()
        status_detail = QWidget()
        status_detail.setObjectName("statusDetail")
        detail_layout = QVBoxLayout(status_detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(1)
        detail_layout.addWidget(self.wechat_account)
        detail_layout.addWidget(self.wechat_verification)
        status_layout.addWidget(self.wechat_status)
        status_layout.addWidget(status_detail, 1)
        self.refresh_button = QPushButton("重新检测")
        self.refresh_button.clicked.connect(
            lambda: self.refresh_wechat_status(manual=True)
        )
        status_layout.addWidget(self.refresh_button)
        outer.addWidget(status)
        section_row = QHBoxLayout()
        section = QLabel("备份对象一览")
        section.setObjectName("sectionTitle")
        section_row.addWidget(section)
        section_row.addStretch()
        self.add_target_button = QPushButton("+ 添加备份对象")
        self.add_target_button.setEnabled(False)
        self.add_target_button.clicked.connect(self.add_target)
        import_existing = QPushButton("添加已有备份对象")
        import_existing.clicked.connect(self.import_existing_archive)
        section_row.addWidget(self.add_target_button)
        section_row.addWidget(import_existing)
        outer.addLayout(section_row)
        self.target_scroll = QScrollArea()
        self.target_scroll.setObjectName("targetScroll")
        self.target_scroll.setWidgetResizable(True)
        self.target_scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self.target_list = QVBoxLayout(holder)
        self.target_list.setContentsMargins(0, 0, 0, 0)
        self.target_list.setSpacing(7)
        self.target_list.setAlignment(Qt.AlignTop)
        self.target_scroll.setWidget(holder)
        outer.addWidget(self.target_scroll, 1)
        footer = QHBoxLayout()
        footer_note = QLabel("聊天记录只在本机处理")
        semantic_role(footer_note, SemanticRole.TEXT_MUTED)
        footer.addWidget(footer_note)
        footer.addStretch()
        settings = QPushButton("设置")
        settings.clicked.connect(self.open_settings)
        logs = QPushButton("日志")
        logs.clicked.connect(self.open_logs)
        diagnostics = QPushButton("诊断")
        diagnostics.clicked.connect(self.generate_diagnostics)
        about = QPushButton("关于")
        about.clicked.connect(lambda: AboutDialog(self.service, self).exec())
        for button in (settings, logs, diagnostics, about):
            footer.addWidget(button)
        outer.addLayout(footer)
        return page

    def _style_sheet(self) -> str:
        return application_style_sheet()

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def refresh_all(self):
        self.run_task("正在读取本地时间胶囊……", self.service.list_targets, self._show_targets)
        self.refresh_wechat_status()

    def refresh_wechat_status(self, *, manual: bool = False):
        if manual:
            self._refresh_result_received = False
            self.refresh_button.setEnabled(False)
            self.refresh_button.setText("正在重新检测…")
        self.run_task(
            "正在检测微信……",
            self.service.detect_wechat,
            self._show_wechat_status,
            on_finished=(self._manual_refresh_finished if manual else None),
        )

    def _manual_refresh_finished(self):
        received = bool(getattr(self, "_refresh_result_received", False))
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("✓ 重新检测完成" if received else "重新检测失败")
        QTimer.singleShot(1600, lambda: self.refresh_button.setText("重新检测"))

    def _show_wechat_status(self, status):
        self._refresh_result_received = True
        self.last_wechat_status = status
        if status.selection_required:
            dialog = AccountSelectorDialog(status.accounts, self.service.settings.selected_account_identity,
                                             status.remembered_missing, self)
            if dialog.exec() == QDialog.Accepted and dialog.selected_identity:
                self.service.select_account(dialog.selected_identity)
                self.refresh_wechat_status()
                return
        presentation = status_presentation(status)
        self.wechat_status.setText(presentation.connection_text)
        semantic_role(self.wechat_status, presentation.connection_role)
        self.wechat_account.setText(presentation.account_text)
        semantic_role(self.wechat_account, SemanticRole.TEXT_SECONDARY)
        self.wechat_verification.setText(presentation.verification_text)
        semantic_role(self.wechat_verification, presentation.verification_role)
        self.wechat_verification.setVisible(bool(presentation.verification_text))
        self._apply_online_permissions(status.operations_allowed)

    def _apply_online_permissions(self, enabled: bool):
        self.add_target_button.setEnabled(enabled and not self.active_mutation)
        for card in self.target_cards:
            card.set_online_actions_enabled(enabled and not self.active_mutation)

    def _show_targets(self, targets: list[TargetSummary]):
        scroll_value = self.target_scroll.verticalScrollBar().value()
        expanded = self.expanded_root
        self._clear_layout(self.target_list)
        self.target_cards = []
        if not targets:
            empty = QLabel("还没有备份对象。点击“添加对象”开始。")
            empty.setAlignment(Qt.AlignCenter)
            self.target_list.addWidget(empty)
            return
        for summary in targets:
            card = TargetCard(summary)
            card.expanded_changed.connect(self._card_expanded)
            card.quick_requested.connect(lambda item, mode=False: self.start_backup(mode, item))
            card.reconciliation_requested.connect(lambda item, mode=True: self.start_backup(mode, item))
            card.history_requested.connect(self.show_avatar_history)
            card.verify_requested.connect(self.verify_capsule)
            card.remove_requested.connect(self.remove_backup_target)
            self.target_list.addWidget(card)
            self.target_cards.append(card)
            card.set_online_actions_enabled(
                bool(self.last_wechat_status and self.last_wechat_status.operations_allowed)
                and not self.active_mutation
            )
            card.set_local_actions_enabled(not self.active_mutation)
            if expanded and summary.root.resolve() == expanded.resolve():
                card.set_expanded(True, emit_signal=False)
                self.current_target = summary
        self.target_scroll.verticalScrollBar().setValue(scroll_value)

    def _card_expanded(self, summary: TargetSummary, expanded: bool):
        if expanded:
            self.current_target = summary
            self.expanded_root = summary.root
            for card in self.target_cards:
                if card.summary.root.resolve() != summary.root.resolve():
                    card.set_expanded(False, emit_signal=False)
        elif self.expanded_root and self.expanded_root.resolve() == summary.root.resolve():
            self.expanded_root = None
            self.current_target = None

    def open_target(self, summary: TargetSummary):
        card = next((item for item in self.target_cards
                     if item.summary.root.resolve() == summary.root.resolve()), None)
        if card:
            card.set_expanded(True)

    def add_target(self):
        if not self._ensure_account_selected():
            return
        dialog = AddTargetDialog(self.service, self.run_task, self)
        if dialog.exec() == QDialog.Accepted:
            self.refresh_all()

    def open_settings(self):
        SettingsDialog(self.service.settings, self).exec()
        self.refresh_all()

    def open_logs(self):
        self.open_local_folder(self.service.settings.app_data_dir / "logs")

    def import_existing_archive(self):
        selected = QFileDialog.getExistingDirectory(self, "选择已有备份对象目录",
                                                     str(self.service.settings.archive_root))
        if selected:
            self.run_task("正在验证已有备份对象……",
                          lambda: self.service.import_existing_archive(Path(selected)),
                          lambda result: self.refresh_all())

    def generate_diagnostics(self):
        self.run_task("正在生成并清洗诊断报告……", self.service.generate_diagnostics,
                      lambda result: DiagnosticsCompletedDialog(result, self).exec())

    def _ensure_account_selected(self) -> bool:
        status = self.last_wechat_status or self.service.detect_wechat()
        if not status.selection_required:
            return status.operations_allowed
        dialog = AccountSelectorDialog(status.accounts, self.service.settings.selected_account_identity,
                                       status.remembered_missing, self)
        if dialog.exec() != QDialog.Accepted or not dialog.selected_identity:
            return False
        self.service.select_account(dialog.selected_identity)
        self.refresh_wechat_status()
        return False

    @staticmethod
    def open_local_file(path: Path):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    @staticmethod
    def open_local_folder(path: Path):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def start_backup(self, reconciliation: bool, summary: TargetSummary | None = None):
        target = summary or self.current_target
        if not target or self.active_mutation or not self._ensure_account_selected():
            return
        if not target.capsule_exists:
            archive_id = self.service.registry.id_for_path(target.root)
            if not self.service.settings.first_backup_notice_suppressed(archive_id):
                dialog = FirstBackupDialog(target.display_name, self)
                if dialog.exec() != QDialog.Accepted:
                    return
                if dialog.do_not_show_again.isChecked():
                    self.service.settings.suppress_first_backup_notice(archive_id)
        self.active_mutation = True
        self._set_mutation_buttons(False)
        root = target.root
        task = (lambda: self.service.reconciliation_backup(root)) if reconciliation \
            else (lambda: self.service.quick_backup(root))
        self.run_task("正在完整核对并更新当前时间胶囊……" if reconciliation else "正在读取新消息并更新……",
                      task, lambda outcome: self._backup_completed(outcome, target.display_name),
                      on_finished=self._mutation_finished)

    def _set_mutation_buttons(self, enabled: bool):
        online = bool(
            self.last_wechat_status and self.last_wechat_status.operations_allowed
        )
        self._apply_online_permissions(enabled and online)
        for card in self.target_cards:
            card.set_local_actions_enabled(enabled)

    def _mutation_finished(self):
        self.active_mutation = False
        self._set_mutation_buttons(True)

    def _backup_completed(self, outcome: BackupOutcome, target_name: str | None = None):
        self.refresh_target_after_operation()
        BackupCompletedDialog(target_name or "备份对象", outcome, self).exec()

    def refresh_target_after_operation(self):
        self.run_task("正在刷新保存信息……", self.service.list_targets, self._show_targets)

    def show_avatar_history(self, summary: TargetSummary):
        self.run_task("正在读取头像历史……", lambda: self.service.avatar_history(summary.root),
                      lambda entries: AvatarHistoryDialog(summary, entries, self).exec())

    def verify_capsule(self, summary: TargetSummary):
        self.run_task("正在离线验证当前时间胶囊……",
                      lambda: self.service.verify_capsule(summary.root), self._integrity_completed)

    def remove_backup_target(self, summary: TargetSummary):
        dialog = RemoveBackupTargetDialog(summary.display_name, self)
        if dialog.exec() != QDialog.Accepted:
            return
        if self.active_mutation:
            return
        self.active_mutation = True
        self._set_mutation_buttons(False)

        def remove_and_refresh():
            result = self.service.remove_backup_target(summary.archive_id)
            return result, self.service.list_targets()

        self.run_task(
            "正在从备份对象一览中移除……",
            remove_and_refresh,
            self._backup_target_removed,
            on_finished=self._mutation_finished,
        )

    def _backup_target_removed(self, outcome):
        result, targets = outcome
        self.expanded_root = None
        self.current_target = None
        self._show_targets(targets)
        if not result.removed:
            QMessageBox.information(self, "备份对象不在列表中", result.message)

    def _integrity_completed(self, result):
        if result.ok and result.manifest_present:
            QMessageBox.information(self, "验证完成", "✓ 当前时间胶囊完整。")
        elif result.ok:
            QMessageBox.information(self, "验证完成", "⚠ 当前时间胶囊尚未建立哈希校验基线。")
        else:
            mismatches = result.count("HASH_MISMATCH")
            missing = result.count("MISSING_ASSET", "MISSING_FILE", "MISSING_REQUIRED_FILE")
            escapes = result.count("PATH_ESCAPE", "MANIFEST_PATH_ESCAPE")
            QMessageBox.warning(self, "完整性异常", "检测到当前时间胶囊完整性异常。\n\n"
                                f"Hash mismatch：{mismatches}\nMissing asset：{missing}\n"
                                f"Path escape：{escapes}\n\n文件不会被自动修改。")
        self.refresh_target_after_operation()

    def run_task(self, label, task, on_success, *, on_finished=None):
        progress = QProgressDialog(label, "", 0, 0, self)
        progress.setWindowTitle("请稍候")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.NonModal)
        progress.setMinimumDuration(250)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        worker = TaskWorker(task)
        self.workers.add(worker)
        self.progress_dialogs.add(progress)

        def failed(error):
            mapped = map_exception(error)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(mapped.title)
            box.setText(mapped.message)
            box.setDetailedText(f"error code: {mapped.code}\n{mapped.technical_summary}")
            box.exec()

        def finished():
            progress.close()
            self.progress_dialogs.discard(progress)
            self.workers.discard(worker)
            if on_finished:
                on_finished()

        worker.signals.succeeded.connect(on_success)
        worker.signals.failed.connect(failed)
        worker.signals.finished.connect(finished)
        self.thread_pool.start(worker)
        return worker

    def closeEvent(self, event: QCloseEvent):
        if self.active_mutation:
            QMessageBox.information(self, "更新正在进行",
                                    "当前时间胶囊正在更新。\n\n为了保护数据，请等待完成后再关闭程序。")
            event.ignore()
            return
        self.service.settings.data["window_size"] = [self.width(), self.height()]
        self.service.settings.save()
        event.accept()
