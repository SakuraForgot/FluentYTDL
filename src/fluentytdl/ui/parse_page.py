from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
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


class ParsePage(QWidget):
    """独立的解析页面

    允许用户手动粘贴链接并触发解析，或使用批量快速下载。
    """

    parse_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("parsePage")

        # 设置页面背景色（类似侧边栏的浅灰底色）
        # Style setup moved to _update_style

        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(30, 30, 30, 30)
        self.vBoxLayout.setSpacing(0)

        # 不再使用 Pivot/StackedWidget，直接作为主视图
        # 1. Standard Page
        # ====================================================
        self.standardPage = QWidget(self)
        self.standardLayout = QVBoxLayout(self.standardPage)
        self.standardLayout.setContentsMargins(0, 0, 0, 0)
        self.standardLayout.setSpacing(0)

        # Center container
        self.centerWidget = QWidget(self.standardPage)
        self.centerLayout = QVBoxLayout(self.centerWidget)
        self.centerLayout.setContentsMargins(0, 0, 0, 0)
        self.centerLayout.setSpacing(20)
        self.centerLayout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.standardLayout.addStretch(1)
        self.standardLayout.addWidget(self.centerWidget, 0, Qt.AlignmentFlag.AlignHCenter)
        self.standardLayout.addStretch(1)

        self.titleLabel = SubtitleLabel(self.tr("精确解析"), self.standardPage)
        self.centerLayout.addWidget(self.titleLabel)

        # 2. 核心操作区 (卡片风格)
        self.inputCard = CardWidget(self)
        self.inputCard.setMaximumWidth(760)
        # 增强卡片样式：更明显的圆角和阴影
        # Style setup moved to _update_style
        self.cardLayout = QVBoxLayout(self.inputCard)
        self.cardLayout.setContentsMargins(20, 20, 20, 20)
        self.cardLayout.setSpacing(15)

        self.instructionLabel = StrongBodyLabel(
            self.tr("在此处粘贴视频链接 (支持 YouTube / X 平台)"), self
        )
        self.cardLayout.addWidget(self.instructionLabel)

        self.hintLabel = CaptionLabel(
            self.tr("提示：如需自动识别剪贴板，请到“设置 → 体验”开启。"), self
        )
        self.hintLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.cardLayout.addWidget(self.hintLabel)

        # 输入框行
        self.inputLayout = QHBoxLayout()

        self.urlInput = LineEdit(self)
        self.urlInput.setPlaceholderText("https://www.youtube.com/... 或 https://x.com/...")
        self.urlInput.setClearButtonEnabled(True)
        self.urlInput.setMinimumWidth(560)
        self.urlInput.returnPressed.connect(self.on_parse_clicked)
        self.urlInput.textChanged.connect(self.on_url_changed)

        self.inputLayout.addWidget(self.urlInput)

        self.pasteBtn = PushButton(self.tr("粘贴"), self)
        self.pasteBtn.setMinimumWidth(72)
        self.pasteBtn.clicked.connect(self.on_paste_clicked)
        self.inputLayout.addWidget(self.pasteBtn)
        self.cardLayout.addLayout(self.inputLayout)

        # 按钮行 (右对齐)
        self.btnLayout = QHBoxLayout()
        self.btnLayout.addStretch(1)

        self.parseBtn = PrimaryPushButton(FluentIcon.SEARCH, self.tr("开始解析"), self)
        self.parseBtn.setMinimumWidth(120)
        self.parseBtn.clicked.connect(self.on_parse_clicked)

        self.btnLayout.addWidget(self.parseBtn)
        self.cardLayout.addLayout(self.btnLayout)

        self.centerLayout.addWidget(self.inputCard)

        # Extra compact tips to reduce emptiness and guide users
        self.tipsLabel = CaptionLabel(
            self.tr("支持格式示例：\n")
            + "- YouTube: https://www.youtube.com/watch?v=... 或 https://youtu.be/...\n"
            + "- X (Twitter): https://x.com/username/status/123456789...\n"
            + self.tr("- 注意：X 平台仅支持单个推文视频解析，暂不支持主页/列表等。\n")
            + self.tr("- YouTube 频道请使用「频道下载」页面"),
            self,
        )
        self.tipsLabel.setWordWrap(True)
        self.tipsLabel.setMaximumWidth(760)
        self.tipsLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.centerLayout.addWidget(self.tipsLabel)

        self.vBoxLayout.addWidget(self.standardPage, 1)

        # Connect to theme changes
        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        page_bg = "transparent" if isDarkTheme() else "#F5F5F5"
        self.setStyleSheet(f"#parsePage {{ background-color: {page_bg}; }}")

        card_bg = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "white"
        card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
        self.inputCard.setStyleSheet(
            f"CardWidget {{ background-color: {card_bg}; border-radius: 12px; border: 1px solid {card_bd}; }}"
        )

    def on_url_changed(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.hintLabel.setText(self.tr("提示：如需自动识别剪贴板，请到“设置 → 体验”开启。"))
            return

        from ..utils.url_router import url_router

        # 同步快速判断
        fast_result = url_router.fast_check(text)
        if fast_result == "youtube_playlist":
            self.hintLabel.setText(self.tr("✅ 已识别为 YouTube 播放列表链接"))
        elif fast_result == "youtube_video":
            self.hintLabel.setText(self.tr("✅ 已识别为 YouTube 视频链接"))
        elif fast_result == "twitter_video":
            self.hintLabel.setText(self.tr("✅ 已识别为 X (Twitter) 视频链接"))
        elif fast_result == "twitter_unsupported":
            self.hintLabel.setText(
                self.tr("❌ 不支持的 X 链接：请提供包含 status/ 的具体推文视频链接")
            )
        elif fast_result == "tco_shortlink":
            self.hintLabel.setText(self.tr("⏳ 正在解析短链接..."))
            self.parseBtn.setEnabled(False)

            # 记录当前处理的文本，用于回调时校验防覆盖
            self._current_check_text = text

            def _on_resolved(result):
                # 校验是否过期
                if getattr(self, "_current_check_text", None) != text:
                    return
                self.parseBtn.setEnabled(True)
                if result.accepted:
                    self.hintLabel.setText(self.tr("✅ 短链接已解析并被支持"))
                else:
                    self.hintLabel.setText(self.tr("❓ 未知或暂不支持的短链接目标"))

            # 使用 QTimer 触发防抖和异步
            QTimer.singleShot(300, lambda: url_router.process_async(text, _on_resolved))
        else:
            self.hintLabel.setText(self.tr("❓ 未知或暂不支持的链接格式"))

    def on_parse_clicked(self) -> None:
        url = self.urlInput.text().strip()
        if not url:
            return

        from ..utils.url_router import url_router

        # t.co 展开是 url_router 里唯一走网络的分支（requests.head，timeout=3s）。
        # 同步调用会把 UI 线程冻住，连"正在解析"都来不及显示，所以先用零成本的
        # fast_check 判定，只有确实是短链才丢进后台线程；其余路径 process() 不联网，
        # 保持原来的同步语义。
        if url_router.fast_check(url) == "tco_shortlink":
            self._begin_shortlink_parse(url)
            return

        self._apply_parse_result(url_router.process(url))

    def _begin_shortlink_parse(self, url: str) -> None:
        """后台展开 t.co 短链，展开完成后再触发解析。"""
        from ..utils.url_router import url_router

        self.parseBtn.setEnabled(False)
        self.parseBtn.setText(self.tr("展开中..."))
        self.hintLabel.setText(self.tr("⏳ 正在解析短链接..."))

        # 与 on_url_changed 同一套过期校验：回调回来时若用户已换了链接，直接丢弃。
        self._pending_parse_url = url

        def _on_resolved(result) -> None:
            if getattr(self, "_pending_parse_url", None) != url:
                return
            self._pending_parse_url = None
            self.parseBtn.setEnabled(True)
            self.parseBtn.setText(self.tr("开始解析"))
            self._apply_parse_result(result)

        url_router.process_async(url, _on_resolved, parent=self)

    def _apply_parse_result(self, result) -> None:
        """统一处理链接预处理结果：被拒则提示，通过则发起解析。"""
        if not result.accepted:
            from qfluentwidgets import InfoBar, InfoBarPosition

            if result.link_type == "t.co" and result.rejection_reason:
                title = self.tr("短链接展开失败")
                content = result.rejection_reason
            else:
                title = self.tr("不支持的链接")
                content = self.tr(
                    "未知的链接格式或目前不支持该平台/类型（X 平台仅支持包含 status/ 的单推文视频）。"
                )

            InfoBar.error(
                title=title,
                content=content,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return

        self.parse_requested.emit(result.normalized_url)

    def on_paste_clicked(self) -> None:
        text = (QApplication.clipboard().text() or "").strip()
        if text:
            self.urlInput.setText(text)
            self.urlInput.setFocus()
