from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def translate_error(error: BaseException) -> dict:
    """将异常对象转换为用户友好的错误字典。

    返回值尽量保持稳定的 keys：title/content/suggestion/raw_error。
    兼容老的 UI 代码的同时提供新的字段。
    """
    raw_original = str(error)
    # 检查是否是结构化的 YtDlpExecutionError
    from ..diagnostics import (
        FALLBACK_CODE,
        diagnose,
        generic_suggestion,
        generic_unknown_title,
    )
    from ..models.errors import YtDlpExecutionError
    from .error_parser import generate_issue_url

    exit_code = 1
    phase = ""
    if isinstance(error, YtDlpExecutionError):
        exit_code = error.exit_code
        raw_original = error.stderr
        # executor 算出的"死在哪一步"，只有它有这个上下文；老的 raise 点留空串。
        phase = getattr(error, "phase", "") or ""

    raw = _strip_ansi(raw_original)

    diag = diagnose(exit_code, raw, phase=phase)
    friendly_content = diag.user_message

    # 规则未命中且兜底也没抽出 HTTP 码 / extractor 名时，才算真正"无法识别"。
    # 兜底给出的标题（"网页请求失败 (HTTP 403)"）比"发生未知错误"信息量更大，要保留。
    is_unrecognized = diag.code == FALLBACK_CODE and not diag.override_title
    # 文案取自 catalog（Diagnostics 上下文），不要在这里写裸中文 —— 英文界面下会直接漏出去。
    display_title = generic_unknown_title() if is_unrecognized else diag.user_title

    issue_url = generate_issue_url(display_title, raw)

    result = {
        "title": display_title,
        "content": friendly_content,
        "suggestion": generic_suggestion(),
        "raw_error": raw,
        "issue_url": issue_url,
        "suggests_component_update": diag.fix_action == "update_component",
        # 新 Diagnose 体系的结构化字段
        "code": diag.code,
        "category": diag.category,
        "severity": diag.severity,
        "retry": diag.retry.to_dict(),
        # 伴随信号的 code 列表。主因是 429 而事件流里有 nsig 提取失败，这种组合才是真正
        # 有诊断价值的东西 —— `_apply_companion_signals()` 正是据此把 `fix_action` 从规则
        # 表里的 `switch_proxy` 改写成 `update_component` 的。不带上这一列，日志里就只
        # 剩一个"和规则表对不上"的 fix_action，看不出是哪条伴随信号改写了它。
        # 只放 code（语言中立），不放 `extra_notes` —— 那些是本地化文案。
        "events": [e.code for e in diag.events],
        "user_title": display_title,
        "user_message": friendly_content,
        "fix_action": diag.fix_action,
        "technical_detail": diag.technical_detail,
        "recovery_hint": diag.recovery_hint,
        "phase": diag.phase,
    }

    if not is_unrecognized:
        result["suggestion"] = ""

    return result
