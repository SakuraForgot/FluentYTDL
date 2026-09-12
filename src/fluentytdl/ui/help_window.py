from __future__ import annotations

import re
from html import escape

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ExpandSettingCard,
    FluentIcon,
    FluentWindow,
    NavigationItemPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SettingCardGroup,
    SubtitleLabel,
)


# CSS for Markdown styling - Card-Based UI (Fluent Settings Style)
# Optimized for readability with color hierarchy and DataGrid-style tables
def get_markdown_css(theme=None) -> str:
    from qfluentwidgets import Theme, isDarkTheme

    if theme is not None:
        is_dark = theme == Theme.DARK
    else:
        is_dark = isDarkTheme()

    # Define colors based on theme
    text_color = "#D4D4D4" if is_dark else "#5e5e5e"
    h1_color = "#FFFFFF" if is_dark else "#202020"
    h1_sub_color = "#A0A0A0" if is_dark else "#767676"
    h2_color = "#E0E0E0" if is_dark else "#202020"
    h3_color = "#E0E0E0" if is_dark else "#202020"
    h3_bg = "#2D2D2D" if is_dark else "#FAFAFA"
    h3_border = "#3E3E42" if is_dark else "#E8E8E8"
    content_bg = "#1E1E1E" if is_dark else "#FFFFFF"
    content_border = "#3E3E42" if is_dark else "#E8E8E8"
    blockquote_bg = "#253041" if is_dark else "#EBF5FF"
    blockquote_border = "#3A96DD" if is_dark else "#0078D4"
    blockquote_text = "#E0E0E0" if is_dark else "#202020"
    link_color = "#6CB8F6" if is_dark else "#0078D4"
    table_border = "#3E3E42" if is_dark else "#F0F0F0"
    table_header_border = "#505050" if is_dark else "#E0E0E0"
    table_header_text = "#A0A0A0" if is_dark else "#767676"
    code_bg = "#2D2D2D" if is_dark else "#F3F3F3"
    code_text = "#D4D4D4" if is_dark else "#333333"
    pre_bg = "#181818" if is_dark else "#2D2D2D"
    pre_text = "#D4D4D4" if is_dark else "#D4D4D4"
    hr_color = "#3E3E42" if is_dark else "#EEEEEE"
    footer_bg = "#2D2D2D" if is_dark else "#FAFAFA"
    footer_border = "#505050" if is_dark else "#CCCCCC"
    footer_text = "#808080" if is_dark else "#999999"

    return f"""
    /* ========== Base Container ========== */
    QTextBrowser {{
        font-family: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 14px;
        line-height: 1.6;
        padding: 30px 50px;
        border: none;
        background-color: transparent;
        color: {text_color};
    }}
    h1 {{ font-size: 28px; font-weight: 600; margin: 0 0 8px 0; color: {h1_color}; letter-spacing: -0.4px; }}
    h1 + p {{ font-size: 14px; color: {h1_sub_color}; margin: 0 0 28px 0; line-height: 1.5; }}
    h2 {{ font-size: 16px; font-weight: 600; margin: 28px 0 14px 0; padding: 0; color: {h2_color}; background: none; border: none; letter-spacing: 0.1px; }}
    h3 {{ font-size: 14px; font-weight: 600; margin: 0; padding: 14px 18px; color: {h3_color}; background-color: {h3_bg}; border: 1px solid {h3_border}; border-bottom: none; border-radius: 8px 8px 0 0; }}
    h3 + p, h3 + ul, h3 + ol, h3 + table {{ margin: 0; padding: 14px 18px 18px 18px; background-color: {content_bg}; border: 1px solid {content_border}; border-top: none; border-radius: 0 0 8px 8px; margin-bottom: 20px; }}
    p {{ margin: 0 0 14px 0; color: {text_color}; line-height: 1.7; font-size: 14px; }}
    ul, ol {{ margin: 8px 0; padding-left: 20px; }}
    li {{ margin-bottom: 8px; color: {text_color}; line-height: 1.65; font-size: 14px; }}
    blockquote {{ margin: 14px 0; padding: 14px 18px; background-color: {blockquote_bg}; border-left: 3px solid {blockquote_border}; border-radius: 6px; font-size: 13px; color: {blockquote_text}; font-style: normal; }}
    blockquote strong {{ color: {link_color}; }}
    table {{ width: 100%; margin: 0; border-collapse: collapse; border: none; font-size: 13px; background-color: transparent; }}
    th {{ background-color: transparent; color: {table_header_text}; font-weight: 600; font-size: 12px; padding: 10px 14px; text-align: left; border-bottom: 1px solid {table_header_border}; border-top: none; border-left: none; border-right: none; }}
    td {{ padding: 12px 14px; color: {text_color}; border-bottom: 1px solid {table_border}; border-top: none; border-left: none; border-right: none; vertical-align: top; line-height: 1.55; }}
    tr:last-child td {{ border-bottom: none; }}
    code {{ font-family: "Cascadia Code", "Consolas", monospace; background-color: {code_bg}; padding: 2px 6px; border-radius: 4px; font-size: 12px; color: {code_text}; border: none; }}
    pre {{ background-color: {pre_bg}; padding: 16px 20px; border-radius: 8px; font-family: "Cascadia Code", "Consolas", monospace; font-size: 13px; color: {pre_text}; margin: 14px 0; overflow-x: auto; }}
    hr {{ border: none; height: 1px; background-color: {hr_color}; margin: 28px 0; }}
    blockquote:last-of-type {{ background-color: {footer_bg}; border-left-color: {footer_border}; font-size: 12px; color: {footer_text}; margin-top: 36px; }}
    strong {{ font-weight: 600; color: {link_color}; }}
    """


