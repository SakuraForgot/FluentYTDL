"""
Cookie 修复对话框

当检测到下载失败由 Cookie 失效引起时，弹出此对话框引导用户修复。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from qfluentwidgets import (
    BodyLabel,
    InfoBarPosition,
    MessageBoxBase,
    PushButton,
    StrongBodyLabel,
    isDarkTheme,
)

from fluentytdl.ui.components.common.custom_info_bar import InfoBar


class CookieRepairDialog(MessageBoxBase):
    """
    Cookie 修复对话框

    提供两个选项：
    1. 自动修复（可能需要 UAC）
    2. 手动导入 Cookie 文件
    """

    repair_requested = Signal()  # 用户点击自动修复
    manual_import_requested = Signal()  # 用户点击手动导入

    def __init__(
        self,
        error_message: str = "",
        parent=None,
        auth_source: str = "browser",
        platform: str = "youtube",
    ):
        super().__init__(parent)

        self.error_message = error_message
        self._auth_source = auth_source
        self._platform = platform
        # 「修复」按钮的文案随模式变化。存下来，show_repair_result() 失败回滚时要用 ——
        # 以前那里硬写"自动修复"，登录模式下点一次失败按钮就从"重新登录"变成"自动修复"。
        self._repair_label = {
            "webview2": self.tr("重新登录"),
            "file": self.tr("重新导入"),
        }.get(auth_source, self.tr("自动修复"))
        self._setup_ui()

    def _platform_label(self) -> str:
        """真相源展示名。跟着 platform 走，不再一律写 YouTube。"""
        from ....auth.auth_service import PLATFORM_LABELS

        return PLATFORM_LABELS.get(self._platform, self._platform)

    def _setup_ui(self):
        """初始化 UI"""
        self.widget.setMinimumWidth(500)

        # 标题
        self.title_label = StrongBodyLabel(self.tr("🔒 检测到 Cookie 验证失败"), self)
        self.title_label.setStyleSheet("font-size: 16px;")
        self.viewLayout.addWidget(self.title_label)

        # 平台名先 tr() 再 format —— 拼接后再翻译，源串会带上运行期内容，永远匹配不到译文
        label = self._platform_label()

        # 根据验证模式动态调整说明文本
        if self._auth_source == "webview2":
            desc_text = (
                self.tr("{} 需要重新验证身份，请选择以下方式修复：\n\n").format(label)
                + self.tr("• 重新登录：点击下方按钮在弹出的登录窗口中重新登录 {}\n").format(label)
                + self.tr("• 手动导入：使用浏览器扩展 Get cookies.txt LOCALLY 导出并导入")
            )
        elif self._auth_source == "file":
            desc_text = (
                self.tr("{} 需要重新验证身份，请选择以下方式修复：\n\n").format(label)
                + self.tr("• 重新导入：选择更新的 Cookie 文件 (Netscape 格式)\n")
                + self.tr("• 推荐使用浏览器扩展 Get cookies.txt LOCALLY 导出\n")
                + self.tr("• 或切换到「登录获取」模式，无需手动导出")
            )
        else:
            desc_text = (
                self.tr("{} 需要重新验证身份，请选择以下方式修复：\n\n").format(label)
                + self.tr("• 自动修复：尝试重新提取 Cookie (Edge 若失败请使用下方方案)\n")
                + self.tr("• 强烈建议：将设置页面的提取来源换为 Firefox 或 LibreWolf\n")
                + self.tr("• 手动导入：使用浏览器扩展 Get cookies.txt LOCALLY 导出并手动导入")
            )
        self.desc_label = BodyLabel(desc_text, self)
        self.desc_label.setWordWrap(True)
        self.viewLayout.addWidget(self.desc_label)

        # 错误详情（可折叠）
        if self.error_message:
            self.error_label = BodyLabel(
                f"错误详情：\n{self._truncate_error(self.error_message)}", self
            )
            self.error_label.setWordWrap(True)

            # 适配暗黑模式的错误颜色
            bg_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(255, 0, 0, 0.05)"
            text_color = "#ff99a4" if isDarkTheme() else "#d13438"

            self.error_label.setStyleSheet(
                f"background-color: {bg_color}; "
                "padding: 8px; "
                "border-radius: 4px; "
                f"color: {text_color};"
            )
            self.viewLayout.addWidget(self.error_label)

        self.viewLayout.setSpacing(16)
        self.viewLayout.setContentsMargins(24, 24, 24, 24)

        # 按钮区域 (MessageBoxBase 已经提供了 self.yesButton 和 self.cancelButton)
        self.cancelButton.setText(self.tr("稍后处理"))
        self.yesButton.setText(self._repair_label)

        try:
            self.yesButton.clicked.disconnect()
        except RuntimeError:
            pass
        self.yesButton.clicked.connect(self._on_auto_repair)

        try:
            self.cancelButton.clicked.disconnect()
        except RuntimeError:
            pass
        self.cancelButton.clicked.connect(self.reject)

        # 自定义 手动导入 按钮
        self.manual_btn = PushButton(self.tr("手动导入 Cookie"), self)
        self.manual_btn.clicked.connect(self._on_manual_import)

        # 将其插入到 cancelButton 和 yesButton 之间
        self.buttonLayout.insertWidget(1, self.manual_btn, 1, Qt.AlignmentFlag.AlignVCenter)

    def _truncate_error(self, error: str, max_lines: int = 5) -> str:
        """截断错误信息避免过长"""
        lines = error.strip().split("\n")
        if len(lines) <= max_lines:
            return error
        return "\n".join(lines[:max_lines]) + f"\n... (还有 {len(lines) - max_lines} 行)"

    def _on_auto_repair(self):
        """自动修复按钮点击"""
        self.yesButton.setEnabled(False)
        self.yesButton.setText(self.tr("修复中..."))
        self.repair_requested.emit()

    def _on_manual_import(self):
        """手动导入按钮点击"""
        self.manual_import_requested.emit()
        self.accept()

    def show_repair_result(self, success: bool, message: str):
        """
        显示修复结果

        Args:
            success: 修复是否成功
            message: 结果消息
        """
        if success:
            InfoBar.info(
                title=self.tr("修复成功"),
                content=message,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            # 延迟关闭对话框，让用户看到成功消息
            from PySide6.QtCore import QTimer

            QTimer.singleShot(1500, self.accept)
        else:
            InfoBar.error(
                title=self.tr("修复失败"),
                content=message,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            # 恢复按钮状态
            self.yesButton.setEnabled(True)
            self.yesButton.setText(self._repair_label)


def show_cookie_repair_dialog(
    error_message: str = "", parent=None, platform: str = "youtube"
) -> CookieRepairDialog:
    """
    显示 Cookie 修复对话框（便捷函数）

    Args:
        error_message: 错误消息
        parent: 父窗口
        platform: 目标平台（youtube / twitter），决定文案里的平台名

    Returns:
        对话框实例（已显示但未 exec）
    """
    dialog = CookieRepairDialog(error_message, parent, platform=platform)
    dialog.show()
    return dialog
