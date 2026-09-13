"""诊断引擎：yt-dlp 输出 → DiagnosticEvent[] → 主因仲裁 → Diagnosis。

与参考实现（jely2002/youtube-dl-gui）的差异：
- 显式 priority 取代数组顺序，避免宽泛 substr 因排序变化而误判
- 事件流不只用于展示，还参与主因仲裁与伴随信号增强
- 保留 FluentYTDL 原有的兜底解析（HTTP 状态码表 + extractor 名提取）
"""

from __future__ import annotations

import re
from typing import Any

from fluentytdl.utils.localized_log import log_text

from ..utils.logger import logger
from .catalog import QT_TRANSLATE_NOOP, localize
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
#: 值一律用 ``QT_TRANSLATE_NOOP("Diagnostics", ...)`` 标记、取用时 ``localize()``：
#: 这些串会直接进错误对话框标题，漏标就是英文界面下的中文残留（ISSUE #88）。
HTTP_STATUS_TRANSLATIONS = {
    400: QT_TRANSLATE_NOOP("Diagnostics", "请求格式错误 (Bad Request)"),
    401: QT_TRANSLATE_NOOP("Diagnostics", "需要身份验证 (Unauthorized)"),
    403: QT_TRANSLATE_NOOP("Diagnostics", "访问被拒绝 (Forbidden)"),
    404: QT_TRANSLATE_NOOP("Diagnostics", "页面/资源不存在 (Not Found)"),
    410: QT_TRANSLATE_NOOP("Diagnostics", "资源已永久删除 (Gone)"),
    412: QT_TRANSLATE_NOOP("Diagnostics", "前提条件失败 (Precondition Failed)"),
    429: QT_TRANSLATE_NOOP("Diagnostics", "请求过于频繁 (Too Many Requests)"),
    500: QT_TRANSLATE_NOOP("Diagnostics", "服务器内部错误 (Internal Server Error)"),
    502: QT_TRANSLATE_NOOP("Diagnostics", "网关错误 (Bad Gateway)"),
    503: QT_TRANSLATE_NOOP("Diagnostics", "服务暂时不可用 (Service Unavailable)"),
    504: QT_TRANSLATE_NOOP("Diagnostics", "网关超时 (Gateway Timeout)"),
}

#: extractor id → 展示名。只有含 CJK 的项需要标记，专名（YouTube / Vimeo …）本就无需翻译；
#: ``localize()`` 对未标记的源串原样返回，所以两类可以混在同一张表里。
EXTRACTOR_NAMES = {
    "youtube": "YouTube",
    "bilibili": QT_TRANSLATE_NOOP("Diagnostics", "哔哩哔哩"),
    "twitter": "X (Twitter)",
    "niconico": "NicoNico",
    "twitch": "Twitch",
    "tiktok": QT_TRANSLATE_NOOP("Diagnostics", "抖音/TikTok"),
    "instagram": "Instagram",
    "facebook": "Facebook",
    "vimeo": "Vimeo",
}

#: 兜底文案模板。**先翻译再 format** —— 拼接后再翻译，源串会带上运行期内容，永远匹配不到译文。
_FALLBACK_HTTP_UNKNOWN = QT_TRANSLATE_NOOP("Diagnostics", "未知 HTTP 状态码")
_FALLBACK_HTTP_TITLE = QT_TRANSLATE_NOOP("Diagnostics", "网页请求失败 (HTTP {code})")
_FALLBACK_HTTP_BODY = QT_TRANSLATE_NOOP(
    "Diagnostics", "服务器返回了错误状态：{desc}。这可能是因为节点被风控或目标网站故障。"
)
_FALLBACK_EXTRACTOR_TITLE = QT_TRANSLATE_NOOP("Diagnostics", "{extractor} 解析失败")
_FALLBACK_EXTRACTOR_BODY = QT_TRANSLATE_NOOP(
    "Diagnostics", "提取组件在处理 {extractor} 的链接时遇到问题：\n{detail}"
)

