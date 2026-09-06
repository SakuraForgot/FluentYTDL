"""五级标识：`session → flow → task → run → attempt`。

```
session            软件本次启动
 └─ flow           一次用户操作链：解析 → 选择 → 创建任务（Playlist 可含多个 task）
     └─ task       用户任务，跨暂停/恢复稳定（= worker.db_id）
         └─ run    一次"执行会话"：一次开始或一次恢复；outcome 挂在这一级
             ├─ attempt 0
             ├─ attempt 1        ← 规则驱动自动重试，run_id 不变
             └─ attempt 2
```

**自动重试不换 `run_id`。** 换了 `run_id` 和 `attempt` 就在表达同一件事，层次失去意义。
只有"软件重启后恢复同一任务"才铸新 run：

```
用户点击下载   task=42 run=A attempt=0
429 →         task=42 run=A attempt=1
429 →         task=42 run=A attempt=2 → 成功
（重启恢复）    task=42 run=B attempt=0
```

**为什么需要 `flow_id`。** `workers.py` 里 `InfoExtractWorker` / `ChannelExtractWorker` /
`VRInfoExtractWorker` / `EntryDetailWorker` 四个类全部跑在 `DownloadWorker` **之前**
—— `parse` / `select` 阶段天然没有 `task_id`。没有 flow，UI 时间线做出来会是"下载段很
漂亮，下载前的解析日志全是孤儿"。前置阶段记 `flow=k72f task=-`，`create_worker()` 拿到
`db_id` 后补一条 `identity` 事件把 flow → task 钉起来。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .events import NO_ID, OUTCOMES, SESSION_ID, emit_event, new_id


@dataclass
class FlowTrace:
    """一次用户操作链的标识。解析/选择阶段持有它。

    `task_id` / `run_id` / `attempt` 也在这里，是为了让 `emit_event()` 面对两种
    trace 时读同一组属性名，不必判断类型。flow 层面它们恒为占位符。
    """

    flow_id: str = ""
    session_id: str = ""
    stage: str = "parse"
    task_id: str = NO_ID
    run_id: str = NO_ID
    attempt: int = 0

    def __post_init__(self) -> None:
        if not self.flow_id:
            self.flow_id = new_id()
        if not self.session_id:
            self.session_id = SESSION_ID

    # ---- 出口的薄封装 ----

    def emit(
        self,
        kind: str,
        *,
        level: str = "INFO",
        stage: str | None = None,
        _depth: int = 2,
        **fields: Any,
    ) -> None:
        """等价于 `emit_event(kind, trace=self, ...)`，纯转发。

        `stage` 只覆盖这一条事件的阶段，**不改 trace 自身的 stage**（那要用
        `enter()`）—— 高扇出的逐行解析共享同一个 flow，改共享状态会互相打脸。

        `_depth=2` 让日志里的 `{name}:{function}:{line}` 指向业务调用点，
        而不是本方法 —— 否则每条事件都显示 `trace.py:emit`，定位价值归零。
        """
        emit_event(kind, trace=self, level=level, stage=stage, fields=fields, _depth=_depth)

    def enter(self, stage: str, **fields: Any) -> None:
        """进入一个阶段。记的是**语义变化**，不是 UI 刷新（进度不进事件层）。"""
        self.stage = stage
        self.emit("stage", _depth=3, **fields)

    # ---- 派生 ----

    def child_task(self, task_id: str | int = NO_ID, **kwargs: Any) -> TaskTrace:
        """在本 flow 下开一个任务 trace（Playlist 会开多个）。"""
        return TaskTrace(
            flow_id=self.flow_id,
            session_id=self.session_id,
            task_id=str(task_id) if task_id not in ("", None) else NO_ID,
            **kwargs,
        )


@dataclass
class TaskTrace(FlowTrace):
    """一个任务在一次执行会话（run）中的标识与累积状态。

    `recovered` / `expected_artifacts` / `actual_artifacts` 一路累积到 run 的终态
    边界，由 `finish()` 一次性裁决 —— 子系统只报告事实，不宣布结果（硬规则 4）。
    """

    stage: str = "download"
    #: 某个异常被容忍了（如"rc≠0 但产物有效"）。这是事实，不是终态。
    recovered: bool = False
    recovery: str = ""
    #: 用户实际请求的产物集合，如 {"video", "subtitle:zh-Hans", "thumbnail"}。
    expected_artifacts: set[str] = field(default_factory=set)
    actual_artifacts: set[str] = field(default_factory=set)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _outcome_emitted: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.run_id or self.run_id == NO_ID:
            self.run_id = new_id()

    # ---- run / attempt ----

    def new_run(self, *, stage: str | None = None) -> str:
        """铸一个新的 run：一次开始或一次恢复。清空上一轮的累积状态。

        **只有启动恢复 / 用户重新开始才调这个**，自动重试调 `next_attempt()`。
        """
        with self._lock:
            self.run_id = new_id()
            self.attempt = 0
            self.recovered = False
            self.recovery = ""
            self.expected_artifacts.clear()
            self.actual_artifacts.clear()
            self._outcome_emitted = False
        if stage:
            self.stage = stage
        return self.run_id

    def next_attempt(self) -> int:
        """自动重试：`attempt` 递增，`run_id` **不变**。

        本方法只推进标识，不记事件 —— `kind=retry` 由掌握 `Diagnosis` 的那个
        错误边界发出，那里才知道 code / 退避秒数。
        """
        self.attempt += 1
        return self.attempt

    def bind_task_id(self, task_id: str | int) -> None:
        """`create_worker()` 拿到 `db_id` 之后调用，并把 flow → task 钉进日志。"""
        new_value = str(task_id)
        if not new_value or new_value == self.task_id:
            return
        self.task_id = new_value
        self.emit("identity", _depth=3, db_id=new_value)

    # ---- 事实累积 ----

    def mark_recovered(self, reason: str) -> None:
        """报告"某个异常被容忍了"。**不得**由此推出 outcome（硬规则 4）。"""
        self.recovered = True
        self.recovery = reason

    def expect_artifacts(self, artifacts: Iterable[str]) -> None:
        """声明用户**实际请求**了哪些产物。这是 `degraded` 唯一合法的被减数。"""
        self.expected_artifacts.update(a for a in artifacts if a)

    def record_artifacts(self, artifacts: Iterable[str]) -> None:
        """记录实际拿到了哪些产物。"""
        self.actual_artifacts.update(a for a in artifacts if a)

    @property
    def missing(self) -> list[str]:
        """`expected − actual`。故意做成只读派生量。

        硬规则 3 说 `degraded` 只能这么算。如果这里是个可写的 bool，早晚有人把
        "系统支持的 − actual" 塞进去，于是"用户没勾封面而封面不存在"也算降级。
        没有 setter，就没有那条路。
        """
        return sorted(self.expected_artifacts - self.actual_artifacts)

    @property
    def degraded(self) -> bool:
        return bool(self.missing)

    # ---- 终态（每 run 恰好一次）----

    @property
    def outcome_emitted(self) -> bool:
        return self._outcome_emitted

    def finish(self, outcome: str, **fields: Any) -> bool:
        """emit 本 run 唯一的 `outcome` 事件。返回是否真的发出。

        exactly-once 由结构保证，不是靠打补丁：`TaskTrace` 本身就是 run 的身份对象
        （`new_run()` 才会重置这个标志），而 `finish()` 是全项目唯一的 outcome 出口。
        这和被否掉的 `Diagnosis._logged` 有本质区别 —— `Diagnosis` 是会被复制、
        重建、跨线程传递的值对象，标志挡不住二次诊断；run 的身份对象不会。

        第二次调用不会静默：重复宣布终态是契约违规，本身就值得在日志里看见。

        `recovered` / `degraded` / `missing` 由本方法从累积状态自行填入，调用方
        传同名字段会被忽略 —— 它们是派生量，不接受外部裁决（硬规则 3）。
        """
        if outcome not in OUTCOMES:
            outcome = "failed"  # 宁可报失败，也不要把未知终态记成成功
        with self._lock:
            if self._outcome_emitted:
                emit_event(
                    "signal",
                    trace=self,
                    level="WARNING",
                    _depth=2,
                    code="duplicate_outcome_suppressed",
                    attempted_outcome=outcome,
                )
                return False
            self._outcome_emitted = True
        emit_event(
            "outcome",
            trace=self,
            level="ERROR" if outcome == "failed" else "INFO",
            _depth=2,
            fields=fields,
            outcome=outcome,
            recovered=self.recovered,
            recovery=self.recovery or None,
            degraded=self.degraded,
            missing=self.missing,
        )
        return True


def new_flow(stage: str = "parse") -> FlowTrace:
    """铸一个新的操作链标识。由发起解析的对话框调用。"""
    return FlowTrace(stage=stage)


# ---- 环境 flow（只给 youtube/ 那一层用）----

#: 当前线程正在服务哪条操作链。
#:
#: **为什么需要它。** `youtube/yt_dlp_cli.py` 是唯一看得见 yt-dlp 原始输出的地方
#: （`youtube_service` 拿到的已经是 `json.loads` 之后的 dict），所以"解析成功但带警告"
#: 这类 `signal` 只能在那里落。而从对话框到那里要穿过 `youtube_service` 的 13 个
#: `run_dump_single_json` 调用点和它上面十几个公开方法 —— 为了一条日志给整个服务层
#: 加一个贯穿参数，代价和收益完全不成比例。
#:
#: **为什么 ContextVar 而不是全局变量。** CPython 里每个线程有自己的顶层 Context，
#: 线程启动时**不会**继承父线程的值。四个解析 worker 各在自己的 QThread 里跑，
#: 播放列表的逐行深解析还会有 2-3 个池线程并发 —— 用全局变量它们会互相覆盖，
#: 用 ContextVar 天然按线程隔离。
#:
#: **它是纯观测降级路径，不是取 trace 的正道。** 有 trace 可传的地方一律显式传参
#: （`FlowTrace` 是 `InfoExtractWorker` 等类的构造参数就是这个原因）。这里未绑定时
#: 返回 None，事件只带 session 标识，绝不为了"找到一个 flow"去猜。
_CURRENT_FLOW: ContextVar[FlowTrace | None] = ContextVar("fluentytdl_current_flow", default=None)


def bind_current_flow(trace: FlowTrace | None) -> None:
    """把本线程的环境 flow 设为 `trace`。在 worker 的 `run()` 里调（**必须在新线程内**）。

    不提供 reset：QThreadPool 会复用线程，下一个 runnable 在自己的 `run()` 开头
    重新绑定即可覆盖。刻意不做上下文管理器 —— `run()` 里 `with` 一层会把整个函数
    体再缩进一级，而这个值在 `run()` 返回后本来就不该有人读。
    """
    _CURRENT_FLOW.set(trace)


def current_flow() -> FlowTrace | None:
    """读本线程的环境 flow，未绑定时返回 None。"""
    try:
        return _CURRENT_FLOW.get()
    except LookupError:  # pragma: no cover - 有 default 时不会发生，纯防御
        return None
