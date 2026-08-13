"""
FluentYTDL 软件更新卡片

在设置页 self.tr("更新") 标签中显示，提供:
- 当前版本和最新版本对比
- 检查更新 / 立即更新按钮
- 更新日志查看
- 下载进度
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
    FluentIcon,
    MessageBox,
    ProgressBar,
    PushButton,
    SettingCard,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
)

from fluentytdl.ui.components.common.custom_info_bar import InfoBar

from ....core.component_update_manager import component_update_manager


class AppUpdateSettingCard(SettingCard):
    """软件更新卡片：显示版本信息、检查/执行更新。"""

    def __init__(self, parent: QWidget | None = None):
        try:
            from fluentytdl import __version__

            current_ver = __version__
        except ImportError:
            current_ver = "unknown"

        super().__init__(
            FluentIcon.APPLICATION,
            "FluentYTDL",
            self.tr("当前版本: {}").format(current_ver),
            parent,
        )
        self._current_version = current_ver
        self._latest_info: dict | None = None
        self._downloading = False

        # 进度条
        self.progressBar = ProgressBar(self)
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        self.progressBar.setFixedWidth(120)
        self.progressBar.setVisible(False)

        # 更新日志按钮
        self.changelogButton = ToolButton(FluentIcon.DICTIONARY, self)
        self.changelogButton.setToolTip(self.tr("查看更新日志"))
        self.changelogButton.installEventFilter(
            ToolTipFilter(self.changelogButton, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.changelogButton.clicked.connect(self._show_changelog)
        self.changelogButton.setVisible(False)

        # 操作按钮
        self.actionButton = PushButton(self.tr("检查更新"), self)
        self.actionButton.clicked.connect(self._on_action_clicked)

        # 布局
        self.hBoxLayout.addWidget(self.progressBar, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(10)
        self.hBoxLayout.addWidget(self.changelogButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(5)
        self.hBoxLayout.addWidget(self.actionButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 连接信号
        component_update_manager.app_update_available.connect(self._on_update_available)
        component_update_manager.app_no_update.connect(self._on_no_update)
        component_update_manager.app_check_error.connect(self._on_check_error)
        component_update_manager.download_progress.connect(self._on_download_progress)
        component_update_manager.download_finished.connect(self._on_download_finished)
        component_update_manager.download_error.connect(self._on_download_error)
        component_update_manager.apply_error.connect(self._on_apply_error)
        component_update_manager.apply_confirm_needed.connect(self._on_apply_confirm_needed)

    # ── 状态机 ────────────────────────────────────────────

    def _on_action_clicked(self) -> None:
        text = self.actionButton.text()
        if text == self.tr("检查更新"):
            self._start_check()
        elif text == self.tr("立即更新"):
            self._start_download()

    def _start_check(self) -> None:
        """开始检查更新。"""
        # 检查版本锁定（beta/pre）
        if component_update_manager.is_locked():
            self._show_locked_dialog()
            return

        self.actionButton.setText(self.tr("正在检查..."))
        self.actionButton.setEnabled(False)
        component_update_manager.check_app_update()

    def _start_download(self) -> None:
        """开始下载更新。"""
        if not self._latest_info:
            return

        url = self._latest_info.get("url", "")
        if not url:
            InfoBar.error(self.tr("错误"), self.tr("下载地址无效"), parent=self.window())
            return

        self._downloading = True
        self.progressBar.setVisible(True)
        self.progressBar.setValue(0)
        self.actionButton.setEnabled(False)
        self.actionButton.setText(self.tr("正在下载..."))
        self.changelogButton.setEnabled(False)

        sha256 = self._latest_info.get("sha256", "")
        component_update_manager.download_app_update(url, sha256)

    # ── 信号回调 ──────────────────────────────────────────

    def _on_update_available(self, info: dict) -> None:
        """有更新可用。"""
        self._latest_info = info
        self.actionButton.setEnabled(True)

        latest_ver = info.get("version", "?")
        is_pre = info.get("is_prerelease", False)
        prefix = self.tr("预发布 ") if is_pre else ""

        self.setTitle(f"FluentYTDL ({prefix}更新)")
        self.setContent(f"当前: {self._current_version}  |  最新: {latest_ver}")
        self.actionButton.setText(self.tr("立即更新"))
        self.changelogButton.setVisible(True)

        InfoBar.info(
            self.tr("发现新版本"),
            f"FluentYTDL {latest_ver} 已可用",
            duration=10000,
            parent=self.window(),
        )

    def _on_no_update(self) -> None:
        """无更新。"""
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("检查更新"))
        self.setContent(self.tr("当前版本: {}  |  已是最新").format(self._current_version))

        InfoBar.info(
            self.tr("已是最新"),
            self.tr("FluentYTDL {} 已是最新版本。").format(self._current_version),
            duration=5000,
            parent=self.window(),
        )

    def _on_check_error(self, msg: str) -> None:
        """检查出错。"""
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("检查更新"))

        if msg == "locked":
            self._show_locked_dialog()
            return

        InfoBar.error(self.tr("检查更新失败"), msg, duration=10000, parent=self.window())

    def _on_download_progress(self, percent: int) -> None:
        """下载进度。"""
        if self._downloading:
            self.progressBar.setValue(percent)
            self.actionButton.setText(f"正在下载... {percent}%")

    def _on_download_finished(self, path: str) -> None:
        """下载完成，请求应用更新。

        `request_app_core_update()` 只校验 + 暂存，不会退出进程，也不抛异常 ——
        失败经 `apply_error` 回来，需要确认经 `apply_confirm_needed` 回来。
        """
        self.progressBar.setValue(100)
        self.actionButton.setText(self.tr("正在安装..."))
        component_update_manager.request_app_core_update(path)

    def _reset_action_state(self) -> None:
        """把卡片恢复成"可以再点一次更新"的样子。"""
        self._downloading = False
        self.progressBar.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("立即更新"))
        self.changelogButton.setEnabled(True)

    def _on_apply_error(self, msg: str) -> None:
        """应用更新失败（updater.exe 或归档缺失等终止性错误）。"""
        self._reset_action_state()
        InfoBar.error(self.tr("更新失败"), msg, duration=15000, parent=self.window())

    def _on_apply_confirm_needed(self, active: int, gen: int) -> None:
        """有活跃下载任务，问一次再决定是否继续。

        用 singleShot 把模态对话框挪出信号发射栈：此刻我们还在
        `request_app_core_update()` 的 emit 里，不能在这里阻塞它。
        """
        QTimer.singleShot(0, lambda: self._ask_confirm(active, gen))

    def _ask_confirm(self, active: int, gen: int) -> None:
        box = MessageBox(
            self.tr("确认更新"),
            self.tr("当前有 {n} 个下载任务正在进行，更新将中断它们。是否继续？").format(n=active),
            self.window(),
        )
        box.yesButton.setText(self.tr("继续更新"))
        box.cancelButton.setText(self.tr("取消"))
        if box.exec():
            self.actionButton.setText(self.tr("正在安装..."))
            component_update_manager.confirm_pending_update(gen)
        else:
            component_update_manager.cancel_pending_update(gen)
            self._reset_action_state()

    def _on_download_error(self, msg: str) -> None:
        """下载出错。"""
        self._reset_action_state()
        InfoBar.error(self.tr("下载失败"), msg, duration=15000, parent=self.window())

    # ── 更新日志 ──────────────────────────────────────────

    def _show_changelog(self) -> None:
        """显示更新日志对话框。"""
        if not self._latest_info:
            return

        from fluentytdl.ui.components.dialogs.update_dialog import UpdateDialog

        dialog = UpdateDialog(
            {
                "version": self._latest_info.get("version", "?"),
                "changelog": self._latest_info.get("changelog", ""),
                "download_url": self._latest_info.get("url", ""),
                "sha256": self._latest_info.get("sha256", ""),
                "install_type": "full",
            },
            parent=self.window(),
        )
        dialog.exec()

    # ── Locked 弹窗 ──────────────────────────────────────

    def _show_locked_dialog(self) -> None:
        """显示锁定版本提示（beta/pre 不支持自动更新）。"""
        try:
            from fluentytdl import __version__

            ver = __version__
        except ImportError:
            ver = "unknown"

        InfoBar.warning(
            self.tr("检测到测试版本"),
            f"当前运行的是 {ver} 测试/预发布版本，不支持自动更新。"
            + self.tr("如需更新请前往 GitHub Releases 下载正式版。"),
            duration=10000,
            parent=self.window(),
        )

    # ── 手动触发检查 ──────────────────────────────────────

    def check_for_update(self) -> None:
        """外部调用：自动检查更新（静默模式，不弹无更新提示）。"""
        if component_update_manager.is_locked():
            return
        component_update_manager.check_app_update()

    def reset_state(self) -> None:
        """重置到初始状态。"""
        self._downloading = False
        self._latest_info = None
        self.progressBar.setVisible(False)
        self.changelogButton.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("检查更新"))
        try:
            from fluentytdl import __version__

            self.setContent(self.tr("当前版本: {}").format(__version__))
        except ImportError:
            pass
