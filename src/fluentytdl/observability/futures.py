"""线程边界的异常黑洞。

两种机制**不能混为一谈**：

- `threading.Thread` 里的未捕获异常 → 由 `threading.excepthook` 兜（装在
  `utils/logger.py`，因为那里已经装了 `sys.excepthook`，且不能反向依赖本包）。
- `ThreadPoolExecutor` 里的异常 → **`threading.excepthook` 管不着**。池内异常被
  装进 `Future` 对象，工作线程本身正常结束，钩子永远不会触发。没人调
  `future.result()` 的话，异常就彻底消失了。

`workers.py` 的 `ChannelTab` 线程池本来就在 `fut.result()` 处 try/except，改走这里
是为了让"池内炸了"和"业务失败"在日志里长得不一样。
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any

from loguru import logger

from fluentytdl.utils.localized_log import log_text

from .events import emit_event


def observe_future(
    future: Future,
    trace: Any = None,
    *,
    label: str = "",
    stage: str | None = None,
) -> Future:
    """给 `Future` 挂一个观测回调，返回原 future（便于链式使用）。

    回调只**观测**，不改变 future 的状态，也不吞掉异常 —— 调用方照旧可以
    `fut.result()` 拿到异常并按业务逻辑处理。
    """

    def _on_done(fut: Future) -> None:
        try:
            if fut.cancelled():
                emit_event(
                    "signal",
                    trace=trace,
                    level="DEBUG",
                    stage=stage,
                    code="future_cancelled",
                    label=label or None,
                )
                return
            exc = fut.exception()
        except Exception:
            # future.exception() 自己可能抛 CancelledError
            return
        if exc is None:
            return
        emit_event(
            "signal",
            trace=trace,
            level="ERROR",
            stage=stage,
            code="future_exception",
            label=label or None,
            error=exc,
        )
        try:
            # 堆栈只进文本日志：事件层记结构化字段，不记多行 traceback
            log_text(
                logger.opt(exception=exc),
                "error",
                "[Futures] 线程池任务异常 label={}",
                label or "-",
            )
        except Exception:
            pass

    try:
        future.add_done_callback(_on_done)
    except Exception:
        pass
    return future


def observe_futures(
    futures: dict[Future, str] | list[Future],
    trace: Any = None,
    *,
    stage: str | None = None,
) -> None:
    """批量挂观测。传 dict 时 value 当作 label（对应 `{executor.submit(...): tab}` 的写法）。"""
    if isinstance(futures, dict):
        for fut, label in futures.items():
            observe_future(fut, trace, label=str(label), stage=stage)
        return
    for fut in futures:
        observe_future(fut, trace, stage=stage)