def _wizard_css(theme=None) -> str:
    from qfluentwidgets import Theme, isDarkTheme

    dark = isDarkTheme() if theme is None else theme == Theme.DARK
    foreground = "#F3F3F3" if dark else "#202020"
    secondary = "#C8C8C8" if dark else "#606060"
    return f"""
        body {{ color: {foreground}; font-size: 14px; }}
        h1 {{ color: {foreground}; font-size: 28px; font-weight: 600; margin: 8px 0 12px 0; }}
        h2 {{ color: {foreground}; font-size: 24px; font-weight: 600; margin: 8px 0 20px 0; }}
        h3 {{ color: {foreground}; font-size: 24px; font-weight: 600; margin: 8px 0 20px 0; }}
        p {{ font-size: 14px; line-height: 145%; margin: 0 0 14px 0; color: {secondary}; }}
        li {{ font-size: 14px; line-height: 145%; margin-bottom: 12px; }}
        td {{ font-size: 14px; color: {secondary}; }}
        strong {{ color: {foreground}; font-weight: 600; }}
    """


_EXPAND_CSS_OVERRIDE = """
QTextBrowser {
    padding: 12px 16px;
}
h2 {
    margin: 16px 0 8px 0;
}
h3 {
    margin: 0;
    padding: 10px 14px;
}
h3 + p, h3 + ul, h3 + ol, h3 + table {
    padding: 10px 14px 12px 14px;
    margin-bottom: 12px;
}
p {
    margin: 0 0 8px 0;
}
ul, ol {
    margin: 4px 0;
}
li {
    margin-bottom: 4px;
}
blockquote {
    margin: 8px 0;
    padding: 10px 14px;
}
table {
    margin: 0;
}
th {
    padding: 8px 10px;
}
td {
    padding: 8px 10px;
}
"""


class _AutoHeightTextBrowser(QTextBrowser):
    """QTextBrowser that sizes to its document content height and auto-adapts to theme changes."""

    def __init__(self, parent=None, is_expand_card=False, is_wizard=False):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if is_wizard else Qt.ScrollBarAlwaysOff
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("background: transparent; border: none;")
        self.document().documentLayout().documentSizeChanged.connect(self._adjustHeight)

        self._is_expand_card = is_expand_card
        self._is_wizard = is_wizard
        self._raw_html = ""

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_theme)

    def _adjustHeight(self):
        if self._is_wizard:
            return
        doc_h = self.document().size().height()
        self.setFixedHeight(int(doc_h) + 4)

    def setHtml(self, html):
        self._raw_html = html
        super().setHtml(html)
        self._adjustHeight()

    def _update_theme(self):
        css = get_markdown_css()
        if self._is_expand_card:
            css += _EXPAND_CSS_OVERRIDE
        elif self._is_wizard:
            css = _wizard_css()
        self.document().setDefaultStyleSheet(css)
        if self._raw_html:
            super().setHtml(self._raw_html)
            self._adjustHeight()


class ExpandHelpCard(ExpandSettingCard):
    """ExpandSettingCard with rich HTML content rendered in the expand area."""

    def __init__(self, icon, title, content, html_body, parent=None):
        super().__init__(icon, title, content, parent)
        self._browser = _AutoHeightTextBrowser(self.view, is_expand_card=True)
        self._browser.setOpenExternalLinks(True)
        self._html_body = QCoreApplication.translate("HelpWindow", html_body)
        self._browser.document().setDefaultStyleSheet(get_markdown_css() + _EXPAND_CSS_OVERRIDE)
        if html_body in (
            _QUICK_DOWNLOAD_HTML,
            _LAZY_MODE_HTML,
            _COOKIE_COMPARE_HTML,
            _POTOKEN_HTML,
            _NETWORK_PROXY_HTML,
            _LOGIN_ERROR_HTML,
            _VR_VIDEO_HTML,
            _FORMAT_COMPAT_HTML,
            _CRASH_RECOVERY_HTML,
            _PERFORMANCE_HTML,
            _ERROR_TABLE_HTML,
            _LOG_REPORT_HTML,
        ):
            self._html_body = re.sub(
                r"<p>([^<]*(?:<(?!/?p>)[^>]*>[^<]*)*)</p>$",
                r"<p>💡 \1</p>",
                self._html_body,
            )
        self._browser.setHtml(self._html_body)
        self.viewLayout.addWidget(self._browser)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self.onThemeChanged)

    def onThemeChanged(self, theme):
        self._browser.document().setDefaultStyleSheet(
            get_markdown_css(theme) + _EXPAND_CSS_OVERRIDE
        )
        self._browser.setHtml(self._html_body)


