from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QThread, Signal

from ..core.config_manager import config_manager
from ..diagnostics import diagnose
from ..models.errors import YtDlpExecutionError
from ..utils.logger import logger
from ..utils.translator import translate_error
from ..youtube.youtube_service import YoutubeServiceOptions, youtube_service
from ..youtube.yt_dlp_cli import YtDlpCancelled
from .executor import DownloadExecutor
from .features import (
    DownloadContext,
    MetadataFeature,
    SponsorBlockFeature,
    SubtitleFeature,
    ThumbnailFeature,
    VRFeature,
)


class DownloadCancelled(Exception):
    pass


class DownloadFailed(Exception):
    """终态失败：错误已经诊断过并 emit 过，外层只需收尾，不要二次上报。

    与 ``DownloadCancelled`` 的区别在于它不是用户取消——会员专属、视频已删除、
    URL 不受支持这类 ``retry.policy == "never"`` 的错误走这条路，避免被 UI
    误标成"任务已取消"。
    """

    pass


class InfoExtractWorker(QThread):
    """解析工人：后台获取视频元数据 (JSON)，不下载"""

    finished = Signal(dict)
    error = Signal(dict)

    def __init__(
        self,
        url: str,
        options: YoutubeServiceOptions | None = None,
        playlist_flat: bool = False,
        *,
        read_cache: bool = True,
    ):
        super().__init__()
        self.url = url
        self.options = options
        self.playlist_flat = playlist_flat
        # 封面模式传 False：解析结果里的 thumbnails[].url 会直接变成下载任务的 URL，
        # 命中缓存等于发一条陈旧直链。跳过读，写照常。
        self.read_cache = read_cache
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if self.playlist_flat:
                info = youtube_service.extract_playlist_flat(
                    self.url, self.options, cancel_event=self._cancel_event
                )
            else:
                info = youtube_service.extract_info_for_dialog_sync(
                    self.url,
                    self.options,
                    read_cache=self.read_cache,
                    cancel_event=self._cancel_event,
                )
            if self._cancel_event.is_set():
                return
            self.finished.emit(info)
        except YtDlpCancelled:
            # Dialog closed; treat as silent cancellation.
            return
        except Exception as exc:
            logger.exception("解析失败: {}", self.url)
            self.error.emit(translate_error(exc))


class ChannelExtractWorker(QThread):
    """频道多标签页智能解析工人"""

    progress = Signal(str)  # 进度消息
    finished_tab = Signal(str, dict)  # (tab_name, info_dict)
    finished_all = Signal(dict)  # 汇总信息和状态，用于更新UI组合
    error = Signal(dict)

    def __init__(
        self,
        base_url: str,
        target_tabs: list[str],
        options: YoutubeServiceOptions | None = None,
    ):
        super().__init__()
        self.base_url = base_url
        self.target_tabs = target_tabs
        self.options = options
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    @staticmethod
    def _tab_display(tab: str) -> str:
        from PySide6.QtCore import QCoreApplication

        return {
            "videos": QCoreApplication.translate("PlaylistWorker", "常规视频"),
            "shorts": "Shorts",
            "streams": QCoreApplication.translate("PlaylistWorker", "直播回放"),
        }.get(tab, tab)

    def _extract_tab(self, tab: str, ydl_opts: dict) -> tuple[str, dict | None, str]:
        """解析单个标签页。在线程池里运行，因此**不发任何 Qt 信号**。

        走服务层的 `extract_channel_flat` 而不是直呼子进程：URL 拼接、streams 的
        `--match-filter`、`channel_tab` TTL 缓存、authcheck 重试与标签页兜底都在那边。
        这里只负责把异常翻译成 UI 语义的 status —— 那是本层的职责。

        Returns:
            (tab, info, status)，status ∈ {"loaded", "unsupported", "empty", "cancelled"}
        """
        try:
            info = youtube_service.extract_channel_flat(
                self.base_url,
                tab=tab,
                base_ydl_opts=ydl_opts,
                cancel_event=self._cancel_event,
            )
        except YtDlpCancelled:
            return tab, None, "cancelled"
        except Exception as e:
            msg = str(e).lower()
            if "does not have a" in msg and "tab" in msg:
                return tab, None, "unsupported"
            # 其他错误也标记为不支持以跳过，避免整个任务崩溃。
            # 注意：这类失败**不进缓存**（服务层只在成功路径 put），
            # 否则一次瞬时网络错误会让该标签页 5 分钟内重试都看不到。
            logger.warning(f"频道 {tab} 标签页解析出错: {e}")
            return tab, None, "unsupported"

        if not info:
            # 保持旧行为：空结果不写入 results，让上层缓存维持原状态
            return tab, None, "empty"

        return tab, info, "loaded"

    def run(self) -> None:
        try:
            results: dict[str, dict] = {}
            tabs = list(self.target_tabs)
            total = len(tabs)
            if not tabs:
                self.finished_all.emit(results)
                return

            from PySide6.QtCore import QCoreApplication

            # 每个标签页都是一次几秒级的 yt-dlp 子进程往返，彼此无依赖。
            # 串行解析 "all" 就是单次耗时的三倍；这里并发跑，总耗时收敛到最慢的那一个。
            # ydl_opts 在这里构建一次共享（每个子任务再各自 dict() 复制），
            # 避免三个线程重复读 cookie 文件、重复探测 exe 路径。
            ydl_opts = dict(youtube_service.build_ydl_options(self.options))
            ydl_opts.update(
                {
                    "skip_download": True,
                    "extract_flat": True,
                    "lazy_playlist": True,
                    "ignoreerrors": False,
                }
            )

            if self._cancel_event.is_set():
                return

            if total == 1:
                self.progress.emit(
                    QCoreApplication.translate("PlaylistWorker", "正在解析 {} ({}/{})...").format(
                        self._tab_display(tabs[0]), 1, 1
                    )
                )
            else:
                self.progress.emit(
                    QCoreApplication.translate(
                        "PlaylistWorker", "正在并行解析 {} 个标签页..."
                    ).format(total)
                )

            cancelled = False
            done = 0
            with ThreadPoolExecutor(
                max_workers=min(3, total), thread_name_prefix="ChannelTab"
            ) as pool:
                futures = [pool.submit(self._extract_tab, tab, ydl_opts) for tab in tabs]
                for fut in as_completed(futures):
                    if self._cancel_event.is_set():
                        cancelled = True
                        break
                    try:
                        tab, info, status = fut.result()
                    except Exception as e:  # noqa: BLE001 - 单个标签页失败不应拖垮整批
                        logger.warning(f"频道标签页任务异常: {e}")
                        continue

                    if status == "cancelled":
                        cancelled = True
                        break
                    if status == "loaded" and info is not None:
                        results[tab] = {"status": "loaded", "data": info}
                        # 信号统一在本 QThread 里发，不从线程池线程发。
                        self.finished_tab.emit(tab, info)
                    elif status == "unsupported":
                        results[tab] = {"status": "unsupported", "data": None}

                    done += 1
                    if total > 1:
                        self.progress.emit(
                            QCoreApplication.translate(
                                "PlaylistWorker", "已完成 {} ({}/{})..."
                            ).format(self._tab_display(tab), done, total)
                        )

            if cancelled or self._cancel_event.is_set():
                return

            self.finished_all.emit(results)

        except Exception as exc:
            logger.exception("频道解析失败: {}", self.base_url)
            self.error.emit(translate_error(exc))


