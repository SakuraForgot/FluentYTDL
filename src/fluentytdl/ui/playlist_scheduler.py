"""
PlaylistScheduler — 播放列表详情抓取调度器

将原本散落在 DownloadConfigWindow 中的四套队列状态和调度逻辑收拢到此独立类中。
外部只需调用：
  - set_viewport(first, last)    — 视口变更时调用，将可见行设为前台优先
  - enqueue_foreground(row)      — 用户点击某行，强制前台入队
  - start_crawl() / stop_crawl() — 开启/关闭后台全量爬取
  - stop_all()                   — 关窗时取消所有任务
  - is_loaded(row) / is_failed(row) / done_count() — 状态查询
  - mark_row_loaded(row)         — 用于封面模式的旁路标记

下游通过信号接收结果：
  - detail_finished(row, info_dict)
  - detail_error(row, error_msg)
  - row_started(row)             — 行真正开始执行时（用于延迟"解析中"指示器）
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..download.extract_manager import AsyncExtractManager
from ..youtube.youtube_service import YoutubeServiceOptions

logger = logging.getLogger(__name__)


class PlaylistScheduler(QObject):
    """
    播放列表详情抓取统一调度器。

    使用三级队列（前台 fg → 暂存 exec → 运行 running）加后台爬虫定时器，
    完全封装了 DownloadConfigWindow 原有的 7 个队列/集合变量和 10+ 个调度方法。

    任务 ID 采用条目 URL（而非行号字符串），搭配 _url_to_row 反向映射实现
    行号无关的稳健调度，彻底规避"行号偏移"导致的数据错乱问题。
    """

    # ── 对外信号 ───────────────────────────────────────────────────────────
    detail_finished = Signal(int, object)  # (row, info_dict)
    detail_error = Signal(int, str)  # (row, error_msg)
    row_started = Signal(int)  # row 真正开始执行（用于延迟 UI 指示器）

    # ── 构造 ───────────────────────────────────────────────────────────────
    def __init__(
        self,
        extract_manager: AsyncExtractManager,
        get_row_url: Callable[[int], str | None],
        total_rows: Callable[[], int],
        options: YoutubeServiceOptions | None = None,
        vr_mode: bool = False,
        exec_limit: int = 3,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mgr = extract_manager
        self._get_row_url = get_row_url
        self._total_rows = total_rows
        self._options = options
        self._vr_mode = vr_mode
        self._exec_limit = exec_limit
        self._lazy_paused: bool = False

        # URL ↔ row 双向映射（task_id = URL，不再用行号字符串）
        self._url_to_row: dict[str, int] = {}
        self._row_to_url: dict[int, str] = {}

        # 状态跟踪（以行号为键）
        self._loaded: set[int] = set()
        self._failed: set[int] = set()
        self._retry_count: dict[int, int] = {}
        self._running: set[int] = set()

        # 三级调度队列
        self._fg_queue: deque[int] = deque()
        self._fg_set: set[int] = set()
        self._bg_queue: deque[int] = deque()
        self._bg_set: set[int] = set()
        self._exec_queue: deque[int] = deque()
        self._exec_set: set[int] = set()

        # 后台全量爬虫
        self._bg_crawl_timer: QTimer | None = None
        self._bg_crawl_index: int = 0
        self._bg_crawl_active: bool = False

        # 侦听 AsyncExtractManager 信号
        self._mgr.signals.task_finished.connect(self._on_mgr_finished)
        self._mgr.signals.task_error.connect(self._on_mgr_error)

    # ── 公开 API ───────────────────────────────────────────────────────────

    def set_options(self, options: YoutubeServiceOptions | None) -> None:
        """更新传递给 yt-dlp 的抓取选项（如 Cookie）。"""
        self._options = options

    def set_vr_mode(self, vr_mode: bool) -> None:
        self._vr_mode = vr_mode

    @property
    def lazy_paused(self) -> bool:
        return self._lazy_paused

    @lazy_paused.setter
    def lazy_paused(self, value: bool) -> None:
        self._lazy_paused = value

    @property
    def is_crawl_active(self) -> bool:
        return self._bg_crawl_active

    def reset(self) -> None:
        """新播放列表加载时重置所有内部状态。"""
        self._url_to_row.clear()
        self._row_to_url.clear()
        self._loaded.clear()
        self._failed.clear()
        self._retry_count.clear()
        self._running.clear()
        self._fg_queue.clear()
        self._fg_set.clear()
        self._bg_queue.clear()
        self._bg_set.clear()
        self._exec_queue.clear()
        self._exec_set.clear()
        self._bg_crawl_index = 0
        self._stop_crawl_timer()

    def stop_all(self) -> None:
        """取消所有待执行与正在执行的任务（关窗时调用）。"""
        self._stop_crawl_timer()
        self._fg_queue.clear()
        self._fg_set.clear()
        self._bg_queue.clear()
        self._bg_set.clear()
        self._exec_queue.clear()
        self._exec_set.clear()
        self._running.clear()
        self._mgr.cancel_all()

    def set_viewport(self, first: int, last: int) -> None:
        """
        视口变更时调用。将 [first-1, last+3] 范围内的行以前台优先入队，
        然后推进执行流水线。
        """
        total = self._total_rows()
        pre_first = max(0, first - 1)
        pre_last = min(total - 1, last + 3)
        for row in range(pre_first, pre_last + 1):
            self._enqueue(row, foreground=True)
        self._pump()

    def enqueue_foreground(self, row: int) -> None:
        """
        显式以前台优先入队单行（如用户点击重试）。
        会自动清除该行的已失败标记，允许重新抓取。
        """
        self._failed.discard(row)
        self._enqueue(row, foreground=True)
        self._pump()

    def start_crawl(self) -> None:
        """启动后台全量爬虫，逐条遍历所有未加载行。"""
        if self._lazy_paused or self._bg_crawl_active:
            return
        total = self._total_rows()
        if total <= 0:
            return
        # 将游标限制在有效范围内
        self._bg_crawl_index = max(0, min(self._bg_crawl_index, total - 1))
        self._bg_crawl_active = True
        t = QTimer(self)
        t.setInterval(400)
        t.timeout.connect(self._crawl_tick)
        t.start()
        self._bg_crawl_timer = t

    def stop_crawl(self) -> None:
        """停止后台爬虫（不取消正在执行的任务）。"""
        self._bg_crawl_active = False
        self._stop_crawl_timer()

    # ── 状态查询 ───────────────────────────────────────────────────────────

    def is_loaded(self, row: int) -> bool:
        return row in self._loaded

    def is_failed(self, row: int) -> bool:
        return row in self._failed

    def is_pending(self, row: int) -> bool:
        """行是否正在排队或执行中（尚未完成）。"""
        return (
            row in self._running
            or row in self._exec_set
            or row in self._fg_set
            or row in self._bg_set
        )

    def loaded_count(self) -> int:
        return len(self._loaded)

    def failed_count(self) -> int:
        return len(self._failed)

    def done_count(self) -> int:
        """已完成行数（成功+失败）。"""
        return len(self._loaded) + len(self._failed)

    def mark_row_loaded(self, row: int) -> None:
        """
        用于封面模式等旁路场景：直接将行标记为已加载，
        而不经过实际的 yt-dlp 抓取流程。
        """
        self._failed.discard(row)
        self._loaded.add(row)

    # ── 内部实现 ───────────────────────────────────────────────────────────

    def _enqueue(self, row: int, foreground: bool) -> bool:
        """将行加入 fg 或 bg 队列。返回 True 表示新入队（而非重复）。"""
        if row in self._loaded or row in self._failed:
            return False
        if row in self._running or row in self._exec_set:
            return False

        url = self._get_row_url(row)
        if not url:
            return False

        if foreground:
            if row in self._fg_set:
                return False
            # 从后台队列提升至前台队列
            if row in self._bg_set:
                try:
                    self._bg_queue.remove(row)
                except ValueError:
                    pass
                self._bg_set.discard(row)
            self._fg_queue.append(row)
            self._fg_set.add(row)
            self._url_to_row[url] = row
            self._row_to_url[row] = url
            return True

        # 后台入队
        if row in self._fg_set or row in self._bg_set:
            return False
        self._bg_queue.append(row)
        self._bg_set.add(row)
        self._url_to_row[url] = row
        self._row_to_url[row] = url
        return True

    def _pipeline_size(self) -> int:
        """当前占用的执行槽数（暂存队列 + 运行中）。"""
        return len(self._exec_queue) + len(self._running)

    def _fill_exec_queue(self) -> None:
        """将 fg/bg 队列中的行移入 exec 队列，直至达到 exec_limit 上限。"""
        while self._pipeline_size() < self._exec_limit:
            row: int | None = None
            if self._fg_queue:
                row = self._fg_queue.popleft()
                self._fg_set.discard(row)
            elif self._bg_queue:
                row = self._bg_queue.popleft()
                self._bg_set.discard(row)
            else:
                break

            if row in self._loaded or row in self._failed:
                continue
            if row in self._running or row in self._exec_set:
                continue

            self._exec_queue.append(row)
            self._exec_set.add(row)

    def _pump(self) -> None:
        """将 exec 队列中的行提交给 AsyncExtractManager 执行。"""
        if self._lazy_paused:
            return
        self._fill_exec_queue()
        while self._exec_queue and self._mgr.has_capacity():
            row = self._exec_queue.popleft()
            self._exec_set.discard(row)

            url = self._get_row_url(row)
            if not url:
                continue

            # 以 URL 作为稳定的 task_id（替换原来不稳定的 str(row)）
            task_id = url
            self._url_to_row[task_id] = row
            self._row_to_url[row] = task_id
            self._running.add(row)

            self.row_started.emit(row)  # 触发延迟"解析中"指示器
            self._mgr.enqueue(task_id, url, self._options, self._vr_mode, high_priority=False)
            self._fill_exec_queue()

    def _crawl_tick(self) -> None:
        """后台爬虫定时器回调：每次将一行加入后台队列并推进流水线。"""
        total = self._total_rows()
        if self._bg_crawl_index >= total:
            self._stop_crawl_timer()
            self._bg_crawl_active = False
            return

        queued = 0
        while self._bg_crawl_index < total and queued < 1:
            row = self._bg_crawl_index
            self._bg_crawl_index += 1
            if row not in self._loaded and row not in self._failed:
                if self._enqueue(row, foreground=False):
                    queued += 1

        self._pump()

        if self._bg_crawl_index >= total:
            self._stop_crawl_timer()
            self._bg_crawl_active = False

    def _stop_crawl_timer(self) -> None:
        if self._bg_crawl_timer is not None:
            self._bg_crawl_timer.stop()
            self._bg_crawl_timer.deleteLater()
            self._bg_crawl_timer = None

    # ── AsyncExtractManager 信号处理 ──────────────────────────────────────

    @Slot(str, object)
    def _on_mgr_finished(self, task_id: str, info: object) -> None:
        row = self._url_to_row.get(task_id)
        if row is None:
            # 兼容旧式整数行号 task_id（过渡期回退）
            try:
                row = int(task_id)
            except (ValueError, TypeError):
                logger.warning("PlaylistScheduler: 未知 task_id=%r (finished)", task_id)
                return

        self._running.discard(row)
        self._exec_set.discard(row)
        self._failed.discard(row)
        self._loaded.add(row)
        self.detail_finished.emit(row, info)
        self._pump()

    @Slot(str, str)
    def _on_mgr_error(self, task_id: str, msg: str) -> None:
        row = self._url_to_row.get(task_id)
        if row is None:
            try:
                row = int(task_id)
            except (ValueError, TypeError):
                logger.warning("PlaylistScheduler: 未知 task_id=%r (error)", task_id)
                return

        self._running.discard(row)
        self._exec_set.discard(row)

        # 自动重试一次
        retries = self._retry_count.get(row, 0)
        if retries < 1:
            self._retry_count[row] = retries + 1
            url = self._get_row_url(row)
            if url:
                self._enqueue(row, foreground=True)
                self._pump()
                return

        self._failed.add(row)
        self.detail_error.emit(row, msg)
        self._pump()
