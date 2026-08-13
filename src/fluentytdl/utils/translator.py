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
    from ..diagnostics import FALLBACK_CODE, diagnose
    from ..models.errors import YtDlpExecutionError
    from .error_parser import generate_issue_url

    exit_code = 1
    if isinstance(error, YtDlpExecutionError):
        exit_code = error.exit_code
        raw_original = error.stderr

    raw = _strip_ansi(raw_original)

    diag = diagnose(exit_code, raw)
    friendly_content = diag.user_message

    # 规则未命中且兜底也没抽出 HTTP 码 / extractor 名时，才算真正"无法识别"。
    # 兜底给出的标题（"网页请求失败 (HTTP 403)"）比"发生未知错误"信息量更大，要保留。
    is_unrecognized = diag.code == FALLBACK_CODE and not diag.override_title
    display_title = "发生未知错误" if is_unrecognized else diag.user_title

    issue_url = generate_issue_url(display_title, raw)

    result = {
        "title": display_title,
        "content": friendly_content,
        "suggestion": "1. 请重试\n2. 查看日志文件\n3. 将此错误反馈给开发者",
        "raw_error": raw,
        "issue_url": issue_url,
        "suggests_component_update": diag.fix_action == "update_component",
        # 新 Diagnose 体系的结构化字段
        "code": diag.code,
        "category": diag.category,
        "severity": diag.severity,
        "retry": diag.retry.to_dict(),
        "user_title": display_title,
        "user_message": friendly_content,
        "fix_action": diag.fix_action,
        "technical_detail": diag.technical_detail,
        "recovery_hint": diag.recovery_hint,
    }

    if not is_unrecognized:
        result["suggestion"] = ""

    return result
