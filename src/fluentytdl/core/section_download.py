"""
FluentYTDL 片段下载模块

提供时间范围下载、无损剪切等功能:
- 时间范围解析
- yt-dlp 片段参数构建
- ffmpeg 无损剪切
- 预览帧抓取
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger


@dataclass
class TimeRange:
    """时间范围"""

    start_seconds: float
    end_seconds: float | None  # None 表示到结束

    @property
    def start_str(self) -> str:
        """格式化开始时间 HH:MM:SS"""
        return _seconds_to_timestr(self.start_seconds)

    @property
    def end_str(self) -> str | None:
        """格式化结束时间"""
        if self.end_seconds is None:
            return None
        return _seconds_to_timestr(self.end_seconds)

    @property
    def duration_seconds(self) -> float | None:
        """时长（秒）"""
        if self.end_seconds is None:
            return None
        return self.end_seconds - self.start_seconds

    def __str__(self) -> str:
        if self.end_seconds is None:
            return tr_text("{0}-结尾", self.start_str)
        return f"{self.start_str}-{self.end_str}"


class SectionCutMode(str, Enum):
    """yt-dlp section-cut quality policy."""

    COARSE = "coarse"
    PRECISE = "precise"


def parse_time_input(text: str) -> float:
    """
    解析用户输入的时间

    支持格式:
    - "1:30" -> 90 秒
    - "1:30:00" -> 5400 秒
    - "90" -> 90 秒
    - "1m30s" -> 90 秒
    - "1h30m" -> 5400 秒

    Args:
        text: 用户输入的时间字符串

    Returns:
        秒数

    Raises:
        ValueError: 无效格式
    """
    text = text.strip()
    if not text:
        raise ValueError(tr_text("时间不能为空"))

    # 格式 1: HH:MM:SS 或 MM:SS
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        elif len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        else:
            raise ValueError(tr_text("无效的时间格式: {0}", text))

    # 格式 2: 1h30m15s
    match = re.match(
        r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?$",
        text,
        re.IGNORECASE,
    )
    if match:
        h, m, s = match.groups()
        total = 0.0
        if h:
            total += int(h) * 3600
        if m:
            total += int(m) * 60
        if s:
            total += float(s)
        if total > 0:
            return total

    # 格式 3: 纯数字（秒）
    try:
        return float(text)
    except ValueError:
        raise ValueError(tr_text("无法解析时间: {0}", text)) from None


def parse_time_range(start: str, end: str | None = None) -> TimeRange:
    """
    解析时间范围

    Args:
        start: 开始时间
        end: 结束时间，None 或空表示到结束

    Returns:
        TimeRange 对象
    """
    start_sec = parse_time_input(start)
    end_sec = None

    if end and end.strip():
        end_sec = parse_time_input(end)
        if end_sec <= start_sec:
            raise ValueError(tr_text("结束时间必须大于开始时间"))

    return TimeRange(start_seconds=start_sec, end_seconds=end_sec)


def _seconds_to_timestr(seconds: float) -> str:
    """将秒数转换为 HH:MM:SS 格式"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60

    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    else:
        return f"{m}:{s:05.2f}"


def build_section_opts(
    time_range: TimeRange, mode: SectionCutMode | str = SectionCutMode.COARSE
) -> dict[str, Any]:
    """
    构建片段下载的 yt-dlp 选项

    Args:
        time_range: 时间范围

    Returns:
        yt-dlp 选项字典
    """
    opts: dict[str, Any] = {}

    # yt-dlp 使用 --download-sections 参数
    # 格式: "*start-end" 或 "*start-inf"
    if time_range.end_seconds is not None:
        section = f"*{time_range.start_seconds}-{time_range.end_seconds}"
    else:
        section = f"*{time_range.start_seconds}-inf"

    opts["download_sections"] = section

    cut_mode = SectionCutMode(mode)
    opts["__fluentytdl_section_cut_mode"] = cut_mode.value
    opts["__fluentytdl_section_duration"] = time_range.duration_seconds or 0.0
    opts["__fluentytdl_section_start"] = time_range.start_seconds
    opts["__fluentytdl_section_end"] = time_range.end_seconds
    # Precise cuts re-encode around cut points. Coarse cuts keep the original
    # streams and therefore naturally align to nearby keyframes.
    if cut_mode is SectionCutMode.PRECISE:
        opts["force_keyframes_at_cuts"] = True

    return opts