def _guide_card(icon: str, content: str, background: str) -> str:
    heading = re.match(r"<strong>(.*?)</strong>(.*)", content, flags=re.DOTALL)
    if heading:
        title, description = heading.groups()
        title = title.rstrip("：: ")
        content = (
            f'<p style="margin:0 0 8px 0;"><strong>{title}</strong></p>'
            f'<p style="font-size:13px; margin:0; line-height:145%;">{description.strip()}</p>'
        )
    return (
        f'<table width="100%" bgcolor="{background}" cellspacing="0" cellpadding="16">'
        '<tr><td width="40" valign="top">'
        f'<span style="font-size:25px;">{icon}</span></td>'
        f'<td valign="top">{content}</td></tr></table>'
    )


def _icon_grid(html: str, icons: tuple[str, ...], background: str, columns: int = 2) -> str:
    """Separate icons, titles and descriptions into evenly spaced guide cards."""

    def render(match):
        items = re.findall(r"<li>(.*?)</li>", match.group(1), flags=re.DOTALL)
        if len(items) != len(icons):
            return match.group(0)
        # Paint the shared row cell, not just the content-height inner table.
        # Qt stretches sibling cells equally when either description wraps.
        cells = [
            f'<td width="{100 // columns}%" valign="top" bgcolor="{background}">'
            + _guide_card(icon, item, background)
            + "</td>"
            for icon, item in zip(icons, items, strict=True)
        ]
        rows = [
            "<tr>" + "".join(cells[i : i + columns]) + "</tr>"
            for i in range(0, len(cells), columns)
        ]
        return '<table width="100%" cellspacing="10" cellpadding="0">' + "".join(rows) + "</table>"

    return re.sub(r"<[uo]l>(.*?)</[uo]l>", render, html, count=1, flags=re.DOTALL)


def _wizard_step_html(source: str, version: str = "?") -> str:
    """Keep presentation outside translated copy so both languages share one layout."""
    from qfluentwidgets import isDarkTheme

    background = "#2D2D2D" if isDarkTheme() else "#FFFFFF"
    html = QCoreApplication.translate("HelpWindow", source)
    if source == _WIZARD_STEP1_HTML:
        from fluentytdl.utils.paths import resource_path

        logo_uri = escape(resource_path("assets", "logo.png").resolve().as_uri(), quote=True)
        html = html.replace("__version__", escape(version))
        title = re.search(r"<h2>(.*?)</h2>", html).group(1)
        paragraphs = re.findall(r"<p>(.*?)</p>", html)
        return (
            '<p align="center" style="margin:24px 0 12px 0;">'
            f'<img src="{logo_uri}" width="104" height="104"></p>'
            f'<h1 align="center">{title}</h1>'
            f'<p align="center" style="font-size:12px; margin:0 0 32px 0;">{paragraphs[0]}</p>'
            f'<p align="center" style="font-size:16px;">{paragraphs[1]}</p>'
            f'<p align="center">{paragraphs[2]}</p>'
        )
    if source == _WIZARD_STEP2_HTML:
        html = _icon_grid(html, ("①", "②", "③"), background, columns=1)
        html = html.replace("</table><p>", "</table><p>👉 ", 1)
    elif source == _WIZARD_STEP3_HTML:
        html = _icon_grid(html, ("🌐", "🖥️", "📄"), background, columns=1)
        html = html.replace("</table><p>", "</table><p>🛡️ ", 1)
    elif source == _WIZARD_STEP4_HTML:
        html = _icon_grid(html, ("🛡️", "🔊", "⚡", "🎵", "🥽", "🔄"), background)
    elif source == _WIZARD_STEP5_HTML:
        html = _icon_grid(html, ("🏠", "📺", "🥽", "📝", "⬇️", "⚙️"), background)
    elif source == _WIZARD_STEP6_HTML:
        title = re.search(r"<h2>(.*?)</h2>", html).group(1)
        subtitle, rest = html.split("</p>", 1)
        subtitle = subtitle.split("<p>", 1)[1]
        rest = rest.replace("<h3>", "<p><strong>", 1).replace("</h3>", "</strong></p>", 1)
        return (
            '<p align="center" style="font-size:48px; margin:16px 0 12px 0;">🚀</p>'
            f'<h1 align="center">{title}</h1>'
            f'<p align="center" style="margin-bottom:28px;">{subtitle}</p>'
            + _guide_card("💡", rest, background)
        )
    return html


_WIZARD_STEP1_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h2>欢迎使用 FluentYTDL Pro</h2><p>版本 __version__</p><p>下载 YouTube 视频、播放列表、频道内容，以及 X 帖子中的视频。</p><p>本向导介绍下载、账号登录和常用设置。</p>",
)

_WIZARD_STEP2_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>开始下载</h3><ol><li>复制视频链接，在“精确解析”页面粘贴。</li><li>点击解析按钮，等待可用格式加载。</li><li>选择画质、音轨和保存位置，然后点击“下载”。</li></ol><p>在“设置 → 功能 → 自动化”中开启“剪贴板自动识别”后，复制支持的链接即可打开解析窗口。</p><p>如提示缺少组件，请前往“设置 → 更新”检查安装状态。</p>",
)

