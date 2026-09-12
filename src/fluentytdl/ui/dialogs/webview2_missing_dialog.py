"""
WebView2 运行时缺失对话框

当用户点击「WebView2 登录」但本机缺少 WebView2 运行时（或运行时版本无效）时，
弹出此对话框引导用户选择后续方案，避免登录流程在缺少运行时的情况下静默失败。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from qfluentwidgets import BodyLabel, MessageBoxBase, PushButton, StrongBodyLabel


class WebView2Action(Enum):
    """WebView2 缺失对话框的三条出路"""

    DOWNLOAD = "download"  # 打开官方下载页
    BROWSER_EXTRACT = "browser_extract"  # 切换到浏览器提取（Edge）
    CANCEL = "cancel"  # 先不处理


class WebView2MissingDialog(MessageBoxBase):
    """
    WebView2 运行时缺失对话框

    提供三个出口（按使用顺序）：
    1. 「打开下载页面」- 前往 Microsoft 官方下载 WebView2 运行时
    2. 「改用浏览器提取」- 切换到 Edge 从本地浏览器提取 Cookie
    3. 「先不登录」- 取消本次登录操作
    """

    def __init__(self, parent: QWidget | None = None, platform: str = "youtube"):
        super().__init__(parent)
        self.platform = platform
        self.action: WebView2Action = WebView2Action.CANCEL
        self._setup_ui()

    def _setup_ui(self) -> None:
        """初始化 UI"""
        self.widget.setMinimumWidth(520)

        platform_name = "YouTube" if self.platform == "youtube" else "X (Twitter)"

        # 标题
        self.title_label = StrongBodyLabel(self.tr("⚠️ 缺少 WebView2 运行时"), self)
        self.title_label.setStyleSheet("font-size: 16px;")
        self.viewLayout.addWidget(self.title_label)

        # 说明
        self.desc_label = BodyLabel(
            self.tr(
                "登录功能依赖 Microsoft Edge WebView2 运行时，但当前系统未检测到可用的运行时。\n\n"
                "请选择以下方式继续：\n"
                "• 前往 Microsoft 官网下载并安装 WebView2 运行时（推荐，安装后可正常登录 {platform_name}）\n"
                "• 切换为「自动从本地浏览器提取」模式（无需安装运行时）\n"
                "• 手动导入 cookies.txt 文件"
            ).format(platform_name=platform_name),
            self,
        )
        self.desc_label.setWordWrap(True)
        self.viewLayout.addWidget(self.desc_label)

        self.viewLayout.setSpacing(16)
        self.viewLayout.setContentsMargins(24, 24, 24, 24)

        # 按钮区域 (MessageBoxBase 已提供 self.yesButton 和 self.cancelButton)
        self.yesButton.setText(self.tr("打开下载页面"))
        self.cancelButton.setText(self.tr("先不登录"))

        try:
            self.yesButton.clicked.disconnect()
        except RuntimeError:
            pass
        self.yesButton.clicked.connect(self._on_download_clicked)

        try:
            self.cancelButton.clicked.disconnect()
        except RuntimeError:
            pass
        self.cancelButton.clicked.connect(self._on_cancel_clicked)

        # 自定义「改用浏览器提取」按钮，插入到 yes/cancel 之间
        self.browserExtractButton = PushButton(self.tr("改用浏览器提取"), self)
        self.browserExtractButton.clicked.connect(self._on_browser_extract_clicked)
        self.buttonLayout.insertWidget(
            1, self.browserExtractButton, 1, Qt.AlignmentFlag.AlignVCenter
        )

    def _on_download_clicked(self) -> None:
        """前往下载 WebView2 运行时"""
        self.action = WebView2Action.DOWNLOAD
        self.accept()

    def _on_browser_extract_clicked(self) -> None:
        """切换到浏览器提取模式"""
        self.action = WebView2Action.BROWSER_EXTRACT
        self.accept()

    def _on_cancel_clicked(self) -> None:
        """取消本次登录"""
        self.action = WebView2Action.CANCEL
        self.reject()


def show_webview2_missing_dialog(
    parent: QWidget | None = None, platform: str = "youtube"
) -> WebView2Action:
    """
    显示 WebView2 缺失对话框（便捷函数）

    Args:
        parent: 父窗口（不可为 None，MaskDialogBase 需要真实父控件）
        platform: 目标平台标识（youtube / twitter）

    Returns:
        用户选择的动作（WebView2Action）
    """
    dialog = WebView2MissingDialog(parent, platform)
    dialog.exec()
    return dialog.action
