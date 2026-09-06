from __future__ import annotations

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


_WIZARD_CSS_OVERRIDE = """
QTextBrowser {
    padding: 20px 40px;
}
table {
    margin: 0 auto;
}
td, th {
    text-align: left;
}
ul, ol {
    text-align: left;
}
blockquote {
    text-align: left;
}
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
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("background: transparent; border: none;")
        self.document().documentLayout().documentSizeChanged.connect(self._adjustHeight)

        self._is_expand_card = is_expand_card
        self._is_wizard = is_wizard
        self._raw_html = ""

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_theme)

    def _adjustHeight(self):
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
            css += _WIZARD_CSS_OVERRIDE
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
        self._browser.setHtml(self._html_body)
        self.viewLayout.addWidget(self._browser)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self.onThemeChanged)

    def onThemeChanged(self, theme):
        self._browser.document().setDefaultStyleSheet(
            get_markdown_css(theme) + _EXPAND_CSS_OVERRIDE
        )
        self._browser.setHtml(self._html_body)


# Wizard step HTML content
_WIZARD_LOGO_URI = r"e:\YouTube\FluentYTDL\assets\logo.png"


def _wizard_step_html(source: str) -> str:
    """把 ``_WIZARD_STEPn_HTML`` 源串翻译成当前语言。

    这些常量在下面用 ``QT_TRANSLATE_NOOP("HelpWindow", ...)`` 标记，lupdate 从**声明处**
    抽取；本函数只做运行期查表，参数永远是变量，所以不影响提取。
    """
    return QCoreApplication.translate("HelpWindow", source)


_WIZARD_STEP1_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:center; padding:40px 0 20px 0;">
  <img src="file:///e:/YouTube/FluentYTDL/assets/logo.png" width="112" height="112" style="margin-bottom:16px;">
  <h1 style="margin:0 0 8px 0; font-size:28px; font-weight:700;">FluentYTDL Pro</h1>
  <p style="color:#767676; font-size:14px; margin:0 0 32px 0;">Version __version__</p>
  <p style="font-size:15px; margin:0 0 8px 0; line-height:1.6;">欢迎使用 FluentYTDL Pro。</p>
  <p style="font-size:13px; color:#767676; margin:0;">本向导将协助您快速熟悉系统的基础工作流、模块分布与核心特性。</p>
</div>
""",
)

_WIZARD_STEP2_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:left; padding:0 20px;">
  <p style="font-size:16px; font-weight:600; margin-bottom:8px;">👉 快捷工作流（推荐）</p>
  <p>您可以在 <strong>设置 → 自动化</strong> 中开启「剪贴板自动识别」。</p>
  <p>开启后，系统在后台监测到支持的媒体链接（包含单视频、播放列表或频道）时，将自动唤醒解析窗口，点击下载即可建立任务。</p>

  <p style="font-size:16px; font-weight:600; margin-top:24px; margin-bottom:8px;">✍️ 标准解析工作流</p>
  <p>1. 复制媒体链接并在主页搜索栏进行粘贴。<br>
  2. 按下回车键或点击右侧解析按钮启动引擎。<br>
  3. 系统将展现可用画质与音轨，按需选择并确认下载。</p>

  <p style="margin-top:20px; font-size:13px; color:#3A8FB7;">✅ 提示：核心解析与合并管线（yt-dlp、FFmpeg）已在系统内预置，无需额外配置环境。</p>
</div>
""",
)

_WIZARD_STEP3_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:left; padding:0 20px;">
  <p style="color:#767676; font-size:13px; margin-bottom:16px;">当需要下载具备年龄限制、会员专属的视频，或遭遇服务端机器人检测拦截时，系统需要身份凭证支持。</p>
  
  <p style="font-size:16px; font-weight:600; margin-bottom:12px;">身份验证接入方案</p>
  <table style="width:100%; margin:0;">
    <tr><th style="width:25%;self.tr(">接入方式</th><th style=")width:15%;">推荐等级</th><th>说明</th></tr>
    <tr><td><strong>WebView2 登录</strong></td><td>⭐⭐⭐</td><td>通过隔离的安全沙盒环境授权账号，稳定性最高，为官方推荐首选。</td></tr>
    <tr><td><strong>本机浏览器提取</strong></td><td>⭐⭐</td><td>通过接口自动提取 Firefox 等浏览器的凭证，部分浏览器因安全策略受限。</td></tr>
    <tr><td><strong>外部文件导入</strong></td><td>⭐</td><td>通过浏览器扩展导出标准的 Netscape 格式文件，并在系统内导入。</td></tr>
  </table>

  <p style="margin-top:20px; font-size:13px; color:#3A8FB7;">🛡️ 隐私合规：系统仅保留维持下载会话所需的验证令牌，自动过滤其他追踪性数据。</p>
</div>
""",
)

