from __future__ import annotations

import os
import threading
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..core.config_manager import config_manager
from ..utils.logger import logger
from ..utils.translator import translate_error
from ..youtube.youtube_service import YoutubeServiceOptions, youtube_service
from ..youtube.yt_dlp_cli import YtDlpCancelled
from .dispatcher import download_dispatcher
from .executor import DownloadExecutor
from .features import (
    DownloadContext,
    MetadataFeature,
    SponsorBlockFeature,
    SubtitleFeature,
    ThumbnailFeature,
    VRFeature,
)
from .strategy import DownloadMode, get_fallback


class DownloadCancelled(Exception):
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
    ):
        super().__init__()
        self.url = url
        self.options = options
        self.playlist_flat = playlist_flat
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
                    self.url, self.options, cancel_event=self._cancel_event
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
            # 1. 如果 URL 看起来像播放列表，先尝试 Flat 解析
            # 2. 如果 Flat 解析发现是单视频（或 URL 不像播放列表），则使用 android_vr 客户端进行深度 VR 解析

            is_playlist_url = "list=" in self.url
            info = None

            if is_playlist_url:
                try:
                    # 尝试作为播放列表解析
                    info = youtube_service.extract_playlist_flat(
                        self.url, cancel_event=self._cancel_event
                    )

                    # 检查是否真的是播放列表
                    if info.get("_type") != "playlist" and not info.get("entries"):
                        # 只有单个条目或不是播放列表，视为单视频，需要重新解析
                        info = None
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
    ):
        super().__init__()
        self.row = row
        self.url = url
        self.options = options
        self.vr_mode = vr_mode
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
                    self.url, self.options, cancel_event=self._cancel_event
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

    支持进度回调与取消（Phase 3 先实现取消；暂停在后续阶段做）。
    """

    progress = Signal(dict)  # 发送 yt-dlp 的进度字典
    completed = Signal()  # 下载完成（避免与 QThread.finished 冲突）
    cancelled = Signal()  # 用户暂停/取消
    error = Signal(dict)  # 发生错误（结构化）
    status_msg = Signal(str)  # 状态文本 (正在合并/正在转换...)
    output_path_ready = Signal(str)  # 最终输出文件路径（尽力解析）
    cookie_error_detected = Signal(str)  # Cookie 错误检测（触发修复流程）
    thumbnail_embed_warning = Signal(str)  # 封面嵌入警告（格式不支持时）

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
        self._format_warning_shown = False  # 防止重复警告

        # 初始化功能模块
        self.features = [
            SponsorBlockFeature(),
            MetadataFeature(),
            SubtitleFeature(),
            ThumbnailFeature(),
            VRFeature(),
        ]
        self.cached_info = cached_info

    def run(self) -> None:
        self.is_running = True
        self.is_cancelled = False
        try:
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
            merged = dict(base_opts)
            merged.update(self.opts)

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

            # === Feature Pipeline: Configuration & Pre-flight ===
            # 构建上下文并运行 Feature 链
            context = DownloadContext(self, merged)

            for feature in self.features:
                feature.configure(merged)
                feature.on_download_start(context)

            # Capture intent flags before stripping
            merged.get("__fluentytdl_use_android_vr", False)
            merged.get("embedsubtitles", False)

            # Strip internal meta options (never pass to yt-dlp)
            for k in list(merged.keys()):
                if isinstance(k, str) and k.startswith("__fluentytdl_"):
                    merged.pop(k, None)

            # === Phase 2: 断点续传支持 ===
            if config_manager.get("enable_resume", True):
                merged["continuedl"] = True  # 继续下载部分文件

            # === 调度策略 ===
            dl_mode_str = config_manager.get("download_mode", "auto")
            mode = DownloadMode(dl_mode_str)
            strategy = download_dispatcher.resolve(mode, merged)

            # 回调定义 (复用)
            def on_progress(data: dict[str, Any]) -> None:
                self.progress.emit(data)

            def on_status(message: str) -> None:
                self.status_msg.emit(message)

            def on_path(path: str) -> None:
                self.output_path = path

            def on_file_created(path: str) -> None:
                self.dest_paths.add(path)

            # === 执行下载 (带自动降级) ===
            while True:
                # 用户可见的模式日志
                label = strategy.label
                logger.info("🚀 启动下载 | 模式: {} | 策略: {}", strategy.mode.value, label)
                self.status_msg.emit(f"🚀 使用策略: {label}")

                self.executor = DownloadExecutor()
                try:
                    # 执行
                    final_path = self.executor.execute(
                        self.url,
                        merged,
                        strategy,
                        on_progress=on_progress,
                        on_status=on_status,
                        on_path=on_path,
                        cancel_check=lambda: self.is_cancelled,
                        on_file_created=on_file_created,
                        cached_info_dict=self.cached_info,
                    )

                    if final_path:
                        self.output_path = final_path
                        self.output_path_ready.emit(final_path)
                        # Success for circuit breaker
                        download_dispatcher.report_result(True)
                        break

                except DownloadCancelled:
                    raise

                except Exception as exc:
                    logger.warning(f"下载失败 (策略={strategy.label}): {exc}")

                    # 报告失败 (触发熔断计数)
                    download_dispatcher.report_result(False)

                    if self.is_cancelled:
                        raise DownloadCancelled() from None

                    # 尝试降级
                    fallback = get_fallback(strategy.mode)
                    if fallback:
                        logger.info(f"正在降级策略: {strategy.mode} -> {fallback.mode}")
                        self.status_msg.emit(f"⚠️ 网络不稳定，自动切换至: {fallback.label}")
                        strategy = fallback

                        # 简单的指数退避，给网络一点喘息时间
                        import time

                        time.sleep(1)
                        continue

                    # 无路可退，抛出异常
                    raise exc

            # === Feature Pipeline: Post-process ===
            # 执行各模块的后处理逻辑（封面嵌入、字幕合并、VR转码等）
            if not self.is_cancelled:
                for feature in self.features:
                    feature.on_post_process(context)
                self.completed.emit()

        except DownloadCancelled:
            self.status_msg.emit("任务已暂停")
            self.cancelled.emit()
        except Exception as exc:
            msg = str(exc)
            # 恢复 SSL / 格式降级 等错误处理逻辑 (简单版)
            if "EOF occurred in violation of protocol" in msg or "_ssl.c" in msg:
                self.status_msg.emit("⚠️ 检测到网络SSL错误，建议检查网络连接后重试")

            logger.exception("下载过程发生异常: {}", self.url)
            # Failure for circuit breaker
            download_dispatcher.report_result(False)
            self.error.emit(translate_error(exc))
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
        convertsubtitles = opts.get("convertsubtitles")
        if isinstance(convertsubtitles, str) and convertsubtitles:
            cmd += ["--convert-subs", convertsubtitles]

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

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"

        # Windows 隐藏窗口
        extra_kw: dict[str, Any] = {}
        if os.name == "nt":
            try:
                extra_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            except Exception:
                pass

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
                    proc.terminate()
                    self.cancelled.emit()
                    return

                try:
                    line = raw.decode("utf-8").rstrip("\r\n")  # type: ignore[union-attr]
                except UnicodeDecodeError:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")  # type: ignore[union-attr]

                if line:
                    logger.debug("[LightweightExtract] {}", line)
                    # 仅在日志中记录，不将原始 yt-dlp 输出暴露给用户

            rc = proc.wait()
            self._proc_ref = None

            if rc != 0:
                logger.warning("[LightweightExtract] yt-dlp 退出码 {}", rc)
                self.status_msg.emit(f"⚠️ 提取完成（退出码: {rc}）")

            self.completed.emit()

        except Exception as exc:
            logger.exception("[LightweightExtract] 提取失败: {}", self.url)
            self.error.emit(translate_error(exc))
        finally:
            self.is_running = False

    def stop(self) -> None:
        """外部调用此方法暂停/取消下载"""
        self.is_cancelled = True
        if self.executor:
            self.executor.terminate()
        # 也终止轻量提取的子进程
        proc = getattr(self, "_proc_ref", None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