_WIZARD_STEP3_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>账号与 Cookie</h3><p>部分视频需要登录。Cookie 让下载工具使用您已登录的会话，但不会授予账号原本没有的访问权限。</p><ul><li><strong>应用内登录：</strong>在独立的 WebView2 窗口登录 YouTube 或 X。</li><li><strong>浏览器提取：</strong>从支持的本地浏览器读取 Cookie，部分浏览器需要管理员权限。</li><li><strong>手动导入：</strong>导入 Netscape 格式的 cookies.txt 文件。</li></ul><p>开启 Cookie 过滤后，仅保留目标平台所需的 Cookie。不要向他人提供 Cookie 文件。</p>",
)

_WIZARD_STEP4_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>常用功能</h3><ul><li><strong>画质检查：</strong>发现实际分辨率低于目标时，按设置提示或暂停任务。</li><li><strong>音轨选择：</strong>根据语言偏好及平台的原音、配音标记选择音轨。</li><li><strong>批量下载：</strong>按需加载播放列表和频道中的视频详情。</li><li><strong>SponsorBlock：</strong>按社区标注处理选定类别的片段。</li><li><strong>VR 处理：</strong>根据预设和设置保留或转换投影格式。</li><li><strong>任务记录：</strong>重新启动后查看已保存的任务，并恢复可继续的下载。</li></ul>",
)

_WIZARD_STEP5_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>页面导航</h3><ul><li><strong>精确解析：</strong>加载视频或播放列表，选择格式后下载。</li><li><strong>频道下载：</strong>加载频道内容，筛选并选择要下载的视频。</li><li><strong>VR 下载：</strong>选择适合 VR 视频的格式和处理选项。</li><li><strong>字幕、封面：</strong>单独获取字幕文件或封面图片。</li><li><strong>任务列表：</strong>查看进度、暂停或恢复任务，以及管理历史记录。</li><li><strong>设置：</strong>管理账号、输出格式、网络连接和组件更新。</li></ul>",
)

_WIZARD_STEP6_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h2>开始使用</h2><p>粘贴一个支持的视频链接，即可创建下载任务。</p><h3>遇到问题时</h3><p>先查看错误说明，再检查网络连接、账号是否有访问权限，以及“设置 → 更新”中的组件版本。</p><p>更新组件可能解决平台变化引起的问题，但不能解决所有访问限制。仍无法下载时，可导出诊断包帮助排查。</p>",
)


class WelcomeGuideWidget(QWidget):
    """The Quick Start Wizard Page."""

    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self.onThemeChanged)
        self.onThemeChanged(qconfig.theme)

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(32, 24, 32, 24)
        self.v_layout.setSpacing(12)

        # Step progress label
        self.step_label = BodyLabel(self.tr("第 {0} 步 / 共 {1} 步").format(1, 6), self)
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.step_label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.v_layout.addWidget(self.step_label)
        self.progress_bar = ProgressBar(self, useAni=False)
        self.progress_bar.setCustomBackgroundColor(QColor(0, 0, 0, 20), QColor(255, 255, 255, 25))
        self.progress_bar.setRange(0, 6)
        self.progress_bar.setValue(1)
        self.progress_bar.setFixedHeight(3)
        self.v_layout.addWidget(self.progress_bar)
        self.v_layout.addSpacing(8)

        # Stack for steps
        self.stack = QStackedWidget(self)

        # Resolve version for step 1
        try:
            from fluentytdl import __version__ as _ver
        except Exception:
            _ver = "?"
        self._ver = _ver
        self.step_browsers = []
        self._step_sources = (
            _WIZARD_STEP1_HTML,
            _WIZARD_STEP2_HTML,
            _WIZARD_STEP3_HTML,
            _WIZARD_STEP4_HTML,
            _WIZARD_STEP5_HTML,
            _WIZARD_STEP6_HTML,
        )
        for i, source in enumerate(self._step_sources):
            html = _wizard_step_html(source, self._ver)

            browser = _AutoHeightTextBrowser(self, is_wizard=True)
            browser.document().setDefaultStyleSheet(_wizard_css())
            browser.document().setDocumentMargin(4)
            browser.setHtml(html)
            self.step_browsers.append(browser)
            self.stack.addWidget(browser)
            setattr(self, f"step{i + 1}_browser", browser)

        self.v_layout.addWidget(self.stack, 1)

        self.v_layout.addSpacing(8)

        # Navigation Buttons
        btn_layout = QHBoxLayout()
        self.skip_btn = PushButton(self.tr("跳过引导"), self)
        self.skip_btn.clicked.connect(self.finished)

        self.prev_btn = PushButton(self.tr("上一步"), self)
        self.prev_btn.setEnabled(False)
        self.prev_btn.clicked.connect(self._prev_step)

        self.next_btn = PrimaryPushButton(self.tr("下一步"), self)
        self.next_btn.clicked.connect(self._next_step)

        for button in (self.skip_btn, self.prev_btn, self.next_btn):
            button.setMinimumHeight(36)
            button.setMinimumWidth(100)
        btn_layout.setSpacing(10)
        btn_layout.addWidget(self.skip_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.prev_btn)
        btn_layout.addWidget(self.next_btn)

        self.v_layout.addLayout(btn_layout)

    def _prev_step(self):
        idx = self.stack.currentIndex()
        if idx > 0:
            self.stack.setCurrentIndex(idx - 1)
        self._update_buttons()

    def _next_step(self):
        idx = self.stack.currentIndex()
        if idx < self.stack.count() - 1:
            self.stack.setCurrentIndex(idx + 1)
        else:
            self.finished.emit()
        self._update_buttons()

    def _update_buttons(self):
        idx = self.stack.currentIndex()
        total = self.stack.count()

        self.progress_bar.setValue(idx + 1)
        self.prev_btn.setEnabled(idx > 0)
        self.step_label.setText(self.tr("第 {0} 步 / 共 {1} 步").format(idx + 1, total))

        if idx == total - 1:
            self.next_btn.setText(self.tr("开始使用"))
        else:
            self.next_btn.setText(self.tr("下一步"))

    def onThemeChanged(self, theme):
        from qfluentwidgets import Theme

        bg_color = "#202020" if theme == Theme.DARK else "#F9F9F9"
        self.setStyleSheet(f"WelcomeGuideWidget {{ background-color: {bg_color}; border: none; }}")

        for browser, source in zip(
            getattr(self, "step_browsers", ()), getattr(self, "_step_sources", ()), strict=True
        ):
            browser.document().setDefaultStyleSheet(_wizard_css(theme))
            browser.setHtml(_wizard_step_html(source, self._ver))