_WIZARD_STEP4_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:left; padding:0 10px;">
  <p style="font-size:16px; font-weight:600; margin-bottom:16px; margin-left:10px;">系统核心特性</p>
  <table style="width:100%; margin:0;">
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🛡️ 质量风控引擎 (Quality Guard)</div>
        <div style="color:#767676; font-size:12px;">前置与后置双重质量校验，拦截分辨率异常降级，保障下载质量。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🔊 智能音轨打分机制</div>
        <div style="color:#767676; font-size:12px;">依据语言学分析评分，优先为您匹配高质量原生音轨或真人配音。</div>
      </td>
    </tr>
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">⚡ 频道与列表极速解析</div>
        <div style="color:#767676; font-size:12px;">构建三层优先调度系统，支持大规模播放列表的视口感知与懒加载。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🎵 智能片段跳过 (SponsorBlock)</div>
        <div style="color:#767676; font-size:12px;">接入众包数据库，在处理管线中自动识别并跳过赞助商与推广片段。</div>
      </td>
    </tr>
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🥽 VR 空间后处理管道</div>
        <div style="color:#767676; font-size:12px;">自动对 VR180/360 视频进行投影转换并注入空间元数据。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🔄 任务状态持久化</div>
        <div style="color:#767676; font-size:12px;">底层基于 SQLite 管理任务生命周期，支持崩溃或意外重启后无缝恢复。</div>
      </td>
    </tr>
  </table>
</div>
""",
)

_WIZARD_STEP5_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:left; padding:0 10px;">
  <p style="font-size:16px; font-weight:600; margin-bottom:16px; margin-left:10px;">侧边栏功能导航</p>
  <table style="width:100%; margin:0;">
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🏠 主页 / 基础解析</div>
        <div style="color:#767676; font-size:12px;">核心工作区。粘贴链接即可解析单个视频或整个播放列表，并自由组装画质与音轨。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">📺 频道下载</div>
        <div style="color:#767676; font-size:12px;">专为频道级别搬运设计。支持动态懒加载频道内的所有视频，并一键批量建立任务。</div>
      </td>
    </tr>
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">🥽 VR 下载</div>
        <div style="color:#767676; font-size:12px;">独立的 VR 处理管线。自动提取 VR 全景格式并重投影，适配主流头显。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">📝 物料提取 (字幕/封面)</div>
        <div style="color:#767676; font-size:12px;">提供独立的提取页面，无需下载巨大视频本体即可高速获取多语种字幕与高清海报。</div>
      </td>
    </tr>
    <tr>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">⬇️ 任务列表</div>
        <div style="color:#767676; font-size:12px;">统一的下载调度中心。支持任务的全局暂停、开始，以及任务队列的批量管理。</div>
      </td>
      <td style="padding:10px; vertical-align:top; width:50%; border:none;">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">⚙️ 设置中心</div>
        <div style="color:#767676; font-size:12px;">提供身份验证 (Cookie) 授权、底层组件维护、并发数调整及其他核心偏好设定。</div>
      </td>
    </tr>
  </table>
</div>
""",
)

_WIZARD_STEP6_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<div style="text-align:center; padding:30px 0 10px 0;">
  <div style="font-size:64px; margin-bottom:16px;">🚀</div>
  <h2 style="margin:0 0 12px 0; font-size:28px; font-weight:700;">一切准备就绪</h2>
  <p style="font-size:14px; margin:0 0 36px 0;">核心服务进程已初始化，您可以开始建立下载任务了。</p>
  
  <div style="text-align:left; border:1px solid #767676; padding:16px 20px; border-radius:8px; display:inline-block; max-width:85%;">
    <p style="font-size:16px; font-weight:600; margin:0 0 10px 0;">⚠️ 核心组件更新预警</p>
    <p style="font-size:13px; margin:0 0 10px 0; line-height:1.6;">流媒体平台的防护策略更新极为频繁。如果您在未来使用中遭遇<strong>大面积解析错误、HTTP 403 异常或频繁的机器人验证拦截</strong>，这通常是因为上游策略发生了改变。</p>
    <p style="font-size:13px; margin:0; line-height:1.6;">👉 <strong>排障第一步：</strong>请立即前往 <strong>「设置 → 更新」</strong> 页面，将内部核心组件升级至最新版本，99% 的解析问题都能由此解决。</p>
  </div>
