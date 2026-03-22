from __future__ import annotations

from typing import Any

from ..video_info import (
    AudioFormatInfo,
    SubtitleLanguageInfo,
    SubtitleTrackInfo,
    ThumbnailInfo,
    VideoFormatInfo,
    VideoInfo,
)


class VideoInfoMapper:
    """Convert raw yt-dlp dictionaries into typed VideoInfo DTOs."""

    @classmethod
    def from_raw(cls, raw: dict[str, Any], source_type: str = "single") -> VideoInfo:
        if not isinstance(raw, dict):
            raw = {}

        source_url = cls.infer_source_url(raw)
        formats_raw_raw = raw.get("formats")
        if isinstance(formats_raw_raw, list):
            formats_raw: list[dict[str, Any]] = [f for f in formats_raw_raw if isinstance(f, dict)]
        else:
            formats_raw = []

        projection_summary_raw = raw.get("__vr_projection_summary")
        if isinstance(projection_summary_raw, dict):
            vr_projection_summary: dict[str, Any] | None = {
                str(k): v for k, v in projection_summary_raw.items()
            }
        else:
            vr_projection_summary = None

        subtitle_tracks = cls.extract_subtitle_tracks(raw)
        subtitle_languages = cls.get_subtitle_languages(subtitle_tracks)

        dto = VideoInfo(
            url=source_url,
            title=str(raw.get("title") or "") or None,
            video_id=str(raw.get("id") or "").strip(),
            source_url=source_url,
            webpage_url=str(raw.get("webpage_url") or "").strip(),
            original_url=str(raw.get("original_url") or "").strip(),
            uploader=str(raw.get("uploader") or raw.get("channel") or "").strip(),
            duration_sec=cls._to_int(raw.get("duration")),
            duration_text=cls._format_duration(raw.get("duration")),
            upload_date_text=cls._format_upload_date(raw.get("upload_date")),
            is_live=bool(raw.get("is_live") or False),
            thumbnail_url=cls.infer_thumbnail(raw),
            thumbnails=cls._extract_thumbnails(raw),
            formats_raw=formats_raw,
            video_formats=cls.clean_video_formats(raw),
            audio_formats=cls.clean_audio_formats(raw),
            subtitle_tracks=subtitle_tracks,
            subtitle_languages=subtitle_languages,
            vr_mode=bool(raw.get("__fluentytdl_vr_mode") or False),
            vr_projection_summary=vr_projection_summary,
            vr_only_format_ids=cls._to_str_list(raw.get("__vr_only_format_ids")),
            android_vr_format_ids=cls._to_str_list(raw.get("__android_vr_format_ids")),
            source_type=source_type,
            raw_info=dict(raw),
        )
        return dto

    @staticmethod
    def infer_source_url(raw: dict[str, Any]) -> str:
        for key in ("webpage_url", "original_url"):
            val = str(raw.get(key) or "").strip()
            if val.startswith("http://") or val.startswith("https://"):
                return val

        url = str(raw.get("url") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url

        vid = str(raw.get("id") or url).strip()
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        return url

    @staticmethod
    def infer_thumbnail(raw: dict[str, Any]) -> str:
        thumb = str(raw.get("thumbnail") or "").strip()
        thumbs = raw.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            preferred_ids = {"mqdefault", "medium", "default", "sddefault", "hqdefault"}
            for t in thumbs:
                if not isinstance(t, dict):
                    continue
                t_id = str(t.get("id") or "").lower()
                if t_id in preferred_ids:
                    u = str(t.get("url") or "").strip()
                    if u:
                        return u

            for t in thumbs:
                if not isinstance(t, dict):
                    continue
                width = t.get("width") or 0
                if 200 <= int(width) <= 400:
                    u = str(t.get("url") or "").strip()
                    if u:
                        return u

            for t in thumbs:
                if not isinstance(t, dict):
                    continue
                u = str(t.get("url") or t.get("src") or "").strip()
                if u:
                    return u

        if thumb:
            if "i.ytimg.com" in thumb or "i9.ytimg.com" in thumb:
                for high_res in ["maxresdefault", "hqdefault", "sddefault"]:
                    if high_res in thumb:
                        return thumb.replace(high_res, "mqdefault")
            return thumb

        return ""

    @classmethod
    def clean_video_formats(cls, raw: dict[str, Any]) -> list[VideoFormatInfo]:
        formats = raw.get("formats")
        if not isinstance(formats, list):
            return []

        out: list[VideoFormatInfo] = []
        seen_height: set[int] = set()
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get("vcodec") == "none":
                continue
            height = int(fmt.get("height") or 0)
            if height < 360:
                continue
            if height in seen_height:
                continue

            ext = str(fmt.get("ext") or "?")
            fps = cls._to_float(fmt.get("fps"))
            res_str = f"{height}p"
            if fps and fps > 30:
                res_str += f" {int(fps)}fps"
            size = cls._format_size(fmt.get("filesize") or fmt.get("filesize_approx"))

            out.append(
                VideoFormatInfo(
                    format_id=str(fmt.get("format_id") or ""),
                    display_text=f"{res_str} - {ext} ({size})",
                    height=height,
                    fps=fps,
                    ext=ext,
                    filesize=cls._to_int(fmt.get("filesize") or fmt.get("filesize_approx")),
                    vcodec=(str(fmt.get("vcodec")) if fmt.get("vcodec") is not None else None),
                )
            )
            seen_height.add(height)

        out.sort(key=lambda x: x.height, reverse=True)
        return out

    @classmethod
    def clean_audio_formats(cls, raw: dict[str, Any]) -> list[AudioFormatInfo]:
        formats = raw.get("formats")
        if not isinstance(formats, list):
            return []

        out: list[AudioFormatInfo] = []
        seen_key: set[tuple[int, str, str]] = set()
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get("vcodec") != "none":
                continue
            if fmt.get("acodec") in (None, "none"):
                continue

            abr_raw = fmt.get("abr") or fmt.get("tbr") or 0
            abr = cls._to_int(abr_raw) or 0
            if abr <= 0:
                continue

            ext = str(fmt.get("ext") or "?").strip().lower() or "?"
            acodec = str(fmt.get("acodec") or "").strip().lower()
            key = (abr, ext, acodec)
            if key in seen_key:
                continue

            size = cls._format_size(fmt.get("filesize") or fmt.get("filesize_approx"))
            out.append(
                AudioFormatInfo(
                    format_id=str(fmt.get("format_id") or ""),
                    display_text=f"{abr}kbps - {ext} ({size})",
                    abr=abr,
                    ext=ext,
                    filesize=cls._to_int(fmt.get("filesize") or fmt.get("filesize_approx")),
                )
            )
            seen_key.add(key)

        out.sort(key=lambda x: x.abr, reverse=True)
        return out

    @classmethod
    def extract_subtitle_tracks(cls, raw: dict[str, Any]) -> list[SubtitleTrackInfo]:
        tracks: list[SubtitleTrackInfo] = []

        subtitles = raw.get("subtitles") or {}
        if isinstance(subtitles, dict):
            for lang_code, sub_list in subtitles.items():
                item = cls._pick_track_item(sub_list)
                if not isinstance(item, dict):
                    continue
                tracks.append(
                    SubtitleTrackInfo(
                        lang_code=str(lang_code),
                        lang_name=str(item.get("name") or ""),
                        is_auto=False,
                        ext=str(item.get("ext") or "vtt"),
                        url=(str(item.get("url")) if item.get("url") is not None else None),
                    )
                )

        auto_subs = raw.get("automatic_captions") or {}
        if isinstance(auto_subs, dict):
            for lang_code, sub_list in auto_subs.items():
                item = cls._pick_track_item(sub_list)
                if not isinstance(item, dict):
                    continue
                tracks.append(
                    SubtitleTrackInfo(
                        lang_code=str(lang_code),
                        lang_name=str(item.get("name") or ""),
                        is_auto=True,
                        ext=str(item.get("ext") or "vtt"),
                        url=(str(item.get("url")) if item.get("url") is not None else None),
                    )
                )

        return tracks

    @classmethod
    def get_subtitle_languages(cls, tracks: list[SubtitleTrackInfo]) -> list[SubtitleLanguageInfo]:
        seen: dict[str, SubtitleLanguageInfo] = {}
        for t in tracks:
            current = seen.get(t.lang_code)
            if current is None or (current.auto and not t.is_auto):
                seen[t.lang_code] = SubtitleLanguageInfo(
                    code=t.lang_code,
                    name=t.lang_name or t.lang_code,
                    auto=t.is_auto,
                    ext=t.ext,
                )

        priority = ["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]

        def sort_key(item: SubtitleLanguageInfo) -> tuple[int, int | str]:
            if item.code in priority:
                return (0, priority.index(item.code))
            return (1, item.code)

        return sorted(seen.values(), key=sort_key)

    @staticmethod
    def _extract_thumbnails(raw: dict[str, Any]) -> list[ThumbnailInfo]:
        thumbs = raw.get("thumbnails")
        if not isinstance(thumbs, list):
            return []

        out: list[ThumbnailInfo] = []
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            url = str(t.get("url") or t.get("src") or "").strip()
            if not url:
                continue
            out.append(
                ThumbnailInfo(
                    url=url,
                    thumbnail_id=str(t.get("id") or ""),
                    width=VideoInfoMapper._to_int(t.get("width")),
                    height=VideoInfoMapper._to_int(t.get("height")),
                )
            )
        return out

    @staticmethod
    def _pick_track_item(sub_list: Any) -> dict[str, Any] | None:
        if isinstance(sub_list, list) and sub_list:
            first = sub_list[0]
            if isinstance(first, dict):
                return first
            return None
        if isinstance(sub_list, dict):
            return sub_list
        return None

    @staticmethod
    def _to_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            s = str(item or "").strip()
            if s:
                out.append(s)
        return out

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        try:
            sec = int(float(seconds))
        except Exception:
            return "--:--"

        if sec < 0:
            return "--:--"

        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _format_upload_date(value: Any) -> str:
        s = str(value or "").strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
        return ""

    @staticmethod
    def _format_size(value: Any) -> str:
        try:
            n = int(value)
        except Exception:
            return "-"
        if n <= 0:
            return "-"

        units = ["B", "KB", "MB", "GB"]
        x = float(n)
        for unit in units:
            if x < 1024 or unit == units[-1]:
                if unit in ("B", "KB"):
                    return f"{int(round(x))}{unit}"
                return f"{x:.1f}{unit}"
            x /= 1024
        return f"{n}B"