class VRInfoExtractWorker(QThread):
    """VR 解析工人：智能处理 VR 视频和播放列表"""

    finished = Signal(dict)
    error = Signal(dict)

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            # 策略：
            # 1. URL 同时带 v= 和 list= ⇒ 这是"播放列表上下文里的单视频"，直接走 VR 单视频路径。
            #    旧实现对这种 URL 先做一次全量 extract_playlist_flat，发现是单视频再重新解析，
            #    等于白白多付一整轮子进程（几秒级）。
            # 2. 只有 list= 没有 v= ⇒ 真播放列表，走 Flat 解析。
            # 3. 其余 ⇒ 单视频，直接 android_vr 深度解析。
            parsed = urlparse(self.url)
            query = parse_qs(parsed.query)
            has_list = bool(query.get("list"))
            has_video = (
                bool(query.get("v"))
                or "/shorts/" in parsed.path
                or "youtu.be" in (parsed.netloc or "")
            )

            info = None

            if has_list and not has_video:
                try:
                    # 尝试作为播放列表解析
                    info = youtube_service.extract_playlist_flat(
                        self.url, cancel_event=self._cancel_event
                    )

                    # 检查是否真的是播放列表
                    if info.get("_type") != "playlist" and not info.get("entries"):
                        # 只有单个条目或不是播放列表，视为单视频，需要重新解析
                        info = None
                except YtDlpCancelled:
                    raise
                except Exception:
                    # 播放列表解析失败，可能是单视频，忽略错误继续尝试 VR 解析
                    info = None

            if self._cancel_event.is_set():
                return

            if info is None:
                # 单视频模式：使用 android_vr 客户端
                info = youtube_service.extract_vr_info_sync(
                    self.url, cancel_event=self._cancel_event
                )

            if self._cancel_event.is_set():
                return

            self.finished.emit(info)

        except YtDlpCancelled:
            return
        except Exception as exc:
            logger.exception("VR 解析失败: {}", self.url)
            self.error.emit(translate_error(exc))


class EntryDetailWorker(QThread):
    """播放列表条目深解析：获取 formats / 最高质量等信息"""

    finished = Signal(int, dict)
    error = Signal(int, str)

    def __init__(
        self,
        row: int,
        url: str,
        options: YoutubeServiceOptions | None = None,
        *,
        vr_mode: bool = False,
        read_cache: bool = True,
    ):
        super().__init__()
        self.row = row
        self.url = url
        self.options = options
        self.vr_mode = vr_mode
        # 封面模式传 False，理由同 InfoExtractWorker：逐行封面直链同样会进下载任务。
        # VR 分支不受影响——VR 与封面是两个互斥的入口，不会同时成立。
        self.read_cache = read_cache
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if self.vr_mode:
                # VR 模式：使用 android_vr 客户端获取详情
                info = youtube_service.extract_vr_info_sync(
                    self.url, cancel_event=self._cancel_event
                )
            else:
                # 普通模式：使用标准流程
                info = youtube_service.extract_video_info(
                    self.url,
                    self.options,
                    read_cache=self.read_cache,
                    cancel_event=self._cancel_event,
                )

            if self._cancel_event.is_set():
                return

            self.finished.emit(self.row, info)
        except YtDlpCancelled:
            return
        except Exception as exc:
            self.error.emit(self.row, str(exc))


