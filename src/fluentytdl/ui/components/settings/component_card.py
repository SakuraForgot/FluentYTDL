"""Component updates in the same horizontal rows as the application updater."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFileDialog
from qfluentwidgets import (
    Action,
    CaptionLabel,
    DropDownPushButton,
    FluentIcon,
    ProgressBar,
    PushButton,
    RoundMenu,
    SettingCard,
    StrongBodyLabel,
)

from ....core.dependency_manager import dependency_manager
from ....core.pot_component import runtime_path
from ..common.custom_info_bar import InfoBar
from .component_error_text import translate


class ComponentSettingCard(SettingCard):
    status_requested = Signal()
    enabled_requested = Signal(bool)

    def __init__(self, component_key, icon, title, content, parent=None):
        super().__init__(icon, title, content or " ", parent)
        self.component_key = component_key
        self._runtime = {}
        self._runtime_status = ""
        self._installed = False
        self._action = "check"
        old_title, old_content = self.titleLabel, self.contentLabel
        self.titleLabel = StrongBodyLabel(title, self)
        self.titleLabel.setToolTip(content)
        self.currentLabel = self._caption("")
        self.currentLabel.setObjectName("contentLabel")
        self.contentLabel = self.currentLabel
        self.vBoxLayout.replaceWidget(old_title, self.titleLabel)
        self.vBoxLayout.replaceWidget(old_content, self.contentLabel)
        old_title.hide()
        old_content.hide()
        old_title.deleteLater()
        old_content.deleteLater()
        self.progressBar = ProgressBar(self)
        self.progressBar.setRange(0, 100)
        self.progressBar.setFixedWidth(120)
        self.hBoxLayout.addWidget(self.progressBar)
        self.hBoxLayout.addSpacing(10)
        self.actionButton = PushButton(self.tr("检查更新"), self)
        self.actionButton.clicked.connect(self._run_action)
        self.hBoxLayout.addWidget(self.actionButton)
        self.hBoxLayout.addSpacing(10)
        if component_key == "pot-provider":
            self.statusButton = PushButton(self.tr("运行状态"), self)
            self.statusButton.clicked.connect(self.status_requested.emit)
            self.hBoxLayout.addWidget(self.statusButton)
            self.hBoxLayout.addSpacing(10)
        self.moreButton = DropDownPushButton(self.tr("更多"), self)
        self.menu = RoundMenu(parent=self.moreButton)
        self.repairAction = Action(FluentIcon.SYNC, self.tr("修复组件"), triggered=self._install)
        self.importAction = Action(FluentIcon.ADD, self.tr("本地导入"), triggered=self._import)
        self.folderAction = Action(
            FluentIcon.FOLDER, self.tr("打开所在文件夹"), triggered=self._open_folder
        )
        self.menu.addActions([self.repairAction, self.importAction])
        self.menu.addSeparator()
        self.menu.addAction(self.folderAction)
        if component_key == "pot-provider":
            self.enabledAction = Action(self.tr("启用 POT"), self)
            self.enabledAction.triggered.connect(self._toggle_enabled)
            self.menu.addSeparator()
            self.menu.addAction(self.enabledAction)
        self.moreButton.setMenu(self.menu)
        self.hBoxLayout.addWidget(self.moreButton)
        self.hBoxLayout.addSpacing(16)
        for signal in (
            dependency_manager.local_refreshed,
            dependency_manager.check_started,
            dependency_manager.check_finished,
            dependency_manager.check_error,
            dependency_manager.download_started,
            dependency_manager.download_progress,
            dependency_manager.download_status,
            dependency_manager.download_error,
        ):
            signal.connect(self._refresh_key)
        dependency_manager.install_finished.connect(self._on_installed)
        self.render()

    def _caption(self, text):
        label = CaptionLabel(text, self)
        label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def showEvent(self, event):
        super().showEvent(event)
        dependency_manager.local_requested.emit(self.component_key)
        self.render()

    def _refresh_key(self, key, *_args):
        if key == self.component_key:
            self.render()

    def _on_installed(self, key):
        if key == self.component_key:
            self._installed = True
            self.render()

    def set_runtime(self, state, status=""):
        self._runtime = state
        self._runtime_status = status
        self.render()

    def _toggle_enabled(self):
        self.enabled_requested.emit(not bool(self._runtime.get("enabled")))

    def _check(self):
        dependency_manager.check_requested.emit(self.component_key)

    def _run_action(self):
        if self._action == "install":
            self._install()
        else:
            self._check()

    def setContent(self, text):
        self.titleLabel.setToolTip(text)
        self.render()

    def setValue(self, _value):
        dependency_manager.local_requested.emit(self.component_key)

    def _install(self):
        dependency_manager.install_requested.emit(self.component_key)

    def _import(self):
        if self.component_key == "pot-provider":
            title = self.tr("选择 POT 上游 Release ZIP")
            file_filter = "POT (*.zip)"
        else:
            name = dependency_manager.components[self.component_key].exe_name
            title = self.tr("选择 {0}").format(name)
            file_filter = f"Executable ({name})"
        path, _ = QFileDialog.getOpenFileName(self.window(), title, "", file_filter)
        if path:
            dependency_manager.import_requested.emit(self.component_key, path)

    def _open_folder(self):
        info = dependency_manager.components[self.component_key]
        path = info.local_result.get("exe_path") or info.last_result.get("exe_path")
        if not path:
            path = info.local_result.get("install_path")
        try:
            folder = runtime_path(Path(path)).parent if path else None
            if folder is None or not folder.is_dir():
                raise FileNotFoundError(self.tr("组件目录尚未创建"))
            os.startfile(folder)
        except OSError as error:
            InfoBar.warning(self.tr("无法打开目录"), str(error), parent=self.window())

    def render(self):
        info = dependency_manager.components[self.component_key]
        remote = info.last_result
        local = info.local_result or remote
        current = local.get("current", "unknown")
        if self.component_key == "pot-provider" and self._runtime.get("state") == "ready":
            current = self._runtime.get("version") or current
        elif self.component_key == "deno" and self._runtime.get("state") == "ready":
            current = self._runtime.get("deno_version") or current
        known = current not in {None, "", "unknown"}
        missing = bool(local) and not known and not local.get("source")
        channel = local.get("current_channel", "")
        current_text = (
            f"{current} ({channel})"
            if known and channel and self.component_key == "yt-dlp"
            else str(current)
            if known
            else self.tr("版本无法读取")
            if local.get("source")
            else self.tr("未安装")
            if local
            else self.tr("正在读取…")
        )
        latest = remote.get("latest")
        latest_text = (
            self.tr("未检查")
            if not remote
            else self.tr("暂无可安装包")
            if latest in {None, "", "unknown"}
            else str(latest)
        )
        if remote.get("latest_channel") and self.component_key == "yt-dlp":
            latest_text += f" ({remote['latest_channel']})"
        summary = self.tr("当前：{0}").format(current_text)
        if remote.get("update_available"):
            summary += f"  →  {latest_text}"
        elif known and remote.get("url"):
            summary += "  ·  " + self.tr("已是最新")
        if self._runtime_status:
            summary += "  ·  " + self._runtime_status
        hint = []
        if self.component_key == "pot-provider":
            identity = self._runtime.get("bundle_id") or local.get("current_bundle_id")
            if identity:
                hint.append(self.tr("组件包：{0}").format(identity))
            if remote.get("upstream_latest"):
                hint.append(self.tr("上游版本：{0}").format(remote["upstream_latest"]))
            elif not known:
                hint.append(self.tr("需要原版 POT 配套组件；旧 Rust 组件不能用于此版本。"))
        if local.get("source") == "custom":
            hint.append(self.tr("当前使用自定义路径；更新只写入托管目录。"))
        if self.component_key == "ffmpeg":
            hint.append(self.tr("本地导入时，ffmpeg.exe 与 ffprobe.exe 须在同一文件夹。"))
        installing = dependency_manager.is_installing(self.component_key)
        checking = dependency_manager.is_checking(self.component_key)
        busy = installing or checking
        for control in (self.actionButton, self.repairAction, self.importAction):
            control.setEnabled(not busy)
        if self.component_key == "pot-provider":
            enabled = bool(self._runtime.get("enabled"))
            stopping = not enabled and self._runtime.get("state") in {
                "checking",
                "waiting",
                "starting",
                "probing",
                "stopping",
            }
            self.enabledAction.setText(self.tr("停用 POT") if enabled else self.tr("启用 POT"))
            self.enabledAction.setEnabled(bool(self._runtime) and not installing and not stopping)
        self._action = (
            "install"
            if missing or remote.get("update_available") or (not known and remote.get("url"))
            else "check"
        )
        self.actionButton.setText(
            self.tr("正在安装…")
            if installing
            else self.tr("正在检查…")
            if checking
            else self.tr("立即更新")
            if remote.get("update_available") and not missing
            else self.tr("安装组件")
            if self._action == "install"
            else self.tr("检查更新")
        )
        self.progressBar.setVisible(installing)
        self.progressBar.setValue(info.progress)
        stages = {
            "preparing": self.tr("正在准备运行依赖…"),
            "validate": self.tr("正在验证组件…"),
            "retrying": self.tr("连接失败，正在重试…"),
            "waiting": self.tr("等待当前任务结束"),
            "installing": self.tr("正在安装…"),
        }
        status = (
            stages.get(info.operation_status, self.tr("正在下载：{0}%").format(info.progress))
            if installing
            else self.tr("操作失败")
            if info.last_error
            else ""
        )
        self.currentLabel.setText(summary + ("  ·  " + status if status else ""))
        error = info.last_error
        self.currentLabel.setToolTip(
            "\n".join([*hint, translate(str(error.get("code", ""))) if error else ""])
        )