</div>
""",
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
        self.v_layout.setContentsMargins(40, 20, 40, 20)

        # Step progress label
        self.step_label = BodyLabel(self.tr("第 {0} 步 / 共 {1} 步").format(1, 6), self)
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.step_label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.v_layout.addWidget(self.step_label)
        self.v_layout.addSpacing(8)

        # Stack for steps
        self.stack = QStackedWidget(self)

        # Resolve version for step 1
        try:
            from fluentytdl import __version__ as _ver
        except Exception:
            _ver = "?"
        self._ver = _ver
        _step1_html = QCoreApplication.translate("HelpWindow", _WIZARD_STEP1_HTML).replace(
            "__version__", _ver
        )

        # Create step browsers
        self.step_browsers = []
        for i, html in enumerate(
            [
                _step1_html,
                _WIZARD_STEP2_HTML,
                _WIZARD_STEP3_HTML,
                _WIZARD_STEP4_HTML,
                _WIZARD_STEP5_HTML,
                _WIZARD_STEP6_HTML,
            ]
        ):
            if i > 0:
                html = QCoreApplication.translate("HelpWindow", html)

            if i == 0:
                from fluentytdl.utils.paths import resource_path

                logo_uri = f"file:///{resource_path('assets', 'logo.png').as_posix()}"
                html = html.replace("file:///e:/YouTube/FluentYTDL/assets/logo.png", logo_uri)

            browser = _AutoHeightTextBrowser(self, is_wizard=True)
            browser.document().setDefaultStyleSheet(get_markdown_css() + _WIZARD_CSS_OVERRIDE)
            browser.setHtml(f'<div style="text-align:center">{html}</div>')
            self.step_browsers.append(browser)
            self.stack.addWidget(browser)
            setattr(self, f"step{i + 1}_browser", browser)

        self.v_layout.addWidget(self.stack, 1)

        self.v_layout.addSpacing(8)

        # Navigation Buttons
        btn_layout = QHBoxLayout()
        self.skip_btn = PrimaryPushButton(self.tr("跳过引导"), self)
        self.skip_btn.clicked.connect(self.finished)

        self.prev_btn = PrimaryPushButton(self.tr("上一步"), self)
        self.prev_btn.setEnabled(False)
        self.prev_btn.clicked.connect(self._prev_step)

        self.next_btn = PrimaryPushButton(self.tr("下一步"), self)
        self.next_btn.clicked.connect(self._next_step)

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

        # 这里不包 ``def tr(text)`` 辅助函数：见 ISSUE #88，本项目统一用完整形式的
        # ``QCoreApplication.translate("HelpWindow", ...)``，源串已在
        # ``_WIZARD_STEPn_HTML`` 处用 QT_TRANSLATE_NOOP 标记。
        _tr_html = _wizard_step_html

        if hasattr(self, "step1_browser"):
            from fluentytdl.utils.paths import resource_path

            logo_uri = f"file:///{resource_path('assets', 'logo.png').as_posix()}"
            step1_html = (
                _tr_html(_WIZARD_STEP1_HTML)
                .replace("__version__", getattr(self, "_ver", "?"))
                .replace("file:///e:/YouTube/FluentYTDL/assets/logo.png", logo_uri)
            )
            self.step1_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step1_browser.setHtml(f'<div style="text-align:center">{step1_html}</div>')
        if hasattr(self, "step2_browser"):
            self.step2_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step2_browser.setHtml(
                f'<div style="text-align:center">{_tr_html(_WIZARD_STEP2_HTML)}</div>'
            )
        if hasattr(self, "step3_browser"):
            self.step3_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step3_browser.setHtml(
                f'<div style="text-align:center">{_tr_html(_WIZARD_STEP3_HTML)}</div>'
            )
        if hasattr(self, "step4_browser"):
            self.step4_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step4_browser.setHtml(
                f'<div style="text-align:center">{_tr_html(_WIZARD_STEP4_HTML)}</div>'
            )
        if hasattr(self, "step5_browser"):
            self.step5_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step5_browser.setHtml(
                f'<div style="text-align:center">{_tr_html(_WIZARD_STEP5_HTML)}</div>'
            )
        if hasattr(self, "step6_browser"):
            self.step6_browser.document().setDefaultStyleSheet(
                get_markdown_css(theme) + _WIZARD_CSS_OVERRIDE
            )
            self.step6_browser.setHtml(
                f'<div style="text-align:center">{_tr_html(_WIZARD_STEP6_HTML)}</div>'
            )


# ============================================================================
# ManualReaderWidget — Help content HTML bodies
# ============================================================================

_QUICK_DOWNLOAD_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>基本操作</h3>
<p>复制任意 YouTube 链接，在主页按 <code>Ctrl+V</code> 或点击粘贴按钮，按回车即可开始解析。</p>

<h3>支持的链接类型</h3>
<table>
<tr><th>类型</th><th>示例</th></tr>
<tr><td>单个视频</td><td><code>youtube.com/watch?v=...</code></td></tr>
<tr><td>播放列表</td><td><code>youtube.com/playlist?list=...</code></td></tr>
<tr><td>频道主页</td><td><code>youtube.com/@channel</code></td></tr>
<tr><td>短视频</td><td><code>youtube.com/shorts/...</code></td></tr>
</table>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>提示：</strong>支持 B 站、Patreon 等数百个网站（底层引擎 yt-dlp 支持），但 FluentYTDL 的 UI 优化专为 YouTube 定制。</p>
""",
)

