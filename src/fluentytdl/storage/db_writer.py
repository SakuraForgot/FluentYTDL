"""
TaskDB 异步写入器

将高频的 update_task_status 操作从主线程迁移到独立后台线程，
避免 SQLite I/O 阻塞 UI 事件循环。

架构：
- 主线程通过 enqueue_* 方法将写操作投递到内部队列
- 后台线程持续消费队列并批量执行 SQLite 写入
- 支持优雅关闭：flush 完所有待写数据后退出
"""

from __future__ import annotations

import logging
from queue import Empty, Queue
from threading import Thread

from ..storage.task_db import task_db

logger = logging.getLogger(__name__)


class TaskDBWriter:
    """
    独立线程的 TaskDB 写入代理。

    主线程调用 enqueue_status / enqueue_result 只是往 Queue 里放一个 tuple，
    纳秒级完成，绝不阻塞 UI。后台 daemon 线程消费队列并执行实际 SQLite 写入。
    """

    def __init__(self) -> None:
        self._queue: Queue[tuple | None] = Queue()
        self._thread = Thread(target=self._run, daemon=True, name="TaskDBWriter")
        self._thread.start()

    def enqueue_status(self, db_id: int, state: str, pct: float, msg: str) -> None:
        """投递状态更新（高频，~5Hz/任务）"""
        self._queue.put(("status", db_id, state, pct, msg))

    def enqueue_result(self, db_id: int, path: str, fsize: int) -> None:
        """投递最终输出路径（低频，每任务 1 次）"""
        self._queue.put(("result", db_id, path, fsize))

    def enqueue_metadata(self, db_id: int, title: str, thumb: str) -> None:
        """投递元数据更新（低频，每任务 1 次）"""
        self._queue.put(("metadata", db_id, title, thumb))

    def flush_and_stop(self, timeout: float = 3.0) -> None:
        """优雅关闭：发送毒丸信号，等待队列清空"""
        self._queue.put(None)  # 毒丸
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        """后台消费循环"""
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                continue

            if item is None:
                # 毒丸：消费剩余项后退出
                self._drain_remaining()
                break

            self._process(item)

    def _drain_remaining(self) -> None:
        """关闭前清空队列中剩余的写操作"""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    self._process(item)
            except Empty:
                break

    def _process(self, item: tuple) -> None:
        """执行单条写操作"""
        try:
            op = item[0]
            if op == "status":
                _, db_id, state, pct, msg = item
                task_db.update_task_status(db_id, state, pct, msg)
            elif op == "result":
                _, db_id, path, fsize = item
                task_db.update_task_result(db_id, path, fsize)
            elif op == "metadata":
                _, db_id, title, thumb = item
                task_db.update_task_metadata(db_id, title, thumb)
        except Exception as e:
            logger.warning(f"[TaskDBWriter] 写入异常: {e}")


# 全局单例
db_writer = TaskDBWriter()
