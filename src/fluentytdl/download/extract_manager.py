from __future__ import annotations

from PySide6.QtCore import QMutex, QMutexLocker, QObject, QRunnable, QThreadPool, Signal, Slot

from ..utils.logger import logger
from ..youtube.youtube_service import YoutubeServiceOptions
from .workers import EntryDetailWorker


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
        read_cache: bool = True,
    ):
        super().__init__()
        self.task_id = task_id

        # Internally reuse the worker logic but hook it up to our shared signals
        self.worker = EntryDetailWorker(
            row=0,  # Dummy row since we use task_id based tracking now
            url=url,
            options=options,
            vr_mode=vr_mode,
            read_cache=read_cache,
        )
        self.signals = signals

        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)

    def _on_finished(self, _, info: dict) -> None:
        self.signals.task_finished.emit(self.task_id, info)

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
    task_finished = Signal(str, dict)  # task_id, dict
    task_error = Signal(str, str)  # task_id, error_msg


class AsyncExtractManager(QObject):
    """
    Manages concurrent yt-dlp metadata extraction tasks (e.g. for Playlist items).
    Delegates queuing and priority logic to higher-level components (like PlaylistScheduler).
    This class is merely a thin wrapper around QThreadPool to manage concurrency limits
    and cancellation of active tasks.
    """

    def __init__(self, max_concurrent: int = 3, parent: QObject | None = None):
        super().__init__(parent)
        self.signals = AsyncExtractorSignals()

        self.max_concurrent = max_concurrent
        self._thread_pool = QThreadPool()
        # Enforce strict maximum concurrency limit to avoid HTTP 429 Too Many Requests.
        self._thread_pool.setMaxThreadCount(max_concurrent)

        self._mutex = QMutex()

        # Currently running tasks: dict[task_id, Runnable]
        self._active_tasks: dict[str, MetadataFetchRunnable] = {}

        self.signals.task_finished.connect(self._cleanup_task)
        self.signals.task_error.connect(self._cleanup_task)

    def enqueue(
        self,
        task_id: str,
        url: str,
        options: YoutubeServiceOptions | None = None,
        vr_mode: bool = False,
        read_cache: bool = True,
        **kwargs,  # Accept legacy kwargs like high_priority to avoid breaking changes
    ) -> None:
        """Add a metadata extraction task directly to the thread pool.

        `read_cache=False` 只由封面模式传：逐行封面会把解析出的 thumbnails[].url
        直接当下载 URL 用，陈旧直链会 403/404。
        """
        with QMutexLocker(self._mutex):
            if task_id in self._active_tasks:
                return

            logger.info(f"AsyncExtractManager starting task {task_id}")
            runnable = MetadataFetchRunnable(
                task_id, url, options, vr_mode, self.signals, read_cache
            )
            runnable.setAutoDelete(True)
            self._active_tasks[task_id] = runnable
            self._thread_pool.start(runnable)

    def active_count(self) -> int:
        with QMutexLocker(self._mutex):
            return len(self._active_tasks)

    def set_concurrency(self, val: int) -> None:
        with QMutexLocker(self._mutex):
            self.max_concurrent = val
            self._thread_pool.setMaxThreadCount(val)

    def has_capacity(self) -> bool:
        with QMutexLocker(self._mutex):
            return len(self._active_tasks) < self.max_concurrent

    def cancel(self, task_id: str) -> None:
        """Cancel a specific extraction task by ID."""
        with QMutexLocker(self._mutex):
            runnable = self._active_tasks.get(task_id)
            if runnable:
                runnable.cancel()
                # Do NOT remove from _active_tasks here. Let _cleanup_task handle it
                # when the thread actually finishes.

    def cancel_all(self) -> None:
        """Cancel all pending and running extraction tasks."""
        with QMutexLocker(self._mutex):
            for runnable in self._active_tasks.values():
                runnable.cancel()

    @Slot(str)
    @Slot(str, dict)
    @Slot(str, str)
    def _cleanup_task(self, task_id: str, *args) -> None:
        """Remove finished/errored tasks from tracker."""
        logger.info(f"AsyncExtractManager cleanup task {task_id}")
        with QMutexLocker(self._mutex):
            if task_id in self._active_tasks:
                del self._active_tasks[task_id]