# ============================================================================
# ManualReaderWidget — Help content HTML bodies
# ============================================================================

_QUICK_DOWNLOAD_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>加载链接</h3><p>在“精确解析”页面粘贴链接，按回车或点击解析按钮。格式加载后，选择保存位置并下载。</p><h3>支持范围</h3><ul><li>YouTube：视频、Shorts、播放列表、频道、字幕、封面和 VR 内容。</li><li>X：包含可下载视频的单个帖子。</li></ul><p>频道内容请使用“频道下载”页面。其他 yt-dlp 支持的网站不属于本应用的支持范围。</p>",
)

_FORMAT_QUALITY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>画质与格式</h3><p>自动模式选择最佳可用格式。分辨率上限不是保证值：实际画质受源视频、访问权限和可用格式影响。</p><p>在专业模式中，可分别选择视频编码、分辨率和音轨。Opus、AAC 是音频编码名称，码率较高不代表源音频一定更好，也不代表无损。</p><h3>缺少高分辨率格式</h3><ul><li>确认源视频是否提供该分辨率；新发布的视频可能仍在处理。</li><li>检查账号是否有访问权限，并按错误提示刷新 Cookie。</li><li>在“设置 → 更新”检查 yt-dlp 和 Deno。</li></ul>",
)

_LAZY_MODE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>自动识别剪贴板链接</h3><p>在“设置 → 功能 → 自动化”开启“剪贴板自动识别”，并选择识别后的操作方式。</p><p>复制支持的链接后，应用会打开对应的解析窗口。确认格式和保存位置，再点击下载。</p><p>不希望复制链接时弹出窗口，可关闭此选项。</p>",
)

_BATCH_MANAGE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>批量任务</h3><p>在任务列表中选择多个任务，再使用工具栏暂停、恢复或移除它们。移除时请确认是否同时删除文件。</p><h3>播放列表格式</h3><p>使用“格式设置”统一指定画质和输出格式。实际可用格式仍取决于每个视频。</p><p>处理大列表时可分批选择，并适当降低详情请求和下载的并发数。</p>",
)

_COOKIE_COMPARE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>获取登录 Cookie</h3><ul><li><strong>应用内登录：</strong>在“设置 → 账号验证”选择 YouTube 或 X，并在 WebView2 窗口完成登录。</li><li><strong>浏览器提取：</strong>从已登录的 Firefox、Edge 等受支持浏览器提取。部分 Chromium 浏览器需要管理员权限。</li><li><strong>手动导入：</strong>选择 Netscape 格式的 cookies.txt 文件。</li></ul><p>不支持自动提取 Chrome 和百分浏览器的 Cookie。可以改用应用内登录、受支持的浏览器或手动导入。</p><p>Cookie 会过期或失效。出现登录提示时，重新获取 Cookie，并确认账号能在浏览器中访问目标内容。</p>",
)

_POTOKEN_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>PO Token 与 Cookie</h3><p>PO Token 用于部分 YouTube 请求的来源验证。Cookie 用于提供账号会话。两者作用不同，不能代替内容访问权限，也不能保证请求一定成功。</p><h3>启用与检测</h3><p>在“设置 → 系统 → 高级”启用 POT 服务。服务默认启用并在后台准备；首次解析会等待就绪，加载失败时停止请求并提示。</p><p>“一键检测”会检查令牌生成和 yt-dlp 连接。失败时请查看检测详情，并检查 Deno、POT Provider 和网络连接。</p>",
)

_NETWORK_PROXY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>连接方式</h3><p>在“设置 → 网络”选择不使用代理、系统代理或手动 HTTP/SOCKS5 代理。</p><p>手动代理的地址和端口必须与代理软件一致，例如 <code>127.0.0.1:7890</code>。</p><p>使用 TUN 模式时，流量由系统路由处理，通常不需要再设置应用内代理。避免重复配置。</p><h3>连接失败</h3><p>确认代理软件正在运行、端口正确，并检查浏览器能否打开同一链接。DNS 或证书错误需要分别检查网络解析和证书设置。</p>",
)