_FORMAT_QUALITY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>自动模式（默认）</h3>
<p>软件默认智能选择最佳画质，优先下载 1080p/4K 高清格式。大多数场景无需手动干预。</p>

<h3>A+B 专业模式</h3>
<p>解析完成后点击「选择格式」按钮，可自由组合视频流和音频流：</p>
<ul>
<li><strong>视频流</strong>：选择画面分辨率（如 4K、1080p）和编码（H.264、VP9、AV1）</li>
<li><strong>音频流</strong>：选择音质（如 Hi-Res Opus 256kbps、AAC 128kbps）</li>
</ul>
<p>例如：4K VP9 视频 + Hi-Res Opus 音频 = 最佳画质+音质的完美文件。</p>

<h3>为什么最高只有 1080p？</h3>
<ul>
<li>视频刚发布，4K/8K 编码需数小时，稍后重试</li>
<li>风控降级：需导入 Cookie + 换干净 IP</li>
<li>缺少 Deno JS 运行时：前往「设置 → 核心组件」检查安装</li>
<li>yt-dlp 过期：更新组件</li>
</ul>
""",
)

_LAZY_MODE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>开启方式</h3>
<p>进入 <strong>设置 → 自动化</strong>，开启「剪贴板自动识别」开关。</p>

<h3>使用效果</h3>
<p>开启后，软件在后台监听剪贴板。只要复制了 YouTube 链接，就会自动弹出下载确认窗口，无需手动粘贴。点击「下载」即开始。</p>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>推荐：</strong>这是最高效的使用方式，尤其适合日常频繁下载的用户。</p>
""",
)

_BATCH_MANAGE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>批量操作</h3>
<p>在下载列表中，使用工具栏上的「批量选择」按钮，可一次性暂停、恢复或删除多个任务。</p>

<h3>播放列表全局格式预设</h3>
<p>解析播放列表后，使用工具栏的「格式设置」按钮统一配置所有视频的分辨率和格式，无需逐个设置。</p>

