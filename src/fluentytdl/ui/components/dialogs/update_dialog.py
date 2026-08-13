"""
更新提示对话框

展示:
- 新版本号（区分稳定版/预发布版）
- 更新日志 (Markdown 渲染)
- 下载进度条
- 按钮: self.tr("立即更新") / self.tr("跳过此版本") / self.tr("稍后提醒")
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

from ....core.component_update_manager import component_update_manager
from ....core.config_manager import config_manager


class UpdateDialog(MessageBoxBase):
    """软件更新弹窗"""

    skip_requested = Signal(str)  # 跳过的版本号

    def __init__(self, update_info: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.update_info = update_info

        # 设置最小尺寸
        self.widget.setMinimumSize(600, 420)

        # 标题（区分稳定版/预发布版）
        version = update_info.get("version", "?")
        is_prerelease = update_info.get("is_prerelease", False)
        if is_prerelease:
            title_text = f"发现预发布版本 {version}"
        else:
            title_text = f"发现新版本 {version}"

        self.titleLabel = SubtitleLabel(title_text, self)
        self.viewLayout.addWidget(self.titleLabel)

        # 预发布提示
        if is_prerelease:
            self.preNotice = BodyLabel(
                self.tr("此为预发布版本，可能存在不稳定因素。建议仅在测试环境中使用。"), self
            )
            self.preNotice.setStyleSheet("color: #e6a817;")
            self.viewLayout.addWidget(self.preNotice)

        # 更新日志
        self.changelog = TextEdit(self)
        self.changelog.setReadOnly(True)
        self.changelog.setMarkdown(update_info.get("changelog") or self.tr("暂无更新说明"))
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
        self.yesButton.setText(self.tr("立即更新"))
        self.cancelButton.setText(self.tr("稍后提醒"))

        self.skipBtn = PushButton(self.tr("跳过此版本"), self.buttonGroup)
        self.buttonLayout.insertWidget(1, self.skipBtn)

        # 信号：断开默认 accept，改为下载
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_update_clicked)
        self.skipBtn.clicked.connect(self._on_skip_clicked)

        # 连接 ComponentUpdateManager 信号
        component_update_manager.download_progress.connect(self._on_progress)
        component_update_manager.download_finished.connect(self._on_downloaded)
        component_update_manager.download_error.connect(self._on_error)

    def _on_update_clicked(self) -> None:
        url = self.update_info.get("url") or self.update_info.get("download_url")
        if not url:
            self.progressLabel.setText(self.tr("下载地址无效"))
            self.progressLabel.show()
            return

        self.yesButton.setEnabled(False)
        self.skipBtn.setEnabled(False)
        self.cancelButton.setEnabled(False)
        self.progressLabel.setText(self.tr("正在下载更新..."))
        self.progressLabel.show()
        self.progressBar.show()

        sha256 = self.update_info.get("sha256") or ""
        component_update_manager.download_app_update(url, sha256)

    def _on_progress(self, percent: int) -> None:
        self.progressBar.setValue(percent)
        self.progressLabel.setText(f"正在下载更新... {percent}%")

    def _on_downloaded(self, path: str) -> None:
        """归档就绪 —— 本对话框的职责到此结束。

        安装决策（活跃任务确认、失败告知）由设置页卡片 + 后端状态机负责：卡片也订阅了
        `download_finished`，会用同一个归档路径调 `request_app_core_update()`，后端按
        路径去重，所以这里再调一次也不会重复触发。必须 `close()` —— 这是个模态框，
        留着会遮住卡片的确认弹窗，而且它自己也没有后续状态可以显示了。
        """
        self.progressLabel.setText(self.tr("下载完成，正在安装..."))
        component_update_manager.request_app_core_update(path)
        self.close()

    def _on_error(self, msg: str) -> None:
        self.progressLabel.setText(msg)
        self.yesButton.setEnabled(True)
        self.skipBtn.setEnabled(True)
        self.cancelButton.setEnabled(True)

    def _on_skip_clicked(self) -> None:
        ver = self.update_info.get("version") or ""
        # 仅 stable 通道支持自动更新，统一使用 skipped_stable_version
        config_manager.set("skipped_stable_version", ver)
        self.reject()

    def hideEvent(self, event) -> None:  # noqa: N802
        """对话框关闭时断开信号，避免多次连接"""
        try:
            component_update_manager.download_progress.disconnect(self._on_progress)
            component_update_manager.download_finished.disconnect(self._on_downloaded)
            component_update_manager.download_error.disconnect(self._on_error)
        except (RuntimeError, TypeError):
            pass
        super().hideEvent(event)
