from __future__ import annotations

import logging

from PySide6.QtCore import QMutex, QMutexLocker, QObject, QRunnable, QThreadPool, Signal, Slot

from ..models.yt_dto import YtMediaDTO
from ..youtube.youtube_service import YoutubeServiceOptions
from .workers import EntryDetailWorker

logger = logging.getLogger(__name__)


class MetadataFetchRunnable(QRunnable):
    """
    A lightweight QRunnable that wraps our existing EntryDetailWorker logic.
    Used for fetching video details concurrently within a QThreadPool.
    """

    def __init__(
        self,
        task_id: str,
        url: str,
        options: YoutubeServiceOptions | None,
        vr_mode: bool,
        signals: AsyncExtractorSignals,
    ):
        super().__init__()
        self.task_id = task_id

        # Internally reuse the worker logic but hook it up to our shared signals
        self.worker = EntryDetailWorker(
            row=0,  # Dummy row since we use task_id based tracking now
            url=url,
            options=options,
            vr_mode=vr_mode,
        )
        self.signals = signals

        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)

    def _on_finished(self, _, dto: YtMediaDTO) -> None:
        self.signals.task_finished.emit(self.task_id, dto)

    def _on_error(self, _, err_msg: str) -> None:
        self.signals.task_error.emit(self.task_id, err_msg)

    def cancel(self) -> None:
        self.worker.cancel()

    @Slot()
    def run(self) -> None:
        self.signals.task_started.emit(self.task_id)
        self.worker.run()


class AsyncExtractorSignals(QObject):
    task_started = Signal(str)  # task_id
    task_finished = Signal(str, YtMediaDTO)  # task_id, dto
    task_error = Signal(str, str)  # task_id, error_msg