<h3>并发控制建议</h3>
<table>
<tr><th>场景</th><th>建议</th></tr>
<tr><td>日常下载</td><td>保持默认并发任务数</td></tr>
<tr><td>大播放列表（500+）</td><td>分批解析，避免触发风控</td></tr>
<tr><td>网络不稳定</td><td>降低并发分片数</td></tr>
</table>
""",
)

_COOKIE_COMPARE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>三种获取方式对比</h3>
<table>
<tr><th>方式</th><th>推荐度</th><th>说明</th></tr>
<tr><td><strong>WebView2 登录获取</strong></td><td>强烈推荐</td><td>弹出安全沙盒窗口登录，一键获取，无兼容性问题</td></tr>
<tr><td><strong>Firefox 自动提取</strong></td><td>推荐</td><td>无需管理员权限，稳定可靠</td></tr>
<tr><td><strong>手动导入</strong></td><td>兜底方案</td><td>用扩展导出 cookies.txt 后导入，需定期更新</td></tr>
</table>

<h3>WebView2 登录获取流程</h3>
<ol>
<li>在设置页点击「WebView2 登录获取」，弹出独立的 WebView2 沙盒窗口</li>
<li>在沙盒中正常登录 Google/YouTube 账号</li>
<li>软件自动检测登录状态，采集 Cookie 并清洗</li>
<li>Cookie 自动同步到 yt-dlp，完成</li>
</ol>

<h3>浏览器兼容性</h3>
<table>
<tr><th>浏览器</th><th>自动提取</th><th>说明</th></tr>
<tr><td>Firefox</td><td>稳定可用</td><td>无需管理员权限，推荐首选</td></tr>
<tr><td>Edge</td><td>需管理员权限</td><td>App-Bound Encryption 限制</td></tr>
<tr><td>Chrome</td><td>基本不可用</td><td>v127+ 加密机制导致第三方无法解密</td></tr>
</table>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>重要：</strong>Chrome 自动提取极不稳定。如果你使用 Chrome 且频繁失败，请切换到 WebView2 或 Firefox。</p>
""",
)

_POTOKEN_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>什么是 PO Token？</h3>
<p><strong>PO Token（Proof of Origin Token）</strong>是 YouTube 用于验证请求来源合法性的数字令牌，向 YouTube 证明self.tr("这个请求来自真实客户端")。</p>

<h3>没有 PO Token 时可能遇到</h3>
<ul>
<li>触发 "Sign in to confirm you're not a bot" 人机验证</li>
<li>高画质格式被隐藏（仅返回 720p 或更低）</li>
<li>直接返回 HTTP 403 拒绝访问</li>
</ul>

<h3>PO Token 与 Cookie 的关系</h3>
<table>
<tr><th></th><th>PO Token</th><th>Cookie</th></tr>
<tr><td>证明什么</td><td>请求来自合法客户端</td><td>用户身份和登录状态</td></tr>
<tr><td>解决什么</td><td>机器人检测、基础格式访问</td><td>年龄限制、会员内容、高码率流</td></tr>
<tr><td>缺失后果</td><td>触发人机验证、画质受限</td><td>无法下载受限内容</td></tr>
</table>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>最佳实践：</strong>两者同时启用是最稳定的配置。PO Token 确保请求不被拦截，Cookie 确保有权限获取完整内容。</p>

<h3>POT Provider 预热</h3>
<p>应用启动时，POT Provider 需要 5-15 秒初始化 BotGuard 环境。预热期间日志显示「正在初始化」，完成后显示「已激活」。如果一直无法激活，请检查 Deno 是否已安装。</p>
""",
)

_NETWORK_PROXY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>默认行为</h3>
<p>FluentYTDL 默认「跟随系统代理」。只要系统或代理软件开启了系统代理，软件即可自动使用。</p>

<h3>代理软件配置</h3>
<table>
<tr><th>软件</th><th>操作</th></tr>
<tr><td>V2RayN</td><td>底部切换为「系统代理」，或开启 TUN</td></tr>
<tr><td>Clash Verge / CFW</td><td>启用「System Proxy」开关，不行就开 TUN</td></tr>
<tr><td>其他</td><td>确保开启self.tr("系统代理")/self.tr("全局模式")，非仅self.tr("规则/PAC 模式")</td></tr>
</table>

<h3>手动配置代理</h3>
<p>如果自动代理不工作，在 <strong>设置 → 网络连接</strong> 中选择「手动配置」，填入代理端口（如 <code>http://127.0.0.1:7890</code>）。</p>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>注意：</strong>不推荐长期使用手动代理。代理软件换端口后需手动同步修改，容易遗漏。</p>

<h3>DNS 错误 / 连接重置</h3>
<ul>
<li><strong>端口未对齐</strong>：检查设置中的端口号是否与代理软件一致</li>
<li><strong>节点被封锁</strong>：更换其他国家/地区的节点</li>
<li><strong>TUN 模式</strong>：开启后软件直接接管系统网络流量，无需配置端口</li>
</ul>
""",
)

