from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ParseState(Enum):
    FLAT = 1  # 刚拿到播放列表目录，只有标题和封面
    FETCHING = 2  # 后台正在深度请求这个视频的 formats
    READY = 3  # formats 获取完毕，可以供用户选择下载


@dataclass
class YtSubtitleDTO:
    """标准化的单条字幕信息 DTO"""

    url: str
    ext: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> YtSubtitleDTO:
        return cls(
            url=str(data.get("url") or ""),
            ext=str(data.get("ext") or "vtt"),
            name=str(data.get("name") or ""),
        )


@dataclass
class YtFormatDTO:
    """标准化的单个流形式 (DTO)，消灭 yt-dlp 原生字典中的不确定性。"""

    format_id: str
    ext: str
    vcodec: str
    acodec: str
    filesize: int
    fps: float
    height: int
    width: int
    url: str
    format_note: str
    resolution: str
    vbr: float
    abr: float
    tbr: float
    container: str
    protocol: str
    video_ext: str
    audio_ext: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> YtFormatDTO:
        """从 yt-dlp 的 raw dict 宽容地萃取格式数据"""
        filesize = data.get("filesize") or data.get("filesize_approx") or 0
        vcodec = data.get("vcodec") or "none"
        acodec = data.get("acodec") or "none"
        fps = data.get("fps")
        height = data.get("height")
        width = data.get("width")

        return cls(
            format_id=str(data.get("format_id", "")),
            ext=str(data.get("ext") or "mp4"),
            vcodec=str(vcodec),
            acodec=str(acodec),
            filesize=int(filesize) if filesize else 0,
            fps=float(fps) if fps else 0.0,
            height=int(height) if height else 0,
            width=int(width) if width else 0,
            url=str(data.get("url") or ""),
            format_note=str(data.get("format_note") or ""),
            resolution=str(data.get("resolution") or ""),
            vbr=float(data.get("vbr") or 0.0),
            abr=float(data.get("abr") or 0.0),
            tbr=float(data.get("tbr") or 0.0),
            container=str(data.get("container") or ""),
            protocol=str(data.get("protocol") or ""),
            video_ext=str(data.get("video_ext") or ""),
            audio_ext=str(data.get("audio_ext") or ""),
        )

    @property
    def has_video(self) -> bool:
        return self.vcodec != "none"

    @property
    def has_audio(self) -> bool:
        return self.acodec != "none"

    @property
    def is_video_only(self) -> bool:
        return self.has_video and not self.has_audio

    @property
    def is_audio_only(self) -> bool:
        return self.has_audio and not self.has_video

    @property
    def filesize_str(self) -> str:
        """UI 层直接读取此属性进行绘制，绝对不自己算"""
        if self.filesize <= 0:
            return "未知大小"
        size = float(self.filesize)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"


@dataclass
class YtMediaDTO:
    """清洗后的标准媒体信息对象，作为 UI 层与内部调度之间的防腐墙"""

    id: str
    title: str
    uploader: str
    duration: int
    thumbnail: str
    is_playlist: bool
    is_live: bool
    view_count: int
    like_count: int
    channel: str
    channel_id: str
    upload_date: str
    webpage_url: str

    # 所有的 formats
    formats: list[YtFormatDTO] = field(default_factory=list)
    # 嵌套支持播放列表
    entries: list[YtMediaDTO] = field(default_factory=list)

    # 字幕信息
    subtitles: dict[str, list[YtSubtitleDTO]] = field(default_factory=dict)

    # VR 支持
    vr_mode: bool = field(default=False)
    vr_projection_summary: dict[str, Any] = field(default_factory=dict)
    vr_only_format_ids: list[str] = field(default_factory=list)
    android_vr_format_ids: list[str] = field(default_factory=list)

    raw_dict: dict[str, Any] = field(default_factory=dict)

    extractor: str = ""
    extractor_key: str = ""

    parse_state: ParseState = field(default=ParseState.READY)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> YtMediaDTO:
        is_playlist = data.get("_type") == "playlist" or "entries" in data

        # 提取封面和 URL，复用现有的提取工具以保持最佳兼容性
        from .video_utils import infer_entry_thumbnail, infer_entry_url

        thumbnail = infer_entry_thumbnail(data)
        webpage_url = infer_entry_url(data)

        # 解析 formats
        raw_formats = data.get("formats", [])
        formats = [YtFormatDTO.from_dict(f) for f in raw_formats if isinstance(f, dict)]

        # 解析 entries (对于 playlist)
        # 如果是 flat 提取，可能直接包含不完全的 dict，也进行 DTO 化
        raw_entries = data.get("entries", [])
        entries = []
        if raw_entries:
            for entry in raw_entries:
                if isinstance(entry, dict):
                    entries.append(YtMediaDTO.from_dict(entry))

        # 解析 subtitles
        raw_subs = data.get("subtitles") or {}
        subtitles: dict[str, list[YtSubtitleDTO]] = {}
        if isinstance(raw_subs, dict):
            for lang, subs_list in raw_subs.items():
                if isinstance(subs_list, list):
                    subtitles[lang] = [
                        YtSubtitleDTO.from_dict(sub)
                        for sub in subs_list
                        if isinstance(sub, dict)  # 只取字典项
                    ]

        duration = data.get("duration")
        duration_sec = int(duration) if duration is not None else 0

        # 状态推断
        state = ParseState.FLAT if not formats and not is_playlist else ParseState.READY

        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            uploader=str(data.get("uploader") or ""),
            duration=duration_sec,
            thumbnail=thumbnail,
            is_playlist=is_playlist,
            is_live=bool(data.get("is_live", False)),
            view_count=int(data.get("view_count") or 0),
            like_count=int(data.get("like_count") or 0),
            channel=str(data.get("channel") or ""),
            channel_id=str(data.get("channel_id") or ""),
            upload_date=str(data.get("upload_date") or ""),
            webpage_url=webpage_url,
            subtitles=subtitles,
            formats=formats,
            entries=entries,
            extractor=str(data.get("extractor") or ""),
            extractor_key=str(data.get("extractor_key") or ""),
            parse_state=state,
            vr_mode=bool(data.get("__fluentytdl_vr_mode") or False),
            vr_projection_summary=data.get("__vr_projection_summary") or {},
            vr_only_format_ids=data.get("__vr_only_format_ids") or [],
            android_vr_format_ids=data.get("__android_vr_format_ids") or [],
            raw_dict=data,  # preserve original for components that specifically need it
        )

    @property
    def video_formats(self) -> list[YtFormatDTO]:
        return [f for f in self.formats if f.has_video]

    @property
    def audio_formats(self) -> list[YtFormatDTO]:
        return [f for f in self.formats if f.has_audio]

    @property
    def duration_str(self) -> str:
        if self.duration <= 0:
            return "00:00"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
