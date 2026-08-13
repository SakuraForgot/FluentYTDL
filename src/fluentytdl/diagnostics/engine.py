"""诊断引擎：yt-dlp 输出 → DiagnosticEvent[] → 主因仲裁 → Diagnosis。

与参考实现（jely2002/youtube-dl-gui）的差异：
- 显式 priority 取代数组顺序，避免宽泛 substr 因排序变化而误判
- 事件流不只用于展示，还参与主因仲裁与伴随信号增强
- 保留 FluentYTDL 原有的兜底解析（HTTP 状态码表 + extractor 名提取）
"""

from __future__ import annotations

import re
from typing import Any

from .models import (
    FALLBACK_CODE,
    Diagnosis,
    DiagnosticEvent,
    Level,
    RetryPolicy,
)
from .rules import LoadedRule, RuleSet, get_rule_set

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_COMPONENT_RE = re.compile(r"^\[([^\]]+)\]\s*")
_HTTP_STATUS_RE = re.compile(r"HTTP Error (\d{3})", re.IGNORECASE)
_EXTRACTOR_RE = re.compile(r"ERROR:\s*\[([^\]]+)\]\s*(.*)", re.IGNORECASE)

#: 兜底文案用的 HTTP 状态码释义。规则表命不中时才走到这里。
HTTP_STATUS_TRANSLATIONS = {
    400: "请求格式错误 (Bad Request)",
    401: "需要身份验证 (Unauthorized)",
    403: "访问被拒绝 (Forbidden)",
    404: "页面/资源不存在 (Not Found)",
    410: "资源已永久删除 (Gone)",
    412: "前提条件失败 (Precondition Failed)",
    429: "请求过于频繁 (Too Many Requests)",
    500: "服务器内部错误 (Internal Server Error)",
    502: "网关错误 (Bad Gateway)",
    503: "服务暂时不可用 (Service Unavailable)",
    504: "网关超时 (Gateway Timeout)",
}

EXTRACTOR_NAMES = {
    "youtube": "YouTube",
    "bilibili": "哔哩哔哩",
    "twitter": "X (Twitter)",
    "niconico": "NicoNico",
    "twitch": "Twitch",
    "tiktok": "抖音/TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "vimeo": "Vimeo",
}


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _is_probable_video_id(token: str) -> bool:
    return 10 <= len(token) <= 15 and all(c.isalnum() or c in "-_" for c in token)


def split_component(text: str) -> tuple[str, str]:
    """``[youtube] rest`` → ``("youtube", "rest")``。无组件时返回 ``("", text)``。"""
    m = _COMPONENT_RE.match(text)
    if not m:
        return "", text
    return m.group(1).strip(), text[m.end() :].strip()


def split_video_id(text: str) -> tuple[str, str]:
    """``dQw4w9WgXcQ: message`` → ``("dQw4w9WgXcQ", "message")``。"""
    head, sep, rest = text.partition(":")
    if sep and _is_probable_video_id(head.strip()):
        return head.strip(), rest.strip()
    return "", text.strip()


def parse_level(line: str) -> tuple[Level | None, str]:
    """剥离 ``ERROR:`` / ``WARNING:`` 前缀。非诊断行返回 ``(None, line)``。"""
    stripped = line.strip()
    upper = stripped.upper()
    if upper.startswith("ERROR:"):
        return "error", stripped[6:].strip()
    if upper.startswith("WARNING:"):
        return "warning", stripped[8:].strip()
    return None, stripped


def match_rule(
    rule_set: RuleSet, level: Level, component: str, message: str
) -> LoadedRule | None:
    """在规则表里找出该行的最佳匹配。

    ``rule_set.rules`` 已按 priority 降序排列，因此首个命中即最高优先级 ——
    这正是相对上游"靠数组顺序"的改进：顺序由显式 priority 决定，可测可审。
    """
    message_lower = message.lower()
    for rule in rule_set.rules:
        if not rule.applies_to_level(level):
            continue
        if not rule.matches_component(component):
            continue
        if rule.find_match(message, message_lower):
            return rule
    return None


def parse_events(stderr: str, rule_set: RuleSet | None = None) -> list[DiagnosticEvent]:
    """逐行解析 yt-dlp 输出，产出全量事件流。"""
    rule_set = rule_set or get_rule_set()
    events: list[DiagnosticEvent] = []

    for idx, raw_line in enumerate(strip_ansi(stderr).splitlines()):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        level, body = parse_level(line)
        if level is None:
            # 非 ERROR:/WARNING: 前缀的行也可能携带过滤器跳过信息
            skip_rule = _match_skip_line(rule_set, line)
            if skip_rule is not None:
                events.append(
                    DiagnosticEvent(
                        code=skip_rule.code,
                        level="warning",
                        component="download",
                        raw_line=line,
                        line_no=idx,
                        priority=skip_rule.priority,
                    )
                )
            continue

        component, rest = split_component(body)
        _video_id, message = split_video_id(rest)

        rule = match_rule(rule_set, level, component, message or rest)
        events.append(
            DiagnosticEvent(
                code=rule.code if rule else rule_set.fallback_code,
                level=level,
                component=component,
                raw_line=line,
                line_no=idx,
                priority=rule.priority if rule else -1,
            )
        )

    return events


