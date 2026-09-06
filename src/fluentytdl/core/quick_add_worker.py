from __future__ import annotations

import typing

from loguru import logger
from PySide6.QtCore import QThread, Signal

from ..models.quick_download_params import QuickDownloadParams
from ..observability import FlowTrace, bind_current_flow, new_flow
from ..utils.quick_opts import quick_params_to_opts
from ..youtube.youtube_service import YoutubeService

if typing.TYPE_CHECKING:
    from .controller import AppController


class QuickAddWorker(QThread):
    """
    Worker to parse URLs and prepare tasks for Quick Download mode.
    Handles playlist limits and fallback to expansion.
    """

    progress = Signal(str)  # status message
    finished_tasks = Signal(list)  # list of tuples: (title, url, opts, thumbnail)
    error = Signal(str)

    def __init__(
        self,
        urls: list[str],
        params: QuickDownloadParams,
        max_playlist_items: int = 500,
        controller: AppController | None = None,
        *,
        flow: FlowTrace | None = None,
    ):
        super().__init__()
        self.urls = urls
        self.params = params
        self.max_playlist_items = max_playlist_items
        self._controller = controller
        # 快速下载是第五条解析路径：它不走那四个解析 worker，直接同步调
        # `extract_info_for_dialog_sync`。没有 trace 的话，这一整批任务在时间线上
        # 就是「凭空出现的 task」，前面的解析一个字都看不到。
        self.trace: FlowTrace = flow if flow is not None else new_flow(stage="parse")

    def run(self):
        bind_current_flow(self.trace)
        self.trace.enter(
            "parse",
            worker="QuickAddWorker",
            mode="quick",
            url_count=len(self.urls),
            max_playlist_items=self.max_playlist_items,
        )
        try:
            tasks = []
            service = YoutubeService()
            base_opts = quick_params_to_opts(self.params)

            for i, url in enumerate(self.urls):
                self.progress.emit(f"正在解析链接 {i + 1}/{len(self.urls)}...")

                try:
                    # We need to peek if it's a playlist and get its size
                    info = service.extract_info_for_dialog_sync(url)
                except Exception as e:
                    logger.warning(f"Failed to extract info for {url}: {e}")
                    if len(self.urls) == 1:
                        raise  # re-raise if it's the only URL to show error to user
                    continue

                is_playlist = "entries" in info

                if not is_playlist:
                    tasks.append(
                        (info.get("title", url), url, dict(base_opts), info.get("thumbnail", ""))
                    )
                    continue

                # It's a playlist
                entries = info.get("entries") or []
                playlist_title = info.get("title") or "Playlist"

                strategy = self.params.playlist_strategy
                from .config_manager import config_manager

                if strategy == "auto":
                    threshold = config_manager.get("quick_playlist_expand_threshold", 50)
                    if len(entries) <= threshold:
                        strategy = "single_worker"
                    else:
                        strategy = "expand_all"

                # single_worker strategy
                if strategy == "single_worker":
                    if len(entries) > self.max_playlist_items:
                        # 强制拦截并降级为逐条入队
                        self.progress.emit(
                            f"播放列表过长 ({len(entries)}), 将截断前 {self.max_playlist_items} 个强制逐条入队..."
                        )
                        for j, entry in enumerate(entries[: self.max_playlist_items]):
                            e_url = entry.get("url") or entry.get("webpage_url")
                            if not e_url:
                                continue
                            opts = dict(base_opts)
                            opts["noplaylist"] = True
                            tasks.append(
                                (
                                    entry.get("title", f"Video {j + 1}"),
                                    e_url,
                                    opts,
                                    entry.get("thumbnails", [{}])[-1].get("url", ""),
                                )
                            )
                    else:
                        # 使用单 Worker
                        opts = dict(base_opts)
                        opts["noplaylist"] = False
                        opts["playlistend"] = len(entries)
                        # Remove "__fluentytdl_playlist_single_worker" as it's not a native yt-dlp option, though maybe it doesn't hurt.
                        # Wait, we already added "playlist_strategy" to quick params.
                        tasks.append(
                            (
                                f"[播放列表] {playlist_title}",
                                url,
                                opts,
                                info.get("thumbnails", [{}])[-1].get("url", ""),
                            )
                        )
                else:
                    # multi_worker strategy
                    limit = min(len(entries), self.max_playlist_items)
                    for j, entry in enumerate(entries[:limit]):
                        e_url = entry.get("url") or entry.get("webpage_url")
                        if not e_url:
                            continue
                        opts = dict(base_opts)
                        opts["noplaylist"] = True
                        tasks.append(
                            (
                                entry.get("title", f"Video {j + 1}"),
                                e_url,
                                opts,
                                entry.get("thumbnails", [{}])[-1].get("url", ""),
                            )
                        )

            if not tasks and len(self.urls) > 0:
                raise RuntimeError("没有任何链接解析成功。")

            self.finished_tasks.emit(tasks)

        except Exception as e:
            logger.exception("QuickAddWorker error")
            self.error.emit(str(e))