_LOGIN_ERROR_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>遇到 "Sign in to confirm you're not a bot"</h3>
<p>按优先级依次尝试：</p>
<ol>
<li><strong>点击应用弹出的自动修复引导</strong> — 自动诊断根因并一键处理</li>
<li><strong>更新 yt-dlp</strong> — 前往「设置 → 核心组件」一键更新（最有效）</li>
<li><strong>刷新 Cookie</strong> — 推荐 WebView2 重新登录，或切换到 Firefox 提取</li>
<li><strong>更换代理节点</strong> — 当前 IP 可能已被标记</li>
</ol>

<h3>需要登录/权限不足</h3>
<p>以下场景需要导入 Cookie：</p>
<ul>
<li>年龄限制（18禁）内容</li>
<li>频道会员专属内容</li>
<li>受强力风控保护的极高码率流</li>
<li>私享视频</li>
</ul>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>提示：</strong>绝大多数公开视频不需要登录。日常使用直接复制链接粘贴解析即可。</p>

<h3>修改账号资料后出现验证错误</h3>
<p>修改用户名/头像触发了 Google 安全风控。解决：浏览器退出 YouTube → 重新登录 → 播放一个视频确认正常 → 重新获取 Cookie。</p>
""",
)

_SPONSORBLOCK_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>工作原理</h3>
<p>SponsorBlock 社区维护了一个众包标注数据库，记录了数百万视频中的赞助广告时间段。yt-dlp 下载时自动查询并处理。</p>

<h3>支持的类别</h3>
<table>
<tr><th>类别</th><th>说明</th></tr>
<tr><td>sponsor</td><td>赞助广告</td></tr>
<tr><td>selfpromo</td><td>频道自我推广</td></tr>
<tr><td>interaction</td><td>订阅提醒、点赞提醒等</td></tr>
</table>

<h3>处理方式</h3>
<ul>
<li><strong>Remove（移除）</strong>：使用 FFmpeg 自动裁剪掉广告片段</li>
<li><strong>Mark（标记）</strong>：将广告段标记为章节，播放时可跳过</li>
</ul>

<p>在 <strong>设置 → 下载选项 → SponsorBlock</strong> 中启用并选择类别和处理方式。</p>
""",
)

_VR_VIDEO_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>自动检测机制</h3>
<p>FluentYTDL 解析视频时自动检测 VR 内容（关键词、格式元数据、分辨率异常），检测到后自动使用 <code>android_vr</code> 客户端获取 VR 专属高分辨率格式（最高 8K）。</p>

<h3>三种投影类型</h3>
<table>
<tr><th>投影类型</th><th>说明</th><th>画面特征</th></tr>
<tr><td>Equirectangular</td><td>最常见的 360 格式</td><td>宽高比 2:1，上下拉伸</td></tr>
<tr><td>Mesh</td><td>VR180 常用</td><td>圆形鱼眼画面</td></tr>
<tr><td>EAC</td><td>YouTube 自研高效率格式</td><td>六面展开图</td></tr>
</table>

<h3>播放说明</h3>
<p>VR 视频在普通播放器中呈畸变是正常的，需用 VR 头显或全景播放器（Skybox、PotPlayer 全景模式）。</p>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>注意：</strong>EAC 格式的 8K 转码极度消耗资源（建议 32GB+ 内存），没有高性能硬件时建议限制在 4K/6K。</p>
""",
)

_FORMAT_COMPAT_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>三个核心概念</h3>
<p>视频文件 = <strong>容器</strong>（MP4/MKV/WebM）+ <strong>视频编码</strong>（H.264/VP9/AV1）+ <strong>音频编码</strong>（AAC/Opus）</p>

<h3>视频编码对比</h3>
<table>
<tr><th>编码</th><th>特点</th><th>兼容性</th></tr>
<tr><td>H.264 (AVC)</td><td>最老牌，兼容性最强</td><td>全平台通吃</td></tr>
<tr><td>VP9</td><td>YouTube 高画质流主力</td><td>PC/手机可以，部分电视不行</td></tr>
<tr><td>AV1</td><td>压缩效率最高</td><td>新设备支持，老设备大多不行</td></tr>
</table>

<h3>容器格式对比</h3>
<table>
<tr><th>容器</th><th>支持编码</th><th>字幕</th><th>多音轨</th></tr>
<tr><td>MP4</td><td>H.264+AAC 完美 / VP9 受限 / Opus 不支持</td><td>单轨</td><td>有限</td></tr>
<tr><td>MKV</td><td>几乎所有编码</td><td>完整多轨</td><td>完整支持</td></tr>
<tr><td>WebM</td><td>VP9/AV1+Opus</td><td>不支持 SRT/ASS</td><td>有限</td></tr>
</table>

<h3>FluentYTDL 智能容器决策</h3>
<ul>
<li>多音轨/多语言字幕嵌入 → 自动升级为 MKV</li>
<li>MP4 + m4a 音频 → MP4（无损 remux）</li>
<li>其他混合编码 → MKV（万能容器）</li>
</ul>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>什么是 remux？</strong>重封装是把音视频数据从一个容器搬到另一个容器，不重新编码，速度极快且画质零损失。</p>
""",
)

