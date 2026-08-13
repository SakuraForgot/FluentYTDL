"""
执行器模块

单管线执行器：使用 yt-dlp Native Pipeline。
负责实际的子进程管理、进度转发、容器决策和后处理编排。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from ..models.errors import YtDlpExecutionError
from ..utils.container_compat import choose_lossless_merge_container
from ..youtube.yt_dlp_cli import (
    log_pot_from_output,
    log_pot_in_argv,
    prepare_yt_dlp_env,
    resolve_yt_dlp_exe,
    ydl_opts_to_cli_args,
)
from .output_parser import YtDlpOutputParser

# 字幕/封面等附属文件后缀，不应被视为主输出文件
_AUXILIARY_EXTENSIONS = frozenset(
    {
        ".vtt",
        ".srt",
        ".ass",
        ".ssa",
        ".sub",
        ".lrc",  # 字幕
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",  # 封面
        ".json",
        ".description",
        ".txt",  # 元数据
    }
)


def _is_auxiliary_file(path: str) -> bool:
    """判断路径是否为附属文件（字幕、封面、元数据等），不应作为主输出路径。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in _AUXILIARY_EXTENSIONS


# 有效媒体文件的最小大小门槛（10 KB）
# 低于此大小的文件几乎不可能是有效的音视频
_MIN_VALID_MEDIA_BYTES = 10 * 1024


# ── 回调协议 ──────────────────────────────────────────────


class ProgressCallback(Protocol):
    def __call__(self, data: dict[str, Any]) -> None: ...


class StatusCallback(Protocol):
    def __call__(self, message: str) -> None: ...


class CancelCheck(Protocol):
    def __call__(self) -> bool: ...


class PathCallback(Protocol):
    def __call__(self, path: str) -> None: ...


# ── 容器决策 ──────────────────────────────────────────────

_SUBTITLE_COMPATIBLE_CONTAINERS = {"mp4", "mkv", "mov", "m4v"}


def determine_merge_container(
    ydl_opts: dict[str, Any],
    video_ext: str | None = None,
    audio_ext: str | None = None,
) -> str:
    """确定最终输出容器格式。

    优先级:
    1. ydl_opts["merge_output_format"] — 用户/预设已指定
    2. 字幕兼容性修正 — webm 不支持 SRT/ASS → mkv
    3. choose_lossless_merge_container(v_ext, a_ext)
    4. 兜底 mkv
    """
    merge_fmt = (ydl_opts.get("merge_output_format") or "").strip().lower()

    if merge_fmt:
        # 字幕兼容性检查
        if ydl_opts.get("embedsubtitles") and merge_fmt == "webm":
            logger.info("[Executor] 字幕嵌入 + webm → 强制 mkv")
            return "mkv"
        return merge_fmt

    # 没有指定容器 → 根据流的 ext 推断
    computed = choose_lossless_merge_container(video_ext, audio_ext)
    if computed:
        if ydl_opts.get("embedsubtitles") and computed not in _SUBTITLE_COMPATIBLE_CONTAINERS:
            logger.info("[Executor] 字幕嵌入 + {} → 强制 mkv", computed)
            return "mkv"
        return computed

    return "mkv"


# ── Win32 工具 ────────────────────────────────────────────


def _win_hide_kwargs() -> dict[str, Any]:
    """Windows: 隐藏子进程窗口。"""
    kw: dict[str, Any] = {}
    if os.name != "nt":
        return kw
    try:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw


# ── 执行器 ────────────────────────────────────────────────


