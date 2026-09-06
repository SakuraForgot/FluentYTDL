from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QThread, Signal

from ..core.config_manager import config_manager
from ..diagnostics import SUBTITLE_WARNING_CODES, DiagnosticLineCollector, diagnose
from ..models.errors import YtDlpExecutionError
from ..models.subtitle_config import SUBTITLE_CONFIG_KEY, SUBTITLE_PREFS_KEY
from ..observability import (
    THUMBNAIL,
    FlowTrace,
    TaskTrace,
    bind_current_flow,
    dump_raw_for_outcome,
    emit_actual,
    emit_event,
    emit_expect,
    emit_success_signals,
    new_flow,
    observe_futures,
    render_argv,
    sanitize_url,
)
from ..utils.aux_files import is_aux_name
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


def _emit_failure_diagnosis(
    trace: FlowTrace,
    err_dict: Mapping[str, Any],
    *,
    stage: str,
    operation: str,
    exc: BaseException | None = None,
) -> None:
    """把 `translate_error()` 已经算出的结构化诊断落成 `kind=diagnosis`。

    **刻意不重跑 `diagnose()`**：`translate_error()` 内部已经做过一次主因仲裁，
    这里只是把它的结论落盘。重跑等于让同一次失败产生两条 `diagnosis`
    （硬规则 2 的 exactly-once 就是冲着这个来的），而且两次仲裁未必给出同一个主因。

    **谁有权调它。** 只有**产生**那个 `Diagnosis` 的 `except` 块 —— 也就是这四个解析
    worker 和 `DownloadWorker` 的失败边界。`download_config_window._show_error` /
    `selection_dialog._show_error` 里那几处 `diagnose(1, raw_error)` 是**展示层重算**，
    一律不许 emit；exactly-once 靠这条约定（架构）保证，不靠 `Diagnosis` 上挂一个
    `_logged` 布尔补丁 —— 那种标记挡不住重新构造、copy、跨线程和二次 diagnose。

    只落 `code` / `category` / `severity` 这些语言中立字段。`user_title` /
    `user_message` 是随界面语言变化的惰性属性，写进日志同一个问题在中英文版里
    长得不一样，没法搜。
    """
    retry = err_dict.get("retry")
    events = err_dict.get("events")
    emit_event(
        "diagnosis",
        trace=trace,
        level="ERROR",
        stage=stage,
        code=err_dict.get("code") or "unknown",
        category=err_dict.get("category") or "unknown",
        severity=err_dict.get("severity") or "fatal",
        fix_action=err_dict.get("fix_action"),
        retry_policy=retry.get("policy") if isinstance(retry, dict) else None,
        # 伴随信号：主因是 429 而事件流里有 nsig 提取失败，这种组合才是真正有诊断价值
        # 的东西 —— `_apply_companion_signals()` 就是据此把 `fix_action` 从规则表的
        # `switch_proxy` 改写成 `update_component` 的。不带上这一列，日志里只剩一个
        # "和规则表对不上"的 fix_action，看不出是哪条伴随信号改写了它。
        events=list(events) if isinstance(events, list) else None,
        operation=operation,
        # 规则没命中时（`code=unknown`），异常原文是唯一的线索。
        # `json_safe` 会把它过一遍 `sanitize_exception()`，路径里的用户名不会漏。
        exception=exc,
        _depth=2,
    )


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
        flow: FlowTrace | None = None,
    ):
        super().__init__()
        self.url = url
        self.options = options
        self.playlist_flat = playlist_flat
        # 封面模式传 False：解析结果里的 thumbnails[].url 会直接变成下载任务的 URL，
        # 命中缓存等于发一条陈旧直链。跳过读，写照常。
        self.read_cache = read_cache
        # 解析阶段天然没有 task_id（`create_worker()` 还没跑），所以事件记
        # `flow=k72f task=-`。调用方传同一个 flow 进来，解析段与随后的下载段才是一条链；
        # 不传则自铸一个，至少本 worker 自己的事件仍能聚成一组。
        self.trace: FlowTrace = flow if flow is not None else new_flow()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        mode = "playlist_flat" if self.playlist_flat else "video"
        bind_current_flow(self.trace)
        self.trace.enter(
            "parse",
            worker="InfoExtractWorker",
            mode=mode,
            read_cache=self.read_cache,
            url=sanitize_url(self.url),
        )
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
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="parse", operation="extract_info_for_dialog", exc=exc
            )
            self.error.emit(err)


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
        *,
        flow: FlowTrace | None = None,
    ):
        super().__init__()
        self.base_url = base_url
        self.target_tabs = target_tabs
        self.options = options
        self.trace: FlowTrace = flow if flow is not None else new_flow()
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
            (tab, info, status)，status ∈ {"loaded", "unsupported", "empty", "failed",
            "cancelled"}

        **`unsupported` 与 `failed` 必须分开。** 前者是事实（这个频道确实没有 Shorts
        标签页，重试一万次结果一样），后者是这一次没拿到（网络超时、cookie 过期、风控）。
        原来两者都返回 `unsupported`，于是断网解析和"真的没有该标签页"在日志与 UI 里
        长得一模一样 —— 而它们的正确处置恰好相反：前者该把标签页永久隐藏，后者必须让
        用户能重试。合并的代价不是少一行日志，是把一次网络抖动伪装成了频道的属性。

        emit 放在这里而不是 `run()` 的收集循环：异常对象只在这里手上。
        `emit_event` 是线程安全的（loguru），而 `self.trace` 显式传入 ——
        `current_flow()` 是 per-thread ContextVar，池内线程取不到外面绑的那个。
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
                self.trace.emit(
                    "decision",
                    stage="parse",
                    subsystem="channel_tab",
                    tab=tab,
                    status="unsupported",
                    reason="tab_absent",
                )
                return tab, None, "unsupported"
            # 真错误。注意：这类失败**不进缓存**（服务层只在成功路径 put），
            # 否则一次瞬时网络错误会让该标签页 5 分钟内重试都看不到。
            logger.warning(f"频道 {tab} 标签页解析出错: {e}")
            err = translate_error(e)
            _emit_failure_diagnosis(
                self.trace,
                err,
                stage="parse",
                operation=f"extract_channel_tab:{tab}",
                exc=e,
            )
            return tab, None, "failed"

        if not info:
            # 保持旧行为：空结果不写入 results，让上层缓存维持原状态
            self.trace.emit(
                "decision",
                stage="parse",
                subsystem="channel_tab",
                tab=tab,
                status="empty",
            )
            return tab, None, "empty"

        return tab, info, "loaded"

    def run(self) -> None:
        bind_current_flow(self.trace)
        self.trace.enter(
            "parse",
            worker="ChannelExtractWorker",
            mode="channel",
            tabs=list(self.target_tabs),
            url=sanitize_url(self.base_url),
        )
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
                # 用 dict 而不是 list：出事时要知道**是哪个标签页**死了。
                # 原来是 list，except 里只剩一句"频道标签页任务异常"，既定位不到 tab，
                # 也没有把那个 tab 标成失败 —— 它会一直停在 unloaded。
                submitted = {pool.submit(self._extract_tab, tab, ydl_opts): tab for tab in tabs}
                # 池内异常被装进 Future，`threading.excepthook` 永远等不到它；
                # 没人调 `result()` 就彻底消失。这里挂的是"观测"，不改变 future 状态。
                observe_futures(submitted, self.trace, stage="parse")
                for fut in as_completed(submitted):
                    if self._cancel_event.is_set():
                        cancelled = True
                        break
                    submitted_tab = submitted[fut]
                    try:
                        tab, info, status = fut.result()
                    except Exception as e:  # noqa: BLE001 - 单个标签页失败不应拖垮整批
                        # `_extract_tab` 自己 catch 了 Exception，所以能到这里的是它**之外**
                        # 的意外。`observe_future` 已落了 `signal code=future_exception`
                        # （我观察到了什么），这里补主因判定 —— 这个 except 是这次失败唯一
                        # 的错误边界，不补的话日志里就只有"有个 future 炸了"而没有 code。
                        logger.warning(f"频道标签页 {submitted_tab} 任务异常: {e}")
                        _emit_failure_diagnosis(
                            self.trace,
                            translate_error(e),
                            stage="parse",
                            operation=f"extract_channel_tab_future:{submitted_tab}",
                            exc=e,
                        )
                        results[submitted_tab] = {"status": "failed", "data": None}
                        done += 1
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
                    elif status == "failed":
                        # 与 unsupported 分开写回：上层据此决定"永久隐藏"还是"允许重试"。
                        results[tab] = {"status": "failed", "data": None}

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
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="parse", operation="extract_channel", exc=exc
            )
            self.error.emit(err)