_CRASH_RECOVERY_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>任务持久化</h3>
<p>FluentYTDL 使用 SQLite 数据库（WAL 模式）存储所有下载任务。任务的每个状态变化（加入队列、开始、进度、完成、错误）都会实时写入数据库。</p>

<h3>崩溃恢复</h3>
<p>如果在下载过程中关闭程序（甚至意外断电/崩溃），重启后任务列表依然完整保留。</p>

<h3>降级策略</h3>
<ul>
<li>重启前处于「运行中」或「解析中」的任务，会自动降级为「暂停」状态，可手动恢复</li>
<li>纯提取任务（封面/字幕）重启后标记为错误，避免产生「幽灵任务」</li>
</ul>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>提示：</strong>这意味着您可以放心地在下载过程中关闭软件，下次打开时任务不会丢失。</p>
""",
)

_COMPONENTS_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>核心组件一览</h3>
<table>
<tr><th>组件</th><th>用途</th></tr>
<tr><td><strong>yt-dlp</strong></td><td>核心下载引擎，解析链接、获取流媒体数据</td></tr>
<tr><td><strong>FFmpeg</strong></td><td>合并音视频轨、转码封装、嵌入封面与字幕</td></tr>
<tr><td><strong>Deno</strong></td><td>JS 运行时，解析 YouTube 加密签名，缺失时丢失大量格式</td></tr>
<tr><td><strong>POT Provider</strong></td><td>动态生成 PO Token，绕过机器人检测</td></tr>
<tr><td><strong>AtomicParsley</strong></td><td>向 MP4/M4A 文件嵌入封面和元数据标签</td></tr>
</table>

<h3>管理方式</h3>
<p>在 <strong>设置 → 核心组件</strong> 页面可查看安装状态、当前版本，并一键检查更新。所有组件首次启动时自动下载安装，开箱即用。</p>
""",
)

_UPDATE_CHANNELS_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>yt-dlp 更新渠道</h3>
<table>
<tr><th>渠道</th><th>更新频率</th><th>稳定性</th><th>适用场景</th></tr>
<tr><td><strong>stable</strong></td><td>数周一次</td><td>最稳定</td><td>日常使用（默认推荐）</td></tr>
<tr><td><strong>nightly</strong></td><td>每日构建</td><td>较稳定</td><td>YouTube 改版后 stable 未修复时</td></tr>
<tr><td><strong>master</strong></td><td>每次代码合并</td><td>可能不稳定</td><td>开发测试</td></tr>
</table>

<h3>紧急自救指南</h3>
<p>当 YouTube 突然大规模改版导致所有下载失败时：</p>
<ol>
<li>首先尝试更新 <strong>stable</strong> 渠道 — 大部分情况下社区会快速修复</li>
<li>如果 stable 还没跟上，临时切换到 <strong>nightly</strong> — 包含最新的修复补丁</li>
<li>修复被合并到 stable 后再切回来</li>
</ol>

<h3>镜像源配置</h3>
<p>中国大陆用户在「设置 → 核心组件」中可切换为 ghproxy 镜像加速 GitHub 下载。组件更新也会使用「设置 → 网络连接」中配置的代理。</p>
""",
)

_PERFORMANCE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>并发分片数</h3>
<table>
<tr><th>设置值</th><th>适用场景</th><th>风险</th></tr>
<tr><td>1-2</td><td>网络不稳定、代理带宽小</td><td>无</td></tr>
<tr><td><strong>4（默认）</strong></td><td>大多数场景的最佳平衡</td><td>无</td></tr>
<tr><td>8-16</td><td>高带宽直连、极速下载</td><td>可能触发 429 限流</td></tr>
<tr><td>16+</td><td>不推荐</td><td>高概率限流、IP 封锁</td></tr>
</table>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>注意：</strong>分片数并非越大越快。YouTube 会对单 IP 的并发连接数监控，超过阈值会触发限流甚至封锁。默认值 4 是最佳平衡点。</p>

<h3>下载限速</h3>
<p>在「设置 → 下载选项」中可设置速率上限，适用场景：</p>
<ul>
<li>避免下载占满全部带宽</li>
<li>降低被风控检测的概率</li>
<li>后台长时间挂机下载</li>
</ul>
""",
)

