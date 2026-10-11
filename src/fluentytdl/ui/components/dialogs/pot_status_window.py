"""Small POT status window; updates remain in the Settings component rows."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QHBoxLayout
from qfluentwidgets import CaptionLabel, PushButton, StrongBodyLabel, SubtitleLabel

from ....utils.ui_text import tr_text
from ....youtube.pot_manager import pot_manager
from ..common.standalone_window import StandaloneWindow


def failure_text(code):
    return {
        "pot_package_missing": tr_text("POT 原版组件尚未安装，请检查更新或修复组件。"),
        "pot_package_corrupt": tr_text("POT 组件文件损坏，请修复组件。"),
        "pot_package_incomplete": tr_text("POT 组件不完整，请修复组件。"),
        "pot_runtime_missing": tr_text("未找到 Deno，请安装或检查运行时路径。"),
        "pot_runtime_invalid": tr_text("Deno 无法运行，请检查或修复运行时。"),
        "pot_runtime_incompatible": tr_text("当前 Deno 与 POT 不兼容，请更新 Deno。"),
        "pot_app_incompatible": tr_text("此 POT 组件需要更新 FluentYTDL 后才能使用。"),
        "pot_plugin_incompatible": tr_text("POT 插件与当前 yt-dlp 不兼容，请更新或修复内核。"),
        "pot_token_rejected": tr_text("POT 服务未能生成令牌，请检查网络和代理后重试。"),
        "pot_plugin_conflict": tr_text("发现另一份 POT 插件，请查看日志中的冲突路径。"),
        "pot_ytdlp_missing": tr_text("未找到 yt-dlp，请安装或检查内核路径。"),
        "pot_cli_probe_failed": tr_text(
            "yt-dlp 未能通过 POT 获取令牌，请检查网络、代理和组件版本。"
        ),
        "pot_timeout": tr_text("POT 验证超时，请检查网络后重试。"),
        "pot_local_start_failed": tr_text("POT 服务启动失败，请查看日志或修复组件。"),
        "pot_local_health_failed": tr_text("POT 进程仍在运行，但服务未正常响应，请重启 POT。"),
        "pot_process_exited": tr_text("POT 服务意外退出，可以重新加载。"),
        "pot_permission_denied": tr_text("无法访问组件文件，请检查目录权限。"),
    }.get(code, tr_text("POT 尚未就绪，请查看日志并重试。"))


class PotStatusWindow(StandaloneWindow):
    SIZE_BOUNDS = ((500, 580), (320, 360))
    MIN_SIZE = (500, 320)

    def __init__(self, anchor=None):
        super().__init__(self.tr("POT 运行状态"), anchor=anchor)
        self.viewLayout.addWidget(SubtitleLabel(self.tr("POT 运行状态"), self))
        row = QHBoxLayout()
        self.statusLabel = StrongBodyLabel(self)
        self.stateLabel = self._caption()
        row.addWidget(self.statusLabel)
        row.addWidget(self.stateLabel, 1)
        self.viewLayout.addLayout(row)
        checks = QGridLayout()
        checks.setHorizontalSpacing(24)
        checks.setVerticalSpacing(8)
        checks.setColumnStretch(1, 1)
        self.checkLabels = {}
        for index, (key, title) in enumerate(
            (
                ("deno", self.tr("Deno 运行时")),
                ("server", self.tr("POT 服务连接")),
                ("plugin", self.tr("yt-dlp 插件")),
                ("token", self.tr("Token 验证")),
            )
        ):
            label = self._caption()
            self.checkLabels[key] = label
            checks.addWidget(StrongBodyLabel(title, self), index, 0)
            checks.addWidget(label, index, 1)
        self.viewLayout.addLayout(checks)
        self.checkedLabel = self._caption()
        self.viewLayout.addWidget(self.checkedLabel)
        self.scopeLabel = self._caption(self.tr("验证范围：yt-dlp 插件调用与 Token 生成。"))
        self.viewLayout.addWidget(self.scopeLabel)
        self.reasonLabel = self._caption()
        self.reasonLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.viewLayout.addWidget(self.reasonLabel)
        self.viewLayout.addStretch()
        actions = QHBoxLayout()
        actions.addStretch()
        self.checkButton = PushButton(self.tr("重新验证"), self)
        self.checkButton.clicked.connect(self._check)
        self.restartButton = PushButton(self.tr("重启 POT"), self)
        self.restartButton.clicked.connect(self._restart)
        actions.addWidget(self.checkButton)
        actions.addWidget(self.restartButton)
        self.viewLayout.addLayout(actions)
        pot_manager.signals.state_changed.connect(self._render, Qt.ConnectionType.QueuedConnection)
        self._render(pot_manager.snapshot())

    def _caption(self, text=""):
        label = CaptionLabel(text, self)
        label.setWordWrap(True)
        label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        return label

    def _disable_actions(self):
        self.checkButton.setEnabled(False)
        self.restartButton.setEnabled(False)

    def _check(self):
        self._disable_actions()
        pot_manager.signals.check_requested.emit()

    def _restart(self):
        self._disable_actions()
        if not pot_manager.snapshot()["enabled"]:
            pot_manager.signals.enabled_requested.emit(True)
        else:
            pot_manager.signals.retry_requested.emit()

    def _render(self, state):
        self.statusLabel.setText(self.tr("运行中") if state["running"] else self.tr("未运行"))
        self.stateLabel.setText(pot_manager.status_brief(state))
        components = dict(state.get("components", {}))
        if state["state"] in {"stopped", "stopping", "waiting"}:
            components = {}
        elif not state["running"]:
            for key in ("server", "token"):
                if components.get(key) == "passed":
                    components[key] = "unchecked"
        results = {
            "passed": self.tr("通过"),
            "failed": self.tr("失败"),
            "checking": self.tr("验证中…"),
        }
        for key, label in self.checkLabels.items():
            result = components.get(key, "unchecked")
            text = results.get(result, self.tr("未验证"))
            if result == "passed":
                if key == "deno" and state.get("deno_version"):
                    text += " · " + state["deno_version"]
                elif key == "server":
                    text += " · " + self.tr("POT {0} · 端口 {1}").format(
                        state.get("version", ""), state.get("port", "")
                    )
                elif key == "plugin":
                    text = self.tr("已由 yt-dlp 加载官方 HTTP 插件")
                elif key == "token":
                    text = self.tr("已生成 · {0} 字符").format(state.get("token_length", 0))
            label.setText(text)
        checked = state.get("checked_at", 0)
        self.checkedLabel.setText(
            self.tr("最近验证：{0}").format(
                datetime.fromtimestamp(checked).strftime("%Y-%m-%d %H:%M:%S")
            )
            if checked
            else self.tr("尚未完成验证")
        )
        reason = failure_text(state["code"]) if state["state"] == "unavailable" else ""
        self.reasonLabel.setText(f"{reason}\n{state['code']}" if reason else "")
        self.reasonLabel.setVisible(bool(reason))
        busy = state["state"] in {"checking", "waiting", "starting", "probing", "stopping"}
        self.checkButton.setEnabled(state["enabled"] and not busy)
        self.restartButton.setEnabled(not busy)
        self.restartButton.setText(self.tr("重启 POT") if state["enabled"] else self.tr("启用 POT"))
        self._update_minimum_height()

    def _update_minimum_height(self):
        # Failure text can wrap onto more lines than the initial compact window.
        # Let Qt measure the whole layout before it compresses the check rows.
        self.viewLayout.invalidate()
        self.setMinimumHeight(
            max(self.MIN_SIZE[1], self.viewLayout.totalHeightForWidth(self.width()))
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "reasonLabel"):
            self._update_minimum_height()

    def closeEvent(self, event):
        pot_manager.signals.state_changed.disconnect(self._render)
        super().closeEvent(event)
