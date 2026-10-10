from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from wechat_exporter import DISPLAY_VERSION, PRODUCT_NAME


class AvatarHistoryDialog(QDialog):
    """Read-only contact avatar observations for one backup target."""

    def __init__(self, summary, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{summary.display_name} 的头像历史")
        self.setMinimumSize(560, 420)
        layout = QVBoxLayout(self)
        title = QLabel(f"{summary.display_name} 的头像历史")
        title.setObjectName("dialogHeading")
        layout.addWidget(title)
        explanation = QLabel(
            "时间表示本软件首次观察到这张头像的时间，不代表联系人实际更换头像的时间。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setAlignment(Qt.AlignTop)
        if not entries:
            rows.addWidget(QLabel("尚未保存可用的联系人头像记录。"))
        for entry in entries:
            row = QHBoxLayout()
            avatar = QLabel()
            avatar.setFixedSize(72, 72)
            avatar.setAlignment(Qt.AlignCenter)
            source = summary.root / entry.path
            pixmap = QPixmap(str(source))
            if not pixmap.isNull():
                avatar.setPixmap(pixmap.scaled(68, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            row.addWidget(avatar)
            text = QVBoxLayout()
            label = "当前头像" if entry.current else "历史头像"
            heading = QLabel(label)
            heading.setObjectName("sectionTitle" if entry.current else "")
            text.addWidget(heading)
            try:
                observed = datetime.fromisoformat(entry.first_observed_at).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                observed = entry.first_observed_at
            text.addWidget(QLabel(f"首次观察：{observed}"))
            text.addWidget(QLabel(f"校验标识：{entry.sha256[:12]}…"))
            row.addLayout(text, 1)
            rows.addLayout(row)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class OnboardingDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.selected_root = settings.archive_root
        self.setWindowTitle(f"欢迎使用{PRODUCT_NAME}")
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        self.pages = QStackedWidget()
        content = [
            (
                "保存属于你的聊天记录",
                "把你选择的微信聊天保存成时间胶囊：文字可以复制，图片保存在本地。",
            ),
            (
                "所有处理都在你的电脑上完成",
                "不上传聊天记录，不发送微信消息，不修改微信数据库。",
            ),
        ]
        for title, body in content:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            heading = QLabel(title)
            heading.setObjectName("onboardingHeading")
            heading.setAlignment(Qt.AlignCenter)
            text = QLabel(body)
            text.setAlignment(Qt.AlignCenter)
            text.setWordWrap(True)
            page_layout.addStretch()
            page_layout.addWidget(heading)
            page_layout.addSpacing(24)
            page_layout.addWidget(text)
            page_layout.addStretch()
            self.pages.addWidget(page)
        location_page = QWidget()
        location_layout = QVBoxLayout(location_page)
        location_title = QLabel("选择保存位置")
        location_title.setObjectName("onboardingHeading")
        location_title.setAlignment(Qt.AlignCenter)
        location_layout.addStretch()
        location_layout.addWidget(location_title)
        location_layout.addSpacing(20)
        location_layout.addWidget(QLabel("时间胶囊将保存到："))
        self.location_label = QLabel(str(self.selected_root))
        self.location_label.setWordWrap(True)
        self.location_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        location_layout.addWidget(self.location_label)
        change = QPushButton("更改位置")
        change.clicked.connect(self.choose_location)
        location_layout.addWidget(change, alignment=Qt.AlignCenter)
        location_layout.addStretch()
        self.pages.addWidget(location_page)
        layout.addWidget(self.pages)
        controls = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.back_button.clicked.connect(self.previous_page)
        self.next_button.clicked.connect(self.next_page)
        self.next_button.setDefault(True)
        controls.addWidget(self.back_button)
        controls.addStretch()
        controls.addWidget(self.next_button)
        layout.addLayout(controls)
        self._sync_buttons()

    def previous_page(self):
        self.pages.setCurrentIndex(max(0, self.pages.currentIndex() - 1))
        self._sync_buttons()

    def next_page(self):
        if self.pages.currentIndex() == self.pages.count() - 1:
            self.accept()
            return
        self.pages.setCurrentIndex(self.pages.currentIndex() + 1)
        self._sync_buttons()

    def _sync_buttons(self):
        self.back_button.setEnabled(self.pages.currentIndex() > 0)
        self.next_button.setText(
            "开始使用" if self.pages.currentIndex() == self.pages.count() - 1 else "下一步"
        )

    def choose_location(self):
        selected = QFileDialog.getExistingDirectory(
            self, "选择保存位置", str(self.selected_root)
        )
        if selected:
            self.selected_root = Path(selected)
            self.location_label.setText(selected)


class AccountSelectorDialog(QDialog):
    def __init__(self, accounts, remembered_identity: str = "", remembered_missing=False, parent=None):
        super().__init__(parent)
        self.accounts = tuple(accounts)
        self.setWindowTitle(
            "检测到多个微信账号" if len(self.accounts) > 1 else "选择微信账号"
        )
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        heading = QLabel("请选择要使用的账号")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        if remembered_missing:
            note = QLabel("此前选择的账号当前不存在。为避免读取错误账号，请重新选择。")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.group = QButtonGroup(self)
        self.radios = []
        for index, account in enumerate(self.accounts):
            radio = QRadioButton(
                f"{account.display_label}\n账号标识：{account.masked_identity}"
            )
            radio.setProperty("stable_id", account.stable_id)
            self.group.addButton(radio)
            self.radios.append(radio)
            layout.addWidget(radio)
            if account.stable_id == remembered_identity or (not remembered_identity and index == 0):
                radio.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("继续")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_identity(self) -> str:
        checked = self.group.checkedButton()
        return str(checked.property("stable_id") or "") if checked else ""


class FirstBackupDialog(QDialog):
    def __init__(self, target_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"保存与 {target_name} 的聊天？")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        text = QLabel(
            "将读取这位联系人的本地聊天记录，\n"
            "并创建一份新的时间胶囊。\n\n"
            "不会发送微信消息，也不会修改微信里的聊天记录。\n\n"
            "目前完整支持保存文字、图片和引用/回复消息。其他消息类型会保留占位信息。"
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.do_not_show_again = QCheckBox("不再提示")
        layout.addWidget(self.do_not_show_again)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("开始保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DeleteSnapshotDialog(QDialog):
    def __init__(self, summary, parent=None):
        super().__init__(parent)
        self.setWindowTitle("删除这份时间胶囊？")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        heading = QLabel("删除这份时间胶囊？")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        created = str(summary.created_at or "")
        try:
            from datetime import datetime

            created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
        body = QLabel(
            f"{created}\n{summary.message_count} 条消息\n\n"
            "只会删除这一份时间胶囊。\n"
            "不会删除微信里的聊天，也不会影响其他时间胶囊。\n\n"
            "删除后无法从微信时间胶囊中恢复。"
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("取消")
        cancel.setAutoDefault(True)
        cancel.setDefault(True)
        delete = buttons.button(QDialogButtonBox.Ok)
        delete.setText("删除")
        delete.setObjectName("dangerButton")
        delete.setAutoDefault(False)
        delete.setDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        cancel.setFocus(Qt.OtherFocusReason)


class RemoveBackupTargetDialog(QDialog):
    def __init__(self, display_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"从备份对象一览中移除 {display_name}？")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        heading = QLabel(f"从备份对象一览中移除 {display_name}？")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        body = QLabel(
            f"这个操作只会把 {display_name} 从微信时间胶囊的列表中移除。\n\n"
            "已保存的聊天、图片、视频、历史头像和其他文件不会被删除。\n\n"
            "以后可以通过“添加已有备份对象”重新加入。"
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("取消")
        cancel.setAutoDefault(True)
        cancel.setDefault(True)
        remove = buttons.button(QDialogButtonBox.Ok)
        remove.setText("移除")
        remove.setObjectName("removeTargetButton")
        remove.setAutoDefault(False)
        remove.setDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        cancel.setFocus(Qt.OtherFocusReason)


class DiagnosticsCompletedDialog(QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("诊断报告已生成")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        heading = QLabel("诊断报告已生成")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        body = QLabel(
            "其中不包含聊天正文、图片、联系人身份或数据库密钥。\n\n"
            f"文件：{result.path.name}"
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        open_folder = QPushButton("打开所在文件夹")
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.path.parent)))
        )
        layout.addWidget(open_folder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("完成")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AddTargetDialog(QDialog):
    def __init__(self, service, run_task, parent=None):
        super().__init__(parent)
        self.service = service
        self.run_task = run_task
        self.selected_contact = None
        self.add_in_progress = False
        self.setWindowTitle("添加备份对象")
        self.setMinimumSize(600, 440)
        layout = QVBoxLayout(self)
        heading = QLabel("搜索微信联系人")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入昵称、备注、微信号或内部 ID")
        self.search_button = QPushButton("搜索")
        self.search_button.clicked.connect(self.search)
        self.search_edit.returnPressed.connect(self.search)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.search_button)
        layout.addLayout(row)
        self.results = QListWidget()
        self.results.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.results, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("添加")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        buttons.accepted.connect(self.confirm_add)
        buttons.rejected.connect(self.reject)
        self.buttons = buttons
        layout.addWidget(buttons)

    def search(self):
        keyword = self.search_edit.text().strip()
        if not keyword:
            return
        self.search_button.setEnabled(False)
        self.run_task(
            "正在搜索联系人……",
            lambda: self.service.search_contacts(keyword),
            self._show_results,
            on_finished=lambda: self.search_button.setEnabled(True),
        )

    def _show_results(self, contacts):
        self.results.clear()
        for contact in contacts:
            detail = []
            if contact.remark:
                detail.append(f"备注：{contact.remark}")
            if contact.nickname:
                detail.append(f"微信昵称：{contact.nickname}")
            if contact.alias:
                detail.append(f"微信号：{contact.alias}")
            item = QListWidgetItem(contact.display_name + "\n" + " · ".join(detail))
            item.setData(Qt.UserRole, contact)
            self.results.addItem(item)

    def _selection_changed(self):
        item = self.results.currentItem()
        self.selected_contact = item.data(Qt.UserRole) if item else None
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            self.selected_contact is not None and not self.add_in_progress
        )

    def _set_add_in_progress(self, active: bool):
        self.add_in_progress = active
        self.search_edit.setEnabled(not active)
        self.search_button.setEnabled(not active)
        self.results.setEnabled(not active)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            self.selected_contact is not None and not active
        )

    def confirm_add(self):
        if self.selected_contact is None or self.add_in_progress:
            return
        answer = QMessageBox.question(
            self,
            "确认添加",
            f"将“{self.selected_contact.display_name}”添加为备份对象？\n\n"
            "软件以后只会在你主动保存时读取这位联系人的聊天记录。",
            QMessageBox.Cancel | QMessageBox.Ok,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        self._set_add_in_progress(True)
        self.run_task(
            "正在添加备份对象……",
            lambda: self.service.add_target(self.selected_contact),
            lambda result: self.accept(),
            on_finished=lambda: self._set_add_in_progress(False),
        )


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(650)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        row = QHBoxLayout()
        self.path_edit = QLineEdit(str(settings.archive_root))
        self.path_edit.setReadOnly(True)
        choose = QPushButton("选择……")
        choose.clicked.connect(self.choose_folder)
        row.addWidget(self.path_edit, 1)
        row.addWidget(choose)
        form.addRow("以后新增备份对象的保存位置", row)
        layout.addLayout(form)
        note = QLabel("更改位置不会移动或改写已有时间胶囊。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("完成")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(
            self, "选择时间胶囊保存位置", str(self.settings.archive_root)
        )
        if selected:
            self.settings.set_archive_root(Path(selected))
            self.path_edit.setText(selected)


class AboutDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel(PRODUCT_NAME)
        title.setObjectName("dialogHeading")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"Version {DISPLAY_VERSION}"))
        body = QLabel(
            "把你自己的微信聊天记录保存成普通文件。\n"
            "每个备份对象始终只有一份持续更新的当前时间胶囊。\n"
            "联系人头像按内容去重保留历史；时间表示首次观察，不代表实际更换时间。\n"
            "所有数据都在本机处理。\n"
            "本软件不是腾讯或微信官方产品。\n\n"
            "支持的消息类型\n"
            "完整支持：文字、图片、引用/回复、系统消息、位置、名片及本地 MP4 视频\n"
            "部分支持：网页/公众号链接、文件卡片、小程序卡片\n"
            "暂未支持：语音、动画表情、拍一拍及无法识别的卡片\n"
            "只保存聊天中实际出现的内容和附件，不抓取链接指向的外部内容。\n\n"
            "微信时间胶囊会在运行时检查当前 Windows 微信是否具备所需的本地读取能力。\n"
            "早期版本：微信更新可能导致部分功能暂时不可用。"
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        licenses = QPushButton("开源软件许可")
        licenses.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(service.project_root / "THIRD_PARTY_LICENSES.txt"))
            )
        )
        layout.addWidget(licenses)
        logs = QPushButton("打开日志文件夹")
        logs.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(service.settings.app_data_dir / "logs"))
            )
        )
        layout.addWidget(logs)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
