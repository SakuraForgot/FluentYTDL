"""
更新提示对话框

展示:
- 新版本号
- 更新日志 (Markdown 渲染)
- 下载进度条
- 按钮: "立即更新" / "跳过此版本" / "稍后提醒"
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
    BodyLabel,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TextEdit,
)

from ...core.app_update_manager import app_update_manager
from ...core.config_manager import config_manager


class UpdateDialog(MessageBoxBase):
    """软件更新弹窗"""

    skip_requested = Signal(str)  # 跳过的版本号

    def __init__(self, update_info: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.update_info = update_info

        # 设置最小尺寸
        self.widget.setMinimumSize(600, 420)

        # 标题
        self.titleLabel = SubtitleLabel(f"\U0001f389 发现新版本 v{update_info['version']}", self)
        self.viewLayout.addWidget(self.titleLabel)

        # 更新日志
        self.changelog = TextEdit(self)
        self.changelog.setReadOnly(True)
        self.changelog.setMarkdown(update_info.get("changelog") or "暂无更新说明")
        self.changelog.setMaximumHeight(250)
        self.viewLayout.addWidget(self.changelog)

        # 进度区 (默认隐藏)
        self.progressLabel = BodyLabel("", self)
        self.progressLabel.hide()
        self.viewLayout.addWidget(self.progressLabel)

        self.progressBar = ProgressBar(self)
        self.progressBar.hide()
        self.viewLayout.addWidget(self.progressBar)

        # 按钮
        self.yesButton.setText("\U0001f680 立即更新")
        self.cancelButton.setText("稍后提醒")

        self.skipBtn = PushButton("跳过此版本", self.buttonGroup)
        self.buttonLayout.insertWidget(1, self.skipBtn)

        # 信号：断开默认 accept，改为下载
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_update_clicked)
        self.skipBtn.clicked.connect(self._on_skip_clicked)

        # 连接 manager 信号
        app_update_manager.download_progress.connect(self._on_progress)
        app_update_manager.download_finished.connect(self._on_downloaded)
        app_update_manager.download_error.connect(self._on_error)

    def _on_update_clicked(self) -> None:
        url = self.update_info.get("download_url")
        if not url:
            self.progressLabel.setText("\u274c 下载地址无效")
            self.progressLabel.show()
            return

        self.yesButton.setEnabled(False)
        self.skipBtn.setEnabled(False)
        self.cancelButton.setEnabled(False)
        self.progressLabel.setText("正在下载更新...")
        self.progressLabel.show()
        self.progressBar.show()

        sha256 = self.update_info.get("sha256") or ""
        app_update_manager.download_update(url, sha256)

    def _on_progress(self, percent: int) -> None:
        self.progressBar.setValue(percent)
        self.progressLabel.setText(f"正在下载更新... {percent}%")

    def _on_downloaded(self, path: str) -> None:
        self.progressLabel.setText("下载完成，正在安装...")
        install_type = self.update_info.get("install_type") or "setup"
        app_update_manager.apply_update(path, install_type)

    def _on_error(self, msg: str) -> None:
        self.progressLabel.setText(f"\u274c {msg}")
        self.yesButton.setEnabled(True)
        self.skipBtn.setEnabled(True)
        self.cancelButton.setEnabled(True)

    def _on_skip_clicked(self) -> None:
        ver = self.update_info.get("version") or ""
        config_manager.set("skipped_app_version", ver)
        self.reject()

    def hideEvent(self, event) -> None:  # noqa: N802
        """对话框关闭时断开信号，避免多次连接"""
        try:
            app_update_manager.download_progress.disconnect(self._on_progress)
            app_update_manager.download_finished.disconnect(self._on_downloaded)
            app_update_manager.download_error.disconnect(self._on_error)
        except (RuntimeError, TypeError):
            pass
        super().hideEvent(event)