class VRInfoExtractWorker(QThread):
    """VR 解析工人：智能处理 VR 视频和播放列表"""

    finished = Signal(dict)
    error = Signal(dict)

    def __init__(self, url: str, *, flow: FlowTrace | None = None):
        super().__init__()
        self.url = url
        self.trace: FlowTrace = flow if flow is not None else new_flow()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        bind_current_flow(self.trace)
        self.trace.enter(
            "parse", worker="VRInfoExtractWorker", mode="vr", url=sanitize_url(self.url)
        )
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
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="parse", operation="extract_vr_info", exc=exc
            )
            self.error.emit(err)


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
        flow: FlowTrace | None = None,
    ):
        super().__init__()
        self.row = row
        self.url = url
        self.options = options
        self.vr_mode = vr_mode
        # 封面模式传 False，理由同 InfoExtractWorker：逐行封面直链同样会进下载任务。
        # VR 分支不受影响——VR 与封面是两个互斥的入口，不会同时成立。
        self.read_cache = read_cache
        self.trace: FlowTrace = flow if flow is not None else new_flow()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        # 逐行深解析，一个播放列表就是几十条 —— 走 DEBUG 只进文件与 JSONL，
        # 不刷控制台。`enter()` 会改共享 flow 的 stage，这里刻意不用它。
        #
        # 这个 `run()` 会在 QThreadPool 的复用线程上跑，所以每次都要重绑环境 flow：
        # 上一个 runnable 留下的值必须被覆盖掉。
        bind_current_flow(self.trace)
        self.trace.emit(
            "stage",
            level="DEBUG",
            stage="parse",
            worker="EntryDetailWorker",
            mode="vr_detail" if self.vr_mode else "detail",
            row=self.row,
            url=sanitize_url(self.url),
        )
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
            # 逐条深解析失败以前**一个字都不落日志**（这个 except 里连
            # `logger.exception` 都没有），播放列表里某几行永远拉不到详情时，
            # 用户看到的只是那几行是空的。这里补上失败边界的 diagnosis ——
            # 一个 500 条的列表若有 500 条失败，那 500 条正是需要看到的东西。
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="parse", operation="entry_detail", exc=exc
            )
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
    # 任务**成功**但有部分内容没拿到时的结构化告知（载荷同 `Diagnosis.to_dict()`）。
    # 与 `error` 分开是刻意的：`error` 的接收端会弹模态对话框、把任务标红，而这里
    # 说的是"视频好了，字幕没下到"—— 那既不该打断用户，也不该让任务看起来失败了。
    task_warning = Signal(dict)
    paused = Signal()  # 已进入暂停状态
    resumed = Signal()  # 已从暂停中恢复
    unified_status = Signal(str, float, str)  # 纯净状态信号：(状态码, 进度, 友好描述)

    def __init__(
        self,
        url: str,
        opts: dict[str, Any],
        cached_info: dict[str, Any] | None = None,
        *,
        flow: FlowTrace | None = None,
    ):
        super().__init__()
        self.url = url
        self.opts = dict(opts)
        # 观测标识。`task_id` 还是 "-"：`db_id` 要等 `create_worker()` 入库后才有，
        # 那里会调 `trace.bind_task_id()` 补一条 identity 事件把 flow → task 钉起来。
        self.trace: TaskTrace = (
            flow.child_task() if flow is not None else TaskTrace(stage="download")
        )
        self.is_cancelled = False
        self.is_running = False
        self.executor: DownloadExecutor | None = None
        # run 级的 raw yt-dlp 原文。**不能**直接用 `executor.raw_lines`：每个 attempt
        # 都新建一个 executor（重试循环里那句 `self.executor = DownloadExecutor()`），
        # 而 dump 出来的文件是以 `run_id` 命名的 —— 只收最后一个 executor 的话，
        # "429 → 重试 → 重试 → 失败"这种最需要复盘的 run 只剩最后一次的输出。
        # 满了淘汰最旧的：真正要看的失败原因在末尾。
        self._raw_lines: deque[str] = deque(maxlen=4000)
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
        # transition 去重基线：`unified_status` 每个进度 tick 都带 `downloading` 触发
        # 收口闭包，只有状态真的变了才该落一条 `kind=transition`（硬规则 1）。
        # 初值 `queued` 对齐 `insert_task` 的入库态，首条 `queued → parsing` 才算得上一次迁移。
        self._last_transition_state = "queued"
        # 本 run 的终态裁决，由 `run()` 重置、各终态边界写入、`_finish_run()` 消费。
        self._run_outcome: str | None = None

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
        # 迟解析的字幕语言结果：只算一次，重试时不再发第二次网络请求
        self._resolved_sub_opts: dict[str, Any] | None = None

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
            # 多流阶段切换（视频流 → 音频流）要落一条 `kind=stage`，而那个判定只在
            # `CleanLogger` 内部看得见。传 trace 而不是让它 import observability：
            # `utils/` 是 Foundation 层，反过来 import 上层是反向依赖；trace 对象
            # 自带 `emit()` 出口，正好把方向掰直。
            trace=self.trace,
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

    def restore_state(self, state: str) -> None:
        """把跨会话恢复出来的状态回填到这个 worker（由 `download_manager` 调用）。

        为什么不是一句 `worker._final_state = state`：**transition 的基线也得跟着走**。
        这个 worker 是从 DB 里恢复出来的壳，`_last_transition_state` 还停在 `__init__`
        写的 `queued`；用户随后点重试/继续时，第一条 `unified_status` 会被记成
        `queued → parsing`，而真相是 `error → parsing` / `paused → downloading`
        —— 时间线上凭空多出一次没发生过的入队。

        **恢复本身不发 transition。** 这里只对齐基线：状态是上个会话早就落库的事实，
        不是本次会话新接受的一次变化（本次会话的第一条迁移由真正跑起来时产生）。
        """
        self._final_state = state
        self._last_transition_state = state

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

    # ── 字幕语言迟解析 ────────────────────────────────────
    def _subtitle_info_for_resolution(self) -> dict[str, Any] | None:
        """拿一份**带字幕字段**的 info dict，拿不到就返回 None（调用方回落正则）。

        播放列表不逐行解析时 `cached_info` 是 flat 的（只有 id/title/url），压根没有
        `subtitles` / `automatic_captions` —— 这种情况下必须真的解析一次。
        `extract_info_sync` 自带 `entry_detail` TTL 缓存，所以在配置窗口或选择弹窗里
        看过这个视频的用户不会为此多付一次请求。
        """
        info = self.cached_info
        if isinstance(info, dict) and self._info_has_subtitle_fields(info):
            return info

        try:
            return youtube_service.extract_info_sync(self.url, cancel_event=self._cancel_event)
        except YtDlpCancelled:
            raise
        except Exception as e:
            # 字幕是 best-effort：解析失败绝不能让下载任务本身失败，
            # 也绝不能静默把字幕关掉 —— 交给调用方走正则模式。
            logger.warning("[Subtitle] 迟解析取 info 失败，将回落正则模式：{}", e)
            return None

    @staticmethod
    def _info_has_subtitle_fields(info: dict[str, Any]) -> bool:
        """这份 info 对"有哪些字幕"是否**说得上话**。

        非空显然算。空也可能算 —— 一个字幕都没有的视频，完整解析出来就是
        `subtitles={} automatic_captions={}`，为它再发一次请求纯属浪费，
        而且请求失败还会把"这视频没字幕"误报成"正则模式"。

        判据是**两个键都在**，不是"任一键为真"：

        - 完整解析：两个键都有（可能都是空的）→ 直接采信
        - flat 播放列表条目：两个键都没有 → 必须解析
        - `selection_dialog._normalize_info_payload:145` 的合成 dict：只有一个
          硬编码的 `"subtitles": {}` → 必须解析。**这条是要点**：按"键在不在"
          一刀切会把它当成权威答案，于是那条路上的每个任务都被判成"没有字幕"
          —— 比多发一次请求糟得多，等于从另一个方向复现本次要修的 bug。

        某些 extractor 若真的不吐 `automatic_captions`，这里会退化成多解析一次。
        方向是对的：宁可多花一次请求，也不能凭空断言"没有字幕"。
        """
        if info.get("subtitles") or info.get("automatic_captions"):
            return True
        return "subtitles" in info and "automatic_captions" in info

    def _resolve_subtitle_prefs(self, opts: dict[str, Any]) -> None:
        """把生产端声明的语言**偏好**换成这个视频上真实存在的字幕键。

        生产端（配置窗口 / 快速下载）只能写偏好：播放列表未逐行解析时它根本不知道
        真实字幕键长什么样。而 `--sub-langs` 的每一项被 yt-dlp 当作**锚定正则**，
        裸 `en` 匹配不到 `en-GB`，一个 `.vtt` 都不会写出来 —— 这就是"字幕开了却全是空"
        的病根（见 `FluentYTDL-字幕下载问题排查报告.md`）。

        必须早于 Feature 链：`SubtitleFeature.on_download_start` 与
        `container_compat.ensure_subtitle_compatible_container()` 都要看到最终语言列表。
        （后者目前只在 UI 层被调用，播放列表逐行循环里没有它 —— 那是另一件事，
        不在本次改动范围内。）

        原地改写传入的 dict；结果缓存在 `self._resolved_sub_opts`，重试不会重复请求。
        """
        prefs = opts.pop(SUBTITLE_PREFS_KEY, None)
        raw_config = opts.pop(SUBTITLE_CONFIG_KEY, None)
        if not prefs:
            return

        if self._resolved_sub_opts is None:
            from ..models.subtitle_config import SubtitleConfig
            from ..processing.subtitle_service import resolve_deferred_langs

            if isinstance(raw_config, dict):
                config = SubtitleConfig.from_dict(raw_config)
            else:
                # 没带上下文（旧任务从库里恢复等）时退回全局设置，
                # 至少 `type_preference` / `max_languages` 不会凭空变。
                config = config_manager.get_subtitle_config()

            self._resolved_sub_opts = resolve_deferred_langs(
                [str(p) for p in prefs],
                self._subtitle_info_for_resolution(),
                config,
                trace=self.trace,
            )

        # 覆盖 write/auto 是刻意的：命中的轨道是人工还是自动，只有解析完才知道。
        # `resolve_deferred_langs` 不会返回 `embedsubtitles` / `convertsubtitles`
        # （`no_match` 关字幕那一种除外），所以生产端的嵌入决策不会被踩掉。
        opts.update(self._resolved_sub_opts)

    def _scan_subtitle_warnings(self, diag_lines: Iterable[str]) -> str | None:
        """任务**成功**之后补跑一轮诊断，专收字幕类警告；返回给用户看的短标题。

        `diagnose()` 只在 `rc != 0` 时被调用，而字幕下不到从来不会让任务失败 ——
        于是"视频好了、字幕全空"这件事在诊断层根本没有入口，用户只能看到一句
        "未找到字幕文件"。这里补上那个入口。

        只认字幕三码，刻意不做通用扫描：成功的下载里 `nsig extraction failed`
        这类警告极其常见（priority 也更高），一并冒出来只会训练用户忽略提示。
        """
        lines = [ln for ln in diag_lines if ln]
        if not lines:
            return None

        from ..diagnostics import parse_events

        seen: set[str] = set()
        hits: list[str] = []
        for ev in parse_events("\n".join(lines)):
            if ev.code not in SUBTITLE_WARNING_CODES or ev.code in seen:
                continue
            seen.add(ev.code)
            hits.append(ev.raw_line)
        if not hits:
            return None

        # 只把命中的那几行喂回 `diagnose`：整段输出会让优先级更高的非字幕警告抢走
        # 主因，而且 `raw_tail` 里混进本地路径 —— 这个载荷是要送到 UI 上的。
        # rc 传 0：`_is_warning_only_primary` 靠它放行警告级主因（这是唯一的合法用法）。
        #
        # **这里的 `diagnose()` 纯粹是 UI 文案生成器，不是观测入口。** 硬规则 2 要守的
        # 是「`count(kind=diagnosis)` 恒等于真正发生了错误」，所以这个成功路径**绝不**
        # emit `kind=diagnosis` —— 同一批行的结构化落点是调用方紧挨着的
        # `emit_success_signals()`（`kind=signal`）。要的只是 `user_title` /
        # `user_message` 那两个惰性本地化属性，自己重造一遍等于把 catalog 抄第二份。
        diag = diagnose(0, "\n".join(hits))
        # 日志里只记 code，**不记** `diag.user_message`：那是随界面语言变化的本地化文案，
        # 写进日志就没法搜索了（同一个问题在中文版和英文版日志里长得不一样）。
        # 用户要看的那句话走 `task_warning` 到 UI。
        logger.warning("[Subtitle] 成功后诊断命中 {} (code={})", sorted(seen), diag.code)
        self.task_warning.emit(diag.to_dict())
        return diag.user_title

    def _begin_run(self) -> None:
        """标记一次执行会话（run）的开始。

        **不是**"任务开始"：同一个 `task_id` 可以有多个 run（重启后恢复、用户重下），
        每个 run 有自己的 `run_id` 和自己唯一的 `outcome`。QThread 结束后不能重用，
        所以"一个 DownloadWorker = 一个 run"这件事由 Qt 的生命周期保证，
        不需要在这里调 `trace.new_run()`。

        `run_id` 落库由 `create_worker()` 挂在 `QThread.started` 上的桥完成 ——
        DB 写入归 `download_manager` 那一侧，本文件不碰 storage。
        """
        if self.opts.get("__fluentytdl_is_cover_direct", False):
            mode = "cover_direct"
        elif self.opts.get("skip_download", False):
            mode = "extract"
        else:
            mode = "video"
        # 下载路径也会调 `youtube_service`（取 info_dict、字幕列表），那些调用同样要能
        # 落 `signal`。绑的是 `TaskTrace`，所以那些事件连 `task` / `run` 都带着。
        bind_current_flow(self.trace)
        self.trace.enter(
            "download",
            mode=mode,
            url=sanitize_url(self.url),
            format=self.opts.get("format") or None,
        )

    def _harvest_raw_lines(self, attempt: int) -> None:
        """把刚结束的 attempt 的 raw 输出收进 run 级缓冲。

        由重试循环的 `finally` 调用 —— `break` / `continue` / `raise` 三条出路都过它，
        所以"哪个 attempt 忘了收"不可能发生。分隔行让 dump 出来的文件自解释：
        没有它，一个 run 里三次 attempt 的输出会连成一片看不出边界。
        """
        executor = self.executor
        if executor is None:
            return
        try:
            lines = list(getattr(executor, "raw_lines", ()))
            if not lines:
                return
            self._raw_lines.append(f"===== FluentYTDL: attempt {attempt} =====")
            self._raw_lines.extend(lines)
        except Exception:
            # 硬规则 5：收原文失败不能影响重试决策本身。
            logger.debug("harvest raw lines failed")

    def _dump_raw_output(self, outcome: str) -> None:
        """在 run 收口处按终态决定要不要留 raw 原文。

        配置读取在**调用方这一侧**：`observability/` 刻意不 import `core.config_manager`
        （后者反过来要调 `emit_config_change()`，直连必成环，`config_snapshot` 同理接的是
        取值 callable）。
        """
        try:
            always = bool(config_manager.get("log_raw_ytdlp", False))
            dump_raw_for_outcome(
                self.trace,
                list(self._raw_lines),
                outcome=outcome,
                always=always,
            )
        except Exception:
            logger.debug("raw dump failed")

    def _finish_run(self) -> None:
        """run 的唯一 outcome 收口（硬规则 4：每个 run 恰好一个 outcome）。

        由 `run()` 的 `finally` 调用 —— 那是无论走哪条终态边界都必经的出口，所以
        exactly-once 是**结构**保证的，不靠标志位打补丁。各边界只在 `_run_outcome`
        上记一个字符串，裁决与 emit 都在这里发生。

        `finish()` 会把 `recovered` / `degraded` / `missing` 从 trace 的累积状态里
        自行填入（executor 的 `mark_recovered()`、P1-C 的 `expect/record_artifacts`
        一路攒到这里），调用方无权覆盖。

        `_run_outcome` 为 None 属于逻辑漏洞（有终态边界忘了记）——落一条
        `stage=finalize` 的 `signal` 把它显式暴露出来，而不是默默按成功处理，
        否则"某条路径不产 outcome"这种回归会被永远埋住。
        """
        outcome = getattr(self, "_run_outcome", None)
        if outcome is None:
            self.trace.emit(
                "signal",
                level="WARNING",
                stage="finalize",
                _depth=3,
                code="run_outcome_unset",
            )
            outcome = "failed"
        # raw dump 排在 `finish()` 之前：这样时间线上 `raw_output_retained` 落在
        # `outcome` 前面，读日志的人先看到"原文留了一份"再看到终态，而不是反过来。
        # `recovered` / `degraded` 在此已经定型（`finish()` 只读不写）。
        self._dump_raw_output(outcome)
        try:
            self.trace.finish(outcome)
        except Exception:
            # 硬规则 5：终态观测再重要，也不能在 run 收尾时把自己炸掉。
            logger.debug("finish outcome emit failed")

    def run(self) -> None:
        self.is_running = True
        self.is_cancelled = False
        self._auto_retries = 0
        # 本 run 的终态裁决（硬规则 4：每个 run 恰好一个 outcome）。各终态边界只往这里
        # 记一个字符串，真正的 emit 收口在下面的 `finally` —— 那是 run() 唯一保证执行的
        # 出口，无论正常返回、异常、还是快速通道里的 `return` 都必经它。快速通道
        # （`_run_*`）在自己的终态边界上写的也是这同一个属性，回到这里由 finally 统一裁决。
        self._run_outcome = None
        # 上一个 run 的原文不能落进以本 run 的 run_id 命名的文件里。
        self._raw_lines.clear()
        self._begin_run()
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
                # `db_id` 正常总是有的（`create_worker()` 入库后即赋值）。兜底用 run_id
                # 而不是 `id(self)`：内存地址会被复用，同一次运行里两个先后创建的 worker
                # 完全可能拿到同一个地址，于是共用一个沙盒目录、互删对方的临时文件。
                db_id_str = str(getattr(self, "db_id", "") or f"run{self.trace.run_id}")
                self.sandbox_dir = os.path.abspath(
                    os.path.join(self.download_dir, ".fluent_temp", f"task_{db_id_str}")
                )
                os.makedirs(self.sandbox_dir, exist_ok=True)

                merged["paths"] = {"home": self.sandbox_dir, "temp": self.sandbox_dir}

            # === 字幕语言迟解析 ===
            # 必须在 Feature 链之前：SubtitleFeature.on_download_start 要看到最终语言列表
            self._resolve_subtitle_prefs(merged)

            # === Feature Pipeline: Configuration & Pre-flight ===
            # 构建上下文并运行 Feature 链
            context = DownloadContext(self, merged)

            for feature in self.features:
                feature.configure(merged)
                feature.on_download_start(context)

            # ── 期望产物（硬规则 3 的被减数）──
            # 必须在这里：opts 到此为最终形态（迟解析 + Feature 链都改完了），且还早于
            # 下面 `__fluentytdl_` 私有键被 pop 掉 —— 字幕迟解析结论就存在那批键里。
            # 放在 Feature 链**之前**会把系统自己做的容器降级记成用户的期望，于是每个
            # 走过 `ensure_subtitle_compatible_container` 的任务都假降级。
            emit_expect(merged, trace=self.trace, stage="select")

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
                attempt_no = self.trace.attempt
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
                    diag = diagnose(exc.exit_code, exc.stderr, exc.parsed_json, phase=exc.phase)

                    # ── 唯一的 diagnosis 持久化点（硬规则 2 + 4）──
                    # 以前 `diag` 只喂了 UI（`force_update` + `error.emit`），日志里只剩
                    # 一句 `ERROR: HTTP Error 429`，读不出 `code=rate_limited_429
                    # category=network retry=backoff` —— 分类在展示时才发生，结果不持久，
                    # 事后没法统计也没法按任务筛时间线。这一条就是整轮重构的起点。
                    #
                    # exactly-once 靠**架构**保证，不靠 `Diagnosis` 上加个 `_logged`
                    # 布尔补丁：那种标记挡不住重新构造 / copy / 跨线程 / 同一段 stderr
                    # 被二次 diagnose。约定是"只有产生 Diagnosis 的这个 except 块负责
                    # emit，UI 层一律只消费"，而产生点在整条下载路径上只有这一个。
                    #
                    # 只记 `code` / `category` / `severity`，**不记** `user_title` /
                    # `user_message`：那两个是惰性本地化属性，会随界面语言变化，
                    # 写进日志就没法搜索了。
                    emit_event(
                        "diagnosis",
                        trace=self.trace,
                        level="ERROR",
                        # 不传 `stage=` 时会从 `self.trace` 继承成 `download`，于是
                        # "连格式都没挑出来就死了"在日志里被记成下载阶段失败。
                        # `diag.phase` 的三个取值（parse / select / download）都在
                        # `STAGES` 闭集里，`build_event` 会自己校验。
                        stage=diag.phase or None,
                        code=diag.code,
                        category=diag.category,
                        severity=diag.severity,
                        component=diag.component or None,
                        exit_code=diag.exit_code,
                        fix_action=diag.fix_action,
                        retry_policy=diag.retry.policy,
                        retry_max=diag.retry.max_attempts,
                        # 伴随信号：主因是 403 而事件流里有 nsig 提取失败，这种组合
                        # 才是真正有诊断价值的东西。
                        events=[e.code for e in diag.events],
                        auto_retries=self._auto_retries,
                    )

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
                        # `_auto_retries` 是**重试预算**（用户介入后会重新给满），
                        # `trace.attempt` 是本 run 内单调递增的**尝试序号**。两者刻意
                        # 分开：合成一个的话，用户修好参数再重试会让日志里的 attempt
                        # 倒退回 0，同一个 run 出现两个 attempt=1，时间线不再有序。
                        # `run_id` 在这里**不变** —— 换了它就和 attempt 表达同一件事。
                        self.trace.next_attempt()
                        # 规则驱动的自动重试以前一个字都不落文件：用户看到任务"卡了
                        # 30 秒"，日志里查不到那 30 秒在等什么。`stage="retry"` 让它
                        # 在时间线上自成一段，而不是混在 download 里。
                        emit_event(
                            "retry",
                            trace=self.trace,
                            stage="retry",
                            trigger="automatic",
                            code=diag.code,
                            policy=diag.retry.policy,
                            delay_sec=delay,
                            budget_used=self._auto_retries,
                            budget_max=diag.retry.max_attempts,
                        )
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
                        # 预算归零但尝试序号继续往上走（见上面自动重试处的注释）
                        self.trace.next_attempt()
                        # `trigger="user"`：这一条和自动重试同为 `kind=retry`，靠 trigger
                        # 区分。合并成一个 kind 是刻意的 —— 时间线上要看的是"这个 run
                        # 试了几次"，而不是"哪几次是人点的"。
                        emit_event(
                            "retry",
                            trace=self.trace,
                            stage="retry",
                            trigger="user",
                            code=diag.code,
                            policy=diag.retry.policy,
                            delay_sec=0.0,
                            budget_reset=True,
                        )
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

                finally:
                    # 每个 attempt 一个新 executor，它的 raw 缓冲会随之消失 —— 在这里
                    # 收上来，`break` / `continue` / `raise` 三条出路都覆盖到。
                    self._harvest_raw_lines(attempt_no)

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
                                elif not self.output_path and not is_aux_name(new_f):
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

                # 字幕缺失不会让 yt-dlp 返回非零，所以永远进不了 `rc != 0` 那条诊断
                # 入口。在宣布完成**之前**补扫一次：放到 force_update("completed")
                # 之后再 emit_status，会把任务状态从 completed 倒退回 processing。
                sub_warning = self._scan_subtitle_warnings(
                    self.executor.diag_lines if self.executor else ()
                )
                if sub_warning:
                    # 走 emit_status 而不是 emit_warning：详细原因 `_scan_subtitle_warnings`
                    # 已经写进日志了，emit_warning 会再记一遍同样的内容。
                    context.emit_status(f"⚠️ {sub_warning}")

                # ── 实际产物 vs 期望产物 ──
                # 同样必须在 force_update("completed") 之前（见上一段注释）。
                # 传的是 `dest_paths` 而不是重扫最终目录：沙盒上岸时的去重改名
                # （`Title.en.vtt` → `Title.en (1).vtt`）会毁掉文件名里的语言段，
                # 而沙盒内的原始名是准的。`output_path` 已是上岸后的路径，容器判定要用它。
                emit_actual(
                    self.dest_paths,
                    trace=self.trace,
                    output_path=self.output_path or "",
                    stage="verify",
                )

                self._clean_logger.force_update("completed", 100.0, "✅ 下载并处理完成！")
                self._run_outcome = "success"
                self.completed.emit()

        except DownloadCancelled:
            self._clean_logger.force_update("cancelled", 0.0, "🗑️ 任务已取消并清理残骸")
            # 延时 1 秒给 yt-dlp 及其子进程释放文件锁，防止 WinError 32
            import time

            time.sleep(1.0)
            self._sweep_part_files()
            self.status_msg.emit("任务已取消")
            self._run_outcome = "cancelled"
            self.cancelled.emit()
        except DownloadFailed as failure:
            # 错误已经在内层诊断并 emit 过了，这里只做残骸清理：
            # 不重复上报，也不发 cancelled，避免 UI 把失败显示成"任务已取消"
            logger.info("任务终态失败（不可重试）: {} — {}", self.url, failure)
            import time

            time.sleep(1.0)
            self._sweep_part_files()
            # 内层只 emit 了 `kind=diagnosis`（这次失败判定成了什么），终态裁决仍归本层：
            # diagnosis 和 outcome 是两件事，前者可能一个 run 出现多条（每次尝试一条），
            # 后者恰好一条。
            self._run_outcome = "failed"
        except Exception as exc:
            msg = str(exc)
            logger.exception("下载过程发生未知异常: {}", self.url)
            pct = getattr(self, "progress_val", 0.0)

            self._clean_logger.force_update("error", pct, f"❌ 错误: {msg}")

            # 兼容旧逻辑：如果是纯文本 Exception，依然通过 translate_error 进行基本的处理
            # 实际上 translate_error 也可以被废弃，我们现在直接传结构化 dict
            err_dict = translate_error(exc)
            # 这是 run 的第二个失败边界：内层 `except YtDlpExecutionError` 管 yt-dlp
            # 自己报的错，这里管管线里出的错（后处理、沙盒移动、feature 抛异常）。
            # 两条路互斥 —— 内层走完必定 raise `DownloadFailed`/`DownloadCancelled`，
            # 那两个都在上面自己的分支里被接住，不会落到这里，所以不会二次 emit。
            _emit_failure_diagnosis(
                self.trace, err_dict, stage="download", operation="pipeline", exc=exc
            )
            self._run_outcome = "failed"
            self.error.emit(err_dict)
        finally:
            self._finish_run()
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
            build_subtitle_args,
            log_pot_from_output,
            log_pot_in_argv,
            prepare_yt_dlp_env,
            resolve_yt_dlp_exe,
        )

        exe = resolve_yt_dlp_exe()
        if exe is None:
            self._run_outcome = "failed"
            self.error.emit({"title": "错误", "message": "yt-dlp 可执行文件未找到"})
            return

        # 构建最精简的 CLI 参数
        # 刻意**不加** `--no-warnings`：纯字幕模式恰恰是最需要 `[info] There are no
        # subtitles for the requested languages` 和字幕限流警告的那条路 —— 这条路上
        # 一个 `.vtt` 都没写的时候，那两行是用户唯一能拿到的原因。
        cmd: list[str] = [str(exe), "--ignore-config", "--newline"]

        # 浅拷贝：下面只读标量/列表，而 `_resolve_subtitle_prefs` 要原地改写 ——
        # 不能让它写到 `self.opts` 这份共享状态上。
        opts = dict(self.opts)

        # 字幕语言迟解析：纯字幕模式恰恰是最需要真实字幕键的那条路。
        # 这条快速通道在 `run()` 的 `merged` 之前就 return 了，所以必须单独调一次。
        self._resolve_subtitle_prefs(opts)

        # ── 期望产物 ──
        # 两处覆写不是可选的：下面 `--skip-download` 是**无条件**追加的（这条路永远不下
        # 主媒体），`build_subtitle_args(allow_embed=False)` 也把嵌入关掉了。照 `opts`
        # 原样记会期望一个 `media`，于是每个纯字幕任务都报"主文件缺失"。
        expect_opts = dict(opts)
        expect_opts["skip_download"] = True
        expect_opts["embedsubtitles"] = False
        emit_expect(
            expect_opts,
            trace=self.trace,
            stage="select",
            component="lightweight_extract",
        )

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

        # 字幕相关。与完整下载路径共用 `build_subtitle_args()` —— 这里原本是一份手抄的
        # 副本，`--sub-langs` 的解析逻辑一改就只有完整路径吃到，纯字幕模式还在走老的。
        # `allow_embed=False`：下面就是 `--skip-download`，没有视频可嵌字幕。
        cmd += build_subtitle_args(opts, allow_embed=False)

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

        # 脱敏后落 `kind=argv`：这条路的 argv 同样带 `--cookies` 与输出路径。
        # 见 `executor._execute_native` 里同名事件的说明。
        emit_event(
            "argv",
            trace=self.trace,
            stage="download",
            component="lightweight_extract",
            argv=render_argv(cmd),
        )
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
        # 这条路没有 Executor，也就没有 `executor.diag_lines`。自己收一份，判据与下载路径
        # 共用 `diagnostics/collect.py`（连 maxlen 默认值一起共用）—— 两边分头维护
        # "哪些行算线索"就是当初 `[download] ... Skipping` 只有一处认得的由来。
        diag_lines = DiagnosticLineCollector()
        # 这条路没有 Executor 的 `on_file_created`，产物只能从输出行里捞。
        # `_RE_WRITING_TO` 同时认字幕和封面（两者都返回 `type="subtitle"` + `path`），
        # 所以纯封面轻量模式也一样有落点。
        produced: set[str] = set()
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
                    self._run_outcome = "cancelled"
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
                    # 存原始行而不是 `parsed.message`：`parse_events` 的 bareLine 通道
                    # 要靠 `[info]` 前缀之后的完整原文匹配。收不收由 collect 判，进度行
                    # 提前排掉，省掉每条都走一遍规则匹配。
                    if parsed.type not in ("progress", "ffmpeg_progress"):
                        diag_lines.feed(line)
                    if parsed.path:
                        # 不按 type 过滤：任何被 yt-dlp 报出路径的行都算"它写了这个文件"，
                        # 归类交给 `found_artifacts()` 按后缀做（那里也是下载路径的判据）。
                        produced.add(parsed.path)
                    if parsed.type == "warning":
                        logger.warning("[LightweightExtract] {}", line)
                    elif parsed.type == "error":
                        logger.error("[LightweightExtract] {}", line)
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
                # 传 `YtDlpExecutionError` 而不是裸 `RuntimeError`：`translate_error()` 只对
                # 前者取 `stderr` 去跑规则表，后者只有 "yt-dlp 退出码 1" 这句话可诊断，
                # 收了一路的诊断行全数丢弃。
                fail = YtDlpExecutionError(exit_code=rc, stderr=diag_lines.as_text())
                err = translate_error(fail)
                _emit_failure_diagnosis(
                    self.trace, err, stage="download", operation="lightweight_extract", exc=fail
                )
                self._run_outcome = "failed"
                self.error.emit(err)
            else:
                # 纯字幕模式下这一扫尤其要紧：一个 `.vtt` 都没写出来时，"✅ 提取完成"
                # 是句彻底的空话。任务仍然算成功（字幕是 best-effort），但原因必须露出来。
                sub_warning = self._scan_subtitle_warnings(diag_lines)
                if sub_warning:
                    self._clean_logger.force_update("processing", 99.0, f"⚠️ {sub_warning}")
                # `_scan_subtitle_warnings` 只把结论推给 UI，一关窗口就没了。同一批行
                # 再走一遍 `signal`，日志里才留下 `code=...`（硬规则 2：成功路径不做
                # 主因仲裁）。传 `self.trace` 而不是 `current_flow()` —— 这里手上就有。
                emit_success_signals(
                    diag_lines.as_text(),
                    trace=self.trace,
                    stage="download",
                    operation="lightweight_extract",
                )
                # 这条路的 `actual` 尤其是整件事的重点：rc=0、UI 写"✅ 提取完成"、
                # 而一个字幕文件都没写出来，正是用户最初报的那个查不出来的问题。
                # 不传 `output_path`：这条路没有主媒体，也就没有容器可判。
                emit_actual(
                    produced,
                    trace=self.trace,
                    stage="verify",
                    component="lightweight_extract",
                )
                self._clean_logger.force_update("completed", 100.0, "✅ 提取完成")
                self._run_outcome = "success"
                self.completed.emit()

        except Exception as exc:
            logger.exception("[LightweightExtract] 提取失败: {}", self.url)
            self._clean_logger.force_update("error", 0.0, f"❌ 错误: {exc}")
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="download", operation="lightweight_extract", exc=exc
            )
            self._run_outcome = "failed"
            self.error.emit(err)
        finally:
            self.is_running = False

    def _run_cover_direct_download(self) -> None:
        """纯图片文件直接下载：当明确得知 URL 就是一个图片时，使用干净的 yt-dlp 避免各种干扰。"""
        import subprocess

        from ..youtube.yt_dlp_cli import prepare_yt_dlp_env, resolve_yt_dlp_exe

        exe = resolve_yt_dlp_exe()
        if exe is None:
            self._run_outcome = "failed"
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

        # ── 期望产物 ──
        # 这条路的期望**不能**交给 `emit_expect(opts)` 推：它下的本来就是一个图片 URL，
        # 产物按后缀归类成 `thumbnail`，而 opts 里没有 `skip_download` —— 套通用规则会
        # 期望出一个 `media`，于是每次封面直下都同时报"主文件缺失 + 多下了一张封面"。
        self.trace.expect_artifacts({THUMBNAIL})
        emit_event(
            "expect",
            trace=self.trace,
            stage="select",
            component="cover_direct",
            artifacts=[THUMBNAIL],
        )

        # 封面直下这条路上面刚拼过 `--proxy`，代理里的账号密码就在 argv 里。
        emit_event(
            "argv",
            trace=self.trace,
            stage="download",
            component="cover_direct",
            argv=render_argv(cmd),
        )
        env = prepare_yt_dlp_env()

        extra_kw: dict[str, Any] = {}
        if os.name == "nt":
            try:
                extra_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            except Exception:
                pass

        self._clean_logger.force_update("downloading", 0.0, "⚡ 正在下载图片...")
        # 这条路只解析 "Destination:" 一行来推进度条，失败时原本什么线索都留不下。
        cover_diag = DiagnosticLineCollector()
        produced: set[str] = set()

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
                    self._run_outcome = "cancelled"
                    self.cancelled.emit()
                    return
                # Minimal parsing for progress bar feeling
                try:
                    line = raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line and "Destination:" in line:
                    dest = line.split("Destination: ")[-1].strip()
                    produced.add(dest)
                    self._clean_logger.force_update(
                        "downloading",
                        50.0,
                        f"正在保存: {os.path.basename(dest)}",
                    )
                elif line:
                    cover_diag.feed(line)

            rc = proc.wait()
            self._proc_ref = None
            if rc != 0:
                self._clean_logger.force_update("error", 100.0, f"❌ 错误: yt-dlp 退出码 {rc}")
                fail = YtDlpExecutionError(exit_code=rc, stderr=cover_diag.as_text())
                err = translate_error(fail)
                _emit_failure_diagnosis(
                    self.trace, err, stage="download", operation="cover_direct", exc=fail
                )
                self._run_outcome = "failed"
                self.error.emit(err)
            else:
                # 这条路上面拼了 `--no-warnings`（:1594），成功时 `cover_diag` 必然是空的 ——
                # 所以**不**跟一个 `emit_success_signals()`：那会是一个恒为 0 的调用，
                # 只会让读代码的人以为封面直下也在扫成功路径的征兆。它收诊断行纯粹是
                # 为了 rc≠0 时喂 `diagnose()`。
                # 不传 `output_path`：产物是一张图，没有容器可判。
                emit_actual(
                    produced,
                    trace=self.trace,
                    stage="verify",
                    component="cover_direct",
                )
                self._clean_logger.force_update("completed", 100.0, "✅ 下载完成")
                self._run_outcome = "success"
                self.completed.emit()
        except Exception as exc:
            logger.exception("[CoverDirect] 提取失败: {}", self.url)
            self._clean_logger.force_update("error", 0.0, f"❌ 错误: {exc}")
            err = translate_error(exc)
            _emit_failure_diagnosis(
                self.trace, err, stage="download", operation="cover_direct", exc=exc
            )
            self._run_outcome = "failed"
            self.error.emit(err)
        finally:
            self.is_running = False

    def stop(self) -> None:
        """向后兼容的别名：调用 cancel() 安全取消下载。"""
        self.cancel()