_ERROR_TABLE_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>错误代码速查表</h3>
<table>
<tr><th>关键词</th><th>错误类型</th><th>核心原因</th><th>解决方案</th></tr>
<tr><td><code>ffmpeg not found</code></td><td>组件缺失</td><td>缺少 FFmpeg 无法合并音视频</td><td>设置 → 核心组件 → 检查更新</td></tr>
<tr><td><code>HTTP Error 403</code></td><td>访问被拒</td><td>IP 被 YouTube 风控</td><td>更新 Cookie + 更换代理节点</td></tr>
<tr><td><code>Sign in</code> / <code>private</code></td><td>权限不足</td><td>需要登录或会员权限</td><td>导入有效 Cookie</td></tr>
<tr><td><code>timed out</code> / <code>10060</code></td><td>网络超时</td><td>无法连接 YouTube 服务器</td><td>检查代理软件系统代理模式</td></tr>
<tr><td><code>Video unavailable</code></td><td>视频无效</td><td>视频被删或区域限制</td><td>确认链接在浏览器中可访问</td></tr>
<tr><td><code>postprocessing</code></td><td>后处理失败</td><td>格式合成出错或磁盘满</td><td>切换下载格式 (MKV/MP4)</td></tr>
</table>

<h3>403 Forbidden 深度解析</h3>
<p>这是最常见的问题。YouTube 会屏蔽「非浏览器」流量。解决步骤：</p>
<ol>
<li><strong>更新 Cookie</strong>（推荐）：使用 WebView2 登录或 Firefox 提取</li>
<li><strong>更换节点</strong>：「万人骑」公共节点容易被封锁，切换到冷门节点</li>
<li><strong>更新 yt-dlp</strong>：社区会持续适配 YouTube 的反爬变化</li>
</ol>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>提示：</strong>403 的 IP 封锁通常需要等 12-24 小时解封。更换节点是最快的解决方案。</p>
""",
)

_LOG_REPORT_HTML = QT_TRANSLATE_NOOP(
    "HelpWindow",
    """
<h3>获取运行日志</h3>
<ol>
<li>进入 <strong>设置页 → 系统 → 日志管理</strong></li>
<li>点击右侧文件夹图标打开日志目录</li>
<li>找到当天 <code>.log</code> 文件（如 <code>app_2026-04-23.log</code>）</li>
</ol>

<p>也可点击「查看日志」在应用内实时预览，支持级别过滤和关键词搜索。</p>

<h3>一键上报 Bug</h3>
<p>遇到未知的解析异常或下载中断时，点击报错卡片处弹出的「反馈此错误」图标按钮，会自动预填错误信息提报给开发者。</p>

<p style="color:#808080; font-size:13px; margin-top:12px;">💡 <strong>提示：</strong>附上日志文件能让开发者更快定位问题。在 GitHub Issue 中粘贴日志内容即可。</p>
""",
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
        self.titleLabel = SubtitleLabel(self.tr("FluentYTDL Pro 全能手册"), self.view)
        self.subtitleLabel = BodyLabel(
            self.tr("集操作指导、设置详解与错误查询于一体的完整指南"), self.view
        )
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
            self.tr("智能自动选择 vs A+B 专业模式"),
            _FORMAT_QUALITY_HTML,
            self.usageGroup,
        )
        self.lazyCard = ExpandHelpCard(
            FluentIcon.CHAT,
            self.tr("懒人模式"),
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
            self.tr("FluentYTDL 如何绕过 YouTube 机器人检测"),
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
            self.tr("任务在崩溃/断电后依然保留"),
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
            self.tr("更新渠道与紧急自救"),
            "stable / nightly / master",
            _UPDATE_CHANNELS_HTML,
            self.componentsGroup,
        )
        self.performanceCard = ExpandHelpCard(
            FluentIcon.SPEED_HIGH,
            self.tr("性能调优"),
            self.tr("并发分片、限速、下载优化"),
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
