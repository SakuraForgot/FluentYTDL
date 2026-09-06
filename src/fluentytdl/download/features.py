from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING, Any

from ..core.config_manager import config_manager
from ..core.hardware_manager import hardware_manager
from ..models.subtitle_config import SUBTITLE_RESOLUTION_KEY
from ..processing.thumbnail_embed import can_embed_thumbnail, get_unsupported_formats_warning
from ..processing.thumbnail_embedder import thumbnail_embedder
from ..utils.logger import logger
from ..utils.spatialmedia import metadata_utils

if TYPE_CHECKING:
    from .workers import DownloadWorker


class DownloadContext:
    """下载上下文，用于在 Feature 和 Worker 之间传递状态"""

    def __init__(self, worker: DownloadWorker, opts: dict[str, Any]):
        self.worker = worker
        self.opts = opts
        self.url = worker.url

    @property
    def output_path(self) -> str | None:
        return self.worker.output_path

    @output_path.setter
    def output_path(self, value: str | None):
        self.worker.output_path = value

    @property
    def dest_paths(self) -> set[str]:
        return self.worker.dest_paths

    @property
    def sandbox_dir(self) -> str | None:
        return getattr(self.worker, "sandbox_dir", None) or None

    def is_in_sandbox(self, path: str | None) -> bool:
        """`path` 是否落在本任务的沙盒目录里。

        沙盒每个任务独享（`workers.py` 下载前建、成功后搬出），所以"整目录扫描"这类
        兜底手段只在沙盒内安全 —— 在共享的下载目录里扫，会把别的视频的产物也算进来。
        纯字幕 / 封面直下这类跳过沙盒的任务在这里一律得到 False。
        """
        sandbox = self.sandbox_dir
        if not sandbox or not path:
            return False
        try:
            root = os.path.normcase(os.path.abspath(sandbox))
            target = os.path.normcase(os.path.abspath(path))
            return os.path.commonpath([root, target]) == root
        except (OSError, ValueError):
            # ValueError: 跨盘符时 commonpath 直接抛
            return False

    def emit_status(self, msg: str):
        if hasattr(self.worker, "_clean_logger"):
            pct = getattr(self.worker, "progress_val", 99.0)
            self.worker._clean_logger.force_update("processing", pct, msg)
        else:
            self.worker.status_msg.emit(msg)

    def emit_warning(self, msg: str):
        logger.warning(msg)
        self.emit_status(f"⚠️ {msg}")

    def emit_thumbnail_warning(self, msg: str):
        self.worker.thumbnail_embed_warning.emit(msg)
        self.emit_status(f"⚠️ {msg}")

    def find_final_merged_file(self) -> str | None:
        """查找最终合并的输出文件"""
        output_path = self.output_path
        if not output_path:
            return None

        # 列出父目录中的实际文件（用于兜底匹配）
        parent_dir = os.path.dirname(output_path)
        actual_files: list[str] | None = None
        if os.path.isdir(parent_dir):
            try:
                actual_files = os.listdir(parent_dir)
            except OSError:
                pass

        # 检查当前 output_path 是否是分片文件
        match = re.search(r"^(.+)\.[fF]\d+\.(\w+)$", output_path)
        if match:
            base_name = match.group(1)
            possible_extensions = [".mp4", ".mkv", ".webm", ".avi", ".mov"]
            for ext in possible_extensions:
                merged_path = base_name + ext
                if os.path.exists(merged_path):
                    return merged_path
        elif os.path.exists(output_path):
            return output_path

        # 如果直接匹配失败，检查 dest_paths (例如 yt-dlp 最终下载了不同扩展名的视频)
        video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"}
        fallback_path = None
        for dest_path in self.dest_paths:
            # 排除纯分片文件
            if not re.search(r"\.[fF]\d+\.\w+$", dest_path):
                if os.path.exists(dest_path):
                    ext = os.path.splitext(dest_path)[1].lower()
                    if ext in video_exts:
                        return dest_path
                    elif fallback_path is None and ext not in {
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                        ".srt",
                        ".vtt",
                        ".ass",
                        ".lrc",
                        ".json",
                    }:
                        fallback_path = dest_path

        if fallback_path:
            return fallback_path

        # ── 兜底：目录扫描 ──
        # yt-dlp 在 Windows 上的 stdout 可能丢失特殊 Unicode 字符（如 U+30FB 片假名中点），
        # 导致解析出的路径与磁盘实际文件名不匹配。
        # 沙盒目录是隔离的（每个任务独立），因此按扩展名扫描是安全的。
        if actual_files:
            # 优先使用 output_path 的扩展名
            expected_ext = os.path.splitext(output_path)[1].lower()
            target_exts = [expected_ext] if expected_ext in video_exts else list(video_exts)

            for ext in target_exts:
                for f in actual_files:
                    if f.lower().endswith(ext):
                        resolved = os.path.join(parent_dir, f)
                        logger.warning(
                            "路径不匹配兜底生效: yt-dlp 报告的文件名与磁盘不一致，"
                            "已通过目录扫描定位到: {}",
                            resolved,
                        )
                        return resolved

        return None

    def find_thumbnail_file(self, video_path: str) -> str | None:
        """查找视频文件对应的封面文件"""
        base_path = os.path.splitext(video_path)[0]
        exts = [".jpg", ".jpeg", ".webp", ".png"]

        for ext in exts:
            candidate = base_path + ext
            if os.path.exists(candidate):
                return candidate

        match = re.match(r"^(.+)\.[fF]\d+$", base_path)
        if match:
            clean_base = match.group(1)
            for ext in exts:
                candidate = clean_base + ext
                if os.path.exists(candidate):
                    return candidate
        return None