_LOGIN_ERROR_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>需要登录或验证</h3><p>先确认当前账号能在浏览器中访问该视频。私享、年龄限制或会员内容可能要求相应权限。</p><ol><li>在“设置 → 更新”检查 yt-dlp。</li><li>通过应用内登录或浏览器提取刷新 Cookie。</li><li>检查网络连接，并按具体错误提示处理。</li></ol><p>更改账号资料后，如会话失效，请重新登录并获取 Cookie。无法仅凭验证失败判断账号或 IP 被封禁。</p>",
)

_SPONSORBLOCK_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>SponsorBlock 片段处理</h3><p>根据社区标注识别赞助广告、自我推广和互动提醒等片段。标注可能不完整或不准确。</p><p>在“设置 → 功能 → 后处理”启用 SponsorBlock，并选择处理类别。</p><p>片段移除会改变输出视频的内容和时长。下载后请检查结果；没有匹配标注时，不会凭空识别广告。</p>",
)

_VR_VIDEO_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>VR 视频</h3><p>在 VR 下载页面选择链接和预设。可用分辨率、投影和立体格式取决于源视频。</p><ul><li><strong>Equirectangular：</strong>等距柱状投影，常用于全景播放。</li><li><strong>Mesh：</strong>带投影映射信息的格式，可见于 VR180 内容。</li><li><strong>EAC：</strong>YouTube 使用的等角立方体投影。</li></ul><p>普通播放器可能显示变形画面，应使用支持对应投影的 VR 播放器。只有预设或设置要求时才转换投影。</p><p>高分辨率转换可能消耗大量内存和时间。根据硬件能力设置分辨率上限。</p>",
)

_FORMAT_COMPAT_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>容器与编码</h3><p>MP4、MKV 和 WebM 是容器；H.264、VP9、AV1 是视频编码；AAC、Opus 是音频编码。播放器需同时支持所用容器和编码。</p><ul><li><strong>MP4：</strong>常用于 H.264/AAC。其他编码、多音轨和字幕的播放效果取决于播放器支持。</li><li><strong>MKV：</strong>支持多种编码及多音轨、多字幕，适合复杂组合。</li><li><strong>WebM：</strong>常见组合为 VP9/AV1 视频与 Opus 音频，不适合直接嵌入 SRT/ASS 字幕。</li></ul><p>应用会根据音轨、字幕和输出设置选择或调整容器。请留意下载前的提示。</p><h3>重封装与转码</h3><p>重封装不重新编码媒体流；转码会重新编码，通常耗时更长，也可能影响画质或音质。更改容器不等于必须转码。</p>",
)

_CRASH_RECOVERY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>任务记录与恢复</h3><p>应用会保存任务记录。重新启动后，可在任务列表查看已保存的状态。</p><p>中断的下载通常恢复为暂停状态，可手动继续；字幕或封面提取可能需要重新开始。</p><p>是否能够断点续传取决于临时文件、链接有效期和服务器支持。意外断电可能丢失尚未保存的状态，建议正常退出应用。</p>",
)

_COMPONENTS_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>应用组件</h3><ul><li><strong>yt-dlp：</strong>读取视频信息并下载媒体。</li><li><strong>FFmpeg：</strong>合并音视频、转换格式和处理字幕。</li><li><strong>Deno：</strong>提供 yt-dlp 使用的 JavaScript 执行环境。</li><li><strong>POT Provider：</strong>为需要来源验证的 YouTube 请求生成令牌。</li><li><strong>AtomicParsley：</strong>处理 MP4/M4A 的封面与标签。</li></ul><p>在“设置 → 更新”查看版本、检查更新或导入本地组件。是否已包含组件取决于所用发行包；缺少组件时按提示安装。</p>",
)

_UPDATE_CHANNELS_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>yt-dlp 更新频道</h3><ul><li><strong>Stable：</strong>正式发布的版本。</li><li><strong>Nightly：</strong>包含较新修复的开发构建。</li><li><strong>Master：</strong>主线开发构建，适合测试。</li></ul><p>在“设置 → 更新”选择频道并检查更新。遇到平台变化导致的问题，可根据发布说明考虑其他频道；开发构建也可能引入新问题。</p><p>更新源用于检查版本信息；下载来源以更新界面中的说明为准。</p>",
)

_PERFORMANCE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>下载并发</h3><p>同时下载任务数决定并行处理多少个视频；分片并发数决定单个视频同时下载多少个分片。</p><p>更多并发不一定更快。网络不稳定、界面响应变慢或出现 HTTP 429 时，可降低并发数后重试。</p><p>HTTP 429 表示请求受到限流，不能据此认定 IP 永久封禁。限制持续时间取决于服务端，应用无法保证固定的恢复时间。</p>",
)