class DownloadExecutor:
    """原生下载执行器 (Native Only)。

    仅使用 yt-dlp 原生管线。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[Any] | None = None
        self._ytdlp_parser = YtDlpOutputParser()

    def execute(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        on_path: PathCallback,
        cancel_check: CancelCheck,
        on_file_created: Callable[[str], None] | None = None,
        cached_info_dict: dict[str, Any] | None = None,
    ) -> str | None:
        """执行下载，返回输出文件路径。

        Args:
            url: 视频 URL。
            ydl_opts: yt-dlp 选项字典。
            on_progress: 进度回调。
            on_status: 状态消息回调。
            on_path: 输出路径回调。
            cancel_check: 取消检查回调。
            on_file_created: 文件创建回调。
            cached_info_dict: (Optional) 预先提取的 info dict，避免重复提取。

        Returns:
            输出文件路径，或 None（如果失败）。

        Raises:
            RuntimeError: 子进程失败或取消。
        """
        # 总是使用原生管线
        return self._execute_native(
            url,
            ydl_opts,
            on_progress=on_progress,
            on_status=on_status,
            on_path=on_path,
            cancel_check=cancel_check,
            on_file_created=on_file_created,
            cached_info_dict=cached_info_dict,
        )

    # ── yt-dlp Native Pipeline ────────────────────────────

    def _execute_native(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        on_path: PathCallback,
        cancel_check: CancelCheck,
        on_file_created: Callable[[str], None] | None = None,
        label: str = "",
        cached_info_dict: dict[str, Any] | None = None,
    ) -> str | None:
        """yt-dlp 原生管线 — 与现有 _download_via_exe() 等效。"""
        exe = resolve_yt_dlp_exe()
        if exe is None:
            raise RuntimeError("yt-dlp 可执行文件未找到")

        ydl_opts["skip_unavailable_fragments"] = True

        progress_prefix = "FLUENTYTDL|"
        cmd: list[str] = [
            str(exe),
            "--ignore-config",
            "--no-warnings",
            "--no-color",
            "--newline",
            "--progress",
            "--progress-template",
            (
                "download:"
                + progress_prefix
                + "download|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s"
                + "|%(progress.speed)s|%(progress.eta)s"
                + "|%(info.vcodec)s|%(info.acodec)s|%(info.ext)s|%(progress.filename)s"
            ),
            "--progress-template",
            (
                "postprocess:"
                + progress_prefix
                + "postprocess|%(progress.status)s|%(progress.postprocessor)s"
            ),
        ]

        cmd += ydl_opts_to_cli_args(ydl_opts)
        cmd.append(url)

        logger.info("[Executor][Native] cmd={}", " ".join(cmd))
        log_pot_in_argv(cmd, stage="Download", task_id=label or "native")

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"
        work_dir = self._resolve_output_dir(ydl_opts)

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            env=env,
            cwd=work_dir,
            **_win_hide_kwargs(),
        )

        # FFmpegFD does not consistently forward its child FFmpeg progress to
        # yt-dlp's stdout. While it is silent, monitor the sandbox .part file
        # so the UI still reflects real download activity.
        if ydl_opts.get("download_sections"):
            threading.Thread(
                target=_monitor_section_part_progress,
                args=(self._proc, work_dir, on_progress),
                daemon=True,
                name="section-part-progress",
            ).start()

        output_path: str | None = None
        self._active_postprocessor = ""
        dest_paths: set[str] = set()
        tail: deque[str] = deque(maxlen=120)
        expected_total_bytes: int = 0  # 累计预期文件大小，用于完整性校验

        proc = self._proc
        assert proc is not None
        assert proc.stdout is not None
        for raw in _iter_process_output(proc.stdout):
            if cancel_check():
                self._terminate_proc()
                raise RuntimeError("用户取消下载")

            line = _decode_line(raw)
            if not line:
                continue
            tail.append(line)
            log_pot_from_output(line, stage="Download")

            parsed = self._ytdlp_parser.parse_line(line)

            if parsed.type == "progress" and parsed.progress:
                # 追踪预期总大小（累加各流的 total_bytes）
                tb = parsed.progress.total_bytes
                if isinstance(tb, (int, float)) and tb > 0:
                    expected_total_bytes = max(expected_total_bytes, int(tb))

                on_progress(
                    {
                        "status": parsed.progress.status,
                        "downloaded_bytes": parsed.progress.downloaded_bytes,
                        "total_bytes": parsed.progress.total_bytes,
                        "speed": parsed.progress.speed,
                        "eta": parsed.progress.eta,
                        "filename": parsed.progress.filename,
                        "info_dict": parsed.progress.info_dict,
                        "label": label,
                    }
                )
                if parsed.progress.filename:
                    p = _abs(parsed.progress.filename)
                    dest_paths.add(p)
                    if on_file_created:
                        on_file_created(p)
                    if not output_path and not _is_auxiliary_file(p):
                        output_path = p
                        on_path(p)

            elif parsed.type == "destination":
                if parsed.path:
                    p = _abs(parsed.path)
                    dest_paths.add(p)
                    if on_file_created:
                        on_file_created(p)
                    if not output_path and not _is_auxiliary_file(p):
                        output_path = p
                        on_path(p)

            elif parsed.type == "warning":
                if parsed.message:
                    on_status("⚠️ " + parsed.message)

            elif parsed.type == "ffmpeg_progress":
                if parsed.progress:
                    on_progress(
                        {
                            "status": "ffmpeg_progress",
                            "time_sec": parsed.progress.info_dict.get("time_sec"),
                            "speed": parsed.progress.info_dict.get("speed"),
                            "output_bytes": parsed.progress.info_dict.get("output_bytes"),
                            "bitrate": parsed.progress.info_dict.get("bitrate"),
                            "postprocessor": getattr(self, "_active_postprocessor", ""),
                        }
                    )

            elif parsed.type == "merge":
                if parsed.path:
                    p = _abs(parsed.path)
                    output_path = p
                    on_path(p)
                if parsed.message:
                    on_status(parsed.message)

            elif parsed.type == "postprocess":
                self._active_postprocessor = parsed.postprocessor or ""
                on_progress(
                    {
                        "status": "postprocess",
                        "postprocessor": parsed.postprocessor or "",
                        "pp_status": parsed.postprocessor_status or "",
                    }
                )
                if parsed.message:
                    on_status(parsed.message)

            elif parsed.type in ("subtitle", "status"):
                if parsed.message:
                    on_status(parsed.message)
                if parsed.path:
                    p = _abs(parsed.path)
                    dest_paths.add(p)
                    if on_file_created:
                        on_file_created(p)

        rc = proc.wait()
        self._proc = None

        if rc != 0:
            last_lines = "\n".join(tail)

            # ━━━ 关卡 1: 文件存在性 + 大小有效性检查 ━━━
            # 容错场景：Windows 下 yt-dlp 常因无法删除 .part-Frag 文件返回 exit code 1
            # 但此时文件实际上已经完整下载并合并成功
            is_valid = False
            valid_path_found = None
            actual_size = 0

            if output_path and os.path.exists(output_path):
                try:
                    actual_size = os.path.getsize(output_path)
                    if actual_size >= _MIN_VALID_MEDIA_BYTES:
                        is_valid = True
                        valid_path_found = output_path
                except OSError:
                    pass

            # 兜底探测：如果日志没截出 output_path，但生成了物理产物
            if not is_valid:
                for d_path in dest_paths:
                    if os.path.exists(d_path) and not _is_auxiliary_file(d_path):
                        try:
                            sz = os.path.getsize(d_path)
                            if sz >= _MIN_VALID_MEDIA_BYTES:
                                is_valid = True
                                valid_path_found = d_path
                                actual_size = sz
                                break
                        except OSError:
                            pass

            # ━━━ 关卡 2: 预期大小比对 ━━━
            # 如果进度回调中记录了预期总大小，且实际文件远小于预期，
            # 说明文件是不完整的残留，不应视为成功
            if is_valid and expected_total_bytes > 0 and actual_size > 0:
                ratio = actual_size / expected_total_bytes
                if ratio < 0.5:
                    logger.warning(
                        "文件大小 ({}) 仅为预期大小 ({}) 的 {:.0%}，判定为不完整下载",
                        actual_size,
                        expected_total_bytes,
                        ratio,
                    )
                    is_valid = False

            if is_valid:
                if not output_path and valid_path_found:
                    output_path = valid_path_found
                logger.warning(
                    "yt-dlp 退出码 {} (非零)，但输出文件有效 ({}, {:.1f} KB)。忽略错误。",
                    rc,
                    output_path,
                    actual_size / 1024,
                )
            else:
                raise YtDlpExecutionError(exit_code=rc, stderr=last_lines)

        return output_path

    # ── 子步骤 ────────────────────────────────────────────

    def _should_run_post_process(self, opts: dict[str, Any]) -> bool:
        """检查是否有需要 yt-dlp 处理的后处理选项。"""
        keys = ["writesubtitles", "writeautomaticsub", "writethumbnail", "addmetadata"]
        if any(opts.get(k) for k in keys):
            return True
        # 检查 postprocessors 列表
        pps = opts.get("postprocessors")
        if isinstance(pps, list) and pps:
            return True
        return False

    def _run_post_download_pass(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        cancel_check: CancelCheck,
        on_file_created: Callable[[str], None] | None = None,
    ) -> None:
        """运行后处理 pass (skip-download)。"""
        # 克隆选项并强制跳过下载
        opts = ydl_opts.copy()
        opts["skip_download"] = True

        # 移除可能冲突的格式选项
        opts.pop("format", None)

        # 这里的 callbacks 只需要 status，以及部分 progress (如下载字幕时)
        # 我们传入 on_progress，但标记为 "补充"
        self._execute_native(
            url,
            opts,
            on_progress=on_progress,
            on_status=on_status,
            on_path=lambda path: None,
            cancel_check=cancel_check,
            on_file_created=on_file_created,
        )

    def _extract_stream_urls(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        cancel_check: CancelCheck,
        check_protocol: bool = True,
    ) -> dict[str, Any]:
        """用 yt-dlp --dump-single-json 提取流 URL。"""
        exe = resolve_yt_dlp_exe()
        if exe is None:
            raise RuntimeError("yt-dlp 可执行文件未找到")

        cmd: list[str] = [str(exe), "--ignore-config", "--no-warnings", "-J"]

        # 传递格式选择器
        fmt = ydl_opts.get("format")
        if isinstance(fmt, str) and fmt:
            cmd += ["-f", fmt]

        # 传递认证参数
        for key, flag in [("cookiefile", "--cookies"), ("proxy", "--proxy")]:
            val = ydl_opts.get(key)
            if isinstance(val, str) and val.strip():
                cmd += [flag, val.strip()]

        cookies_from_browser = ydl_opts.get("cookiesfrombrowser")
        if isinstance(cookies_from_browser, (list, tuple)) and cookies_from_browser:
            cmd += ["--cookies-from-browser", str(cookies_from_browser[0])]

        # extractor-args
        extractor_args = ydl_opts.get("extractor_args")
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

        cmd.append(url)

        logger.debug("[Executor] 提取 URL cmd={}", " ".join(cmd))
        log_pot_in_argv(cmd, stage="Download", task_id="extract_stream_urls")

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_win_hide_kwargs(),
        )

        stdout, stderr = proc.communicate()

        log_pot_from_output(stderr, stage="Download")

        if cancel_check():
            raise RuntimeError("用户取消下载")

        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp 信息提取失败 (rc={proc.returncode}): {stderr[:500]}")

        try:
            info = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"yt-dlp JSON 解析失败: {e}") from e

        return self._parse_stream_info(info, check_protocol=check_protocol)

    def _parse_stream_info(
        self, info: dict[str, Any], check_protocol: bool = True
    ) -> dict[str, Any]:
        """从 yt-dlp JSON 信息中提取流 URL。"""
        result: dict[str, Any] = {
            "title": info.get("title") or info.get("id") or "video",
        }

        requested_formats = info.get("requested_formats")

        # Debug Log
        if requested_formats:
            logger.debug(
                "[Executor] requested_formats: {}",
                [(f.get("format_id"), f.get("vcodec"), f.get("acodec")) for f in requested_formats],
            )
        else:
            logger.debug("[Executor] No requested_formats found, using single format info.")

        if not requested_formats:
            # 没有 requested_formats → 可能是预合流或单文件
            stream_url = info.get("url")
            if stream_url:
                # 假设为主视频流 (可能是 Muxed 或 Video-Only 或 Audio-Only)
                # Check protocol compatibility for Aria2
                proto = info.get("protocol", "")
                if check_protocol and proto in ("m3u8", "m3u8_native", "rtsp"):
                    raise RuntimeError(f"流协议 {proto} 需要 yt-dlp native 处理")

                vcodec = info.get("vcodec")
                acodec = info.get("acodec")
                is_audio_only = vcodec == "none" and acodec != "none"

                fmt_info = {
                    "url": stream_url,
                    "ext": info.get("ext", "mp4"),
                    "http_headers": info.get("http_headers", {}),
                    "filesize": info.get("filesize") or info.get("filesize_approx"),
                }

                if is_audio_only:
                    result["audio"] = fmt_info
                else:
                    result["video"] = fmt_info
            return result

        # 遍历 requested_formats 自动归类
        for fmt in requested_formats:
            proto = fmt.get("protocol", "")
            if check_protocol and proto in ("m3u8", "m3u8_native", "rtsp"):
                raise RuntimeError(f"流协议 {proto} 需要 yt-dlp native 处理")

            # 提取信息
            stream_url = fmt.get("url", "")
            if not stream_url:
                continue

            headers = fmt.get("http_headers", {})
            ext = fmt.get("ext", "")
            filesize = fmt.get("filesize") or fmt.get("filesize_approx")

            fmt_data = {
                "url": stream_url,
                "ext": ext,
                "http_headers": headers,
                "filesize": filesize,
            }

            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            has_video = vcodec and vcodec != "none"
            has_audio = acodec and acodec != "none"

            # 判定类型
            if has_video:
                # 任何包含视频流的都视为视频 (包括 Muxed)
                # 如果已经存在 video (例如之前的 stream)，则覆盖 (通常 requested_formats 顺序不管是怎样的, 只要有 video 就行)
                result["video"] = fmt_data
            elif has_audio:
                # 只有音频
                result["audio"] = fmt_data

        return result

    def _resolve_output_dir(self, ydl_opts: dict[str, Any]) -> str:
        """解析输出目录。"""
        paths = ydl_opts.get("paths")
        if isinstance(paths, dict):
            home = paths.get("home")
            if isinstance(home, str) and home.strip():
                d = home.strip()
                os.makedirs(d, exist_ok=True)
                return d

        # 从配置获取
        try:
            from ..core.config_manager import config_manager

            d = str(config_manager.get("download_dir") or "").strip()
            if d:
                os.makedirs(d, exist_ok=True)
                return d
        except Exception:
            pass

        return os.getcwd()

    def _terminate_proc(self) -> None:
        """终止当前子进程并尽可能杀死整个进程树防止锁释放失败。"""
        if self._proc:
            import platform

            try:
                if platform.system() == "Windows":
                    import subprocess

                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                    )
                else:
                    self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def terminate(self) -> None:
        """外部调用：终止执行器的子进程。"""
        self._terminate_proc()


# ── 工具查找 ──────────────────────────────────────────────


def _iter_process_output(stream) -> Any:
    """Yield subprocess output frames separated by either LF or CR.

    yt-dlp prints normal events with newlines, while FFmpeg continuously
    rewrites its progress line with carriage returns. Iterating over the pipe
    directly only sees the latter after FFmpeg exits, which leaves the UI
    stuck on the preceding status message.
    """
    pending = b""
    while True:
        reader = getattr(stream, "read1", None)
        chunk = reader(4096) if callable(reader) else stream.read(4096)
        if not chunk:
            break
        pending += chunk
        frames = re.split(rb"[\r\n]+", pending)
        pending = frames.pop()
        for frame in frames:
            if frame:
                yield frame
    if pending:
        yield pending


def _monitor_section_part_progress(
    proc: subprocess.Popen[Any], work_dir: str, on_progress: ProgressCallback
) -> None:
    """Emit fallback progress from a growing section-download .part file."""
    previous_bytes = 0
    previous_tick = time.monotonic()
    root = Path(work_dir)
    while proc.poll() is None:
        total_bytes = 0
        try:
            for candidate in root.glob("*.part"):
                if candidate.is_file():
                    total_bytes += candidate.stat().st_size
        except OSError:
            pass

        now = time.monotonic()
        if total_bytes > 0:
            speed = 0.0
            if total_bytes >= previous_bytes and now > previous_tick:
                speed = (total_bytes - previous_bytes) / (now - previous_tick)
            on_progress(
                {
                    "status": "section_file_progress",
                    "output_bytes": total_bytes,
                    "speed": speed,
                }
            )
        previous_bytes = total_bytes
        previous_tick = now
        time.sleep(0.75)


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg 可执行文件。"""
    try:
        from ..core.config_manager import config_manager

        ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if ffmpeg_path and Path(ffmpeg_path).exists():
            return ffmpeg_path
    except Exception:
        logger.debug("_find_ffmpeg: config_manager lookup failed")
    try:
        from ..utils.paths import locate_runtime_tool

        return str(locate_runtime_tool("ffmpeg.exe", "ffmpeg/ffmpeg.exe"))
    except Exception:
        logger.debug("_find_ffmpeg: locate_runtime_tool failed")
    return shutil.which("ffmpeg")


# ── 辅助函数 ──────────────────────────────────────────────


def _decode_line(raw: bytes) -> str:
    """健壮的行解码 (UTF-8 → GBK → replace)。"""
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            line = raw.decode("gbk")
        except UnicodeDecodeError:
            line = raw.decode("utf-8", errors="replace")
    return line.rstrip("\r\n")


def _abs(path: str) -> str:
    """安全的 abspath。"""
    try:
        return os.path.abspath(path)
    except Exception:
        return path


_RE_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """清理文件名中不安全的字符。"""
    s = _RE_UNSAFE.sub("_", name).strip(". ")
    return s[:200] if s else "download"