#: 进程无任何输出时的占位（会出现在 technical_detail / raw_error 展示区）。
_EMPTY_OUTPUT = QT_TRANSLATE_NOOP("Diagnostics", "未知错误，无输出")

#: 伴随信号补充说明。``models.Diagnosis.extra_notes`` 声明"已本地化"，这里必须 localize()。
_NOTE_YTDLP_OUTDATED = QT_TRANSLATE_NOOP(
    "Diagnostics", "⚠️ 检测到核心组件 (yt-dlp) 版本过旧，建议立即更新以排除兼容性问题。"
)
_NOTE_STALE_TOOLCHAIN = QT_TRANSLATE_NOOP(
    "Diagnostics",
    "⚠️ 同时检测到 nsig/签名提取失败，这通常意味着 yt-dlp 已落后于站点改版。"
    "建议优先更新核心组件，而不是更换代理节点。",
)
_NOTE_JS_RUNTIME_MISSING = QT_TRANSLATE_NOOP(
    "Diagnostics",
    "⚠️ 同时检测到缺少 JS Runtime（Deno/Node/Bun/QuickJS）。这会导致 YouTube "
    "缺失大量格式，且更新 yt-dlp 无法解决 —— 请先装好 JS Runtime 再排查其他问题。",
)


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


def match_rule(rule_set: RuleSet, level: Level, component: str, message: str) -> LoadedRule | None:
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
            # 无 ERROR:/WARNING: 前缀的行照样可能是唯一的原因说明。
            # 先试 bareLine 规则（规则表已按 priority 降序，首个命中即最高），
            # 再回落到既有的过滤器跳过通道。
            bare_rule = _match_bare_line(rule_set, line) or _match_skip_line(rule_set, line)
            if bare_rule is not None:
                events.append(
                    DiagnosticEvent(
                        code=bare_rule.code,
                        level="warning",
                        component="download",
                        raw_line=line,
                        line_no=idx,
                        priority=bare_rule.priority,
                    )
                )
            continue

        component, rest = split_component(body)
        _video_id, message = split_video_id(rest)

        rule = match_rule(rule_set, level, component, message or rest)
        if rule is not None and rule.demote_to_warning and level == "error":
            # 子请求失败的 `ERROR:` 行降级成 warning 事件，详见 `LoadedRule.demote_to_warning`。
            # 只改事件级别，不动 priority —— priority 要留着在**同一行**上压过更泛化的
            # 规则（字幕 429 压 `rate_limited_429`），而级别决定它在跨行仲裁里的分层。
            level = "warning"
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


def _match_bare_line(rule_set: RuleSet, line: str) -> LoadedRule | None:
    """显式声明 ``bareLine`` 的规则：允许匹配没有级别前缀的整行。

    ``[info] There are no subtitles for the requested languages`` 是这条通道的
    由来 —— 它既不是 WARNING 也不是 ERROR，却是"字幕开着却一个文件都没写"最直接
    的一句解释。走整行匹配而不是 ``match_rule``：这类行没有级别、也没有可靠的
    ``[component]`` 结构可拆。
    """
    line_lower = line.lower()
    for rule in rule_set.rules:  # 已按 priority 降序
        if not rule.bare_line:
            continue
        if rule.find_match(line, line_lower):
            return rule
    return None


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


def _is_warning_only_primary(
    exit_code: int, primary: DiagnosticEvent, rule: LoadedRule | None
) -> bool:
    """rc != 0，但唯一的候选主因只是一条警告级规则。

    判据刻意分两层：

    - ``primary.level == "warning"`` —— `pick_primary` 的排序键让任何 error 级事件
      压过任何 warning 级事件，所以这一条等价于"全场没有 error 级事件"；
    - ``rule.severity == "warning"`` —— 按**规则**的严重级而不是**行**的级别判。
      `pot_token_required` 是 ``severity: fatal`` 且 ``appliesTo: both``，它以
      ``WARNING:`` 行出现时仍然是真正的失败原因，必须继续被采纳。

    rc == 0 时一律返回 False：成功任务的警告扫描（字幕三码）就靠这条路。
    """
    if exit_code == 0:
        return False
    if primary.level != "warning":
        return False
    return rule is not None and rule.severity == "warning"