def build_section_cli_args(
    time_range: TimeRange, mode: SectionCutMode | str = SectionCutMode.COARSE
) -> list[str]:
    """
    构建片段下载的 CLI 参数

    Args:
        time_range: 时间范围

    Returns:
        CLI 参数列表
    """
    if time_range.end_seconds is not None:
        section = f"*{time_range.start_seconds}-{time_range.end_seconds}"
    else:
        section = f"*{time_range.start_seconds}-inf"

    args = ["--download-sections", section]
    if SectionCutMode(mode) is SectionCutMode.PRECISE:
        args.append("--force-keyframes-at-cuts")
    return args


def section_filename_suffix(time_range: TimeRange) -> str:
    """Return a filesystem-safe, human-readable suffix for a section output."""

    def _safe(seconds: float | None) -> str:
        if seconds is None:
            return "end"
        total = int(round(seconds))
        hours, remain = divmod(total, 3600)
        minutes, secs = divmod(remain, 60)
        return f"{hours:02d}-{minutes:02d}-{secs:02d}"

    return f" [clip {_safe(time_range.start_seconds)}-{_safe(time_range.end_seconds)}]"


def lossless_cut(
    input_path: str | Path,
    output_path: str | Path,
    start_seconds: float,
    end_seconds: float | None = None,
    ffmpeg_path: str | None = None,
) -> Path:
    """
    使用 ffmpeg 进行无损剪切

    Args:
        input_path: 输入文件
        output_path: 输出文件
        start_seconds: 开始时间（秒）
        end_seconds: 结束时间（秒），None 到结束
        ffmpeg_path: ffmpeg 路径

    Returns:
        输出文件路径
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    ffmpeg = ffmpeg_path or "ffmpeg"

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_seconds),  # 快速定位
        "-i",
        str(input_path),
    ]

    if end_seconds is not None:
        duration = end_seconds - start_seconds
        cmd.extend(["-t", str(duration)])

    # 无损复制
    cmd.extend(
        [
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(tr_text("ffmpeg 剪切失败: {0}", result.stderr))
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(tr_text("剪切操作超时")) from e

    return output_path


def extract_frame(
    video_path: str | Path,
    time_seconds: float,
    output_path: str | Path | None = None,
    ffmpeg_path: str | None = None,
) -> Path:
    """
    从视频中抓取指定时间的帧

    Args:
        video_path: 视频文件路径
        time_seconds: 时间位置（秒）
        output_path: 输出图片路径，None 自动生成
        ffmpeg_path: ffmpeg 路径

    Returns:
        输出图片路径
    """
    video_path = Path(video_path)
    ffmpeg = ffmpeg_path or "ffmpeg"

    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_frame_{int(time_seconds)}.jpg"
    else:
        output_path = Path(output_path)

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        str(time_seconds),
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",  # 高质量
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(tr_text("抓帧失败: {0}", result.stderr))
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(tr_text("抓帧操作超时")) from e

    return output_path


def get_video_duration(
    video_path: str | Path,
    ffprobe_path: str | None = None,
) -> float:
    """
    获取视频时长

    Args:
        video_path: 视频文件路径
        ffprobe_path: ffprobe 路径

    Returns:
        时长（秒）
    """
    video_path = Path(video_path)
    ffprobe = ffprobe_path or "ffprobe"

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(video_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(tr_text("获取时长失败: {0}", result.stderr))
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError) as e:
        raise RuntimeError(tr_text("获取视频时长失败: {0}", e)) from e


def generate_preview_frames(
    video_path: str | Path,
    count: int = 10,
    output_dir: str | Path | None = None,
    ffmpeg_path: str | None = None,
) -> list[Path]:
    """
    生成视频预览帧（用于时间线预览）

    Args:
        video_path: 视频文件路径
        count: 预览帧数量
        output_dir: 输出目录
        ffmpeg_path: ffmpeg 路径

    Returns:
        预览帧路径列表
    """
    video_path = Path(video_path)

    if output_dir is None:
        output_dir = video_path.parent / f"{video_path.stem}_frames"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 获取视频时长
    try:
        duration = get_video_duration(video_path)
    except RuntimeError:
        duration = 0

    if duration <= 0:
        return []

    # 计算时间点
    interval = duration / (count + 1)
    frames = []

    for i in range(count):
        time_sec = interval * (i + 1)
        try:
            frame_path = extract_frame(
                video_path,
                time_sec,
                output_dir / f"frame_{i:03d}.jpg",
                ffmpeg_path,
            )
            frames.append(frame_path)
        except RuntimeError as e:
            log_text(logger, "warning", "抓帧失败 ({0}s): {1}", time_sec, e)

    return frames
