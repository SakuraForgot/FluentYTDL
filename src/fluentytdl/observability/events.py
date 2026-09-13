"""Observability Event 层的数据模型与唯一出口。

## 为什么要有这一层

项目本来不缺日志，缺的是**把已有判定接到日志上的那根线**：`error_rules.json` 里 53 条
规则、`diagnostics/engine.py` 的三级仲裁、`quality_guard` 的 intent/verdict 全都算得
出结论，但结论只喂了 UI，从不落文件。于是日志里能看到 `ERROR: HTTP Error 429`，
看不到 `code=rate_limited_429 category=network retry=backoff` —— 分类在展示时才发生，
结果不持久，事后无法统计、无法按任务筛时间线。

## 五条硬规则（破了任何一条，两个月后这层就退化成"7 条观测通道 2.0"）

1. `transition` 只有一个权威产生点。
2. `diagnosis` 只属于失败错误边界；成功路径的异常征兆只能是 `signal`，
   且成功路径**绝不调用 `diagnose()`**（那是主因仲裁）。
3. `degraded` 只能由 `expected − actual` 得出，绝不是"系统支持的 − actual"。
4. 每个 `run_id` 恰好一个 `outcome`；子系统只报告事实，不得宣布最终结果。
5. observability 永远 best-effort —— `emit_event()` 吞掉自己的一切异常，
   绝不把异常抛回业务层，否则日志系统会在错误现场自己炸掉。

## 与 loguru 的接法

**不改任何 sink 的 format 字符串。** 人读的那份由 `render_text()` 自己把 key=value
渲染进 message（`youtube/yt_dlp_cli.py::log_pot_in_argv` 已是此做法），机器读的那份走
`logger.bind(fytdl=...)` 进 `extra`，只给 JSONL sink 消费。这样 `log_viewer_window` 与
`log_signal_handler` 完全无感，也不存在 `{extra[task]}` 的 KeyError。
"""

from __future__ import annotations

import math
import sys
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath
from typing import Any, Literal

from ..utils.log_runtime import SESSION_ID
from ..utils.logger import logger
from .sanitize import sanitize_exception, sanitize_path

# ── 封闭集合 ────────────────────────────────────────────────

Stage = Literal[
    "startup",
    "parse",
    "select",
    "preflight",
    "download",
    "postprocess",
    "verify",
    "finalize",
    "retry",
    "cancel",
]

EventKind = Literal[
    "stage",  # 进入某个阶段（语义变化，不是 UI 刷新）
    "decision",  # 做了什么选择，以及为什么
    "expect",  # 用户/系统请求的是什么
    "actual",  # 实际拿到的是什么（不一致用 matched=false 表达）
    "signal",  # 我观察到了什么征兆（成功路径也可以有）
    "diagnosis",  # 这次失败最终判定为什么（只属于失败边界）
    "recovery",  # 报告一个事实：某个异常被容忍了
    "retry",  # 要重试了，为什么、等多久
    "transition",  # 状态已被系统接受（唯一产生点见硬规则 1）
    "outcome",  # run 的终态裁决（每 run 恰好一条）
    "config",  # 生效配置快照 / 变更
    "argv",  # 脱敏后的命令行
    "identity",  # 把 flow 和 task 钉在一起
]

Outcome = Literal[
    "success",
    "failed",
    "cancelled",
    "paused",
    "interrupted",  # 执行中被强杀，由启动恢复审计补记
    "restored_pending",  # 从未开始执行，只是队列状态被恢复
]

STAGES: frozenset[str] = frozenset(
    {
        "startup",
        "parse",
        "select",
        "preflight",
        "download",
        "postprocess",
        "verify",
        "finalize",
        "retry",
        "cancel",
    }
)

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "stage",
        "decision",
        "expect",
        "actual",
        "signal",
        "diagnosis",
        "recovery",
        "retry",
        "transition",
        "outcome",
        "config",
        "argv",
        "identity",
    }
)

#: 刻意不设 `mismatch`（用 `actual matched=false` 表达，少一个 kind）与 `ui_progress`
#: （进度每次刷新都 emit 会把 JSONL 变成进度数据库；普通进度维持 Qt Signal 通道）。
OMITTED_EVENT_KINDS: frozenset[str] = frozenset({"mismatch", "ui_progress"})

OUTCOMES: frozenset[str] = frozenset(
    {"success", "failed", "cancelled", "paused", "interrupted", "restored_pending"}
)

#: 组合优先级：一个 run 里若有多个候选终态，取最"坏"的那个。
#: `recovered` / `degraded` 是正交标记，不参与这个排序。
OUTCOME_PRIORITY: dict[str, int] = {
    "failed": 60,
    "cancelled": 50,
    "interrupted": 40,
    "paused": 30,
    "restored_pending": 20,
    "success": 10,
}

_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})

#: 标识字段缺失时的占位符。用 `-` 而不是空串，是为了让文本日志里
#: `task=-` 一眼看出"这个阶段本来就没有 task"，而不是像 `task=` 那样疑似 bug。
NO_ID = "-"

