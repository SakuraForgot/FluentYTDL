"""
TaskDB 异步写入器

将高频的 update_task_status 操作从主线程迁移到独立后台线程，
避免 SQLite I/O 阻塞 UI 事件循环。

架构：
- 主线程通过 enqueue_* 方法将写操作投递到内部队列
- 后台线程阻塞等第一条，再把此刻已排好的项一并取走，合并去重后包成**一个事务**提交
- 支持优雅关闭：flush 完所有待写数据后退出

为什么必须批量：状态更新是 ~5Hz/任务，逐条 commit 就是每秒每任务一次 WAL 提交。
任务列表与历史融合之后同时在库里的行数上升一个量级，这是最直接的写放大来源。
"""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread

from fluentytdl.utils.localized_log import log_text

from ..storage.task_db import task_db
from ..utils.logger import logger


class TaskDBWriter:
    """
    独立线程的 TaskDB 写入代理。

    主线程调用 enqueue_status / enqueue_result 只是往 Queue 里放一个 tuple，
    纳秒级完成，绝不阻塞 UI。后台 daemon 线程消费队列并执行实际 SQLite 写入。
    """

    # 单个事务最多合并多少条写操作。上限的意义是「别让一个事务长时间占住
    # TaskDB._write_lock」—— 主线程也会同步写库（插入新任务、删除记录），
    # 200 条 UPDATE 在 WAL 里是亚毫秒级，而它替换掉的是 200 次独立 commit。
    _BATCH_MAX = 200

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

    def enqueue_quality(
        self, db_id: int, actual_height: int, target_height: int, deviation: str
    ) -> None:
        """投递质量检查结果"""
        self._queue.put(("quality", db_id, actual_height, target_height, deviation))

    def enqueue_run_identity(
        self, db_id: int, session_id: str, run_id: str, flow_id: str = ""
    ) -> None:
        """投递观测标识（低频，每次 run 开始 1 次）。

        走写入线程而不是在 worker 线程里直接调 `task_db`：run 刚起步时主线程正忙着
        建 UI 行、写队列里还压着一批状态更新，同步取 `_write_lock` 会让下载线程白等。
        这一列只有下次启动的恢复审计会读，晚几十毫秒落盘没有任何影响。
        """
        self._queue.put(("run_identity", db_id, session_id, run_id, flow_id))

    def flush_and_stop(self, timeout: float = 3.0) -> None:
        """优雅关闭：发送毒丸信号，等待队列清空"""
        self._queue.put(None)  # 毒丸
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        """后台消费循环：阻塞等第一条，然后把已排好的一并取走，合成一个事务。"""
        while True:
            try:
                first = self._queue.get(timeout=0.5)
            except Empty:
                continue

            if first is None:
                # 毒丸：消费剩余项后退出
                self._drain_remaining()
                break

            batch, poisoned = self._collect(first)
            self._process_batch(batch)
            if poisoned:
                self._drain_remaining()
                break

    def _collect(self, first: tuple) -> tuple[list[tuple], bool]:
        """取走队列里**此刻已经排好**的项（含 first，最多 _BATCH_MAX 条）。

        故意不做「再等 X 毫秒攒一批」：等待会给状态更新加上一段固定延迟，而队列本来
        就在高频写入时自然堆积 —— 忙的时候批次自然变大，闲的时候一条就是一条。
        返回值第二项表示是否在途中撞到毒丸（撞到就不再往后取）。
        """
        batch = [first]
        while len(batch) < self._BATCH_MAX:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is None:
                return batch, True
            batch.append(item)
        return batch, False

    def _drain_remaining(self) -> None:
        """关闭前清空队列中剩余的写操作"""
        rest: list[tuple] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is not None:
                rest.append(item)
        self._process_batch(rest)

    def _process_batch(self, items: list[tuple]) -> None:
        """把一批写操作合并去重后放进**一个**事务提交。"""
        if not items:
            return
        if len(items) == 1:
            self._process(items[0])
            return

        merged = self._coalesce(items)
        try:
            with task_db.batch():
                for item in merged:
                    self._process(item)
        except Exception as e:
            # 事务已整批回滚 —— 退化成逐条写，别让一次异常吞掉整批状态更新
            log_text(logger, "warning", "[TaskDBWriter] 批量写入回滚，改为逐条重试: {0}", e)
            for item in merged:
                self._process(item)

    @staticmethod
    def _coalesce(items: list[tuple]) -> list[tuple]:
        """同一 `(op, db_id)` 只保留最后一条。

        五种写操作都是**整列覆盖**（status 覆盖 state/progress/status_text，
        metadata / result / quality / run_identity 各覆盖自己那几列），同键的中间态
        本来就会被后一条盖掉，所以 5Hz 的进度更新在一个批次里能塌成 1 条 UPDATE。

        保留的是**最后一次出现的位置**（先 pop 再插入），于是不同 op 之间的相对先后
        与原队列一致 —— `metadata` 和 `result` 都会写 output_path，顺序反过来会让
        已完成任务的最终路径被解析期的空路径覆盖。
        """
        merged: dict[tuple, tuple] = {}
        for item in items:
            key = (item[0], item[1])
            merged.pop(key, None)
            merged[key] = item
        return list(merged.values())

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
            elif op == "quality":
                _, db_id, actual_height, target_height, deviation = item
                task_db.update_task_quality(db_id, actual_height, target_height, deviation)
            elif op == "run_identity":
                _, db_id, session_id, run_id, flow_id = item
                task_db.update_task_run_identity(db_id, session_id, run_id, flow_id)
        except Exception as e:
            log_text(logger, "warning", "[TaskDBWriter] 写入异常: {0}", e)


# 全局单例
db_writer = TaskDBWriter()
