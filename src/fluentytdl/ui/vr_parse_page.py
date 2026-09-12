from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)


class VRParsePage(QWidget):
    """VR 专用下载页面

    允许用户粘贴 VR 视频链接，自动使用 android_vr 客户端解析。
    """

    parse_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("vrParsePage")

        # Style setup moved to _update_style

        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(30, 30, 30, 30)
        self.vBoxLayout.setSpacing(0)

        # Center container
        self.centerWidget = QWidget(self)
        self.centerLayout = QVBoxLayout(self.centerWidget)
        self.centerLayout.setContentsMargins(0, 0, 0, 0)
        self.centerLayout.setSpacing(20)
        self.centerLayout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.vBoxLayout.addStretch(1)
        self.vBoxLayout.addWidget(self.centerWidget, 0, Qt.AlignmentFlag.AlignHCenter)
        self.vBoxLayout.addStretch(1)

        # 1. 顶部标题
        self.titleLabel = SubtitleLabel(self.tr("🥽  VR 视频下载"), self)
        self.centerLayout.addWidget(self.titleLabel)

        # 2. VR 说明卡片
        self.infoCard = CardWidget(self)
        self.infoCard.setMaximumWidth(760)
        # Style setup moved to _update_style
        infoLayout = QVBoxLayout(self.infoCard)
        infoLayout.setContentsMargins(20, 16, 20, 16)
        infoLayout.setSpacing(8)

        infoTitle = StrongBodyLabel(self.tr("为什么需要 VR 模式？"), self.infoCard)
        infoLayout.addWidget(infoTitle)

        infoText = CaptionLabel(
            self.tr("普通模式可能无法获取 VR 视频的完整投影或全部格式。\n")
            + self.tr("VR 模式使用专用客户端获取 VR 格式，实际分辨率取决于源视频和访问条件。\n")
            + self.tr("解析后会自动识别投影类型（Equi / Mesh / EAC）和立体模式（3D / 2D）。\n")
            + self.tr("注意：VR 模式不支持 Cookies，部分年龄限制视频可能无法下载。"),
            self.infoCard,
        )
        infoText.setWordWrap(True)
        infoText.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        infoLayout.addWidget(infoText)

        self.centerLayout.addWidget(self.infoCard)

        # 3. 核心操作区
        self.inputCard = CardWidget(self)
        self.inputCard.setMaximumWidth(760)
        # Style setup moved to _update_style
        self.cardLayout = QVBoxLayout(self.inputCard)
        self.cardLayout.setContentsMargins(20, 20, 20, 20)
        self.cardLayout.setSpacing(15)

        self.instructionLabel = StrongBodyLabel(self.tr("粘贴 YouTube VR 视频链接"), self)
        self.cardLayout.addWidget(self.instructionLabel)

        # 输入框行
        self.inputLayout = QHBoxLayout()

        self.urlInput = LineEdit(self)
        self.urlInput.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.urlInput.setClearButtonEnabled(True)
        self.urlInput.setMinimumWidth(560)
        self.urlInput.returnPressed.connect(self.on_parse_clicked)

        self.inputLayout.addWidget(self.urlInput)

        self.pasteBtn = PushButton(self.tr("粘贴"), self)
        self.pasteBtn.setMinimumWidth(72)
        self.pasteBtn.clicked.connect(self.on_paste_clicked)
        self.inputLayout.addWidget(self.pasteBtn)
        self.cardLayout.addLayout(self.inputLayout)

        # 按钮行 (右对齐)
        self.btnLayout = QHBoxLayout()
        self.btnLayout.addStretch(1)

        self.parseBtn = PrimaryPushButton(FluentIcon.SEARCH, self.tr("开始 VR 解析"), self)
        self.parseBtn.setMinimumWidth(140)
        self.parseBtn.clicked.connect(self.on_parse_clicked)

        self.btnLayout.addWidget(self.parseBtn)
        self.cardLayout.addLayout(self.btnLayout)

        self.centerLayout.addWidget(self.inputCard)

        # 4. 底部提示
        self.tipsLabel = CaptionLabel(
            self.tr("适用场景：\n")
            + self.tr("- VR180 / VR360 视频，需要完整 SBS/OU 数据\n")
            + self.tr("- 需要超过 1440p 分辨率的 VR 视频（4K/5K/8K）\n")
            + self.tr("- 普通视频请使用左侧「新建任务」页面"),
            self,
        )
        self.tipsLabel.setWordWrap(True)
        self.tipsLabel.setMaximumWidth(760)
        self.tipsLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.centerLayout.addWidget(self.tipsLabel)

        # Connect to theme changes
        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def on_parse_clicked(self) -> None:
        url = self.urlInput.text().strip()
        if url:
            self.parse_requested.emit(url)

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        page_bg = "transparent" if isDarkTheme() else "#F5F5F5"
        self.setStyleSheet(f"#vrParsePage {{ background-color: {page_bg}; }}")

        card_bg = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "white"
        card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
        card_style = f"CardWidget {{ background-color: {card_bg}; border-radius: 12px; border: 1px solid {card_bd}; }}"
        self.infoCard.setStyleSheet(card_style)
        self.inputCard.setStyleSheet(card_style)

    def on_paste_clicked(self) -> None:
        text = (QApplication.clipboard().text() or "").strip()
        if text:
            self.urlInput.setText(text)
            self.urlInput.setFocus()
