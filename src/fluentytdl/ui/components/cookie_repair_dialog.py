"""
Cookie 修复对话框

当检测到下载失败由 Cookie 失效引起时，弹出此对话框引导用户修复。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)


class CookieRepairDialog(QDialog):
    """
    Cookie 修复对话框

    提供两个选项：
    1. 自动修复（可能需要 UAC）
    2. 手动导入 Cookie 文件
    """

    repair_requested = Signal()  # 用户点击自动修复
    manual_import_requested = Signal()  # 用户点击手动导入

    def __init__(self, error_message: str = "", parent=None, auth_source: str = "browser"):
        super().__init__(parent)

        self.error_message = error_message
        self._auth_source = auth_source
        self._setup_ui()

    def _setup_ui(self):
        """初始化 UI"""
        self.setWindowTitle("Cookie 已失效")
        self.setMinimumWidth(500)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # 标题
        title_label = StrongBodyLabel("🔒 检测到 Cookie 验证失败", self)
        title_label.setStyleSheet("font-size: 16px;")
        layout.addWidget(title_label)

        # 根据验证模式动态调整说明文本
        if self._auth_source == "dle":
            desc_text = (
                "YouTube 需要重新验证身份，请选择以下方式修复：\n\n"
                "• 重新登录：点击下方按钮在浏览器中重新登录 YouTube\n"
                "• 手动导入：使用浏览器扩展 Get cookies.txt LOCALLY 导出并导入"
            )
        elif self._auth_source == "file":
            desc_text = (
                "YouTube 需要重新验证身份，请选择以下方式修复：\n\n"
                "• 重新导入：选择更新的 Cookie 文件 (Netscape 格式)\n"
                "• 推荐使用浏览器扩展 Get cookies.txt LOCALLY 导出\n"
                "• 或切换到「登录获取」模式，无需手动导出"
            )
        else:
            desc_text = (
                "YouTube 需要重新验证身份，请选择以下方式修复：\n\n"
                "• 自动修复：尝试重新提取 Cookie (Chrome/Edge 若失败请使用下方方案)\n"
                "• 强烈建议：将设置页面的提取来源换为 Firefox 或 LibreWolf\n"
                "• 手动导入：使用浏览器扩展 Get cookies.txt LOCALLY 导出并手动导入"
            )
        desc_label = BodyLabel(desc_text, self)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # 错误详情（可折叠）
        if self.error_message:
            error_label = BodyLabel(f"错误详情：\n{self._truncate_error(self.error_message)}", self)
            error_label.setWordWrap(True)
            error_label.setStyleSheet(
                "background-color: rgba(255, 0, 0, 0.05); "
                "padding: 8px; "
                "border-radius: 4px; "
                "color: #d13438;"
            )
            layout.addWidget(error_label)

        layout.addStretch(1)

        # 按钮区域
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        # 取消按钮
        self.cancel_btn = PushButton("稍后处理", self)
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)

        button_layout.addStretch(1)

        # 手动导入按钮
        self.manual_btn = PushButton("手动导入 Cookie", self)
        self.manual_btn.clicked.connect(self._on_manual_import)
        button_layout.addWidget(self.manual_btn)

        # 自动修复按钮（主要操作）
        self.repair_btn = PrimaryPushButton("自动修复", self)
        self.repair_btn.clicked.connect(self._on_auto_repair)
        button_layout.addWidget(self.repair_btn)

        layout.addLayout(button_layout)

    def _truncate_error(self, error: str, max_lines: int = 5) -> str:
        """截断错误信息避免过长"""
        lines = error.strip().split("\n")
        if len(lines) <= max_lines:
            return error
        return "\n".join(lines[:max_lines]) + f"\n... (还有 {len(lines) - max_lines} 行)"

    def _on_auto_repair(self):
        """自动修复按钮点击"""
        self.repair_btn.setEnabled(False)
        self.repair_btn.setText("修复中...")
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
            InfoBar.success(
                title="修复成功",
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
                title="修复失败",
                content=message,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            # 恢复按钮状态
            self.repair_btn.setEnabled(True)
            self.repair_btn.setText("自动修复")


def show_cookie_repair_dialog(error_message: str = "", parent=None) -> CookieRepairDialog:
    """
    显示 Cookie 修复对话框（便捷函数）

    Args:
        error_message: 错误消息
        parent: 父窗口

    Returns:
        对话框实例（已显示但未 exec）
    """
    dialog = CookieRepairDialog(error_message, parent)
    dialog.show()
    return dialog
