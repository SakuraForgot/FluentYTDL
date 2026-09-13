"""
日志信号处理器

将 loguru 日志通过 Qt Signal 转发，实现实时日志显示
"""

from __future__ import annotations

import threading
from collections import deque

from loguru import logger
from PySide6.QtCore import QObject, QTimer, Signal

from .log_runtime import health_snapshot, record_failure


class LogSignalHandler(QObject):
    """将 loguru 日志转发为 Qt Signal

    使用方式:
        handler = LogSignalHandler()
        handler.log_received.connect(your_slot)
        handler.install()

        # 不再需要时
        handler.uninstall()

    两条信号刻意分开：`log_received` 是**人读的一行文本**，`event_received` 是
    Observability Event 的**结构化 dict**（`logger.bind(fytdl=...)` 放进 `extra` 的
    那份）。同一条记录会同时走两条 —— 文本页要按时间顺序看全部日志，时间线页要按
    flow/task/run 分组，两种需求靠同一份文本都满足不了。

    本模块属 Foundation 层，**不 import `observability`**（`observability` 依赖
    `utils.logger`，反向 import 必成环）。这里只把 `extra["fytdl"]` 这个纯 dict
    原样转出去，不碰事件层的任何类型。
    """

    # 信号: (时间, 级别, 模块, 消息)
    log_received = Signal(str, str, str, str)
    display_record_received = Signal(dict)
    # 信号: Observability Event 的扁平 dict（含本处补的 `_time` / `_level`）
    event_received = Signal(dict)
    records_received = Signal(list)
    health_changed = Signal(dict)

    _instance: LogSignalHandler | None = None

    def __new__(cls) -> LogSignalHandler:
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._sink_id: int | None = None
        self._initialized = True
        self._pending = deque()
        self._pending_lock = threading.Lock()
        self._timer = QTimer(self)
        self._timer.setInterval(75)
        self._timer.timeout.connect(self._drain)
        self._last_health = None
        self._clients = 0
        self._release_sink = False

    def acquire(self):
        if self._clients == 0:
            self._release_sink = not self.is_installed
        self._clients += 1
        self.install()

    def release(self):
        self._clients = max(0, self._clients - 1)
        if self._clients == 0 and self._release_sink:
            self.uninstall()

    def install(self, level: str = "DEBUG") -> None:
        """安装到 loguru

        Args:
            level: 最低日志级别，默认 DEBUG
        """
        if self._sink_id is not None:
            return  # 已安装

        self._timer.start()
        self._sink_id = logger.add(
            self._emit_log,
            level=level,
            format="{message}",  # 我们自己解析 record
            enqueue=True,  # 异步
            diagnose=False,
        )

    def uninstall(self) -> None:
        """从 loguru 移除"""
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except ValueError:
                pass  # sink 已被移除
            self._sink_id = None
            self._timer.stop()
            with self._pending_lock:
                self._pending.clear()

    def _emit_log(self, message) -> None:
        """loguru sink 回调"""
        record = message.record

        extra = record["extra"]
        payload = dict(extra.get("localized") or {})
        payload.update(
            id=extra.get("record_id", payload.get("id")),
            time=record["time"].isoformat(),
            _ts=record["time"].timestamp(),
            level=record["level"].name,
            module=record.get("name") or "",
            raw=record["message"],
            session=extra.get("session"),
            origin=extra.get("origin"),
            event=extra.get("fytdl"),
        )
        payload["exception"] = (extra.get("display_record") or {}).get("exception", "")
        with self._pending_lock:
            if len(self._pending) >= 4000:
                # Only the display queue is lossy. Disk sinks are independent.
                victim = next(
                    (
                        i
                        for i, r in enumerate(self._pending)
                        if (r.get("event") or {}).get("kind") not in ("diagnosis", "outcome")
                        and r["level"] not in ("ERROR", "CRITICAL")
                    ),
                    0,
                )
                del self._pending[victim]
                record_failure("display_dropped")
            self._pending.append(payload)

    def _drain(self):
        with self._pending_lock:
            batch = [self._pending.popleft() for _ in range(min(150, len(self._pending)))]
        if batch:
            self.records_received.emit(batch)
        # Legacy consumers keep their original signals; the viewer uses batches.
        for record in batch:
            if record.get("message_id"):
                self.display_record_received.emit(record)
            else:
                self.log_received.emit(
                    record["time"], record["level"], record["module"], record["raw"]
                )
            if isinstance(record.get("event"), dict):
                self.event_received.emit(
                    dict(
                        record["event"],
                        _event_id=record["id"],
                        _ts=record["_ts"],
                        _time=record["time"],
                        _level=record["level"],
                    )
                )
        health = health_snapshot()
        if health != self._last_health:
            self._last_health = health
            self.health_changed.emit(health)

    @property
    def is_installed(self) -> bool:
        """是否已安装"""
        return self._sink_id is not None


# 全局单例
log_signal_handler = LogSignalHandler()
