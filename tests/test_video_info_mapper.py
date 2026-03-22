"""Unit tests for models.mappers.video_info_mapper."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from fluentytdl.models.mappers.video_info_mapper import (  # pyright: ignore[reportMissingImports]
    VideoInfoMapper,
)


def _sample_raw_info() -> dict:
    return {
        "id": "abc123",
        "title": "Sample Video",
        "uploader": "Uploader",
        "duration": 125,
        "upload_date": "20260310",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "thumbnail": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        "thumbnails": [
            {
                "id": "maxresdefault",
                "url": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
                "width": 1280,
            },
            {"id": "mqdefault", "url": "https://i.ytimg.com/vi/abc123/mqdefault.jpg", "width": 320},
        ],
        "formats": [
            {
                "format_id": "v1080",
                "vcodec": "avc1",
                "acodec": "none",
                "height": 1080,
                "fps": 30,
                "ext": "mp4",
                "filesize": 1024 * 1024,
            },
            {
                "format_id": "v720",
                "vcodec": "avc1",
                "acodec": "none",
                "height": 720,
                "fps": 60,
                "ext": "mp4",
                "filesize": 512 * 1024,
            },
            {
                "format_id": "a128",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "ext": "m4a",
                "filesize": 128 * 1024,
            },
            {
                "format_id": "a96",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 96,
                "ext": "webm",
                "filesize": 96 * 1024,
            },
        ],
        "subtitles": {
            "zh-Hans": [{"ext": "vtt", "name": "中文", "url": "https://sub/zh"}],
            "en": [{"ext": "vtt", "name": "English", "url": "https://sub/en"}],
        },
        "automatic_captions": {
            "en": [{"ext": "vtt", "name": "English Auto", "url": "https://sub/en-auto"}],
        },
    }


def test_infer_source_url_prefers_webpage_url():
    raw = {"webpage_url": "https://www.youtube.com/watch?v=xyz", "url": "https://cdn.test/hls.m3u8"}
    assert VideoInfoMapper.infer_source_url(raw) == "https://www.youtube.com/watch?v=xyz"


def test_infer_source_url_fallback_to_id():
    raw = {"id": "xyz987", "url": "xyz987"}
    assert VideoInfoMapper.infer_source_url(raw) == "https://www.youtube.com/watch?v=xyz987"


def test_infer_thumbnail_prefers_medium_thumbnail():
    raw = {
        "thumbnail": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
        "thumbnails": [
            {"id": "sddefault", "url": "https://i.ytimg.com/vi/abc/sddefault.jpg"},
            {"id": "mqdefault", "url": "https://i.ytimg.com/vi/abc/mqdefault.jpg"},
        ],
    }
    picked = VideoInfoMapper.infer_thumbnail(raw)
    assert picked.endswith("sddefault.jpg") or picked.endswith("mqdefault.jpg")


def test_clean_video_formats_dedupe_and_sort():
    raw = {
        "formats": [
            {"format_id": "v1", "vcodec": "avc1", "height": 720, "ext": "mp4"},
            {"format_id": "v2", "vcodec": "vp9", "height": 1080, "ext": "webm"},
            {"format_id": "v3", "vcodec": "avc1", "height": 720, "ext": "mp4"},
        ]
    }
    items = VideoInfoMapper.clean_video_formats(raw)
    assert [f.height for f in items] == [1080, 720]


def test_clean_audio_formats_dedupe_and_sort():
    raw = {
        "formats": [
            {"format_id": "a1", "vcodec": "none", "acodec": "mp4a", "abr": 96, "ext": "m4a"},
            {"format_id": "a2", "vcodec": "none", "acodec": "opus", "abr": 128, "ext": "webm"},
            {"format_id": "a3", "vcodec": "none", "acodec": "opus", "abr": 128, "ext": "webm"},
        ]
    }
    items = VideoInfoMapper.clean_audio_formats(raw)
    assert [f.abr for f in items] == [128, 96]
    assert len(items) == 2


def test_from_raw_builds_dto_with_expected_fields():
    raw = _sample_raw_info()
    dto = VideoInfoMapper.from_raw(raw, source_type="single")

    assert dto.video_id == "abc123"
    assert dto.source_url == "https://www.youtube.com/watch?v=abc123"
    assert dto.duration_sec == 125
    assert dto.duration_text == "02:05"
    assert dto.upload_date_text == "2026-03-10"
    assert dto.thumbnail_url.endswith("mqdefault.jpg")
    assert len(dto.video_formats) == 2
    assert len(dto.audio_formats) == 2
    assert dto.subtitle_languages[0].code == "zh-Hans"


def test_from_raw_handles_invalid_input_gracefully():
    dto = VideoInfoMapper.from_raw({}, source_type="playlist_entry")
    assert dto.source_type == "playlist_entry"
    assert dto.source_url == ""
    assert dto.video_formats == []
    assert dto.audio_formats == []