_ERROR_TABLE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>常见错误</h3><table><tr><th>错误</th><th>检查方法</th></tr><tr><td>FFmpeg 未找到</td><td>在“设置 → 更新”检查组件是否已安装。</td></tr><tr><td>HTTP 403</td><td>请求被拒绝。检查账号权限、Cookie、链接有效期和网络连接，并检查 yt-dlp 更新。</td></tr><tr><td>需要登录 / private</td><td>确认账号可在浏览器访问内容，再获取 Cookie。</td></tr><tr><td>连接超时</td><td>检查网络、代理地址和端口。</td></tr><tr><td>Video unavailable</td><td>确认视频未被删除，并检查地区和访问限制。</td></tr><tr><td>后处理失败</td><td>查看错误详情，检查磁盘空间、文件权限及格式兼容性。</td></tr></table><p>同一错误码可能有多种原因。不要仅凭 403 认定 IP 被封禁，也不要假定刷新 Cookie 能解决所有失败。</p>",
)

_LOG_REPORT_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    "<h3>查看日志</h3><p>在“设置 → 系统 → 日志管理”打开日志窗口或日志目录。日志窗口支持级别筛选和关键词搜索。</p><h3>反馈问题</h3><p>错误详情中的“反馈此错误”可准备问题描述。您也可以在任务时间线中选择任务，导出诊断包。</p><p>请说明复现步骤、预期结果和实际结果。发送前检查附件，不要附上 Cookie、密码或其他账号凭据。</p>",
)