class DownloadWorker(QThread):
    """下载工人：执行实际下载任务

    支持 threading.Event 红绿灯暂停/继续以及安全取消。
    """

    progress = Signal(dict)  # 发送 yt-dlp 的进度字典
    completed = Signal()  # 下载完成（避免与 QThread.finished 冲突）
    cancelled = Signal()  # 用户取消
    error = Signal(dict)  # 发生错误（结构化）
    status_msg = Signal(str)  # 状态文本 (正在合并/正在转换...)
    output_path_ready = Signal(str)  # 最终输出文件路径（尽力解析）
    thumbnail_embed_warning = Signal(str)  # 封面嵌入警告（格式不支持时）
    paused = Signal()  # 已进入暂停状态
    resumed = Signal()  # 已从暂停中恢复
    unified_status = Signal(str, float, str)  # 纯净状态信号：(状态码, 进度, 友好描述)

    def __init__(self, url: str, opts: dict[str, Any], cached_info: dict[str, Any] | None = None):
        super().__init__()
        self.url = url
        self.opts = dict(opts)
        self.is_cancelled = False
        self.is_running = False
        self.executor: DownloadExecutor | None = None
        # Best-effort output location for UI “open folder” action.
        self.output_path: str | None = None
        self.download_dir: str | None = None
        # Best-effort: all destination paths seen in yt-dlp output.
        # This is important for paused/cancelled tasks where final output_path may be unknown.
        self.dest_paths: set[str] = set()  # 格式选择状态追踪（防止格式自动降级到音频）
        self._original_format: str | None = None
        self._ssl_error_count = 0
        self._format_warning_shown = False
        # 规则驱动的自动重试计数（仅进程内，不落库）
        self._auto_retries = 0

        # ── 红绿灯系统 (threading.Event) ──
        # _pause_event: 默认 set()=绿灯(放行), clear()=红灯(暂停)
        # _cancel_event: 默认未触发, set()=取消
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始: 绿灯放行
        self._cancel_event = threading.Event()

        # 初始化功能模块
        self.features = [
            SponsorBlockFeature(),
            MetadataFeature(),
            SubtitleFeature(),
            ThumbnailFeature(),
            VRFeature(),
        ]
        self.cached_info = cached_info

        # 预加载恢复属性，保证 UI 重建时即刻非空
        self.v_title = ""
        self.v_thumbnail = ""
        if cached_info:
            self.v_title = cached_info.get("title", "")
            self.v_thumbnail = cached_info.get("thumbnail", "")

        self.v_duration = 0.0
        if cached_info:
            self.v_duration = float(cached_info.get("duration", 0.0) or 0.0)

        from ..utils.clean_logger import CleanLogger

        self._clean_logger = CleanLogger(
            self._on_clean_update,
            duration=self.v_duration,
            section_cut_mode=str(opts.get("__fluentytdl_section_cut_mode") or ""),
            section_duration=float(opts.get("__fluentytdl_section_duration") or 0.0),
            section_start=float(opts.get("__fluentytdl_section_start") or 0.0),
            section_stream_layout=str(opts.get("__fluentytdl_section_stream_layout") or ""),
            section_estimated_bytes=int(opts.get("__fluentytdl_section_estimated_bytes") or 0),
        )

    def _on_clean_update(self, state: str, pct: float, msg: str) -> None:
        self._final_state = state
        self.progress_val = pct
        self.status_text = msg
        self.unified_status.emit(state, pct, msg)

    @property
    def effective_state(self) -> str:
        """权威状态推断：消除 _final_state 与 QThread 状态的不一致窗口。

        所有 UI 组件和 Filter 都应当读此 property 而非自行组合推断。
        """
        if self.isRunning():
            fs = getattr(self, "_final_state", "downloading")
            # Worker 线程正在跑但 CleanLogger 已标记暂停
            if fs == "paused":
                return "paused"
            return "running"
        fs = getattr(self, "_final_state", "queued")
        if fs in ("completed", "error", "cancelled", "paused", "quality_guard"):
            return fs
        if self.isFinished():
            return "completed"
        return "queued"

    # ── 红绿灯 API (线程安全，可从任意线程调用) ──

    def resume_suspension(self, action: str = "retry") -> None:
        """从挂起状态恢复。action 可以是 'retry' 或 'cancel'。"""
        if hasattr(self, "suspend_event") and getattr(self, "is_suspended", False):
            self.suspend_action = action
            self.suspend_event.set()

    def pause(self) -> None:
        """暂停下载：红灯亮起，Worker 线程将在下次进度回调时自动阻塞。"""
        if self._cancel_event.is_set():
            return
        self._pause_event.clear()

        # 通知 CleanLogger
        pct = getattr(self, "progress_val", 0.0)
        self._clean_logger.force_update("paused", pct, "⏸️ 下载已暂停")

        self.paused.emit()
        logger.info("红灯 下载已暂停: {}", self.url)

    def resume(self) -> None:
        """继续下载：绿灯亮起，Worker 线程将从阻塞点恢复执行。"""
        if self._cancel_event.is_set():
            return
        self._pause_event.set()

        # 通知 CleanLogger
        pct = getattr(self, "progress_val", 0.0)
        self._clean_logger.force_update("downloading", pct, "▶️ 继续下载...")

        self.resumed.emit()
        logger.info("绿灯 下载已恢复: {}", self.url)

    def cancel(self) -> None:
        """取消下载：设置取消标记 + 唤醒可能的暂停阻塞 + 终止子进程。"""
        self._cancel_event.set()
        self.is_cancelled = True
        self._pause_event.set()
        if self.executor:
            self.executor.terminate()
        proc = getattr(self, "_proc_ref", None)
        if proc is not None:
            import platform

            try:
                if platform.system() == "Windows":
                    import subprocess

                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                    )
                else:
                    proc.terminate()
            except Exception:
                logger.debug("Failed to terminate process for {}", self.url)
        logger.info("下载已取消: {}", self.url)

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停状态。"""
        return not self._pause_event.is_set() and not self._cancel_event.is_set()

    def _sweep_part_files(self) -> None:
        """物理清除所有因为取消而残留的残骸文件"""
        import os
        import shutil
        import time

        from ..utils.logger import logger

        if hasattr(self, "sandbox_dir") and self.sandbox_dir and os.path.exists(self.sandbox_dir):
            logger.info("💥 执行沙盒清理: {}", self.sandbox_dir)
            for _ in range(5):
                try:
                    shutil.rmtree(self.sandbox_dir, ignore_errors=True)
                    if not os.path.exists(self.sandbox_dir):
                        break
                except OSError:
                    pass
                time.sleep(0.5)

        sweep_list = set()
        if self.output_path:
            sweep_list.add(self.output_path)
        sweep_list.update(self.dest_paths)

        # 非沙盒模式（纯提取任务等）兜底清理
        for f in sweep_list:
            if os.path.exists(f) and os.path.isfile(f):
                try:
                    os.remove(f)
                    logger.info("已物理清除残骸: {}", f)
                except Exception:
                    pass

    def _clean_part_files(self) -> None:
        """清理 sandbox 内的 .part/.ytdl 残骸，避免 403 后断点续传撞过期 token。"""
        if not hasattr(self, "sandbox_dir") or not self.sandbox_dir:
            return
        if not os.path.exists(self.sandbox_dir):
            return
        for f in os.listdir(self.sandbox_dir):
            if f.endswith((".part", ".ytdl")):
                try:
                    os.remove(os.path.join(self.sandbox_dir, f))
                    logger.info("已清理残骸文件: {}", f)
                except OSError:
                    pass

    def _wait_if_paused(self) -> None:
        """红绿灯检查点：如果红灯则阻塞，直到绿灯或取消。"""
        while not self._pause_event.is_set():
            self._pause_event.wait(timeout=0.5)
            if self._cancel_event.is_set():
                raise DownloadCancelled()

    def run(self) -> None:
        self.is_running = True
        self.is_cancelled = False
        self._auto_retries = 0
        try:
            # ======================================================================
            # 图片直接下载通道：完全无视视频逻辑，发起极简 yt-dlp 请求
            # ======================================================================
            if self.opts.get("__fluentytdl_is_cover_direct", False):
                logger.info("⚡ 检测到纯图片直接下载，走极简通道")
                self.status_msg.emit("⚡ 正在直接下载封面图片...")
                self._run_cover_direct_download()
                return

            # ======================================================================
            # 快速通道：纯字幕/纯封面提取 — 完全绕过 Executor / Strategy / Feature 管线
            # ======================================================================
            if self.opts.get("skip_download", False):
                logger.info("⚡ 检测到纯提取任务 (skip_download)，走快速原生通道")
                self.status_msg.emit("⚡ 原生直接提取（字幕/封面）...")
                self._run_lightweight_extract()
                return

            # 合并 YoutubeService 的基础反封锁/网络配置
            base_opts = youtube_service.build_ydl_options()
            import copy

            merged = copy.deepcopy(base_opts)
            merged.update(copy.deepcopy(self.opts))

            # === 防止单个任务变异为播放列表下载造成无限死循环 ===
            merged["noplaylist"] = True

            # 保存原始格式选择（用于错误恢复）
            self._original_format = merged.get("format")
            if self._original_format:
                logger.info("原始格式选择已保存: {}", self._original_format)

            # DEBUG: 记录音频处理相关选项
            logger.debug(
                "DownloadWorker options - postprocessors: {}", merged.get("postprocessors")
            )
            logger.debug("DownloadWorker options - addmetadata: {}", merged.get("addmetadata"))
            logger.debug(
                "DownloadWorker options - writethumbnail: {}", merged.get("writethumbnail")
            )
            if merged.get("__fluentytdl_section_cut_mode"):
                logger.info(
                    "[Section] mode={} layout={} estimate={}B range={}..{}",
                    merged.get("__fluentytdl_section_cut_mode"),
                    merged.get("__fluentytdl_section_stream_layout") or "unknown",
                    merged.get("__fluentytdl_section_estimated_bytes") or 0,
                    merged.get("__fluentytdl_section_start"),
                    merged.get("__fluentytdl_section_end"),
                )

            # Derive download directory from outtmpl (best effort).
            try:
                paths = merged.get("paths")
                outtmpl = merged.get("outtmpl")

                if isinstance(paths, dict) and paths.get("home"):
                    self.download_dir = os.path.abspath(str(paths.get("home")))
                elif isinstance(outtmpl, str) and outtmpl.strip():
                    parent = os.path.dirname(outtmpl)
                    if parent:
                        self.download_dir = os.path.abspath(parent)
                    else:
                        self.download_dir = os.path.abspath(os.getcwd())
                else:
                    self.download_dir = os.path.abspath(os.getcwd())
            except Exception:
                self.download_dir = os.path.abspath(os.getcwd())

            # === 沙盒模式分离临时文件与最终目录 ===
            if not self.opts.get("skip_download", False) and not self.opts.get(
                "__fluentytdl_is_cover_direct", False
            ):
                db_id_str = str(getattr(self, "db_id", id(self)))
                self.sandbox_dir = os.path.abspath(
                    os.path.join(self.download_dir, ".fluent_temp", f"task_{db_id_str}")
                )
                os.makedirs(self.sandbox_dir, exist_ok=True)

                merged["paths"] = {"home": self.sandbox_dir, "temp": self.sandbox_dir}

            # === Feature Pipeline: Configuration & Pre-flight ===
            # 构建上下文并运行 Feature 链
            context = DownloadContext(self, merged)

            for feature in self.features:
                feature.configure(merged)
                feature.on_download_start(context)

            # === Phase 2: 断点续传支持 ===
            if config_manager.get("enable_resume", True):
                merged["continuedl"] = True  # 继续下载部分文件

            # 回调定义 (复用)
            def on_progress(data: dict[str, Any]) -> None:
                # ── 红绿灯检查点 ──
                self._wait_if_paused()
                if self._cancel_event.is_set():
                    raise DownloadCancelled()

                # 为老 UI 绑定原始速度变量，防止 UI 一直卡在下载展示流而不显示后处理文本
                self.downloaded_bytes = data.get("downloaded_bytes", 0)
                self.total_bytes = data.get("total_bytes", 0)
                self.speed_val = data.get("speed", 0)
                self.eta_val = data.get("eta", 0)

                # 将原生 dict 对象丢给 CleanLogger → unified_status 单通道输出
                self._clean_logger.handle_progress(data)

            def on_status(message: str) -> None:
                self._clean_logger.handle_status(message)

            def on_path(path: str) -> None:
                self.output_path = path

            def on_file_created(path: str) -> None:
                self.dest_paths.add(path)

            # === 执行下载 ===
            logger.info("🚀 启动下载...")

            while True:
                # 让 UI 瞬间响应，不再傻等
                self._clean_logger.force_update("parsing", 0.0, "🔍 正在拉取元数据...")
                self.status_msg.emit("🚀 准备启动执行器...")

                self.executor = DownloadExecutor()
                try:
                    # 执行
                    final_path = self.executor.execute(
                        self.url,
                        merged,
                        on_progress=on_progress,
                        on_status=on_status,
                        on_path=on_path,
                        cancel_check=lambda: self.is_cancelled,
                        on_file_created=on_file_created,
                        cached_info_dict=self.cached_info,
                    )

                    if final_path:
                        self.output_path = final_path
                        if not hasattr(self, "sandbox_dir"):
                            self.output_path_ready.emit(final_path)

                    break  # 跳出 while 循环，进入后续处理

                except YtDlpExecutionError as exc:
                    logger.exception("yt-dlp 执行错误: {}", self.url)
                    pct = getattr(self, "progress_val", 0.0)

                    # 使用新的诊断引擎生成结构化错误
                    diag = diagnose(exc.exit_code, exc.stderr, exc.parsed_json)

                    # 更新内部状态和日志
                    self._clean_logger.force_update(
                        "error", pct, f"❌ {diag.user_title}: {diag.user_message}"
                    )

                    # ── 规则驱动的重试分流 ──
                    # 规则表在 retry.policy 里声明了"这类错误该怎么处理"，
                    # 避免所有错误都挂起弹框、卡住整条批量队列。
                    if diag.retry.is_automatic and self._auto_retries < diag.retry.max_attempts:
                        delay = diag.retry.delay_for(self._auto_retries)
                        self._auto_retries += 1
                        attempt_note = (
                            f"第 {self._auto_retries}/{diag.retry.max_attempts} 次自动重试"
                        )
                        if delay > 0:
                            self.status_msg.emit(f"{attempt_note}，{int(delay)} 秒后开始…")
                            self._clean_logger.force_update(
                                "parsing", pct, f"⏳ {attempt_note}（等待 {int(delay)} 秒）"
                            )
                            # 用 cancel 事件的 wait 做退避，取消时立即返回而不是等满一轮
                            if self._cancel_event.wait(timeout=delay):
                                raise DownloadCancelled() from None
                        if self.is_cancelled or self._cancel_event.is_set():
                            raise DownloadCancelled() from None
                        self.status_msg.emit(attempt_note)
                        self._clean_logger.force_update("parsing", pct, f"🔄 {attempt_note}")
                        continue

                    err_dict = diag.to_dict()
                    err_dict["worker_id"] = id(self)
                    err_dict["auto_retries"] = self._auto_retries

                    if diag.retry.policy == "never":
                        # 会员专属、视频已删除、URL 不支持…… 用户点什么都救不回来，
                        # 直接失败让批量队列继续走下一个，而不是无谓地挂起。
                        self.error.emit(err_dict)
                        raise DownloadFailed(diag.code) from None

                    # after_fix（以及自动重试耗尽的场景）：挂起等用户介入
                    self.is_suspended = True
                    self.suspend_event = threading.Event()
                    self.suspend_action = "cancel"

                    self.error.emit(err_dict)

                    self.status_msg.emit("挂起等待修复...")

                    # 阻塞等待用户选择：重试或取消
                    self.suspend_event.wait()
                    self.is_suspended = False

                    if self.suspend_action == "retry":
                        self._auto_retries = 0  # 用户已介入修复，自动重试预算重新给满
                        self.status_msg.emit("重新尝试下载...")
                        self._clean_logger.force_update("parsing", pct, "正在重试...")
                        continue
                    else:
                        raise DownloadCancelled() from None

                except DownloadCancelled:
                    raise

                except Exception as exc:
                    logger.warning(f"下载失败: {exc}")
                    if self.is_cancelled:
                        raise DownloadCancelled() from None
                    # 直接抛出，让外层 except 做精准诊断
                    raise exc

            # === Feature Pipeline: Post-process ===
            # 执行各模块的后处理逻辑（封面嵌入、字幕合并、VR转码等）
            if not self.is_cancelled:
                for feature in self.features:
                    try:
                        feature.on_post_process(context)
                    except Exception as e:
                        logger.exception(
                            "后处理功能 {} 发生异常: {}", feature.__class__.__name__, e
                        )
                        context.emit_warning(f"后处理异常 ({feature.__class__.__name__}): {str(e)}")

                # Strip internal meta options after all features have post-processed.
                # Must run AFTER on_post_process so that protection gates like
                # __fluentytdl_keep_thumbnail still work (context.opts is the same
                # dict reference as merged). These keys are never passed to yt-dlp
                # because ydl_opts_to_cli_args() only reads specific known keys.
                for k in list(merged.keys()):
                    if isinstance(k, str) and k.startswith("__fluentytdl_"):
                        merged.pop(k, None)

                # ── 转移上岸 (Extraction) ──
                if hasattr(self, "sandbox_dir") and os.path.exists(self.sandbox_dir):
                    self._clean_logger.force_update("completed", 99.0, "📦 正在整理文件...")
                    import shutil

                    final_moved_path = None
                    try:
                        for root, _, files in os.walk(self.sandbox_dir):
                            for f in files:
                                if f.endswith(".part") or f.endswith(".ytdl"):
                                    continue

                                rel_path = os.path.relpath(root, self.sandbox_dir)
                                target_dir = (
                                    os.path.join(self.download_dir, rel_path)
                                    if rel_path != "."
                                    else self.download_dir
                                )
                                os.makedirs(target_dir, exist_ok=True)

                                src = os.path.join(root, f)
                                dst = os.path.join(target_dir, f)

                                # 确保目标文件名唯一，避免覆盖
                                def get_unique_path(target_path: str) -> str:
                                    if not os.path.exists(target_path):
                                        return target_path
                                    base, ext = os.path.splitext(target_path)
                                    counter = 1
                                    while True:
                                        new_path = f"{base} ({counter}){ext}"
                                        if not os.path.exists(new_path):
                                            return new_path
                                        counter += 1

                                dst = get_unique_path(dst)
                                shutil.move(src, dst)

                                # 提取新的文件名以便更新追踪
                                new_f = os.path.basename(dst)

                                # Check if this is the main output path
                                if self.output_path and os.path.basename(self.output_path) == f:
                                    final_moved_path = dst
                                elif not self.output_path and not new_f.endswith(
                                    (
                                        ".jpg",
                                        ".jpeg",
                                        ".png",
                                        ".webp",
                                        ".srt",
                                        ".vtt",
                                        ".ass",
                                        ".lrc",
                                    )
                                ):
                                    final_moved_path = dst

                        if final_moved_path:
                            self.output_path = final_moved_path
                            self.output_path_ready.emit(final_moved_path)
                        elif self.output_path and not self.output_path.startswith(self.sandbox_dir):
                            self.output_path_ready.emit(self.output_path)

                        # Clean up sandbox
                        shutil.rmtree(self.sandbox_dir, ignore_errors=True)
                    except Exception as e:
                        logger.warning("移动沙盒文件失败: {}", e)
                        if self.output_path:
                            self.output_path_ready.emit(self.output_path)
                else:
                    if self.output_path:
                        self.output_path_ready.emit(self.output_path)

                self._clean_logger.force_update("completed", 100.0, "✅ 下载并处理完成！")
                self.completed.emit()

        except DownloadCancelled:
            self._clean_logger.force_update("cancelled", 0.0, "🗑️ 任务已取消并清理残骸")
            # 延时 1 秒给 yt-dlp 及其子进程释放文件锁，防止 WinError 32
            import time

            time.sleep(1.0)
            self._sweep_part_files()
            self.status_msg.emit("任务已取消")
            self.cancelled.emit()
        except DownloadFailed as failure:
            # 错误已经在内层诊断并 emit 过了，这里只做残骸清理：
            # 不重复上报，也不发 cancelled，避免 UI 把失败显示成"任务已取消"
            logger.info("任务终态失败（不可重试）: {} — {}", self.url, failure)
            import time

            time.sleep(1.0)
            self._sweep_part_files()
        except Exception as exc:
            msg = str(exc)
            logger.exception("下载过程发生未知异常: {}", self.url)
            pct = getattr(self, "progress_val", 0.0)

            self._clean_logger.force_update("error", pct, f"❌ 错误: {msg}")

            # 兼容旧逻辑：如果是纯文本 Exception，依然通过 translate_error 进行基本的处理
            # 实际上 translate_error 也可以被废弃，我们现在直接传结构化 dict
            err_dict = translate_error(exc)
            self.error.emit(err_dict)
        finally:
            self.is_running = False
            self.executor = None

    # ── 小文件快速通道 ────────────────────────────────────
    def _run_lightweight_extract(self) -> None:
        """纯字幕/封面提取：完全绕过 Executor / Strategy / Feature 管线，
        直接用最干净的 subprocess 调用 yt-dlp。
        仅保留 Cookie、输出路径、ffmpeg、extractor-args 等必需参数。
        """
        import subprocess

        from ..youtube.yt_dlp_cli import (
            log_pot_from_output,
            log_pot_in_argv,
            prepare_yt_dlp_env,
            resolve_yt_dlp_exe,
        )

        exe = resolve_yt_dlp_exe()
        if exe is None:
            self.error.emit({"title": "错误", "message": "yt-dlp 可执行文件未找到"})
            return

        # 构建最精简的 CLI 参数
        cmd: list[str] = [str(exe), "--ignore-config", "--no-warnings", "--newline"]

        opts = self.opts

        # 从 youtube_service 获取基础选项（仅一次）
        try:
            base_opts = youtube_service.build_ydl_options()
        except Exception:
            base_opts = {}

        # Cookie（必须保留，否则可能无法访问受限视频）
        cookiefile = opts.get("cookiefile") or base_opts.get("cookiefile")
        if isinstance(cookiefile, str) and cookiefile:
            cmd += ["--cookies", cookiefile]

        # 输出路径
        outtmpl = opts.get("outtmpl")
        if isinstance(outtmpl, str) and outtmpl:
            cmd += ["-o", outtmpl]

        paths = opts.get("paths")
        if isinstance(paths, dict):
            home = paths.get("home")
            if isinstance(home, str) and home.strip():
                cmd += ["-P", home.strip()]
                self.download_dir = os.path.abspath(home.strip())

        # ffmpeg 位置（字幕转换可能需要）
        ffmpeg_loc = base_opts.get("ffmpeg_location")
        if isinstance(ffmpeg_loc, str) and ffmpeg_loc.strip():
            cmd += ["--ffmpeg-location", ffmpeg_loc.strip()]

        # skip_download
        cmd.append("--skip-download")

        # 字幕相关
        if opts.get("writesubtitles"):
            cmd.append("--write-subs")
        if opts.get("writeautomaticsub"):
            cmd.append("--write-auto-subs")
        subtitleslangs = opts.get("subtitleslangs")
        if isinstance(subtitleslangs, (list, tuple)) and subtitleslangs:
            cmd += ["--sub-langs", ",".join(str(lang) for lang in subtitleslangs)]

        convert_subs = opts.get("convertsubtitles")
        if isinstance(convert_subs, str) and convert_subs:
            cmd += ["--convert-subs", convert_subs]

        # 封面相关
        if opts.get("writethumbnail"):
            cmd.append("--write-thumbnail")

        # extractor-args（含 POT Provider 配置）
        extractor_args = base_opts.get("extractor_args")
        if isinstance(extractor_args, dict):
            for ie_key, ie_args in extractor_args.items():
                if not isinstance(ie_args, dict):
                    continue
                parts = []
                for k, v in ie_args.items():
                    if isinstance(v, (list, tuple)):
                        parts.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        parts.append(f"{k}={v}")
                if parts:
                    cmd += ["--extractor-args", f"{ie_key}:{';'.join(parts)}"]

        # JS runtimes
        js_runtimes = base_opts.get("js_runtimes")
        if isinstance(js_runtimes, dict):
            for runtime_id, cfg in js_runtimes.items():
                rid = str(runtime_id or "").strip()
                if not rid:
                    continue
                path = ""
                if isinstance(cfg, dict):
                    path = str(cfg.get("path") or "").strip()
                elif isinstance(cfg, str):
                    path = cfg.strip()
                value = f"{rid}:{path}" if path else rid
                cmd += ["--js-runtimes", value]

        cmd.append(self.url)

        logger.info("[LightweightExtract] cmd={}", " ".join(cmd))
        log_pot_in_argv(cmd, stage="Download", task_id="lightweight_extract")

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"

        # Windows 隐藏窗口
        extra_kw: dict[str, Any] = {}
        if os.name == "nt":
            try:
                extra_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            except Exception:
                pass

        from ..download.output_parser import YtDlpOutputParser

        parser = YtDlpOutputParser()
        self._clean_logger.force_update("parsing", 0.0, "⚡ 正在初始化提取引擎...")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                env=env,
                cwd=self.download_dir or os.getcwd(),
                **extra_kw,
            )
            self._proc_ref = proc  # 用于取消

            assert proc.stdout is not None
            for raw in proc.stdout:
                if self.is_cancelled:
                    import platform

                    try:
                        if platform.system() == "Windows":
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                            )
                        else:
                            proc.terminate()
                    except Exception:
                        pass
                    self.cancelled.emit()
                    return

                try:
                    line = raw.decode("utf-8").rstrip("\r\n")  # type: ignore[union-attr]
                except UnicodeDecodeError:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")  # type: ignore[union-attr]

                if line:
                    logger.debug("[LightweightExtract] {}", line)
                    log_pot_from_output(line, stage="Download")
                    parsed = parser.parse_line(line)
                    if parsed.type == "progress" and parsed.progress:
                        prog_dict = {
                            "status": parsed.progress.status,
                            "downloaded_bytes": parsed.progress.downloaded_bytes,
                            "total_bytes": parsed.progress.total_bytes,
                            "speed": parsed.progress.speed,
                            "eta": parsed.progress.eta,
                            "filename": parsed.progress.filename,
                            "info_dict": parsed.progress.info_dict,
                        }
                        self._clean_logger.handle_progress(prog_dict)
                    elif parsed.type == "subtitle":
                        msg = "📝 正在保存字幕..."
                        if parsed.path:
                            msg = f"📝 正在保存字幕: {os.path.basename(parsed.path)}"
                        # 注入伪进度以产生视觉推进感
                        self._clean_logger.force_update("downloading", 50.0, msg)
                    elif parsed.type == "status":
                        self._clean_logger.handle_status(parsed.message or line)
                    elif parsed.message:
                        self._clean_logger.handle_status(parsed.message)
                    else:
                        self._clean_logger.handle_status(line)

            rc = proc.wait()
            self._proc_ref = None

            if rc != 0:
                logger.warning("[LightweightExtract] yt-dlp 退出码 {}", rc)
                self._clean_logger.force_update("error", 100.0, f"❌ 错误: yt-dlp 退出码 {rc}")
                from ..youtube.error_translator import translate_error

                self.error.emit(translate_error(RuntimeError(f"yt-dlp 退出码 {rc}")))
            else:
                self._clean_logger.force_update("completed", 100.0, "✅ 提取完成")
                self.completed.emit()

        except Exception as exc:
            logger.exception("[LightweightExtract] 提取失败: {}", self.url)
            self._clean_logger.force_update("error", 0.0, f"❌ 错误: {exc}")
            self.error.emit(translate_error(exc))
        finally:
            self.is_running = False

    def _run_cover_direct_download(self) -> None:
        """纯图片文件直接下载：当明确得知 URL 就是一个图片时，使用干净的 yt-dlp 避免各种干扰。"""
        import subprocess

        from ..youtube.yt_dlp_cli import prepare_yt_dlp_env, resolve_yt_dlp_exe

        exe = resolve_yt_dlp_exe()
        if exe is None:
            self.error.emit({"title": "错误", "message": "yt-dlp 可执行文件未找到"})
            return

        cmd: list[str] = [str(exe), "--ignore-config", "--no-warnings", "--newline"]

        opts = self.opts
        outtmpl = opts.get("outtmpl")
        if isinstance(outtmpl, str) and outtmpl:
            cmd += ["-o", outtmpl]

        # Paths
        paths = opts.get("paths")
        if isinstance(paths, dict):
            home = paths.get("home")
            if isinstance(home, str) and home.strip():
                cmd += ["-P", home.strip()]
                self.download_dir = os.path.abspath(home.strip())

        # Proxy
        proxy = opts.get("proxy")
        if isinstance(proxy, str) and proxy:
            cmd += ["--proxy", proxy]

        cmd.append(self.url)
        logger.info("[CoverDirect] cmd={}", " ".join(cmd))
        env = prepare_yt_dlp_env()

        extra_kw: dict[str, Any] = {}
        if os.name == "nt":
            try:
                extra_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            except Exception:
                pass

        self._clean_logger.force_update("downloading", 0.0, "⚡ 正在下载图片...")

        try:
            cwd = self.download_dir or os.getcwd()
            os.makedirs(cwd, exist_ok=True)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                env=env,
                cwd=cwd,
                **extra_kw,
            )
            self._proc_ref = proc
            assert proc.stdout is not None

            for raw in proc.stdout:
                if self.is_cancelled:
                    try:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                            )
                        else:
                            proc.terminate()
                    except Exception:
                        pass
                    self.cancelled.emit()
                    return
                # Minimal parsing for progress bar feeling
                try:
                    line = raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line and "Destination:" in line:
                    self._clean_logger.force_update(
                        "downloading",
                        50.0,
                        f"正在保存: {os.path.basename(line.split('Destination: ')[-1])}",
                    )

            rc = proc.wait()
            self._proc_ref = None
            if rc != 0:
                self._clean_logger.force_update("error", 100.0, f"❌ 错误: yt-dlp 退出码 {rc}")
                from ..youtube.error_translator import translate_error

                self.error.emit(translate_error(RuntimeError(f"yt-dlp 退出码 {rc}")))
            else:
                self._clean_logger.force_update("completed", 100.0, "✅ 下载完成")
                self.completed.emit()
        except Exception as exc:
            logger.exception("[CoverDirect] 提取失败: {}", self.url)
            self._clean_logger.force_update("error", 0.0, f"❌ 错误: {exc}")
            from ..youtube.error_translator import translate_error

            self.error.emit(translate_error(exc))
        finally:
            self.is_running = False

    def stop(self) -> None:
        """向后兼容的别名：调用 cancel() 安全取消下载。"""
        self.cancel()