class AsyncExtractManager(QObject):
    """
    Manages concurrent yt-dlp metadata extraction tasks (e.g. for Playlist items).
    Enforces a strict maximum concurrency limit to avoid HTTP 429 Too Many Requests.
    Uses two FIFO queues:
    - foreground queue: viewport-visible items, always consumed first
    - background queue: lazy backfill items, consumed only when foreground is empty
    Both queues preserve natural top-to-bottom order.
    Background items use a lower concurrency limit to avoid competing with foreground.
    """

    def __init__(
        self, max_concurrent: int = 3, bg_concurrent: int = 1, parent: QObject | None = None
    ):
        super().__init__(parent)
        self.signals = AsyncExtractorSignals()

        self.max_concurrent = max_concurrent
        self.bg_concurrent = bg_concurrent
        self._thread_pool = QThreadPool()
        # We handle queuing manually, so tell the pool to only accept what we give it
        self._thread_pool.setMaxThreadCount(max_concurrent)

        self._mutex = QMutex()

        # Currently running tasks: dict[task_id, tuple[Runnable, is_foreground]]
        self._active_tasks: dict[str, tuple[MetadataFetchRunnable, bool]] = {}

        # Foreground FIFO queue for visible rows.
        self._foreground_queue: list[tuple[str, str, YoutubeServiceOptions | None, bool]] = []
        self._foreground_set: set[str] = set()

        # Background FIFO queue for lazy backfill rows.
        self._pending_queue: list[tuple[str, str, YoutubeServiceOptions | None, bool]] = []
        self._pending_set: set[str] = set()

        self.signals.task_finished.connect(self._cleanup_task)
        self.signals.task_error.connect(self._cleanup_task)

    def enqueue(
        self,
        task_id: str,
        url: str,
        options: YoutubeServiceOptions | None = None,
        vr_mode: bool = False,
        high_priority: bool = False,
    ) -> None:
        """Add a metadata extraction task to the foreground/background FIFO queues."""
        with QMutexLocker(self._mutex):
            if task_id in self._active_tasks:
                return

            if task_id in self._foreground_set:
                return

            if task_id in self._pending_set:
                if not high_priority:
                    return
                for i, (tid, _, _, _) in enumerate(self._pending_queue):
                    if tid == task_id:
                        item = self._pending_queue.pop(i)
                        self._pending_set.discard(task_id)
                        self._foreground_queue.append(item)
                        self._foreground_set.add(task_id)
                        break
            else:
                if high_priority:
                    self._foreground_queue.append((task_id, url, options, vr_mode))
                    self._foreground_set.add(task_id)
                else:
                    self._pending_queue.append((task_id, url, options, vr_mode))
                    self._pending_set.add(task_id)

            logger.info(
                f"AsyncExtractManager enqueued task {task_id} (high_priority={high_priority}). Queues: FG={len(self._foreground_queue)} BG={len(self._pending_queue)}"
            )
            self._pump_queue()

    def active_count(self) -> int:
        with QMutexLocker(self._mutex):
            return len(self._active_tasks)

    def has_capacity(self) -> bool:
        with QMutexLocker(self._mutex):
            return len(self._active_tasks) < self.max_concurrent

    def _start_task(
        self,
        task_id: str,
        url: str,
        options: YoutubeServiceOptions | None,
        vr_mode: bool,
        is_foreground: bool,
    ) -> None:
        """Internal helper to create a runnable and start it immediately. Assumes lock is held."""
        logger.info(f"AsyncExtractManager starting task {task_id} (fg={is_foreground})")
        runnable = MetadataFetchRunnable(task_id, url, options, vr_mode, self.signals)
        runnable.setAutoDelete(True)  # Ensure auto delete is explicit
        self._active_tasks[task_id] = (runnable, is_foreground)
        self._thread_pool.start(runnable)

    def _pump_queue(self) -> None:
        """Starts tasks from the pending queue if slots are available. Assumes lock is held."""
        # 统计当前前台和后台任务数量
        fg_active = sum(1 for _, is_fg in self._active_tasks.values() if is_fg)
        bg_active = sum(1 for _, is_fg in self._active_tasks.values() if not is_fg)

        while len(self._active_tasks) < self.max_concurrent:
            if self._foreground_queue:
                task_id, url, options, vr_mode = self._foreground_queue.pop(0)
                self._foreground_set.discard(task_id)
                self._start_task(task_id, url, options, vr_mode, is_foreground=True)
                fg_active += 1
            elif self._pending_queue:
                # 只有当总活跃数不足 bg_concurrent 时，才允许启动后台任务
                # 注意：如果前台任务已经占满了 >= bg_concurrent 甚至到达 max_concurrent，
                # 后台任务只能被饿死（等待）直到总数降下来。这符合降级逻辑。
                total_active = len(self._active_tasks)
                if total_active < self.bg_concurrent:
                    task_id, url, options, vr_mode = self._pending_queue.pop(0)
                    self._pending_set.discard(task_id)
                    self._start_task(task_id, url, options, vr_mode, is_foreground=False)
                    bg_active += 1
                else:
                    # 前台空了，但当前并发额度（对于后台而言）已满
                    break
            else:
                break

    def cancel(self, task_id: str) -> None:
        """Cancel a specific extraction task by ID (e.g. when scrolling out of view)."""
        with QMutexLocker(self._mutex):
            # Check if it's currently running
            task_tuple = self._active_tasks.get(task_id)
            if task_tuple:
                runnable, _ = task_tuple
                runnable.cancel()
                # Do NOT remove from _active_tasks here. Let _cleanup_task handle it
                # when the thread actually finishes so we don't start a new thread
                # while the cancelled one is still shutting down.
                return

            for i, (tid, _, _, _) in enumerate(self._foreground_queue):
                if tid == task_id:
                    self._foreground_queue.pop(i)
                    self._foreground_set.discard(task_id)
                    return

            for i, (tid, _, _, _) in enumerate(self._pending_queue):
                if tid == task_id:
                    self._pending_queue.pop(i)
                    self._pending_set.discard(task_id)
                    break

    def cancel_all(self) -> None:
        """Cancel all pending and running extraction tasks."""
        with QMutexLocker(self._mutex):
            self._foreground_queue.clear()
            self._foreground_set.clear()
            self._pending_queue.clear()
            self._pending_set.clear()
            for runnable, _ in self._active_tasks.values():
                runnable.cancel()
            # _cleanup_task will handle emptying _active_tasks as they finish

    @Slot(str)
    @Slot(str, dict)
    @Slot(str, str)
    def _cleanup_task(self, task_id: str, *args) -> None:
        """Remove finished/errored tasks from tracker and pump the queue."""
        logger.info(f"AsyncExtractManager cleanup task {task_id}")
        with QMutexLocker(self._mutex):
            if task_id in self._active_tasks:
                del self._active_tasks[task_id]
            # When a task finishes, pump the queue to start the next one
            self._pump_queue()