class DownloadFeature:
    """下载功能模块基类"""

    def configure(self, ydl_opts: dict[str, Any]) -> None:
        pass

    def on_download_start(self, context: DownloadContext) -> None:
        pass

    def on_post_process(self, context: DownloadContext) -> None:
        pass


class SponsorBlockFeature(DownloadFeature):
    def configure(self, ydl_opts: dict[str, Any]) -> None:
        if not config_manager.get("sponsorblock_enabled", False):
            return
        categories = config_manager.get(
            "sponsorblock_categories", ["sponsor", "selfpromo", "interaction"]
        )
        action = config_manager.get("sponsorblock_action", "remove")
        if not categories:
            return
        if action == "remove":
            ydl_opts["sponsorblock_remove"] = categories
        elif action == "mark":
            ydl_opts["sponsorblock_mark"] = categories
        logger.info(f"[SponsorBlock] Enabled: action={action}, categories={categories}")


class MetadataFeature(DownloadFeature):
    def configure(self, ydl_opts: dict[str, Any]) -> None:
        if config_manager.get("embed_metadata", True):
            pps = ydl_opts.setdefault("postprocessors", [])
            if not any(p.get("key") == "FFmpegMetadata" for p in pps):
                pps.append({"key": "FFmpegMetadata"})
            logger.info("[Metadata] Enabled")


