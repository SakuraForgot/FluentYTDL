"""成功路径上的异常征兆 —— `kind=signal`，**不是** `kind=diagnosis`（硬规则 2）。

一次操作**成功了**却带着警告，是这个项目最难查的一类问题：字幕三码没匹配上、
nsig 提取失败导致降级、PO Token 被拒、请求的音轨语言压根不存在。它们全都在
yt-dlp 的输出里躺着，而唯一读那段输出的代码只在失败时才跑。

**为什么不能在这里调 `diagnose()`。** `diagnose()` 是**主因仲裁**：它从一堆事件里
挑出"这次失败是因为什么"，产出带 `severity` / `fix_action` / `retry` 的 `Diagnosis`。
在 rc=0 的路径上调它，日志里就会出现 `kind=diagnosis severity=fatal` 却 `rc=0` 的
诡异语义，`count(kind=diagnosis)` 从此不再等于"真正发生了错误"，所有基于事件的
统计一起失效。

区别一句话：

- `signal` = **我观察到了什么**（每条独立，可以有零条也可以有十条）
- `diagnosis` = **这次失败最终判定为什么**（每次失败恰好一条，属于错误边界）
"""

from __future__ import annotations

from typing import Any

from .events import emit_event

#: 单次操作最多落多少条 `signal`。播放列表解析可能对每个条目各刷一条同样的警告，
#: `DiagnosticLineCollector` 的去重能挡掉字面相同的，但"同一 code、不同语言代码"
#: 这种仍会逐条通过。真正有诊断价值的是**出现过哪些 code**，不是出现了几百次。
MAX_SIGNALS = 24


def emit_success_signals(
    output: str,
    *,
    trace: Any = None,
    stage: str | None = None,
    operation: str = "",
    **common: Any,
) -> int:
    """把一段**成功**操作的输出里的异常征兆落成 `signal` 事件，返回落了几条。

    `operation_succeeded=true` 是刻意冗余的字段：光看 `code=nsig_extraction_failed`
    分不清这次到底成没成，而这正是"用户说下载好了但字幕没有"这类工单的关键上下文。

    只记 `code` / `level` / `component` / `raw_line`，不记任何本地化文案 ——
    `Diagnosis.user_title` 之类会随界面语言变化，写进日志就破坏可搜索性。
    """
    if not output:
        return 0
    try:
        # 函数内 import：`observability` 是 `diagnostics` 的**下游**，而
        # `observability/__init__` 被下载/解析路径普遍导入。放在模块顶层会让
        # 每一次 `from ..observability import ...` 都顺带加载整个规则引擎。
        from ..diagnostics.collect import extract_diagnostic_lines
        from ..diagnostics.engine import parse_events

        lines = extract_diagnostic_lines(output)
        if not lines:
            return 0
        events = parse_events("\n".join(lines))
    except Exception:
        # 硬规则 5：观测永远 best-effort。规则表坏了也不能让解析/下载跟着失败。
        return 0

    emitted = 0
    seen_codes: set[str] = set()
    for ev in events:
        if emitted >= MAX_SIGNALS:
            emit_event(
                "signal",
                trace=trace,
                level="DEBUG",
                stage=stage,
                code="signals_truncated",
                operation=operation or None,
                dropped=len(events) - emitted,
                **common,
            )
            break
        # 同一 code 在一次操作里只落第一条：后面的除了刷屏没有新信息，而
        # `raw_line` 已经把那一条的具体内容带上了。
        if ev.code in seen_codes:
            continue
        seen_codes.add(ev.code)
        emit_event(
            "signal",
            trace=trace,
            # 成功路径上的警告不该以 WARNING 刷控制台（nsig 降级是常态），但必须进
            # 文件与 JSONL。控制台 sink 是 INFO，文件 sink 是 DEBUG —— 正好。
            level="WARNING" if ev.level == "error" else "DEBUG",
            stage=stage,
            code=ev.code,
            severity_hint=ev.level,
            component=ev.component or None,
            operation=operation or None,
            operation_succeeded=True,
            raw_line=ev.raw_line[:400],
            **common,
        )
        emitted += 1
    return emitted
