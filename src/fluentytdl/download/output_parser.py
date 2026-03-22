"""
输出解析器模块

统一解析 yt-dlp 的命令行输出，转换为结构化的进度/状态数据。
从 workers.py 提取的核心解析逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── 解析结果类型 ──────────────────────────────────────────


@dataclass
class DownloadProgress:
    """标准化的下载进度数据。"""

    status: str  # "downloading" | "postprocess" | "finished" | "error"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: int | None = None  # bytes/s
    eta: int | None = None  # seconds
    percent: float | None = None
    filename: str | None = None
    info_dict: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedLine:
    """单行解析结果。"""

    type: str  # "progress" | "destination" | "merge" | "postprocess" | "status" | "subtitle" | "unknown"
    progress: DownloadProgress | None = None
    path: str | None = None  # 目标文件路径
    message: str | None = None  # 状态消息
    postprocessor: str | None = None  # 后处理器名


# ── yt-dlp 输出解析器 ────────────────────────────────────


class YtDlpOutputParser:
    """解析 yt-dlp CLI 的 stdout 输出行。"""

    # 结构化进度行前缀 (--progress-template)
    PROGRESS_PREFIX = "FLUENTYTDL|"

    # [download] 95.0% of ~15.30MiB at 2.50MiB/s ETA 00:03
    _RE_PROGRESS_FULL = re.compile(
        r"^\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+~?(?P<total>[\d\.]+)"
        r"(?P<tunit>[KMGTPE]i?B)\s+at\s+(?P<speed>[\d\.]+)(?P<sunit>[KMGTPE]i?B)/s"
        r"\s+ETA\s+(?P<eta>\d{1,2}:\d{2}(?::\d{2})?)",
        re.IGNORECASE,
    )

    # [download] 15.30MiB at 2.50MiB/s ETA 00:03  (total unknown)
    _RE_PROGRESS_PARTIAL = re.compile(
        r"^\[download\]\s+(?P<done>[\d\.]+)(?P<unit>[KMGTPE]i?B)\s+at\s+"
        r"(?P<speed>[\d\.]+)(?P<sunit>[KMGTPE]i?B)/s\s+ETA\s+"
        r"(?P<eta>\d{1,2}:\d{2}(?::\d{2})?)",
        re.IGNORECASE,
    )

    # [download] Destination: path/to/file.mp4
    _RE_DEST = re.compile(r"^\[download\]\s+Destination:\s+(?P<path>.+)$")

    # [Merger] Merging formats into "path/to/file.mp4"
    _RE_MERGE = re.compile(r'^\[Merger\]\s+Merging formats into\s+"?(?P<path>[^"]+)"?$')

    # [ExtractAudio] Destination: path/to/file.mp3
    _RE_EXTRACT_AUDIO = re.compile(r"^\[ExtractAudio\]\s+Destination:\s+(?P<path>.+)$")

    # 后处理器名称映射
    _PP_NAMES: dict[str, str] = {
        "MoveFiles": "移动文件",
        "Merger": "合并音视频",
        "FFmpegMerger": "合并音视频",
        "EmbedThumbnail": "嵌入封面",
        "FFmpegMetadata": "嵌入元数据",
        "FFmpegThumbnailsConvertor": "转换封面格式",
        "FFmpegExtractAudio": "提取音频",
        "FFmpegVideoConvertor": "转换视频格式",
        "FFmpegEmbedSubtitle": "嵌入字幕",
        "SponsorBlock": "跳过赞助片段",
        "ModifyChapters": "修改章节",
    }

    def parse_line(self, line: str) -> ParsedLine:
        """解析 yt-dlp 输出的一行。"""
        if not line:
            return ParsedLine(type="unknown")

        # 1. 结构化进度行 (FLUENTYTDL|...)
        if line.startswith(self.PROGRESS_PREFIX):
            return self._parse_structured_progress(line)

        # 2. 字幕下载提示
        if "Writing video subtitles to:" in line:
            parts = line.split(":", 1)
            path = parts[1].strip() if len(parts) > 1 else None
            return ParsedLine(
                type="subtitle",
                path=path,
                message=line,
            )

        # 2.5 封面下载提示
        if "Writing video thumbnail" in line and "to:" in line:
            parts = line.split("to:", 1)
            path = parts[1].strip() if len(parts) > 1 else None
            return ParsedLine(
                type="subtitle",  # 复用 subtitle 类型以复用 executor 中的路径跟踪逻辑
                path=path,
                message=line,
            )

        # 3. 字幕转换
        if "[FFmpegSubtitlesConvertor]" in line:
            return ParsedLine(type="status", message=line)

        # 4. 合并/提取音频
        if (
            line.startswith("[Merger]")
            or line.startswith("[ExtractAudio]")
            or "Merging formats" in line
        ):
            m = self._RE_MERGE.match(line)
            if m:
                return ParsedLine(type="merge", path=m.group("path").strip(), message=line)
            m = self._RE_EXTRACT_AUDIO.match(line)
            if m:
                return ParsedLine(type="merge", path=m.group("path").strip(), message=line)
            return ParsedLine(type="status", message=line)

        # 5. 下载目标路径
        m = self._RE_DEST.match(line)
        if m:
            return ParsedLine(type="destination", path=m.group("path").strip())

        # 6. [download] 百分比进度
        if line.startswith("[download]"):
            return self._parse_download_line(line)

        return ParsedLine(type="unknown", message=line)

    def _parse_structured_progress(self, line: str) -> ParsedLine:
        """解析 FLUENTYTDL|download|... 或 FLUENTYTDL|postprocess|... 格式。"""
        parts = line.split("|")

        if len(parts) >= 3 and parts[1] == "download":
            downloaded_s = parts[2] if len(parts) > 2 else ""
            total_s = parts[3] if len(parts) > 3 else ""
            speed_s = parts[4] if len(parts) > 4 else ""
            eta_s = parts[5] if len(parts) > 5 else ""
            vcodec = parts[6] if len(parts) > 6 else ""
            acodec = parts[7] if len(parts) > 7 else ""
            # ext = parts[8] if len(parts) > 8 else ""  # unused
            filename = parts[9] if len(parts) > 9 else ""

            downloaded = _safe_int(downloaded_s)
            total = _safe_int(total_s)
            speed = _safe_int(speed_s)
            eta = _parse_eta_value(eta_s)
            percent = (downloaded / total * 100.0) if total and total > 0 else None

            return ParsedLine(
                type="progress",
                progress=DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    total_bytes=total or None,
                    speed=speed or None,
                    eta=eta,
                    percent=percent,
                    filename=filename if filename and filename != "NA" else None,
                    info_dict={"vcodec": vcodec, "acodec": acodec},
                ),
            )

        if len(parts) >= 3 and parts[1] == "postprocess":
            status = parts[2] if len(parts) > 2 else ""
            pp = parts[3] if len(parts) > 3 else ""
            pp_display = self._PP_NAMES.get(pp, pp) if pp else "处理"
            status_names = {"started": "开始", "processing": "处理中", "finished": "完成"}
            status_display = status_names.get(status, status) if status else ""
            if pp_display and status_display:
                msg = f"后处理: {pp_display} ({status_display})"
            elif pp_display:
                msg = f"后处理: {pp_display}..."
            else:
                msg = "后处理中..."
            return ParsedLine(type="postprocess", postprocessor=pp, message=msg)

        return ParsedLine(type="unknown", message=line)

    def _parse_download_line(self, line: str) -> ParsedLine:
        """解析 [download] 百分比进度行。"""
        m = self._RE_PROGRESS_FULL.match(line)
        if m:
            pct = float(m.group("pct"))
            total = _size_to_bytes(m.group("total"), m.group("tunit"))
            speed = _size_to_bytes(m.group("speed"), m.group("sunit"))
            eta = _parse_eta_hms(m.group("eta"))
            downloaded = int(total * pct / 100.0) if total > 0 else 0
            return ParsedLine(
                type="progress",
                progress=DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    total_bytes=total or None,
                    speed=speed or None,
                    eta=eta,
                    percent=pct,
                ),
            )

        m2 = self._RE_PROGRESS_PARTIAL.match(line)
        if m2:
            downloaded = _size_to_bytes(m2.group("done"), m2.group("unit"))
            speed = _size_to_bytes(m2.group("speed"), m2.group("sunit"))
            eta = _parse_eta_hms(m2.group("eta"))
            return ParsedLine(
                type="progress",
                progress=DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    speed=speed or None,
                    eta=eta,
                ),
            )

        return ParsedLine(type="status", message=line)


# ── 工具函数 ──────────────────────────────────────────────


def _safe_int(s: str) -> int:
    """安全地将字符串转为 int，NA 或空值返回 0。"""
    if not s or s == "NA":
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _size_to_bytes(value: str, unit: str) -> int:
    """将 '15.3' + 'MiB' 转为字节数。"""
    try:
        n = float(value)
    except (ValueError, TypeError):
        return 0

    u = unit.upper().rstrip("B").rstrip("I")
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(n * multipliers.get(u, 1))


def _parse_eta_hms(eta: str) -> int | None:
    """解析 HH:MM:SS 或 MM:SS 格式的 ETA 为秒。"""
    if not eta:
        return None
    try:
        parts = eta.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])
    except (ValueError, TypeError):
        return None


def _parse_eta_value(s: str) -> int | None:
    """解析 ETA 字符串：可能是秒数或 HH:MM:SS 格式。"""
    if not s or s == "NA":
        return None
    s = s.strip()
    if ":" in s:
        return _parse_eta_hms(s)
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None