class SubtitleFeature(DownloadFeature):
    def on_download_start(self, context: DownloadContext) -> None:
        opts = context.opts
        if opts.get("embedsubtitles"):
            fmt = (opts.get("merge_output_format") or "").lower()
            if fmt == "webm":
                opts["merge_output_format"] = "mkv"
                logger.info("[SubEmbed] WebM → MKV")
            elif not fmt:
                opts["merge_output_format"] = "mkv"
                logger.info("[SubEmbed] 未指定 → MKV")
            # `--embed-subs` 嵌入完就把外置字幕文件删了，于是 on_post_process 无从校验
            # 字幕到底下没下到。先用 `--keep-subs` 留住，校验完再清理（见下）。
            opts["keepsubtitles"] = True
        else:
            logger.warning("[SubEmbed] embedsubtitles=False")

    def on_post_process(self, context: DownloadContext) -> None:
        opts = context.opts
        # 只有在启用了字幕下载时才执行后处理
        if not opts.get("writesubtitles") and not opts.get("writeautomaticsub"):
            return

        from ..processing import subtitle_processor

        # 纠正 output_path：分片文件（如 .f136.mp4）在合并后已被删除，需要找到最终的合并文件
        final_output = context.find_final_merged_file()
        if final_output:
            context.output_path = final_output

        try:
            result = subtitle_processor.process(
                output_path=context.output_path,
                opts=opts,
                status_callback=context.emit_status,
                # executor 解析 `Writing video subtitles to:` 时已经记下了精确路径，
                # 这是最可靠的一级定位，以前整个丢掉了
                dest_paths=context.dest_paths,
                # 整目录兜底扫描只在任务沙盒内安全，见 _find_subtitle_files 的说明
                allow_dir_scan=context.is_in_sandbox(context.output_path),
            )

            if not result.success:
                # 字幕是 best-effort：任务照样算成功，但用户必须知道字幕去哪了。
                # 以前这里只有一行 logger.warning，UI 上什么都看不到。
                logger.warning(
                    "字幕后处理失败: {}（reason={} located_by={}）",
                    result.message,
                    result.reason,
                    result.located_by,
                )
                if result.reason == "not_found":
                    context.emit_warning(self._explain_missing(opts))
                elif result.reason == "all_invalid":
                    context.emit_warning(f"字幕文件校验失败：{result.message}")
                # `video_missing` 不再提示：视频本身没落盘时早有各自的失败提示，
                # 这里再冒一条字幕警告只会盖住真正的原因

            # 内嵌模式下的外置文件是 `--keep-subs` 特意留到现在的，校验完就没用了；
            # 坏文件同样是残骸，一起清掉。
            if opts.get("embedsubtitles"):
                self._cleanup_external_subtitles(
                    list(result.processed_files) + [p for p, _ in result.invalid_files]
                )
        except Exception as e:
            logger.exception("字幕后处理异常: {}", e)

    @staticmethod
    def _cleanup_external_subtitles(paths: list[str]) -> None:
        """清理内嵌后残留的外置字幕文件。

        `on_download_start` 置的 `keepsubtitles` 让这些文件活到后处理，校验完即可删除。
        """
        cleaned = 0
        for sub_file in paths:
            try:
                if os.path.exists(sub_file):
                    os.remove(sub_file)
                    cleaned += 1
            except OSError as e:
                logger.warning("清理外置字幕残留失败: {} - {}", sub_file, e)
        if cleaned:
            logger.info("已清理 {} 个内嵌后的外置字幕文件", cleaned)

    @staticmethod
    def _explain_missing(opts: dict[str, Any]) -> str:
        """一个字幕文件都没有时，说出**具体**原因。

        原因早就备在 `SUBTITLE_RESOLUTION_KEY` 里（`subtitle_service.build_resolution_meta`），
        只是从来没人读 —— 用户能看到的只有一句"未找到字幕文件"。
        """
        meta = opts.get(SUBTITLE_RESOLUTION_KEY) or {}
        mode = meta.get("mode")
        prefs = "、".join(meta.get("prefs") or []) or "默认语言"
        matched = "、".join(meta.get("matched") or [])
        available = [str(x) for x in (meta.get("available") or [])]
        avail_txt = "、".join(available[:6]) + ("…" if len(available) > 6 else "")

        if mode == "no_match":
            return f"字幕未命中任何可用语言（想要 {prefs}；该视频只有 {avail_txt or '无可用字幕'}）"
        if mode == "pattern":
            return f"未获取到字幕：按正则模式请求 {prefs}，yt-dlp 没有匹配到任何字幕轨道"
        if matched:
            return (
                f"未获取到字幕：已请求 {matched}，但一个文件都没写出（可能被限速或需要 PO Token）"
            )
        langs = "、".join(str(x) for x in (opts.get("subtitleslangs") or [])) or prefs
        return f"未获取到字幕：已请求 {langs}，但一个文件都没写出"


