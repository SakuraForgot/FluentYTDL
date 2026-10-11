"""Startup-result replay and one recoverable prompt per POT incident."""

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication
from qfluentwidgets import MessageBoxBase, PushButton, StrongBodyLabel, SubtitleLabel

from ..notification.notification_center import notification_center
from ..notification.notification_model import Notification
from ..youtube.pot_manager import pot_manager
from .components.dialogs.pot_status_window import PotStatusWindow, failure_text


class PotStartupController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._seen = set()
        self._pending = None
        self._dialog = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)
        pot_manager.signals.state_changed.connect(
            self._on_state, Qt.ConnectionType.QueuedConnection
        )
        QApplication.instance().aboutToQuit.connect(self._close)
        self._on_state(pot_manager.snapshot())

    def _on_state(self, state):
        if not state["enabled"] or state["state"] != "unavailable":
            self._pending = None
            if state["state"] == "ready" and self._dialog:
                self._dialog.reject()
            return
        if state["incident"] in self._seen:
            return
        self._pending = state
        if not self._timer.isActive():
            self._timer.start(0)

    def _show_pending(self):
        state = self._pending
        if state is None:
            return
        if not self.window.isVisible() or QApplication.activeModalWidget() is not None:
            self._timer.start(250)
            return
        self._seen.add(state["incident"])
        self._pending = None
        notification_center.push(
            Notification(
                type="pot_unavailable",
                title=self.tr("YouTube 支持组件未就绪"),
                message=failure_text(state["code"]),
                severity="warning",
                metadata={"action": "manage_pot", "incident": state["incident"]},
            )
        )
        box = MessageBoxBase(self.window)
        box.viewLayout.addWidget(SubtitleLabel(self.tr("YouTube 支持组件未就绪"), box))
        body = StrongBodyLabel(
            self.tr("暂时无法解析或下载 YouTube 链接。") + "\n" + failure_text(state["code"]), box
        )
        body.setWordWrap(True)
        box.viewLayout.addWidget(body)
        box.yesButton.setText(self.tr("重试加载"))
        box.cancelButton.setText(self.tr("稍后"))
        manage = PushButton(self.tr("查看状态"), box)
        box.buttonLayout.insertWidget(1, manage)
        open_panel = []
        manage.clicked.connect(lambda: (open_panel.append(True), box.reject()))
        self._dialog = box
        accepted = box.exec()
        self._dialog = None
        box.deleteLater()
        if accepted:
            pot_manager.signals.retry_requested.emit()
        elif open_panel:
            PotStatusWindow.show_singleton(self.window)

    def _close(self):
        self._timer.stop()
        self._pending = None
        if self._dialog:
            self._dialog.reject()
        panel = PotStatusWindow.singleton()
        if panel:
            panel.close()