#: SABR 只解释"挑不到格式"这一类主因。403 / 429 / 需要登录都有自己的正解，
#: 不许被它顶掉 —— 那些场景下格式选择根本没轮到。
_SABR_SYMPTOM_CODES = ("format_unavailable", "no_formats_found")


def _apply_sabr_override(diag: Diagnosis, rule_set: RuleSet) -> None:
    """SABR 强制流：把"所选格式不可用"纠正成"YouTube 限制了可下载的流"。

    yt-dlp 打出 ``Some tv client https formats have been skipped as they are
    missing a url. YouTube is forcing SABR streaming for this client.`` 之后，
    带下载 URL 的格式全被丢掉，选片器于是挑不到东西，最终只留一句
    ``ERROR: Requested format is not available``。规则表照文本匹配会判成
    `format_unavailable`（"换一档画质通常就能下载"）——**画质本身存在，换档位
    不可能有用**，这条引导把用户送进死胡同。

    ``sabr_formats_skipped`` 是 warning 级规则（`_is_warning_only_primary` 拦着
    它自己抢主因），所以纠正只能在这里做：它作为伴随信号进 `diag.events`，
    由本函数改写主因。
    """
    if not diag.has_event("sabr_formats_skipped"):
        return
    if diag.code not in _SABR_SYMPTOM_CODES and not (
        # 只有"连兜底都没抽出东西"的 unknown 才让 SABR 接管；兜底已经抽出
        # HTTP 码 / extractor 名时那个判词信息量更大，不能被顶掉
        diag.code == FALLBACK_CODE
        and not diag.override_title
        # 且必须真有一条 error 级事件。`_is_warning_only_primary` 拒绝采纳纯警告
        # 主因之后也会落到兜底，从那里接管等于把那道护栏绕了过去 —— 下载已经完成
        # 只是 Windows 删不掉 `.part-Frag` 的 rc != 0，会被报成"YouTube 限流了流"。
        and any(ev.level == "error" for ev in diag.events)
    ):
        return

    rule = rule_set.by_code("sabr_formats_skipped")
    diag.code = "sabr_formats_skipped"
    diag.category = rule.category if rule else "toolchain"
    # 规则里写 warning 是为了不抢主因；一旦成为主因，它描述的就是一次真失败。
    diag.severity = "recoverable"
    diag.fix_action = rule.fix_action if rule else "enable_pot_provider"
    if rule is not None:
        diag.retry = rule.retry
    # 兜底路径可能已经写过文案，不清掉就还是旧判词
    diag.override_title = ""
    diag.override_message = ""
    # 刻意不 return：调用方后面的 stale_toolchain 分支若同时看到 nsig /
    # ytdlp_outdated，应当把 fix_action 再升级成 update_component。


def _apply_companion_signals(diag: Diagnosis, rule_set: RuleSet) -> None:
    """伴随信号增强：主因之外的事件可以改写引导方向。

    典型场景：``WARNING: nsig extraction failed`` 后跟 ``ERROR: HTTP Error 403``。
    单看 403 会把用户引去换代理节点，但真正的处置是先更新 yt-dlp。

    **缺 JS runtime 必须先于"组件过旧"判定**：yt-dlp 缺 runtime 时的警告里带
    "some formats may be missing"，措辞和 nsig 失败高度相似，但处置完全相反 ——
    一个是装 Deno，一个是更新 yt-dlp。把它放在最前面并 return，避免下面的
    stale_toolchain 分支把用户引向更新组件这条死路。
    """
    if diag.has_event("js_runtime_missing") and diag.code != "js_runtime_missing":
        diag.extra_notes.append(localize(_NOTE_JS_RUNTIME_MISSING))
        diag.fix_action = "install_js_runtime"
        return

    _apply_sabr_override(diag, rule_set)

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
        diag.extra_notes.append(localize(_NOTE_YTDLP_OUTDATED))
    else:
        diag.extra_notes.append(localize(_NOTE_STALE_TOOLCHAIN))
    diag.fix_action = "update_component"


