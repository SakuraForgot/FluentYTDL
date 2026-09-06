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
    total_bytes_is_estimate: bool = False
    speed: int | None = None  # bytes/s
    eta: int | None = None  # seconds
    percent: float | None = None
    filename: str | None = None
    info_dict: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedLine:
    """单行解析结果。"""

    type: str
    """行类别，取值：

    `progress` | `ffmpeg_progress` | `destination` | `merge` | `postprocess` |
    `subtitle` | `status` | `warning` | `error` | `info` | `unknown`

    `error` / `info` 是后加的：`ERROR:` 行以前和
    `[info] There are no subtitles for the requested languages` 一样落进 `unknown`
    兜底，而 `unknown` 在 `executor` 里没有任何分支 —— 那句"为什么没有字幕"就是这么
    消失的（见 `FluentYTDL-字幕下载问题排查报告.md`）。
    """

    progress: DownloadProgress | None = None
    path: str | None = None  # 目标文件路径
    message: str | None = None  # 状态消息
    postprocessor: str | None = None  # 后处理器名
    postprocessor_status: str | None = None


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

    # stderror warnings from yt-dlp
    _RE_WARNING = re.compile(r"^WARNING:\s*(.*)", re.IGNORECASE)

    # ERROR: [youtube] xxx: Video unavailable
    # `--no-warnings` 压不住 ERROR，但以前没有分支接它，一样落进 unknown 被丢掉
    _RE_ERROR = re.compile(r"^ERROR:\s*(.*)", re.IGNORECASE)

    # [info] Downloading 1 format(s): 315+251
    _RE_INFO = re.compile(r"^\[info\]\s*(.*)", re.IGNORECASE)

    # [info] Writing video subtitles to: D:\path\Title.en-GB.vtt
    # [info] Writing video thumbnail 41 to: D:\path\Title.webp
    # 显式锚在 ` to: ` 上：Windows 路径自带盘符冒号，`split(":", 1)` 只是碰巧没出事
    _RE_WRITING_TO = re.compile(
        r"\bWriting\s+(?P<kind>video\s+subtitles|video\s+thumbnail(?:\s+\S+)?)\s+to:\s*"
        r"(?P<path>\S.*?)\s*$",
        re.IGNORECASE,
    )

    # [download] Destination: path/to/file.mp4
    _RE_DEST = re.compile(r"^\[download\]\s+Destination:\s+(?P<path>.+)$")

    # [Merger] Merging formats into "path/to/file.mp4"
    _RE_MERGE = re.compile(r'^\[Merger\]\s+Merging formats into\s+"?(?P<path>[^"]+)"?$')

    # [ExtractAudio] Destination: path/to/file.mp3
    _RE_EXTRACT_AUDIO = re.compile(r"^\[ExtractAudio\]\s+Destination:\s+(?P<path>.+)$")

    _RE_FFMPEG_PROGRESS = re.compile(
        r"size=\s*(?P<size>[\d.]+)\s*(?P<size_unit>[kKMGT]?B)\s+"
        r"time=(?P<time>\d{2}:\d{2}:\d{2}\.\d{2})"
        r".*?(?:bitrate=\s*(?P<bitrate>\S+).*?)?speed=\s*(?P<speed>[\d.]+)x"
    )

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
        "FFmpegSubtitlesConvertor": "转换字幕格式",
        "SponsorBlock": "跳过赞助片段",
        "ModifyChapters": "修改章节",
    }

    def parse_line(self, line: str) -> ParsedLine:
        """解析 yt-dlp 输出的一行。"""
        if not line:
            return ParsedLine(type="unknown")

        # 0. Warning / Error
        wm = self._RE_WARNING.match(line)
        if wm:
            return ParsedLine(type="warning", message=wm.group(1).strip())

        em = self._RE_ERROR.match(line)
        if em:
            return ParsedLine(type="error", message=em.group(1).strip())

        # 1. 结构化进度行 (FLUENTYTDL|...)
        if line.startswith(self.PROGRESS_PREFIX):
            return self._parse_structured_progress(line)

        # 2. 字幕 / 封面落盘路径
        #    两者都复用 `subtitle` 类型：executor 靠它把路径记进 `dest_paths`，
        #    而 `dest_paths` 是字幕后处理第 1 级定位的唯一来源。
        wt = self._RE_WRITING_TO.search(line)
        if wt:
            return ParsedLine(
                type="subtitle",
                path=wt.group("path").strip().strip('"'),
                message=line,
            )

        # 2.5 其余 `[info]` 行
        #     必须排在上面两条之后 —— 它们本身就是 `[info]` 前缀。
        #     `[info] There are no subtitles for the requested languages` 走这条：
        #     以前它落进末尾的 `unknown`，而 executor 没有 `unknown` 分支，于是
        #     "字幕为什么是空的"这个唯一线索被静默丢弃。
        #     `message` 刻意保留 `[info]` 前缀（不像 warning/error 那样剥掉）：
        #     `clean_logger:157` 靠 `startswith("[info]")` 撑着"正在获取流媒体元数据"
        #     这个界面状态，剥掉前缀会把它一起弄没。
        if self._RE_INFO.match(line):
            return ParsedLine(type="info", message=line)

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

        m = self._RE_FFMPEG_PROGRESS.search(line)
        if m:
            time_str = m.group("time")
            speed = m.group("speed") + "x"
            time_sec = _parse_eta_hms(time_str[:8]) or 0.0
            try:
                ms = float("0." + time_str[-2:])
                time_sec += ms
            except ValueError:
                pass
            return ParsedLine(
                type="ffmpeg_progress",
                progress=DownloadProgress(
                    status="ffmpeg_progress",
                    speed=0,
                    eta=0,
                    info_dict={
                        "time_sec": time_sec,
                        "speed": speed,
                        "output_bytes": _ffmpeg_size_to_bytes(
                            m.group("size"), m.group("size_unit")
                        ),
                        "bitrate": m.group("bitrate") or "",
                    },
                ),
            )

        # 6. [download] 百分比进度
        if line.startswith("[download]"):
            return self._parse_download_line(line)

        return ParsedLine(type="unknown", message=line)

    def _parse_structured_progress(self, line: str) -> ParsedLine:
        """解析 FLUENTYTDL|download|... 或 FLUENTYTDL|postprocess|... 格式。"""
        parts = line.split("|")

        if len(parts) >= 11 and parts[1] == "download":
            downloaded_s = parts[2]
            total_s = parts[3]
            estimate_s = parts[4]
            speed_s = parts[5]
            eta_s = parts[6]
            vcodec = parts[7]
            acodec = parts[8]
            # ext = parts[9] # unused
            filename = parts[10]
            height = parts[11] if len(parts) > 11 and parts[11] != "NA" else None
            width = parts[12] if len(parts) > 12 and parts[12] != "NA" else None
            format_id = parts[13] if len(parts) > 13 and parts[13] != "NA" else None
            title = "|".join(parts[14:]) if len(parts) > 14 and parts[14] != "NA" else None

            downloaded = _safe_int(downloaded_s)
            total = _safe_int(total_s)
            estimate = _safe_int(estimate_s)
            effective_total = total if total > 0 else estimate
            speed = _safe_int(speed_s)
            eta = _parse_eta_value(eta_s)
            percent = (downloaded / effective_total * 100.0) if effective_total > 0 else None

            return ParsedLine(
                type="progress",
                progress=DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded,
                    total_bytes=effective_total or None,
                    total_bytes_is_estimate=(total <= 0 and estimate > 0),
                    speed=speed or None,
                    eta=eta,
                    percent=percent,
                    filename=filename if filename and filename != "NA" else None,
                    info_dict={
                        "vcodec": vcodec,
                        "acodec": acodec,
                        "height": height,
                        "width": width,
                        "format_id": format_id,
                        "title": title,
                    },
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
            return ParsedLine(
                type="postprocess",
                postprocessor=pp,
                postprocessor_status=status,
                message=msg,
            )

        return ParsedLine(type="unknown", message=line)

    def _parse_download_line(self, line: str) -> ParsedLine:
        """解析 [download] 百分比进度行。"""
        if line.startswith("[download] Downloading item") or line.startswith(
            "[download] Downloading video"
        ):
            return ParsedLine(type="status", message=line)

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


def _ffmpeg_size_to_bytes(value: str, unit: str) -> int:
    """Parse ffmpeg's decimal output-size units (normally kB)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0
    multipliers = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    return int(amount * multipliers.get((unit or "B").upper(), 1))


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