class ThumbnailFeature(DownloadFeature):
    def on_post_process(self, context: DownloadContext) -> None:
        if not context.opts.get("embedthumbnail"):
            self._cleanup_thumbnail_files(context)
            return
        if not context.opts.get("writethumbnail"):
            return

        final_output = context.find_final_merged_file()
        if final_output:
            context.output_path = final_output

        files = self._locate_files(context)
        if not files:
            return

        if not thumbnail_embedder.is_available():
            context.emit_thumbnail_warning("⚠️ 封面嵌入工具不可用")
            return

        for v, t in files:
            self._process_single_file(context, v, t)
        self._cleanup_thumbnail_files(context)

    def _process_single_file(self, context: DownloadContext, video_path: str, thumb_path: str):
        ext = os.path.splitext(video_path)[1].lower().lstrip(".")
        if not can_embed_thumbnail(ext):
            w = get_unsupported_formats_warning(ext)
            if w:
                context.emit_thumbnail_warning(w)
            return
        context.emit_status(f"[封面嵌入] 正在处理: {os.path.basename(video_path)}")
        res = thumbnail_embedder.embed_thumbnail(
            video_path,
            thumb_path,
            progress_callback=lambda msg: context.emit_status(f"[封面嵌入] {msg}"),
        )
        if res.success:
            context.emit_status("[封面嵌入] ✓ 成功")
        elif res.skipped:
            context.emit_thumbnail_warning(res.message)
        else:
            context.emit_thumbnail_warning(f"封面嵌入失败: {res.message}")

    def _locate_files(self, context: DownloadContext) -> list[tuple[str, str]]:
        files = []
        paths = set()
        if context.output_path:
            paths.add(context.output_path)
        paths.update(context.dest_paths)

        for p in paths:
            if not os.path.exists(p):
                continue
            if re.search(r"\.[fF]\d+\.\w+$", p):
                continue
            t = context.find_thumbnail_file(p)
            if t:
                files.append((p, t))

        if not files:  # Fallback scan
            output_dir = None
            if context.output_path:
                output_dir = os.path.dirname(context.output_path)
            elif context.dest_paths:
                output_dir = os.path.dirname(next(iter(context.dest_paths)))

            if output_dir and os.path.exists(output_dir):
                v_files, t_files = [], []
                v_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4a", ".mp3", ".flac", ".opus"}
                t_exts = {".jpg", ".jpeg", ".png", ".webp"}
                for f in os.listdir(output_dir):
                    fp = os.path.join(output_dir, f)
                    if not os.path.isfile(fp):
                        continue
                    if re.search(r"\.[fF]\d+\.\w+$", f):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext in v_exts:
                        v_files.append(fp)
                    elif ext in t_exts:
                        t_files.append(fp)

                for v in v_files:
                    vb = os.path.splitext(os.path.basename(v))[0]
                    for t in t_files:
                        if os.path.splitext(os.path.basename(t))[0] == vb:
                            files.append((v, t))
                            context.output_path = v
                            break
        return files

    def _cleanup_thumbnail_files(self, context: DownloadContext) -> None:
        if context.opts.get("__fluentytdl_keep_thumbnail"):
            return
        if not context.opts.get("writethumbnail"):
            return
        paths = set()
        if context.output_path and os.path.exists(context.output_path):
            paths.add(context.output_path)
        for p in context.dest_paths:
            if os.path.exists(p):
                paths.add(p)
        exts = [".webp", ".jpg", ".jpeg", ".png"]
        for p in paths:
            b = os.path.splitext(p)[0]
            for e in exts:
                t = b + e
                if os.path.exists(t):
                    try:
                        os.remove(t)
                    except Exception:
                        pass


