"""Typed video metadata models used by DTO migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ThumbnailInfo:
    url: str
    thumbnail_id: str = ""
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class VideoFormatInfo:
    format_id: str
    display_text: str
    height: int
    fps: float | None = None
    ext: str = ""
    filesize: int | None = None
    vcodec: str | None = None


@dataclass(slots=True)
class AudioFormatInfo:
    format_id: str
    display_text: str
    abr: int
    ext: str = ""
    filesize: int | None = None


@dataclass(slots=True)
class SubtitleTrackInfo:
    lang_code: str
    lang_name: str
    is_auto: bool
    ext: str
    url: str | None = None


@dataclass(slots=True)
class SubtitleLanguageInfo:
    code: str
    name: str
    auto: bool
    ext: str


@dataclass(slots=True)
class VideoInfo:
    # Backward-compatible fields
    url: str
    title: str | None = None

    # Identity
    video_id: str = ""
    source_url: str = ""
    webpage_url: str = ""
    original_url: str = ""

    # Basic
    uploader: str = ""
    duration_sec: int | None = None
    duration_text: str = ""
    upload_date_text: str = ""
    is_live: bool = False

    # Media
    thumbnail_url: str = ""
    thumbnails: list[ThumbnailInfo] = field(default_factory=list)
    formats_raw: list[dict[str, Any]] = field(default_factory=list)
    video_formats: list[VideoFormatInfo] = field(default_factory=list)
    audio_formats: list[AudioFormatInfo] = field(default_factory=list)

    # Subtitle
    subtitle_tracks: list[SubtitleTrackInfo] = field(default_factory=list)
    subtitle_languages: list[SubtitleLanguageInfo] = field(default_factory=list)

    # VR
    vr_mode: bool = False
    vr_projection_summary: dict[str, Any] | None = None
    vr_only_format_ids: list[str] = field(default_factory=list)
    android_vr_format_ids: list[str] = field(default_factory=list)

    # Meta
    source_type: str = "single"
    raw_info: dict[str, Any] = field(default_factory=dict)
