from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)


class CoverDownloadPage(QWidget):
    """Cover download page

    Allows users to paste links and download thumbnails specifically.
    """

    parse_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("coverDownloadPage")

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

        # 1. Title
        self.titleLabel = SubtitleLabel("封面下载", self)
        self.centerLayout.addWidget(self.titleLabel)

        # 2. Input Card
        self.inputCard = CardWidget(self)
        self.inputCard.setMaximumWidth(760)
        # Style setup moved to _update_style
        self.cardLayout = QVBoxLayout(self.inputCard)
        self.cardLayout.setContentsMargins(20, 20, 20, 20)
        self.cardLayout.setSpacing(15)

        self.instructionLabel = BodyLabel("在此处粘贴 YouTube 视频链接以下载封面", self)
        self.cardLayout.addWidget(self.instructionLabel)

        # Input row
        self.inputLayout = QHBoxLayout()

        self.urlInput = LineEdit(self)
        self.urlInput.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.urlInput.setClearButtonEnabled(True)
        self.urlInput.setMinimumWidth(560)
        self.urlInput.returnPressed.connect(self.on_parse_clicked)

        self.inputLayout.addWidget(self.urlInput)

        self.pasteBtn = PushButton("粘贴", self)
        self.pasteBtn.setMinimumWidth(72)
        self.pasteBtn.clicked.connect(self.on_paste_clicked)
        self.inputLayout.addWidget(self.pasteBtn)
        self.cardLayout.addLayout(self.inputLayout)

        # Button row
        self.btnLayout = QHBoxLayout()
        self.btnLayout.addStretch(1)

        self.parseBtn = PrimaryPushButton(FluentIcon.SEARCH, "获取封面", self)
        self.parseBtn.setMinimumWidth(120)
        self.parseBtn.clicked.connect(self.on_parse_clicked)

        self.btnLayout.addWidget(self.parseBtn)
        self.cardLayout.addLayout(self.btnLayout)

        self.centerLayout.addWidget(self.inputCard)

        self.tipsLabel = CaptionLabel(
            "提示：将解析视频并显示封面预览，支持保存最高质量封面。",
            self,
        )
        self.tipsLabel.setWordWrap(True)
        self.tipsLabel.setMaximumWidth(760)
        self.centerLayout.addWidget(self.tipsLabel)

        # Connect to theme changes
        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        page_bg = "transparent" if isDarkTheme() else "#F5F5F5"
        self.setStyleSheet(f"#coverDownloadPage {{ background-color: {page_bg}; }}")

        card_bg = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "white"
        card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
        self.inputCard.setStyleSheet(
            f"CardWidget {{ background-color: {card_bg}; border-radius: 12px; border: 1px solid {card_bd}; }}"
        )

    def on_parse_clicked(self) -> None:
        url = self.urlInput.text().strip()
        if url:
            self.parse_requested.emit(url)

    def on_paste_clicked(self) -> None:
        text = (QApplication.clipboard().text() or "").strip()
        if text:
            self.urlInput.setText(text)
            self.urlInput.setFocus()