class ManualReaderWidget(ScrollArea):
    """User Manual Page built with ExpandSettingCard for rich content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = QWidget(self)
        self.view.setObjectName("view")
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setContentsMargins(36, 20, 36, 36)
        self.vBoxLayout.setSpacing(24)

        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setObjectName("manualScrollArea")

        from PySide6.QtWidgets import QFrame

        self.setFrameShape(QFrame.Shape.NoFrame)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self.onThemeChanged)
        self.onThemeChanged(qconfig.theme)

        self._initUI()

    def onThemeChanged(self, theme):
        from qfluentwidgets import Theme

        bg_color = "#202020" if theme == Theme.DARK else "#F9F9F9"
        self.setStyleSheet(
            f"QWidget#view, #manualScrollArea {{ background-color: {bg_color}; border: none; }}"
        )

    def _initUI(self):
        # ========== Hero Section ==========
        self.titleLabel = SubtitleLabel(self.tr("FluentYTDL Pro 使用手册"), self.view)
        self.subtitleLabel = BodyLabel(self.tr("下载操作、常用设置与问题排查"), self.view)
        self.subtitleLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))

        self.vBoxLayout.addWidget(self.titleLabel)
        self.vBoxLayout.addWidget(self.subtitleLabel)
        self.vBoxLayout.addSpacing(10)

        # ========== Section 1: Core Operations ==========
        self.usageGroup = SettingCardGroup(self.tr("📘 核心操作指南"), self.view)

        self.quickDownloadCard = ExpandHelpCard(
            FluentIcon.PASTE,
            self.tr("快速下载"),
            self.tr("视频、播放列表、频道链接解析"),
            _QUICK_DOWNLOAD_HTML,
            self.usageGroup,
        )
        self.formatCard = ExpandHelpCard(
            FluentIcon.VIDEO,
            self.tr("画质与格式选择"),
            self.tr("自动选择与手动选择音视频流"),
            _FORMAT_QUALITY_HTML,
            self.usageGroup,
        )
        self.lazyCard = ExpandHelpCard(
            FluentIcon.CHAT,
            self.tr("剪贴板自动识别"),
            self.tr("复制链接即弹下载窗口"),
            _LAZY_MODE_HTML,
            self.usageGroup,
        )
        self.batchCard = ExpandHelpCard(
            FluentIcon.ACCEPT,
            self.tr("批量任务管理"),
            self.tr("多选、暂停、恢复、删除任务"),
            _BATCH_MANAGE_HTML,
            self.usageGroup,
        )

        self.usageGroup.addSettingCard(self.quickDownloadCard)
        self.usageGroup.addSettingCard(self.formatCard)
        self.usageGroup.addSettingCard(self.lazyCard)
        self.usageGroup.addSettingCard(self.batchCard)
        self.vBoxLayout.addWidget(self.usageGroup)

        # ========== Section 2: Identity & Network ==========
        self.identityGroup = SettingCardGroup(self.tr("🔐 身份验证与网络"), self.view)

        self.cookieCard = ExpandHelpCard(
            FluentIcon.PEOPLE,
            self.tr("Cookie 获取方式对比"),
            self.tr("WebView2 / Firefox / 手动导入 — 如何选择？"),
            _COOKIE_COMPARE_HTML,
            self.identityGroup,
        )
        self.potokenCard = ExpandHelpCard(
            FluentIcon.DEVELOPER_TOOLS,
            self.tr("PO Token 与反机器人检测"),
            self.tr("PO Token 的用途与使用限制"),
            _POTOKEN_HTML,
            self.identityGroup,
        )
        self.networkCard = ExpandHelpCard(
            FluentIcon.WIFI,
            self.tr("网络与代理配置"),
            self.tr("系统代理、TUN 模式、手动代理"),
            _NETWORK_PROXY_HTML,
            self.identityGroup,
        )
        self.loginErrorCard = ExpandHelpCard(
            FluentIcon.CANCEL,
            self.tr("需要登录 / 机器人检测错误"),
            "Sign in to confirm you’re not a bot",
            _LOGIN_ERROR_HTML,
            self.identityGroup,
        )

        self.identityGroup.addSettingCard(self.cookieCard)
        self.identityGroup.addSettingCard(self.potokenCard)
        self.identityGroup.addSettingCard(self.networkCard)
        self.identityGroup.addSettingCard(self.loginErrorCard)
        self.vBoxLayout.addWidget(self.identityGroup)

        # ========== Section 3: Advanced Features ==========
        self.advancedGroup = SettingCardGroup(self.tr("🚀 进阶功能"), self.view)

        self.sponsorblockCard = ExpandHelpCard(
            FluentIcon.MUSIC,
            self.tr("SponsorBlock 广告跳过"),
            self.tr("自动移除视频中的赞助片段"),
            _SPONSORBLOCK_HTML,
            self.advancedGroup,
        )
        self.vrCard = ExpandHelpCard(
            FluentIcon.VIDEO,
            self.tr("VR 视频下载"),
            self.tr("最高 8K VR，android_vr 客户端"),
            _VR_VIDEO_HTML,
            self.advancedGroup,
        )
        self.formatCompatCard = ExpandHelpCard(
            FluentIcon.PEOPLE,
            self.tr("视频格式与编码兼容性"),
            self.tr("为什么有些视频在手机/电视上无法播放"),
            _FORMAT_COMPAT_HTML,
            self.advancedGroup,
        )
        self.crashRecoveryCard = ExpandHelpCard(
            FluentIcon.SAVE,
            self.tr("崩溃恢复与任务持久化"),
            self.tr("查看保存的任务状态与恢复限制"),
            _CRASH_RECOVERY_HTML,
            self.advancedGroup,
        )

        self.advancedGroup.addSettingCard(self.sponsorblockCard)
        self.advancedGroup.addSettingCard(self.vrCard)
        self.advancedGroup.addSettingCard(self.formatCompatCard)
        self.advancedGroup.addSettingCard(self.crashRecoveryCard)
        self.vBoxLayout.addWidget(self.advancedGroup)

        # ========== Section 4: Components & Updates ==========
        self.componentsGroup = SettingCardGroup(self.tr("🔧 组件与更新"), self.view)

        self.componentsCard = ExpandHelpCard(
            FluentIcon.DOWNLOAD,
            self.tr("核心组件一览"),
            "yt-dlp、FFmpeg、Deno、POT Provider、AtomicParsley",
            _COMPONENTS_HTML,
            self.componentsGroup,
        )
        self.updateCard = ExpandHelpCard(
            FluentIcon.UPDATE,
            self.tr("更新频道与故障排查"),
            "stable / nightly / master",
            _UPDATE_CHANNELS_HTML,
            self.componentsGroup,
        )
        self.performanceCard = ExpandHelpCard(
            FluentIcon.SPEED_HIGH,
            self.tr("性能调优"),
            self.tr("并发任务、分片与请求限流"),
            _PERFORMANCE_HTML,
            self.componentsGroup,
        )

        self.componentsGroup.addSettingCard(self.componentsCard)
        self.componentsGroup.addSettingCard(self.updateCard)
        self.componentsGroup.addSettingCard(self.performanceCard)
        self.vBoxLayout.addWidget(self.componentsGroup)

        # ========== Section 5: Troubleshooting ==========
        self.errorGroup = SettingCardGroup(self.tr("❌ 故障排查"), self.view)

        self.errorTableCard = ExpandHelpCard(
            FluentIcon.INFO,
            self.tr("错误代码速查表"),
            self.tr("HTTP 403、超时、FFmpeg 缺失等"),
            _ERROR_TABLE_HTML,
            self.errorGroup,
        )
        self.logCard = ExpandHelpCard(
            FluentIcon.GITHUB,
            self.tr("日志收集与 Bug 上报"),
            self.tr("如何获取日志并有效反馈"),
            _LOG_REPORT_HTML,
            self.errorGroup,
        )

        self.errorGroup.addSettingCard(self.errorTableCard)
        self.errorGroup.addSettingCard(self.logCard)
        self.vBoxLayout.addWidget(self.errorGroup)

        # ========== Footer ==========
        self.vBoxLayout.addStretch(1)


class HelpWindow(FluentWindow):
    """Independent Help Center Window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("帮助中心"))
        self.resize(900, 650)

        desktop = self.screen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.guide_interface = WelcomeGuideWidget(self)
        self.guide_interface.setObjectName("welcomeGuideInterface")
        self.guide_interface.finished.connect(self.close)

        self.manual_interface = ManualReaderWidget(self)
        self.manual_interface.setObjectName("manual_interface")

        self.addSubInterface(
            self.guide_interface,
            FluentIcon.COMPLETED,
            self.tr("快速入门"),
            position=NavigationItemPosition.TOP,
        )

        self.addSubInterface(
            self.manual_interface,
            FluentIcon.BOOK_SHELF,
            self.tr("用户手册"),
            position=NavigationItemPosition.TOP,
        )

        self.stackedWidget.setCurrentWidget(self.guide_interface)