def _match_skip_line(rule_set: RuleSet, line: str) -> LoadedRule | None:
    """无 ERROR:/WARNING: 前缀的过滤器跳过行（``[download] ... skipping``）。

    这类行不是错误，归一成 warning 事件，避免混入错误判定。
    """
    if "skipping" not in line.lower() and "not in range" not in line.lower():
        return None
    rule = rule_set.by_code("input_filter_skipped")
    if rule is None:
        return None
    return rule if rule.find_match(line, line.lower()) else None


def pick_primary(events: list[DiagnosticEvent]) -> DiagnosticEvent | None:
    """三级仲裁选出主因。

    1. error 优先于 warning
    2. priority 降序
    3. 出现位置靠后者优先（yt-dlp 的致命错误通常落在末尾）
    """
    if not events:
        return None
    return max(
        events,
        key=lambda ev: (0 if ev.level == "warning" else 1, ev.priority, ev.line_no),
    )


def _apply_companion_signals(diag: Diagnosis, rule_set: RuleSet) -> None:
    """伴随信号增强：主因之外的事件可以改写引导方向。

    典型场景：``WARNING: nsig extraction failed`` 后跟 ``ERROR: HTTP Error 403``。
    单看 403 会把用户引去换代理节点，但真正的处置是先更新 yt-dlp。
    """
    stale_toolchain = any(
        diag.has_event(code)
        for code in ("nsig_extraction_failed", "signature_extraction_failed", "ytdlp_outdated")
    )
    if not stale_toolchain or diag.code in (
        "nsig_extraction_failed",
        "signature_extraction_failed",
        "ytdlp_outdated",
    ):
        return

    if diag.has_event("ytdlp_outdated"):
        diag.extra_notes.append(
            "⚠️ 检测到核心组件 (yt-dlp) 版本过旧，建议立即更新以排除兼容性问题。"
        )
    else:
        diag.extra_notes.append(
            "⚠️ 同时检测到 nsig/签名提取失败，这通常意味着 yt-dlp 已落后于站点改版。"
            "建议优先更新核心组件，而不是更换代理节点。"
        )
    diag.fix_action = "update_component"


def _build_fallback(diag: Diagnosis, clean_msg: str) -> None:
    """规则未命中时的兜底文案：HTTP 状态码表 + extractor 名提取。"""
    http_match = _HTTP_STATUS_RE.search(clean_msg)
    if http_match:
        code_int = int(http_match.group(1))
        desc = HTTP_STATUS_TRANSLATIONS.get(code_int, "未知 HTTP 状态码")
        diag.override_title = f"网页请求失败 (HTTP {code_int})"
        diag.override_message = (
            f"服务器返回了错误状态：{desc}。这可能是因为节点被风控或目标网站故障。"
        )
        diag.category = "network"

    ext_match = _EXTRACTOR_RE.search(clean_msg)
    if ext_match and not http_match:
        extractor_raw = ext_match.group(1).strip()
        err_detail = ext_match.group(2).strip()
        extractor_name = EXTRACTOR_NAMES.get(extractor_raw.lower(), extractor_raw)
        message = f"提取组件在处理 {extractor_name} 的链接时遇到问题：\n{err_detail}"
        if len(message) > 300:
            message = message[:297] + "..."
        diag.override_title = f"{extractor_name} 解析失败"
        diag.override_message = message
        if not diag.component:
            diag.component = extractor_raw


def diagnose(
    exit_code: int,
    stderr: str,
    parsed_json: dict[str, Any] | None = None,
    rule_set: RuleSet | None = None,
) -> Diagnosis:
    """核心诊断入口：退出码 + stderr → 结构化 Diagnosis。"""
    rule_set = rule_set or get_rule_set()
    raw_tail = stderr or "未知错误，无输出"
    clean_msg = strip_ansi(raw_tail)

    diag = Diagnosis(exit_code=exit_code, raw_tail=raw_tail)

    # 1. JSON 快照层：yt-dlp 结构化错误优先于文本匹配
    if isinstance(parsed_json, dict):
        err_obj = parsed_json.get("error")
        err_type = err_obj.get("_type") if isinstance(err_obj, dict) else None
        if err_type == "premium_only":
            rule = rule_set.by_code("members_only")
            diag.code = "members_only"
            diag.category = "auth"
            diag.severity = "fatal"
            diag.fix_action = rule.fix_action if rule else "extract_cookie"
            diag.retry = rule.retry if rule else RetryPolicy(policy="never")
            return diag

    # 2. 逐行事件流 + 主因仲裁
    diag.events = parse_events(clean_msg, rule_set)
    primary = pick_primary(diag.events)

    if primary is not None and primary.code != rule_set.fallback_code:
        rule = rule_set.by_code(primary.code)
        diag.code = primary.code
        diag.component = primary.component
        if rule is not None:
            diag.category = rule.category
            diag.severity = rule.severity
            diag.fix_action = rule.fix_action
            diag.retry = rule.retry
        _apply_companion_signals(diag, rule_set)
        return diag

    # 3. 兜底
    diag.code = FALLBACK_CODE
    if primary is not None:
        diag.component = primary.component
    _build_fallback(diag, clean_msg)
    _apply_companion_signals(diag, rule_set)
    return diag