class VRFeature(DownloadFeature):
    def on_post_process(self, context: DownloadContext) -> None:
        if not context.opts.get("__fluentytdl_use_android_vr"):
            return

        opts = context.opts
        proj = str(opts.get("__vr_projection") or "").lower()
        convert = bool(opts.get("__vr_convert_eac") or False)
        auto_convert = config_manager.get("vr_eac_auto_convert", False)

        if not (proj and proj != "unknown") and not ((convert or auto_convert) and proj == "eac"):
            if (convert or auto_convert) and proj == "mesh":
                context.emit_warning("Mesh 格式暂不支持转码")
            return

        final_file = context.find_final_merged_file() or context.output_path
        if not final_file or not os.path.exists(final_file):
            logger.warning("[VR] 无法找到最终文件")
            return

        ffmpeg_exe = opts.get("ffmpeg_location") or "ffmpeg"
        if os.path.isdir(ffmpeg_exe):
            ffmpeg_exe = os.path.join(ffmpeg_exe, "ffmpeg.exe")

        needs_convert = (convert or auto_convert) and proj == "eac"
        if needs_convert:
            if not self._check_ffmpeg_v360(ffmpeg_exe):
                context.emit_warning("FFmpeg 不支持 v360")
                needs_convert = False

            h = int(opts.get("height") or 0)
            if h == 0:
                h = 2160
            if h > int(config_manager.get("vr_max_resolution", 2160)):
                context.emit_warning(f"跳过 VR 转码: 分辨率过高 ({h}p)")
                needs_convert = False

        if needs_convert:
            context.emit_status("VR 投影转换 (EAC -> Equi)...")
            ext = os.path.splitext(final_file)[1]
            out_conv = os.path.splitext(final_file)[0] + "_equi" + ext

            cmd = self._build_cmd(ffmpeg_exe, final_file, out_conv)
            dur = 0.0
            try:
                dur = float(opts.get("duration") or 0)
            except Exception:
                pass

            if self._run_ffmpeg(cmd, context, dur):
                if not config_manager.get("vr_keep_source", True):
                    os.remove(final_file)
                    os.rename(out_conv, final_file)
                else:
                    bak = os.path.splitext(final_file)[0] + ".eac" + ext
                    if os.path.exists(bak):
                        os.remove(bak)
                    os.rename(final_file, bak)
                    os.rename(out_conv, final_file)
                proj = "equirectangular"
            else:
                context.emit_warning("VR 转码失败")
                if os.path.exists(out_conv):
                    os.remove(out_conv)

        self._inject_meta(context, final_file, proj, opts)

    def _build_cmd(self, exe, inp, out):
        cmd = [exe, "-y", "-i", inp, "-vf", "v360=eac:e"]
        hw = config_manager.get("vr_hw_accel_mode", "auto")
        encs = hardware_manager.get_gpu_encoders()
        gpu = ""
        if hw in ("gpu", "auto") and encs:
            if "h264_nvenc" in encs:
                gpu = "h264_nvenc"
            elif "h264_qsv" in encs:
                gpu = "h264_qsv"
            elif "h264_amf" in encs:
                gpu = "h264_amf"

        if gpu:
            cmd.extend(["-c:v", gpu])
            if gpu == "h264_nvenc":
                cmd.extend(["-preset", "p4", "-cq", "20"])
            elif gpu == "h264_qsv":
                cmd.extend(["-global_quality", "20"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
            th = hardware_manager.get_optimal_ffmpeg_threads(True)
            if config_manager.get("vr_cpu_priority", "low") == "low":
                th = max(1, th - 1)
            if th > 0:
                cmd.extend(["-threads", str(th)])

        cmd.extend(["-c:a", "copy", out])
        return cmd

    def _check_ffmpeg_v360(self, exe):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            res = subprocess.run(
                [exe, "-filters"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=si,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return "v360" in res.stdout
        except Exception:
            return False

    def _run_ffmpeg(self, cmd, ctx, dur):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                startupinfo=si,
                encoding="utf-8",
                errors="replace",
            )
            while True:
                if p.stdout is None:
                    break
                line = p.stdout.readline()
                if not line and p.poll() is not None:
                    break
                if line and "time=" in line:
                    t = line[line.find("time=") + 5 :].split(" ")[0]
                    ctx.emit_status(f"VR 转换... ({t})")
            return p.returncode == 0
        except Exception:
            return False

    def _inject_meta(self, ctx, f, proj, opts):
        if os.path.splitext(f)[1].lower() not in (".mp4", ".mov"):
            return
        md = metadata_utils.Metadata()
        stereo = str(opts.get("__vr_stereo_mode") or "").lower()
        if stereo == "stereo_tb":
            md.stereo_mode = "top-bottom"
        elif stereo == "stereo_sbs":
            md.stereo_mode = "left-right"
        elif stereo == "mono":
            md.stereo_mode = "none"
        if proj == "equirectangular":
            md.projection = "equirectangular"

        if not md.stereo_mode and not md.projection:
            return

        ctx.emit_status("注入 VR 元数据...")
        tmp = f + ".tmp.mp4"
        try:
            metadata_utils.inject_metadata(f, tmp, md, lambda x: None)
            if os.path.exists(tmp):
                os.remove(f)
                os.rename(tmp, f)
                ctx.emit_status("VR 元数据注入成功")
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
