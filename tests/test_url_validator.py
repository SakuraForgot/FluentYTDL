"""Tests for UrlValidator video-ID extraction and thumbnail derivation (P5)."""

import sys
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.utils.validators import UrlValidator  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=87DyyMV0kCY",
        "https://www.youtube.com/watch?v=87DyyMV0kCY&t=30s",
        "https://www.youtube.com/watch?t=30s&v=87DyyMV0kCY",
        "https://m.youtube.com/watch?v=87DyyMV0kCY",
        "https://youtu.be/87DyyMV0kCY",
        "https://youtu.be/87DyyMV0kCY?si=abcdef",
        "https://www.youtube.com/shorts/87DyyMV0kCY",
        "https://www.youtube.com/embed/87DyyMV0kCY",
        "https://www.youtube.com/live/87DyyMV0kCY",
        "https://www.youtube.com/v/87DyyMV0kCY",
        # 带播放列表上下文的单视频：必须取到视频 ID，而不是列表 ID
        "https://www.youtube.com/watch?v=87DyyMV0kCY&list=PLabcdefghijklmnop",
    ],
)
def test_extracts_video_id(url):
    assert UrlValidator.extract_video_id(url) == "87DyyMV0kCY"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://www.youtube.com/@SomeChannel",
        "https://www.youtube.com/@SomeChannel/videos",
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "https://www.youtube.com/playlist?list=PLabcdefghijklmnop",
        "https://x.com/alwayspriyesh/status/2085710231657668716?s=20",
        "https://example.com/watch?v=tooshort",
        # 12 位以上不得被截成 11 位
        "https://www.youtube.com/watch?v=87DyyMV0kCYEXTRA",
    ],
)
def test_no_video_id(url):
    assert UrlValidator.extract_video_id(url) == ""


def test_thumbnail_url_from_video_url():
    assert (
        UrlValidator.youtube_thumbnail_url("https://youtu.be/87DyyMV0kCY")
        == "https://i.ytimg.com/vi/87DyyMV0kCY/mqdefault.jpg"
    )


def test_thumbnail_url_empty_without_id():
    assert UrlValidator.youtube_thumbnail_url("https://www.youtube.com/@SomeChannel") == ""
    assert UrlValidator.youtube_thumbnail_url("") == ""


def test_ids_with_dash_and_underscore_survive():
    assert UrlValidator.extract_video_id("https://youtu.be/a-B_c1d2E3f") == "a-B_c1d2E3f"
