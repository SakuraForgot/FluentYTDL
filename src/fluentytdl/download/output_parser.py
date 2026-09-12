"""
输出解析器模块

统一解析 yt-dlp 的命令行输出，转换为结构化的进度/状态数据。
从 workers.py 提取的核心解析逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.utils.ui_text import tr_text

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

    role: str | None = None
    """`path` 这个文件是什么角色 —— **只在 yt-dlp 自己说清楚了的时候才填**。

    取值是 `download/staging.py` 的 `Kind` 子集：`media` | `subtitle` | `thumbnail`。

    `None` 不是"未知所以随便猜"，而是一条硬信息：**报告者没有声明角色**。
    `[download] Destination:` 与 `%(progress.filename)s` 都只给路径不给类别（同一种行
    既可能是视频流、也可能是 DASH 分片或字幕），所以它们恒为 `None`；这类路径不进
    `add_reported()`，留给 `staging.reconcile()` 那唯一一次集中式兜底分类。

    这么分是《下载产物事务层》第一条不变量的落点：**角色只在进入 Manifest 时判定
    一次**。以前 `:151-157` 把 `Writing video subtitles` 和 `Writing video thumbnail`
    一律返回 `type="subtitle"`，正则**明明抓到了** `kind` 却当场丢掉 —— 于是下游
    (`features._cleanup_thumbnail_files` 按后缀拼名字、`subtitle_processor` 的四级定位
    扫目录) 只能各自再猜一遍，那正是"少删误删"的根因。字幕那一侧的四级定位已经在
    Step 3 整体退休 —— 它现在只校验调用方交来的路径。
    """


# ── 嵌入证据 ──────────────────────────────────────────────

#: 后处理器名 → 嵌入证据类别（`staging.Manifest.embed_evidence` 的取值）。
#:
#: 这是"字幕/封面到底嵌进去了没有"的**权威通道**：判据是结构化的
#: `postprocessor_status == "finished"`，不是人类日志行、更不是 yt-dlp 的最终 rc。
#: 删除外挂字幕的唯一合法理由就是这条证据成立（见《下载产物事务层》「删除只有一个
#: 理由」），所以它必须来自 yt-dlp 自己报告的后处理状态。
#:
#: **短拼写才是真的**（yt-dlp 2026.08.30 实测，三个后处理器同时印证）：
#: `%(progress.postprocessor)s` 吐的是**剥掉 `FFmpeg` 前缀和 `PP` 后缀**之后的名字 ——
#: `FFmpegMergerPP` → `Merger`、`FFmpegExtractAudioPP` → `ExtractAudio`、
#: `FFmpegMetadataPP` → `Metadata`、`MoveFilesAfterDownloadPP` → `MoveFiles`。
#: 所以 `FFmpegEmbedSubtitlePP` 报的是 `EmbedSubtitle` —— `clean_logger.py:369` 判对了，
#: `_PP_NAMES` 里那些 `FFmpeg*` 键从来没被命中过。
#:
#: 长拼写仍然留着：它不命中就是死键，代价为零；而万一哪个版本改回长名，漏掉证据的
#: 后果是把嵌入成功误判成失败 ⇒ 外挂字幕被保留 —— 那是安全的一侧。
EMBED_EVIDENCE_BY_PP: dict[str, str] = {
    "FFmpegEmbedSubtitle": "subtitle",
    "EmbedSubtitle": "subtitle",
    "EmbedThumbnail": "thumbnail",
    "FFmpegThumbnail": "thumbnail",
}


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

    # 后处理器名称映射。
    #
    # **短拼写是实际到达的那一个**：`%(progress.postprocessor)s` 剥掉了 `FFmpeg` 前缀和
    # `PP` 后缀（见 `EMBED_EVIDENCE_BY_PP` 上方的实测记录）。这张表原先只给
    # `Metadata` / `ExtractAudio` / `SubtitlesConvertor` / `ThumbnailsConvertor` /
    # `VideoConvertor` 登记了 `FFmpeg*` 长键，于是它们**一个都没命中过** ——「后处理:
    # ExtractAudio (完成)」这种半英文状态就是这么来的。长键一并留着：不命中即死键，
    # 代价为零，而版本改回长名时它就是兜底。
    _PP_NAMES: dict[str, str] = {
        "MoveFiles": QT_TRANSLATE_NOOP("RuntimeText", "移动文件"),
        "Merger": QT_TRANSLATE_NOOP("RuntimeText", "合并音视频"),
        "EmbedThumbnail": QT_TRANSLATE_NOOP("RuntimeText", "嵌入封面"),
        "EmbedSubtitle": QT_TRANSLATE_NOOP("RuntimeText", "嵌入字幕"),
        "Metadata": QT_TRANSLATE_NOOP("RuntimeText", "嵌入元数据"),
        "ThumbnailsConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换封面格式"),
        "ExtractAudio": QT_TRANSLATE_NOOP("RuntimeText", "提取音频"),
        "VideoConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换视频格式"),
        "SubtitlesConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换字幕格式"),
        "SponsorBlock": QT_TRANSLATE_NOOP("RuntimeText", "跳过赞助片段"),
        "ModifyChapters": QT_TRANSLATE_NOOP("RuntimeText", "修改章节"),
        # ── 长拼写兜底（当前版本不会命中）──
        "FFmpegMerger": QT_TRANSLATE_NOOP("RuntimeText", "合并音视频"),
        "FFmpegMetadata": QT_TRANSLATE_NOOP("RuntimeText", "嵌入元数据"),
        "FFmpegThumbnailsConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换封面格式"),
        "FFmpegExtractAudio": QT_TRANSLATE_NOOP("RuntimeText", "提取音频"),
        "FFmpegVideoConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换视频格式"),
        "FFmpegEmbedSubtitle": QT_TRANSLATE_NOOP("RuntimeText", "嵌入字幕"),
        "FFmpegSubtitlesConvertor": QT_TRANSLATE_NOOP("RuntimeText", "转换字幕格式"),
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
        #    `type` 仍是 `subtitle`（executor / workers 的既有分支靠它把路径记进
        #    `dest_paths`），但**角色改由 `role` 承载**：正则本来就抓了 `kind`，
        #    以前在这里被丢掉，下游只好按后缀再猜一遍。
        wt = self._RE_WRITING_TO.search(line)
        if wt:
            kind = (wt.group("kind") or "").lower()
            return ParsedLine(
                type="subtitle",
                path=wt.group("path").strip().strip('"'),
                message=line,
                role="thumbnail" if "thumbnail" in kind else "subtitle",
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
        # 人类日志行的前缀同样是短名（实测 `[Merger]` / `[ExtractAudio]` / `[Metadata]`），
        # 所以判短名，长名留作版本兜底。
        if "[SubtitlesConvertor]" in line or "[FFmpegSubtitlesConvertor]" in line:
            return ParsedLine(type="status", message=line)

        # 4. 合并/提取音频
        #    这两行是 yt-dlp **明确声明**了角色的主媒体路径（`[Merger]` 的产物就是
        #    合并成品，`[ExtractAudio]` 的产物就是提取出的音频），所以带 `role="media"`。
        if (
            line.startswith("[Merger]")
            or line.startswith("[ExtractAudio]")
            or "Merging formats" in line
        ):
            m = self._RE_MERGE.match(line)
            if m:
                return ParsedLine(
                    type="merge", path=m.group("path").strip(), message=line, role="media"
                )
            m = self._RE_EXTRACT_AUDIO.match(line)
            if m:
                return ParsedLine(
                    type="merge", path=m.group("path").strip(), message=line, role="media"
                )
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
            pp_display = tr_text(self._PP_NAMES.get(pp, pp)) if pp else tr_text("处理")
            status_names = {
                "started": tr_text("开始"),
                "processing": tr_text("处理中"),
                "finished": tr_text("完成"),
            }
            status_display = status_names.get(status, status) if status else ""
            if pp_display and status_display:
                msg = tr_text("后处理: {0} ({1})", pp_display, status_display)
            elif pp_display:
                msg = tr_text("后处理: {0}...", pp_display)
            else:
                msg = tr_text("后处理中...")
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
