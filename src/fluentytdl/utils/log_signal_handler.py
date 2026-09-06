"""
日志信号处理器

将 loguru 日志通过 Qt Signal 转发，实现实时日志显示
"""

from __future__ import annotations

from loguru import logger
from PySide6.QtCore import QObject, Signal


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
    # 信号: Observability Event 的扁平 dict（含本处补的 `_time` / `_level`）
    event_received = Signal(dict)

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

    def install(self, level: str = "DEBUG") -> None:
        """安装到 loguru

        Args:
            level: 最低日志级别，默认 DEBUG
        """
        if self._sink_id is not None:
            return  # 已安装

        self._sink_id = logger.add(
            self._emit_log,
            level=level,
            format="{message}",  # 我们自己解析 record
            enqueue=True,  # 异步
        )

    def uninstall(self) -> None:
        """从 loguru 移除"""
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except ValueError:
                pass  # sink 已被移除
            self._sink_id = None

    def _emit_log(self, message) -> None:
        """loguru sink 回调"""
        record = message.record

        time_str = record["time"].strftime("%H:%M:%S")
        level = record["level"].name
        module = record.get("name", "") or ""
        msg = record["message"]

        self.log_received.emit(time_str, level, module, msg)

        # 结构化事件另发一路。`_time` / `_level` 带下划线前缀，标明它们是**本处补的**
        # 展示辅助字段，不是事件字段本身（事件字段的封闭集合在 `observability/events.py`）。
        # 整段吞异常：日志转发绝不能把业务线程绊倒。
        try:
            payload = record["extra"].get("fytdl")
            if isinstance(payload, dict):
                self.event_received.emit(dict(payload, _time=time_str, _level=level))
        except Exception:
            pass

    @property
    def is_installed(self) -> bool:
        """是否已安装"""
        return self._sink_id is not None


# 全局单例
log_signal_handler = LogSignalHandler()
