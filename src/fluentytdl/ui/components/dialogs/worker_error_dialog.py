"""
后台任务错误挂起面板
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from qfluentwidgets import BodyLabel, MessageBoxBase, PushButton, StrongBodyLabel, TextEdit


class WorkerErrorDialog(MessageBoxBase):
    """
    后台任务错误面板，用于展示 `diagnose_error` 产生的结构化错误。
    提供【去设置页排查】和【重试所有挂起任务】功能。

    快捷修复按钮**跟着 `fix_action` 走**：以前"获取 cookie" / "更新 yt-dlp"两个按钮
    无条件常显，于是 media / toolchain 类失败也在劝用户去换 cookie —— 给错的动作
    比不给动作更糟。
    """

    # 信号
    retry_all_requested = Signal()
    go_settings_requested = Signal()
    fetch_cookie_requested = Signal()
    update_ytdlp_requested = Signal()
    #: 通用修复动作（`fix_action` 原样透传给 `fix_registry.execute_fix_action`）
    fix_requested = Signal(str)

    #: 只有这两个动作是"重新拿 cookie"能解决的
    _COOKIE_FIX_ACTIONS = ("extract_cookie", "relogin")
    #: 事件流里出现这些码，说明工具链确实旧了，"更新 yt-dlp"才有意义
    _STALE_TOOLCHAIN_CODES = (
        "nsig_extraction_failed",
        "signature_extraction_failed",
        "ytdlp_outdated",
    )

    def __init__(self, err_data: dict[str, Any], parent=None):
        super().__init__(parent)
        self.err_data = err_data
        self._setup_ui()

    def _setup_ui(self):
        self.widget.setMinimumWidth(500)

        # 标题
        title_text = self.err_data.get("user_title", self.tr("下载遇到错误"))
        self.title_label = StrongBodyLabel(f"❌ {title_text}", self)
        self.title_label.setStyleSheet("font-size: 16px;")
        self.viewLayout.addWidget(self.title_label)

        # 错误信息
        content_text = self.err_data.get("user_message", self.tr("未知错误"))
        self.content_label = BodyLabel(content_text, self)
        self.content_label.setWordWrap(True)
        self.viewLayout.addWidget(self.content_label)

        # 修复建议
        # `Diagnosis.to_dict()` 里没有 `suggestion` 键（只有 `translate_error()`
        # 那条路在"连兜底都认不出"时才填通用步骤），所以这一段平时不出现；
        # 可执行的动作走下面的修复按钮，用 `recovery_hint` 当标签。
        suggestion_text = self.err_data.get("suggestion") or ""
        if suggestion_text:
            self.suggestion_label = BodyLabel(self.tr("💡 修复建议：\n") + suggestion_text, self)
            self.suggestion_label.setWordWrap(True)
            self.viewLayout.addWidget(self.suggestion_label)

        # 错误详情
        tech_detail = self.err_data.get("technical_detail", "")
        if tech_detail:
            self.tech_edit = TextEdit(self)
            self.tech_edit.setReadOnly(True)
            self.tech_edit.setPlainText(tech_detail)
            self.tech_edit.setMaximumHeight(100)
            self.viewLayout.addWidget(self.tech_edit)

        self.viewLayout.setSpacing(16)
        self.viewLayout.setContentsMargins(24, 24, 24, 24)

        # 配置按钮
        self.yesButton.setText(self.tr("重试所有挂起任务"))
        self.cancelButton.setText(self.tr("稍后处理 (关闭)"))

        # 添加一个去设置的自定义按钮
        self.settings_btn = PushButton(self.tr("去设置页排查"), self)
        self.buttonLayout.insertWidget(0, self.settings_btn)
        self.settings_btn.clicked.connect(self._on_settings_clicked)

        fix_action = self.err_data.get("fix_action") or ""
        insert_at = 1

        # 快捷修复：获取新的 cookie —— 只在失败确实和登录态有关时才给
        self.fetch_cookie_btn: PushButton | None = None
        if fix_action in self._COOKIE_FIX_ACTIONS or self.err_data.get("category") == "auth":
            self.fetch_cookie_btn = PushButton(self.tr("快速获取新的cookie"), self)
            self.buttonLayout.insertWidget(insert_at, self.fetch_cookie_btn)
            self.fetch_cookie_btn.clicked.connect(self._on_fetch_cookie_clicked)
            insert_at += 1

        # 快捷修复：更新 yt-dlp —— fix_action 直接指向组件更新，或者事件流里带着
        # nsig / 签名提取失败这类"工具链确实旧了"的伴随信号
        self.update_ytdlp_btn: PushButton | None = None
        if fix_action == "update_component" or self._event_codes() & set(
            self._STALE_TOOLCHAIN_CODES
        ):
            self.update_ytdlp_btn = PushButton(self.tr("更新 yt-dlp 并重试"), self)
            self.buttonLayout.insertWidget(insert_at, self.update_ytdlp_btn)
            self.update_ytdlp_btn.clicked.connect(self._on_update_ytdlp_clicked)
            insert_at += 1

        # 其余修复动作（启用 POT 引擎、安装 JS Runtime、检查代理……）走通用按钮。
        # 标签用 catalog 给出的 `recovery_hint`，和 InfoBar 上那套引导一致。
        self.fix_btn: PushButton | None = None
        needs_generic_fix = bool(fix_action) and not (
            fix_action in self._COOKIE_FIX_ACTIONS or fix_action == "update_component"
        )
        if needs_generic_fix:
            hint = self.err_data.get("recovery_hint") or self.tr("去处理")
            self.fix_btn = PushButton(hint, self)
            self.buttonLayout.insertWidget(insert_at, self.fix_btn)
            self.fix_btn.clicked.connect(self._on_fix_clicked)

        # 覆盖 yesButton 事件
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_yes_clicked)

    def _event_codes(self) -> set[str]:
        """事件流里出现过的 code 集合。

        `events` 有两种形态：`Diagnosis.to_dict()` 给的是事件字典列表，
        `translate_error()` 给的是纯 code 字符串列表 —— 两条路都要认。
        """
        codes: set[str] = set()
        for ev in self.err_data.get("events") or []:
            if isinstance(ev, dict):
                code = ev.get("code")
            else:
                code = ev
            if isinstance(code, str) and code:
                codes.add(code)
        return codes

    def _on_fix_clicked(self):
        self.fix_requested.emit(self.err_data.get("fix_action") or "")
        self.reject()

    def _on_yes_clicked(self):
        self.retry_all_requested.emit()
        self.accept()

    def _on_settings_clicked(self):
        self.go_settings_requested.emit()
        self.reject()

    def _on_fetch_cookie_clicked(self):
        self.fetch_cookie_requested.emit()
        self.reject()

    def _on_update_ytdlp_clicked(self):
        self.update_ytdlp_requested.emit()
        self.accept()
