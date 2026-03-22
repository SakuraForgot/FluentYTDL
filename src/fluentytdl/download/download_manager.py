from __future__ import annotations

import json
import os
from collections import deque
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

from ..core.config_manager import config_manager
from ..storage.db_writer import db_writer
from ..storage.task_db import task_db
from .workers import DownloadWorker


class DownloadManager(QObject):
    # 通知 UI：任务列表/状态变化（新增/结束/删除/暂停等）
    task_updated = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.active_workers: list[DownloadWorker] = []
        self._pending_workers: deque[DownloadWorker] = deque()
        self.load_unfinished_tasks()

    def load_unfinished_tasks(self) -> None:
        """从 TaskDB 加载未能完成的会话（崩溃或退出留下的）"""
        tasks = task_db.get_all_tasks()
        # tasks 是按照 created_at DESC 排序的，反转以按先后顺序加载
        for row in reversed(tasks):
            state = row.get("state", "queued")
            if state in ("completed", "error"):
                continue

            # 如果重启前是运行状态，自动降级为暂停，防止重启瞬间并发爆炸
            if state in ("running", "downloading"):
                state = "paused"
                task_db.update_task_status(
                    row["id"], state, row.get("progress", 0.0), "⏸️ 下载已暂停 (应用重启)"
                )

            opts = json.loads(row.get("ydl_opts_json", "{}"))
            cached = {"title": row.get("title", ""), "thumbnail": row.get("thumbnail_url", "")}

            worker = self.create_worker(
                row["url"], opts, cached_info=cached, restore_db_id=row["id"]
            )

            # 手工同步 Worker 上下文使其与 DB 呈现一致
            worker._final_state = state
            worker.progress_val = row.get("progress", 0.0)
            worker.status_text = row.get("status_text", "")
            worker.v_title = row.get("title", "")
            worker.v_thumbnail = row.get("thumbnail_url", "")
            worker.output_path = row.get("output_path", "")
            worker.total_bytes = row.get("file_size", 0)

            if state == "queued":
                self._pending_workers.append(worker)
            elif state == "paused":
                # 对于 paused 状态，调用 pause 会设置红绿灯
                worker._cancel_event.clear()
                worker._pause_event.clear()

        # 最后不 pump()，要等 UI 初始化完后再由其他流程触发或用户手动恢复

    def _max_concurrent(self) -> int:
        try:
            n = int(config_manager.get("max_concurrent_downloads", 3) or 3)
        except Exception:
            n = 3
        # Use 32-bit int max to avoid overflow in UI / Qt validators.
        return max(1, min(2_147_483_647, n))

    def _running_count(self) -> int:
        return sum(1 for w in self.active_workers if w.isRunning())

    def running_count(self) -> int:
        return self._running_count()

    def pending_count(self) -> int:
        return len(self._pending_workers)

    def has_active_tasks(self) -> bool:
        return self.running_count() > 0 or self.pending_count() > 0

    def _remove_from_pending(self, worker: DownloadWorker) -> None:
        if not self._pending_workers:
            return
        try:
            self._pending_workers = deque([w for w in self._pending_workers if w is not worker])
        except Exception:
            pass

    def is_queued(self, worker: DownloadWorker) -> bool:
        return any(w is worker for w in self._pending_workers)

    def pump(self) -> None:
        """Start queued downloads until reaching the concurrency limit."""

        limit = self._max_concurrent()
        while self._pending_workers and self._running_count() < limit:
            w = self._pending_workers.popleft()
            try:
                if w.isRunning() or w.isFinished():
                    continue
                w.start()
            except Exception:
                continue
        self.task_updated.emit()

    def create_worker(
        self,
        url: str,
        opts: dict[str, Any],
        cached_info: dict[str, Any] | None = None,
        restore_db_id: int = 0,
    ) -> DownloadWorker:
        worker = DownloadWorker(url, opts, cached_info=cached_info)

        # 1. 登记入库，建立 Worker 的持久化主键
        if restore_db_id > 0:
            worker.db_id = restore_db_id
        else:
            db_id = task_db.insert_task(url, opts)
            worker.db_id = db_id
            # 只有新创建的才录入缓存元数据，恢复的不用覆盖
            if cached_info:
                t_title = cached_info.get("title", "")
                t_thumb = cached_info.get("thumbnail", "")
                db_writer.enqueue_metadata(db_id, t_title, str(t_thumb) if t_thumb else "")

        # 3. 建立单写者“过桥”连接 (强制在 QObject 的宿主线程即主线程执行写操作)
        def _on_unified_status(state: str, pct: float, msg: str):
            db_writer.enqueue_status(worker.db_id, state, pct, msg)

        def _on_output_ready(path: str):
            fsize = 0
            if path and os.path.exists(path):
                fsize = os.path.getsize(path)
            if fsize == 0:
                fsize = getattr(worker, "total_bytes", 0)
            db_writer.enqueue_result(worker.db_id, path, fsize)

        def _on_completed():
            path = getattr(worker, "output_path", "")
            fsize = 0
            if path and os.path.exists(path):
                fsize = os.path.getsize(path)
            if fsize == 0:
                fsize = getattr(worker, "total_bytes", 0)
            if path:
                db_writer.enqueue_result(worker.db_id, path, fsize)
            self.task_updated.emit()

        worker.unified_status.connect(_on_unified_status, Qt.ConnectionType.QueuedConnection)
        worker.output_path_ready.connect(_on_output_ready, Qt.ConnectionType.QueuedConnection)

        self.active_workers.append(worker)

        # When a worker ends, free a slot and pump queued tasks.
        try:
            worker.finished.connect(lambda: self._on_worker_finished(worker))
        except Exception:
            pass
        worker.completed.connect(_on_completed)
        worker.cancelled.connect(lambda: self.task_updated.emit())
        worker.error.connect(lambda *_: self.task_updated.emit())
        return worker

    def _on_worker_finished(self, worker: DownloadWorker) -> None:
        # Worker ended => free slot
        self._remove_from_pending(worker)
        self.pump()

    def start_worker(self, worker: DownloadWorker) -> bool:
        """Start worker if a slot is available; otherwise queue it."""

        self._remove_from_pending(worker)

        if worker.isRunning():
            return True
        if worker.isFinished():
            return False

        if self._running_count() < self._max_concurrent():
            try:
                worker.start()
                self.task_updated.emit()
                return True
            except Exception:
                return False

        self._pending_workers.append(worker)
        self.task_updated.emit()
        return False

    def stop_all(self) -> None:
        # Clear queued tasks first (so they won't start after pausing).
        self._pending_workers.clear()
        for worker in list(self.active_workers):
            if worker.isRunning():
                worker.stop()

    def shutdown(self, grace_ms: int = 2000) -> bool:
        """Stop all workers and wait for them to exit.

        Returns True if all workers have stopped within the grace period.
        """

        self.stop_all()

        all_stopped = True
        for worker in list(self.active_workers):
            if not worker.isRunning():
                continue
            try:
                # Try graceful wait first.
                if not worker.wait(grace_ms):
                    all_stopped = False
                    # Last resort: terminate the thread.
                    try:
                        worker.terminate()
                    except Exception:
                        pass
                    try:
                        worker.wait(500)
                    except Exception:
                        pass
            except Exception:
                all_stopped = False

        # 确保所有待写数据落盘后再退出
        db_writer.flush_and_stop(timeout=3.0)

        return all_stopped

    def start_all(self) -> None:
        # QThread 结束后不能重用；“继续/重试”应由 UI 重建 worker。
        self.pump()

    def remove_worker(self, worker: DownloadWorker) -> None:
        self._remove_from_pending(worker)
        if worker in self.active_workers:
            if worker.isRunning():
                worker.stop()
                worker.wait()
            self.active_workers.remove(worker)
            self.task_updated.emit()


download_manager = DownloadManager()