#: `as_dict()` 里由标识层占用的键；同名的业务字段会被改写成 `<key>_`。
_RESERVED_KEYS = frozenset({"kind", "stage", "session", "flow", "task", "run", "attempt"})

_MAX_DEPTH = 4
_MAX_ITEMS = 64
_MAX_TEXT = 2000
_MAX_REPR = 200
_MAX_RENDERED = 300


def new_id(length: int = 6) -> str:
    """短随机标识。够短，人能在日志里一眼比对；够长，单次会话内不会撞。"""
    return uuid.uuid4().hex[:length]


#: 软件本次启动的标识。模块首次导入时铸造。


def worst_outcome(*outcomes: str) -> str:
    """按 `OUTCOME_PRIORITY` 取最坏的终态，全部非法时退回 `failed`。"""
    known = [o for o in outcomes if o in OUTCOME_PRIORITY]
    if not known:
        return "failed"
    return max(known, key=lambda o: OUTCOME_PRIORITY[o])


# ── JSON-safe normalizer ────────────────────────────────────


def _truncate(text: str, limit: int = _MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_repr(value: Any) -> str:
    try:
        return _truncate(repr(value), _MAX_REPR)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _sort_key(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def json_safe(value: Any, depth: int = 0) -> Any:
    """把任意值归一成 `json.dumps()` 一定吃得下的形状。

    没有这一步，文本 sink 打得出来、JSONL sink 却
    `TypeError: Object of type WindowsPath is not JSON serializable` ——
    而且是日志系统在错误现场自己炸（硬规则 5）。

    `Path` 会顺手过一遍 `sanitize_path()`：日志是用户会直接贴到 Issue 里的东西，
    而绝对路径里带着 Windows 用户名。
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN / Infinity 在严格 JSON 里非法，json.dumps 默认却会写出来
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, Enum):
        return json_safe(value.value, depth)
    if isinstance(value, PurePath):
        return sanitize_path(value)
    if isinstance(value, BaseException):
        return sanitize_exception(value)
    if isinstance(value, (bytes, bytearray)):
        return _truncate(bytes(value).decode("utf-8", "replace"))
    if depth >= _MAX_DEPTH:
        return _safe_repr(value)
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_ITEMS]
        return {str(k): json_safe(v, depth + 1) for k, v in items}
    if isinstance(value, AbstractSet):
        # 排序用原值的 str()：归一后的类型可能混杂，直接 sorted 会 TypeError
        return [json_safe(v, depth + 1) for v in sorted(value, key=_sort_key)[:_MAX_ITEMS]]
    if isinstance(value, (list, tuple, deque)):
        return [json_safe(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return _safe_repr(value)


# ── 文本渲染 ────────────────────────────────────────────────


def _render_value(value: Any) -> str:
    if value is None:
        return NO_ID
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return "[" + ",".join(_render_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={_render_value(v)}" for k, v in value.items()) + "}"
    text = _truncate(str(value), _MAX_RENDERED)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def render_fields(fields: Mapping[str, Any]) -> str:
    """把业务字段渲染成 `k=v k=v` 一行 —— `Event.render_text()` 的尾段。

    单独开成公开函数，是给**只拿到扁平 dict、拿不到 `Event` 的消费方**用的：
    JSONL 里读回来的一行、`log_signal_handler.event_received` 转出来的那份 dict，
    都已经是 `as_dict()` 之后的形态。UI 时间线自己写一套 `k=v` 渲染的话，同一条事件
    在「全部日志」页和「任务时间线」页会长得不一样 —— 而这两页正是要对照着看的。
    """
    return " ".join(f"{k}={_render_value(json_safe(v))}" for k, v in fields.items())


# ── Event ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """一条观测事件。不可变 —— 产生即定稿，谁都别想事后改它。"""

    kind: str
    stage: str
    session: str = SESSION_ID
    flow: str = NO_ID
    task: str = NO_ID
    run: str = NO_ID
    attempt: int = 0
    fields: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """机器可读的扁平 dict，保证 JSON-safe。

        扁平而非嵌套，是为了让 JSONL 能直接用 `jq 'select(.code=="...")'` 过滤。
        业务字段若与标识键同名，会被改写成 `<key>_` 而不是覆盖标识 ——
        标识一旦被覆盖，整条时间线就串不起来了。
        """
        out: dict[str, Any] = {
            "kind": self.kind,
            "stage": self.stage,
            "session": self.session,
            "flow": self.flow,
            "task": self.task,
            "run": self.run,
            "attempt": self.attempt,
        }
        for key, value in self.fields.items():
            name = str(key)
            if name in _RESERVED_KEYS:
                name = f"{name}_"
            out[name] = json_safe(value)
        return out

    def render_text(self) -> str:
        """人读的一行，例如::

        [download] flow=k72f task=42 run=a91c attempt=0 kind=decision subsystem=subtitle
        """
        head = (
            f"[{self.stage}] flow={self.flow} task={self.task} "
            f"run={self.run} attempt={self.attempt} kind={self.kind}"
        )
        if not self.fields:
            return head
        return f"{head} {render_fields(self.fields)}"


# ── 出口 ────────────────────────────────────────────────────

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """同一种契约违规只提醒一次 —— 提醒本身不该变成噪音源。"""
    if message in _warned:
        return
    _warned.add(message)
    try:
        logger.warning("[Observability] {}", message)
    except Exception:
        pass


_emit_failures = 0
_MAX_EMIT_FAILURE_REPORTS = 5


def _report_emit_failure(exc: BaseException) -> None:
    """记录失败时的最后兜底：直接写 stderr，不再碰 logger（它可能就是坏的那个）。"""
    global _emit_failures
    _emit_failures += 1
    if _emit_failures > _MAX_EMIT_FAILURE_REPORTS:
        return
    try:
        stream = getattr(sys, "__stderr__", None) or sys.stderr
        if stream is not None:
            stream.write(f"[Observability] emit_event failed: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def build_event(
    kind: str,
    /,
    *,
    trace: Any = None,
    stage: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> Event:
    """由 kind + trace 组装 `Event`。单独暴露出来便于测试断言，不产生日志。"""
    if kind not in EVENT_KINDS:
        hint = (
            " (this kind is intentionally omitted; see module docstring)"
            if kind in OMITTED_EVENT_KINDS
            else ""
        )
        _warn_once(f"unknown EventKind: {kind!r}{hint}")
    resolved_stage = stage or getattr(trace, "stage", None) or "startup"
    if resolved_stage not in STAGES:
        _warn_once(f"unknown Stage: {resolved_stage!r}")
    return Event(
        kind=kind,
        stage=str(resolved_stage),
        session=str(getattr(trace, "session_id", None) or SESSION_ID),
        flow=str(getattr(trace, "flow_id", None) or NO_ID),
        task=str(getattr(trace, "task_id", None) or NO_ID),
        run=str(getattr(trace, "run_id", None) or NO_ID),
        attempt=int(getattr(trace, "attempt", 0) or 0),
        fields=dict(fields or {}),
    )


def emit_event(
    kind: str,
    /,
    *,
    trace: Any = None,
    level: str = "INFO",
    stage: str | None = None,
    fields: Mapping[str, Any] | None = None,
    _depth: int = 1,
    **kwargs: Any,
) -> None:
    """记录一条观测事件。**永不抛异常**（硬规则 5）。

    Args:
        kind: `EventKind` 之一，**位置参数**。写成 positional-only 是为了让业务字段
            也能叫 `kind`（`as_dict()` 会把它改名成 `kind_`）—— 否则那会是
            `TypeError: got multiple values for argument 'kind'`，而参数绑定错误发生在
            函数体之前，本函数的 try/except 兜不住，硬规则 5 就破了。
        trace: `FlowTrace` / `TaskTrace`，缺省时只带 session 标识。
        level: loguru 级别名。非法值降级为 INFO 而不是抛异常。
        stage: 覆盖 trace 当前阶段（例如在 download 阶段里记一条 verify 事件）。
        fields: 业务字段的显式入口。**`trace` / `level` / `stage` / `fields` 这四个名字
            不能当业务字段名用**（它们是本门面自己的参数），需要时走这里传。
        **kwargs: 业务字段的便捷写法。语言中立 —— 只记 `code` / `category` /
            `severity`，**绝不记本地化文案**：`Diagnosis.user_title` 之类会随界面语言
            变化，写进日志就破坏了可搜索性。
    """
    try:
        merged: dict[str, Any] = dict(fields) if fields else {}
        merged.update(kwargs)
        event = build_event(kind, trace=trace, stage=stage, fields=merged)
        resolved_level = level.upper() if isinstance(level, str) else "INFO"
        if resolved_level not in _LEVELS:
            _warn_once(f"unknown log level: {level!r}")
            resolved_level = "INFO"
        # _depth 让 {name}:{function}:{line} 指向真正的业务调用点，而不是本函数
        # 或 `FlowTrace.emit()` 这类转发层
        logger.bind(fytdl=event.as_dict()).opt(depth=_depth).log(
            resolved_level, "{}", event.render_text()
        )
    except Exception as exc:  # noqa: BLE001 - 硬规则 5：绝不上抛
        _report_emit_failure(exc)


def emit_events(
    kind: str,
    /,
    items: Iterable[Mapping[str, Any]],
    *,
    trace: Any = None,
    level: str = "INFO",
    stage: str | None = None,
    **common: Any,
) -> None:
    """批量记录同类事件（例如把 `parse_events()` 的结果逐条落成 `signal`）。

    每条 item 的字段覆盖 `common` 里的同名项。
    """
    for item in items:
        merged = dict(common)
        merged.update(item)
        emit_event(kind, trace=trace, level=level, stage=stage, fields=merged, _depth=2)
