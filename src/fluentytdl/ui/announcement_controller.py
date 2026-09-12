"""Window-owned announcement dialogs and bell actions."""

from PySide6.QtCore import QObject, Qt, QTimer
from qfluentwidgets import MessageBoxBase, SubtitleLabel

from ..notification.announcement_service import AnnouncementService
from ..notification.notification_center import notification_center
from ..utils.control_center_text import text
from .components.common.announcement_markdown import AnnouncementMarkdown
from .components.common.custom_info_bar import InfoBar


class AnnouncementDialog(MessageBoxBase):
    def __init__(self, item, parent, mandatory=False, notice=""):
        super().__init__(parent)
        self.mandatory = mandatory
        self.titleLabel = SubtitleLabel(item["title"], self)
        self.titleLabel.setWordWrap(True)
        self.titleLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.viewLayout.addWidget(self.titleLabel)
        if notice:
            from PySide6.QtGui import QColor
            from qfluentwidgets import CaptionLabel

            label = CaptionLabel(notice, self)
            label.setWordWrap(True)
            label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
            self.viewLayout.addWidget(label)
        content = AnnouncementMarkdown(self)
        content.setMarkdown(item.get("body", ""))
        self.viewLayout.addWidget(content, 1)
        self.yesButton.setText(text("acknowledge") if mandatory else text("close"))
        self.cancelButton.hide()
        self.setClosableOnMaskClicked(False)
        self._fit_parent()

    def _fit_parent(self):
        available = self.parentWidget().size()
        self.widget.setFixedSize(
            max(240, min(680, available.width() - 48)), max(240, min(640, available.height() - 64))
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "titleLabel"):
            self._fit_parent()

    def reject(self):
        if not self.mandatory:
            super().reject()


class AnnouncementController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.service = AnnouncementService(self)
        self.pending = []
        self.dialog = None
        self.manual = False
        self.offline = False
        self.confirmed = set()
        self.service.available.connect(self.received)
        self.service.failed.connect(self.failed)
        self.visibility_timer = QTimer(self)
        self.visibility_timer.setInterval(1500)
        self.visibility_timer.timeout.connect(self.show_next)
        self.visibility_timer.start()
        notification_center.announcement_requested.connect(self.open_detail)
        notification_center.announcement_refresh_requested.connect(self.refresh)
        from ..core.component_update_manager import component_update_manager
        from ..core.dependency_manager import dependency_manager

        self._reported_check_failure = False
        dependency_manager.check_error.connect(
            lambda key, message: self.check_failed(
                message, key in dependency_manager._silent_checks
            )
        )
        component_update_manager.app_check_error.connect(
            lambda message: self.check_failed(message, component_update_manager.is_silent_check)
        )

    def check_failed(self, message, silent):
        if not silent or message == "locked" or self._reported_check_failure:
            return
        from ..notification.notification_model import Notification

        self._reported_check_failure = True
        notification_center.push(
            Notification(
                type="update_check_failed",
                title=text("check_failed"),
                message=text("automatic_checks_failed") + "\n" + message,
                severity="warning",
            )
        )

    def refresh(self):
        self.manual = True
        self.service.refresh_requested.emit()

    def failed(self, message):
        if self.manual:
            InfoBar.warning(text("announcements"), message, parent=self.window, duration=5000)
            self.manual = False

    def received(self, items, offline):
        self.pending = list(items)
        self.offline = offline
        if (
            self.dialog
            and self.dialog.mandatory
            and not any((item["id"], item["revision"]) == self.dialog_identity for item in items)
        ):
            self.dialog.mandatory = False
            self.dialog.reject()
        if self.manual:
            InfoBar.success(
                text("announcements"),
                text("announcements_refreshed"),
                parent=self.window,
                duration=3000,
            )
            self.manual = False
        self.show_next()

    def show_next(self):
        if self.dialog or not self.pending or not self.window.isVisible():
            return
        item = self.pending.pop(0)
        if (item["id"], item["revision"]) in self.confirmed:
            QTimer.singleShot(0, self.show_next)
            return
        self.dialog_identity = (item["id"], item["revision"])
        self.dialog = AnnouncementDialog(
            item, self.window, mandatory=True, notice=text("offline_notice") if self.offline else ""
        )
        self.dialog.accepted.connect(lambda: self.confirm(item))
        self.dialog.finished.connect(self.closed)
        self.dialog.show()

    def open_detail(self, item):
        if self.dialog:
            return
        active = (item["id"], item["revision"]) in self.service.active
        self.dialog = AnnouncementDialog(
            item, self.window, notice="" if active else text("inactive")
        )
        self.dialog.finished.connect(self.closed)
        self.dialog.show()

    def confirm(self, item):
        self.confirmed.add((item["id"], item["revision"]))
        self.service.acknowledge_requested.emit(item)

    def closed(self, *_):
        self.dialog.deleteLater()
        self.dialog = None
        QTimer.singleShot(0, self.show_next)
