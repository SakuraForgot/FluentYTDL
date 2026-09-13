"""
Clean Logger 模块
统一汇聚下载引擎底层的进度回调、状态消息与合并钩子，
产出干净统一的 (状态码, 进度(0-100), 友好信息字符串) 并回调给外层。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fluentytdl.utils.ui_text import tr_text


class _StreamPhase:
    """单次下载任务中的多流阶段追踪器"""

    # 阶段定义: (阶段名, 输出进度范围起始, 输出进度范围宽度)
    VIDEO = ("video", 0.0, 50.0)  # 0%  -> 50%
    AUDIO = ("audio", 50.0, 45.0)  # 50% -> 95%
    POST = ("post", 95.0, 4.0)  # 95% -> 99%
    SINGLE = ("single", 0.0, 95.0)  # 单流模式: 0% -> 95%

    def __init__(self):
        self._phase = None
        self._is_multi_stream = False
        self._stream_count = 0

    def detect_phase(self, info_dict: dict) -> tuple:
        """根据 vcodec/acodec 判断当前下载的流类型，返回 (阶段, 是否发生了切换)"""
        vcodec = (info_dict.get("vcodec") or "").lower()
        acodec = (info_dict.get("acodec") or "").lower()

        has_video = vcodec and vcodec != "none" and vcodec != "na"
        has_audio = acodec and acodec != "none" and acodec != "na"

        if self._phase is None:
            if has_video and not has_audio:
                self._phase = self.VIDEO
                self._is_multi_stream = True
            elif has_audio and not has_video:
                self._phase = self.SINGLE if self._stream_count == 0 else self.AUDIO
            else:
                self._phase = self.SINGLE
            self._stream_count += 1
            return (self._phase, True)

        if self._phase == self.VIDEO and has_audio and not has_video:
            self._phase = self.AUDIO
            self._stream_count += 1
            return (self._phase, True)

        return (self._phase, False)

    def map_progress(self, phase: tuple, raw_pct: float) -> float:
        """将单流内的原始百分比映射到全局进度"""
        if phase is None:
            phase = self.SINGLE
        _, start, width = phase
        return start + (raw_pct / 100.0) * width


class CleanLogger:
    """
    状态收敛日志器。

    接收 yt-dlp 原始数据、进度及日志，进行多模态翻译，并提供单一的 emit_status 回调。
    状态码参考：
    - "queued": 等待中
    - "downloading": 正在下载
    - "processing": 后处理中 (合并、转码等)
    - "finished": 完成
    - "error": 发生错误
    - "paused": 暂停
    """

    def __init__(
        self,
        callback: Callable[[str, float, str], None],
        duration: float = 0.0,
        playlist_tracker: Any = None,
        section_cut_mode: str = "",
        section_duration: float = 0.0,
        section_start: float = 0.0,
        section_stream_layout: str = "",
        section_estimated_bytes: int = 0,
        *,
        trace: Any = None,
    ):
        """
        :param callback: 向外发射的清理后信号方法 (状态码, float进度, 友好状态文案)
        :param playlist_tracker: 可选的 PlaylistProgressTracker 实例
        :param trace: 可选的 `TaskTrace`，仅用于把多流阶段切换落成 `kind=stage`。
            为什么传对象而不是 import observability：`utils/` 是 Foundation 层，
            反向 import 上层会成环；trace 自带 `emit()` 出口，方向天然是对的。
        """
        self.callback = callback
        self.playlist_tracker = playlist_tracker
        self.trace = trace
        self._current_state = "queued"
        self._current_percent = 0.0
        self._current_msg = tr_text("等待下载...")
        self._stream_phase = _StreamPhase()
        self._phase_just_switched = False
        self._duration = duration
        self._section_cut_mode = section_cut_mode
        self._section_duration = section_duration
        self._section_start = section_start
        self._section_stream_layout = section_stream_layout
        self._section_estimated_bytes = max(0, int(section_estimated_bytes or 0))
        self._active_postprocessor = ""
        self._last_ffmpeg_bytes = 0
        self._last_ffmpeg_tick = 0.0

    def _emit(self, state: str, percent: float, msg: str) -> None:
        # 进度不后退规则（仅在同一阶段内生效）
        # 阶段切换时允许视觉上的 "重置"（实际是映射后的递增）
        if state in ("downloading", "processing", "finished"):
            if percent < self._current_percent and percent != 0.0:
                if not getattr(self, "_phase_just_switched", False):
                    percent = self._current_percent
        self._phase_just_switched = False

        self._current_state = state
        self._current_percent = percent
        self._current_msg = msg
        self.callback(state, percent, msg)

    def force_update(self, state: str, percent: float, msg: str) -> None:
        """人工强制刷新指定状态"""
        self._emit(state, percent, msg)

    def _emit_phase_stage(self, phase: tuple) -> None:
        """把一次多流阶段切换（视频流 → 音频流 / 单流启动）落成一条 `kind=stage`。

        为什么单独记这个：进度不进事件层（那会把 JSONL 灌成进度数据库），但
        "音轨阶段到底有没有开始过"是排查"多音轨视频只下到视频没有声音"的头号问题
        —— `detect_phase()` 返回 `switched=True` 恰好就是这个语义变化点，且是全项目
        唯一看得见 vcodec/acodec 切换的地方。

        best-effort：没有 trace（测试、独立调用）或 emit 失败都不该影响下载（硬规则 5）。
        """
        trace = getattr(self, "trace", None)
        if trace is None:
            return
        try:
            phase_name = phase[0] if phase else "single"
            # `stage="download"` 固定：这是下载流水线内部的子阶段切换，不是流水线换段。
            # 用 `phase=` 字段区分 video/audio/single，`stage` 维持 grep 友好的稳定值。
            trace.emit("stage", stage="download", _depth=3, phase=phase_name)
        except Exception:
            pass

    def handle_status(self, raw_status_msg: str) -> None:
        """处理一般性的 status 消息 (通常来自 FFmpeg 等后处理步骤、或者字幕转换)"""
        if self._current_state in ("completed", "error", "paused", "cancelled"):
            return

        msg = raw_status_msg.strip()

        if "Deleting original file" in msg:
            return

        if msg.startswith("⚠️"):
            # `executor._execute_native` 的 warning 分支唯一的出口。翻译链认不出它，
            # 结尾的 `if processed_msg:` 会静默丢掉 —— 而去掉 `--no-warnings` 之后，
            # 这些行恰恰是"任务成功但字幕为空"的唯一线索。
            # 沿用当前状态与进度：一条警告不代表阶段发生了变化，更不代表失败。
            self._emit(self._current_state, self._current_percent, msg)
            return

        if "There are no subtitles for the requested languages" in msg:
            # 这句是 `[info]` 级 —— `--no-warnings` 管不着它，它是死在下面那个
            # `[info]` 提前 return 上的（"📡 正在获取流媒体元数据"）。
            # 而它恰恰是"字幕开着却一个文件都没有"最直接的一句解释。
            # 同样沿用当前状态：字幕是 best-effort，任务照旧成功。
            self._emit(
                self._current_state, self._current_percent, tr_text("⚠️ 请求的语言没有可用字幕")
            )
            return

        if "Retrying" in msg or "retrying" in msg:
            import re

            m = re.search(r"\((\d+/\d+)\)", msg)
            retry_count = m.group(1) if m else ""
            if "fragment" in msg.lower():
                processed_msg = tr_text("🔄 切片下载超时，正在重试... {0}", retry_count)
            else:
                processed_msg = tr_text("🔄 网络请求失败，正在重试... {0}", retry_count)

            if self.playlist_tracker:
                final_msg = f"[{self.playlist_tracker.current_item}/{self.playlist_tracker.total_items}] {self.playlist_tracker.current_title} — {processed_msg}"
                self._emit("processing", self.playlist_tracker.overall_percent, final_msg)
            else:
                self._emit("processing", self._current_percent, processed_msg)
            return

        # 拦截前置准备动作 (Parsing Phase)
        if "] Extracting URL" in msg:
            self._emit("parsing", 0, tr_text("🔍 正在解析目标地址..."))
            return
        elif msg.startswith("[hlsnative]"):
            self._emit("parsing", 0, tr_text("🧩 正在组装 m3u8 碎片地图..."))
            return

        # 播放列表进度拦截
        if self.playlist_tracker:
            pt_res = self.playlist_tracker.parse_line(msg)
            if pt_res:
                p_state, p_pct, p_msg = pt_res
                self._emit(p_state, p_pct, p_msg)
                return
            elif msg.startswith("[download] Destination:") or msg.startswith(
                "[ExtractAudio] Destination:"
            ):
                # 获取当前视频的文件名（有时可以从中提取标题）
                import os

                # Extract filename without path and extension
                basename = os.path.basename(msg.split("Destination: ")[1].strip())
                title = os.path.splitext(basename)[0]
                self.playlist_tracker.current_title = title
                self._emit(
                    "downloading",
                    self.playlist_tracker.overall_percent,
                    self.playlist_tracker.build_status_text(),
                )
                return
            elif "has already been downloaded" in msg:
                self.playlist_tracker.mark_item_completed()
                self._emit(
                    "downloading",
                    self.playlist_tracker.overall_percent,
                    self.playlist_tracker.build_status_text(),
                )
                return

        # 翻译并判断是否为后处理阶段
        processed_msg = ""
        # 英文日志到中文的翻译
        if "Merging formats into" in msg or "[Merger]" in msg:
            processed_msg = tr_text("📦 正在无损合并音视频 (FFmpeg)...")
        elif "[ExtractAudio]" in msg:
            processed_msg = tr_text("🎵 正在提取独立音频流...")
        elif "Writing video subtitles to" in msg:
            processed_msg = tr_text("📝 正在下载字幕...")
        # 人类日志行的前缀也是短名（实测：`[Merger]` / `[ExtractAudio]` / `[Metadata]` /
        # `[ThumbnailsConvertor]` / `[MoveFiles]`），所以 `[FFmpegSubtitlesConvertor]`
        # 这个写法从来没匹配过 —— 判短名，长名留作版本兜底。
        elif "[SubtitlesConvertor]" in msg or "[FFmpegSubtitlesConvertor]" in msg:
            processed_msg = tr_text("📝 正在转换字幕格式...")
        elif "Embedding subtitles in" in msg:
            processed_msg = tr_text("📝 正在内嵌字幕轨道...")
        elif "Writing metadata to" in msg or "[MetadataParser]" in msg:
            processed_msg = tr_text("🏷️ 正在写入视频元数据 (标题/作者)...")
        elif "ThumbnailsConvertor" in msg:
            processed_msg = tr_text("🖼️ 正在转换视频封面图...")
        elif "EmbedThumbnail" in msg:
            processed_msg = tr_text("🖼️ 正在嵌入视频封面图...")
        elif "Writing video thumbnail" in msg:
            processed_msg = tr_text("🖼️ 正在下载视频封面图...")

        if processed_msg:
            # 单 Worker 播放列表模式：替换 msg 但保持整体进度格式
            if self.playlist_tracker:
                if "Merging formats into" in msg or "[Merger]" in msg:
                    # 合并开始，可以认为当前条目下载已经 100%（不过 yt-dlp 通常会有 separate log）
                    pass
                msg = tr_text(
                    "[{0}/{1}] {2} — 后处理: {3}",
                    self.playlist_tracker.current_item,
                    self.playlist_tracker.total_items,
                    self.playlist_tracker.current_title,
                    processed_msg.replace("...", ""),
                )
                self._emit("processing", self.playlist_tracker.overall_percent, msg)
            else:
                self._emit("processing", self._current_percent, processed_msg)
        elif msg.startswith("[info]") and self._current_state in ("queued", "parsing"):
            # 翻译链认不出的 `[info]`。这条 return 以前排在翻译链**前面**，于是
            # `[info] Writing video subtitles to:` 永远走不到上面那条字幕翻译 ——
            # 那行翻译一直是死代码。现在只在真的还没开始下载时才当作"取元数据"：
            # 下载中途的 `[info] Downloading 1 format(s)` 若也走这条，状态会从
            # downloading 倒退回 parsing。
            self._emit("parsing", 0, tr_text("📡 正在获取流媒体元数据..."))

    def handle_progress(self, progress_data: dict[str, Any]) -> None:
        """处理 yt-dlp 的原生 progress 回调 (来自 dict)"""
        if self._current_state in ("completed", "error", "paused", "cancelled"):
            return

        status = progress_data.get("status", "downloading")

        if status == "section_file_progress" and self._section_cut_mode:
            output_bytes = int(progress_data.get("output_bytes") or 0)
            rate = float(progress_data.get("speed") or 0.0)
            raw_pct = 0.0
            if self._section_estimated_bytes:
                raw_pct = min(99.0, output_bytes * 100.0 / self._section_estimated_bytes)
            elif output_bytes:
                # No upstream size/bitrate is available. Keep the bar moving
                # while clearly marking the total as an estimate-in-progress.
                raw_pct = 90.0 * (1.0 - 1.0 / (1.0 + output_bytes / (10 * 1024 * 1024)))
            pct = round(self._stream_phase.map_progress(_StreamPhase.SINGLE, raw_pct), 1)
            layout = self._section_layout_label()
            total = (
                self._format_bytes(self._section_estimated_bytes)
                if self._section_estimated_bytes
                else tr_text("估算中")
            )
            speed = self._format_bytes(rate) + "/s" if rate else tr_text("计算中")
            self._emit(
                "downloading",
                pct,
                tr_text(
                    "✂️ 裁切下载 · {0} | 已写入 {1}/{2} | ⬇️ {3}",
                    layout,
                    self._format_bytes(output_bytes),
                    total,
                    speed,
                ),
            )
            return

        if status == "ffmpeg_progress":
            is_precise_cut = (
                self._section_cut_mode == "precise"
                and self._active_postprocessor == "ModifyChapters"
            )
            # Both coarse and precise clips begin with FFmpegFD downloading the
            # selected range. Precise clips only switch to re-encoding when
            # yt-dlp subsequently starts ModifyChapters.
            is_section_download = bool(self._section_cut_mode) and not is_precise_cut
            if not is_precise_cut and not is_section_download:
                return
            time_sec = progress_data.get("time_sec", 0.0)
            speed = progress_data.get("speed", "1x")

            # ffmpeg's -ss input seek may report either source timestamps or
            # timestamps rebased to zero, depending on the selected stream.
            # Normalize the former to the requested clip duration.
            clip_time = float(time_sec or 0.0)
            if self._section_start and clip_time > self._section_duration:
                clip_time -= self._section_start

            if self._section_duration > 0:
                raw_pct = (clip_time / self._section_duration) * 100.0
                raw_pct = min(100.0, max(0.0, raw_pct))
            else:
                raw_pct = 50.0  # unknown duration

            phase = _StreamPhase.POST if is_precise_cut else _StreamPhase.SINGLE
            pct = self._stream_phase.map_progress(phase, raw_pct)
            pct = round(pct, 1)
            if is_precise_cut:
                msg = tr_text("✂️ 正在精确裁切与重编码 {0:.1f}% | 速度: {1}", raw_pct, speed)
                self._emit("processing", pct, msg)
            else:
                output_bytes = int(progress_data.get("output_bytes") or 0)
                output_rate = self._update_ffmpeg_output_rate(output_bytes)
                written = self._format_bytes(output_bytes)
                total = (
                    self._format_bytes(self._section_estimated_bytes)
                    if self._section_estimated_bytes
                    else tr_text("估算中")
                )
                layout = self._section_layout_label()
                rate = self._format_bytes(output_rate) + "/s" if output_rate else tr_text("计算中")
                msg = tr_text(
                    "✂️ 裁切下载 · {0} {1:.1f}% | 已写入 {2}/{3} | ⬇️ {4} | 媒体速度 {5}",
                    layout,
                    raw_pct,
                    written,
                    total,
                    rate,
                    speed,
                )
                self._emit("downloading", pct, msg)
            return

        if status == "postprocess":  # 来自 FLUENTYTDL|postprocess| 钩子
            pp_name = progress_data.get("postprocessor", "Unknown")
            pp_status = progress_data.get("pp_status", "")  # started/finished
            self._active_postprocessor = str(pp_name)

            if pp_status == "started":
                if pp_name == "ModifyChapters" and self._section_cut_mode == "precise":
                    msg = tr_text("✂️ 片段下载完成，正在准备精确裁切…")
                elif pp_name == "Merger":
                    msg = tr_text("📦 正在无损合并音视频 (FFmpeg)...")
                elif pp_name == "EmbedSubtitle":
                    msg = tr_text("📝 正在内嵌字幕轨道...")
                # `FFmpegMetadataPP` 报的是 `Metadata`（剥掉 `FFmpeg` 前缀和 `PP` 后缀，
                # 实测见 `output_parser.EMBED_EVIDENCE_BY_PP` 上方）—— 长键从未命中，
                # 于是元数据一直掉进下面那条泛用分支。`MetadataParser` 是另一个后处理器，
                # 它本来就是短名。
                elif pp_name in ("Metadata", "MetadataParser", "FFmpegMetadata"):
                    msg = tr_text("🏷️ 正在写入视频元数据 (标题/作者)...")
                elif pp_name == "ThumbnailsConvertor":
                    msg = tr_text("🖼️ 正在转换视频封面图...")
                elif pp_name == "EmbedThumbnail":
                    msg = tr_text("🖼️ 正在嵌入视频封面图...")
                elif pp_name == "MoveFiles":
                    msg = tr_text("🚚 正在移动文件...")
                elif pp_name == "SponsorBlock":
                    msg = tr_text("⏭️ 正在标记/跳过赞助片段...")
                else:
                    msg = tr_text("⚙️ 正在执行后期处理 ({0})...", pp_name)
                self._emit("processing", self._current_percent, msg)
            return

        dl_bytes = progress_data.get("downloaded_bytes") or 0
        tot_bytes = (
            progress_data.get("total_bytes") or progress_data.get("total_bytes_estimate") or 0
        )
        speed = progress_data.get("speed") or 0
        eta = progress_data.get("eta") or 0
        filename = progress_data.get("filename", "")

        pct = 0.0
        if tot_bytes > 0:
            pct = (dl_bytes / tot_bytes) * 100.0
        elif dl_bytes > 0:
            # 未知总大小时：对数增长曲线，缓慢逼近 90% 但永远不会到达
            # 公式: pct = 90 * (1 - 1/(1 + dl_bytes/10MB))
            # 效果: 10MB->45%, 50MB->81%, 100MB->86%, 500MB->89%
            pct = 90.0 * (1.0 - 1.0 / (1.0 + dl_bytes / (10 * 1024 * 1024)))

        pct = round(pct, 1)

        if status == "downloading":
            # 💡 流类型识别：优先按文件名后缀判断字幕/封面，
            # 因为字幕下载时 info_dict 的 vcodec/acodec 仍是主视频的值，不可靠。
            stream_key = "data"
            info = progress_data.get("info_dict", {})
            vcodec = info.get("vcodec", "") if info else ""
            acodec = info.get("acodec", "") if info else ""

            # 1. 文件名后缀优先（最准确）
            if filename:
                lower_name = filename.lower()
                if lower_name.endswith((".vtt", ".srt", ".ass", ".ssa", ".sub", ".lrc")):
                    stream_key = "subtitle"
                elif lower_name.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    stream_key = "cover"
                elif lower_name.endswith((".m4a", ".mp3", ".aac", ".ogg", ".wav", ".opus")):
                    stream_key = "audio"
                elif lower_name.endswith((".mp4", ".webm", ".mkv", ".flv", ".mov")):
                    stream_key = "video"

            # 2. 文件名无法判断时，回退到 codec 判断
            if stream_key == "data":
                if vcodec and vcodec.lower() != "none":
                    stream_key = "video"
                elif acodec and acodec.lower() != "none":
                    stream_key = "audio"

            stream_type = {
                "data": tr_text("📦 数据流"),
                "subtitle": tr_text("📝 字幕"),
                "cover": tr_text("🖼️ 封面"),
                "audio": tr_text("🎵 音频流"),
                "video": tr_text("🎬 视频流"),
            }[stream_key]

            # ── 多流阶段感知 ──
            if info:
                phase, switched = self._stream_phase.detect_phase(info)
                if switched:
                    self._phase_just_switched = True
                    self._emit_phase_stage(phase)
                pct = self._stream_phase.map_progress(phase, pct)
                pct = round(pct, 1)

            speed_str = self._format_bytes(speed) + "/s"
            downloaded_str = self._format_bytes(dl_bytes)
            total_str = self._format_bytes(tot_bytes) if tot_bytes > 0 else "?"
            eta_str = self._format_time(eta)

            prefix = tr_text("✂️ 正在下载裁切片段 | ") if self._section_cut_mode else ""
            detail_text = tr_text(
                "{0}{1} | ⬇️ {2} | {3}/{4} | 剩余: {5}",
                prefix,
                stream_type,
                speed_str,
                downloaded_str,
                total_str,
                eta_str,
            )

            # 如果是播放列表模式，劫持最终输出
            if self.playlist_tracker:
                if info and info.get("title"):
                    self.playlist_tracker.current_title = info.get("title")
                self.playlist_tracker.update_item_progress(pct, speed, eta, dl_bytes)
                if pct >= 100.0:
                    self.playlist_tracker.mark_item_completed()

                # 发射组装好的文本，但是进度条是总体进度
                final_pct = self.playlist_tracker.overall_percent
                final_msg = self.playlist_tracker.build_status_text()
                self._emit("downloading", final_pct, final_msg)
            else:
                self._emit("downloading", pct, detail_text)

        elif status == "finished":
            # [download] 标明 finish，但后续可能有 [ffmpeg] 处理
            if self._section_cut_mode == "precise":
                self._emit("processing", 95.0, tr_text("✂️ 片段下载完成，正在准备精确裁切…"))
            elif self._section_cut_mode:
                self._emit("processing", 95.0, tr_text("✂️ 正在整理裁切文件…"))
            else:
                self._emit("processing", 99.0, tr_text("下载流完毕，等待后续合并与处理..."))

    def _format_bytes(self, bytes_val: float) -> str:
        if not bytes_val:
            return "0.00B"
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_val < 1024.0:
                return f"{bytes_val:.2f}{unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.2f}TB"

    def _update_ffmpeg_output_rate(self, output_bytes: int) -> float:
        """Estimate write/network throughput from consecutive FFmpeg reports."""
        now = time.monotonic()
        rate = 0.0
        if self._last_ffmpeg_tick and output_bytes >= self._last_ffmpeg_bytes:
            elapsed = now - self._last_ffmpeg_tick
            if elapsed > 0:
                rate = (output_bytes - self._last_ffmpeg_bytes) / elapsed
        self._last_ffmpeg_tick = now
        self._last_ffmpeg_bytes = output_bytes
        return rate

    def _section_layout_label(self) -> str:
        labels = {
            "muxed": tr_text("整合流（视频 + 音频）"),
            "video_audio": tr_text("视频 + 音频分流"),
            "video": tr_text("视频流"),
            "audio": tr_text("音频流"),
        }
        return labels.get(self._section_stream_layout, tr_text("媒体流"))

    def _format_time(self, seconds: int) -> str:
        if not seconds:
            return "--:--"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"