def _build_fallback(diag: Diagnosis, clean_msg: str) -> None:
    """规则未命中时的兜底文案：HTTP 状态码表 + extractor 名提取。"""
    http_match = _HTTP_STATUS_RE.search(clean_msg)
    if http_match:
        code_int = int(http_match.group(1))
        desc = localize(HTTP_STATUS_TRANSLATIONS.get(code_int, _FALLBACK_HTTP_UNKNOWN))
        # 先翻译模板再 format：反过来会让源串带上运行期的状态码，永远匹配不到译文。
        diag.override_title = localize(_FALLBACK_HTTP_TITLE).format(code=code_int)
        diag.override_message = localize(_FALLBACK_HTTP_BODY).format(desc=desc)
        diag.category = "network"

    ext_match = _EXTRACTOR_RE.search(clean_msg)
    if ext_match and not http_match:
        extractor_raw = ext_match.group(1).strip()
        err_detail = ext_match.group(2).strip()
        extractor_name = localize(EXTRACTOR_NAMES.get(extractor_raw.lower(), extractor_raw))
        message = localize(_FALLBACK_EXTRACTOR_BODY).format(
            extractor=extractor_name, detail=err_detail
        )
        if len(message) > 300:
            message = message[:297] + "..."
        diag.override_title = localize(_FALLBACK_EXTRACTOR_TITLE).format(extractor=extractor_name)
        diag.override_message = message
        if not diag.component:
            diag.component = extractor_raw


def diagnose(
    exit_code: int,
    stderr: str,
    parsed_json: dict[str, Any] | None = None,
    rule_set: RuleSet | None = None,
    *,
    phase: str = "",
) -> Diagnosis:
    """核心诊断入口：退出码 + stderr → 结构化 Diagnosis。

    Args:
        phase: 失败发生在哪个阶段（`parse` / `select` / `download`，取值来自
            `observability.STAGES` 闭集）。只有 `DownloadWorker` 这条路能算出它
            （靠 executor 见过哪些输出行），所以写成 keyword-only + 默认空串 ——
            展示层重算诊断的那几个调用点不用改。
    """
    rule_set = rule_set or get_rule_set()
    raw_tail = stderr or localize(_EMPTY_OUTPUT)
    clean_msg = strip_ansi(raw_tail)

    diag = Diagnosis(
        exit_code=exit_code, raw_tail=raw_tail, phase=phase, rules_version=rule_set.version
    )

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
        if _is_warning_only_primary(exit_code, primary, rule):
            # 任务真的失败了（rc != 0），可全场只有警告级线索 —— 拿它当主因等于
            # 张口就错。`pick_primary` 让 error 压过 warning，所以走到这里就意味着
            # **一条 ERROR: 行都没有**；而 `executor` 的两级体积校验会在没有 ERROR:
            # 行的情况下照样抛 `YtDlpExecutionError`（Windows 上 `.part-Frag` 删不掉
            # 就是这条路）。去掉 `--no-warnings` 之后，那一刻顶上来的很可能只是一条
            # 字幕限流警告 —— 把"字幕没下到"报成整个任务的失败主因，比不报更糟。
            # 落到兜底后 `_apply_companion_signals` 照旧跑，伴随信号不丢。
            log_text(
                logger,
                "debug",
                "[diagnostics] rc={} 只有警告级线索 {}，不采纳为主因",
                exit_code,
                primary.code,
            )
        else:
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


def diagnose_exception(exc: OSError, *, phase: str = "") -> Diagnosis:
    """Classify typed local failures without guessing from localized OS messages."""
    import errno

    code = "unknown"
    if isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) in (2, 3):
        code = "file_missing"
    elif isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32):
        code = "permission_denied"
    elif exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
        code = "disk_full"
    if code == "unknown":
        return diagnose(1, str(exc), phase=phase)
    return Diagnosis(
        code=code,
        category="filesystem",
        severity="fatal",
        fix_action="change_download_dir",
        raw_tail=str(exc),
        phase=phase,
        rules_version=get_rule_set().version,
    )
