"""Presentation cleaning must remove noise without changing source evidence."""

from copy import deepcopy
from datetime import datetime

from fluentytdl.models.media_inspection import InspectionResult
from fluentytdl.ui.media_info_presenter import (
    clean_values,
    date_text,
    duration,
    present_media,
    scaled,
)


def tag(key, value, source="probe", scope="container"):
    return {"key": key, "value": value, "source": source, "scope": scope}


def fixture():
    return InspectionResult(
        "C:/Videos/sample.mp4",
        fields=[
            tag("container", "mov,mp4,m4a,3gp,3g2,mj2"),
            tag("size", 1048576),
            tag("duration", 1384.5),
            tag("total_bitrate", 3_000_000, "derived"),
            tag("title", [" Title "]),
            tag("title", ["Title"], "native_tag"),
            tag("authors_json", ["Doe, Jane", "ESPN"]),
            tag("artist", ["Doe, Jane; ESPN"]),
            tag("artist", ["ESPN"], "native_tag"),
            tag("uploader", None),
            tag("date", ["10292", "20260920"]),
            tag("comment", ["https://example.com/VideoA"]),
            tag("webpage_url", ["https://example.com/VideoA"]),
        ],
        streams=[
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "average_rate": {"fraction": "60000/1001", "fps": 60000 / 1001},
                "reported_frames": None,
                "reported_bitrate": None,
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
            },
        ],
    )


def test_clean_cards_deduplicate_authors_and_leave_evidence_untouched():
    result = fixture()
    before = deepcopy(result)
    cards = {c.key: c for c in present_media(result)}
    assert cards["content"].hero == "Title"
    assert cards["content"].rows == [("作者 / 艺术家", "Doe, Jane · ESPN"), ("年份", "2026")]
    assert cards["source"].rows == [
        ("日期", "2026-09-20"),
        ("备注 / 来源链接", "https://example.com/VideoA"),
    ]
    assert result == before


def test_readable_units_and_no_unknown_placeholder_rows():
    cards = {c.key: c for c in present_media(fixture())}
    assert cards["video:0"].hero == "1920 × 1080"
    assert ("平均帧率", "59.94 fps") in cards["video:0"].rows
    assert not any("码率" in label or "帧数" in label for label, _ in cards["video:0"].rows)
    assert ("采样率", "48 kHz") in cards["audio:1"].rows
    assert ("文件大小", "1 MiB") in cards["file"].rows
    assert ("总平均码率（估算）", "3 Mbit/s") in cards["file"].rows
    assert "not_analyzed" not in "\n".join(c.summary() for c in cards.values())


def test_reference_rate_does_not_become_measured_average():
    result = fixture()
    result.streams[0].update(average_rate=None, reference_rate={"fps": 30, "fraction": "30/1"})
    video = next(c for c in present_media(result) if c.key == "video:0")
    assert ("参考帧率", "30 fps") in video.rows
    assert not any(label == "平均帧率" for label, _ in video.rows)


def test_different_titles_and_case_sensitive_urls_are_retained():
    result = fixture()
    result.fields += [
        tag("title", ["Alternate"], "native_tag"),
        tag("webpage_url", ["https://example.com/videoA"]),
    ]
    cards = {c.key: c for c in present_media(result)}
    assert ("其他标题", "Alternate") in cards["content"].rows
    links = next(value for label, value in cards["source"].rows if label == "备注 / 来源链接")
    assert links.splitlines() == ["https://example.com/VideoA", "https://example.com/videoA"]


def test_multitrack_and_attached_cover_are_separate_types():
    result = fixture()
    result.streams += [
        {"index": 2, "codec_type": "audio", "codec_name": "opus", "channels": 6},
        {
            "index": 3,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        },
    ]
    cards = {c.key: c for c in present_media(result)}
    assert cards["audio:1"].title == "音频 1" and cards["audio:2"].title == "音频 2"
    assert "video:3" not in cards and "attachments" in cards


def test_empty_invalid_tags_do_not_create_cards():
    result = InspectionResult(
        "a.webm", fields=[tag("artist", ["", " N/A ", "unknown"]), tag("date", "10292")]
    )
    assert [c.key for c in present_media(result)] == ["file"]
    assert clean_values([None, "N/A", "", "  A  ", "A", "Doe, Jane"]) == ["A", "Doe, Jane"]
    assert date_text(["20260230", "10292", "2024-02-29"]) == "2024-02-29"
    assert duration("nan") == "" and duration(1384.5) == "23:04.500"
    assert scaled("inf", ["B", "KiB"], 1024) == ""


def test_all_reference_properties_and_extra_technical_data_have_card_locations():
    result = fixture()
    result.file["mtime_ns"] = int(datetime(2026, 9, 20, 17, 38).timestamp() * 1_000_000_000)
    result.streams[0].update(
        reported_bitrate=11_481_000,
        bits_per_raw_sample="10",
        pix_fmt="yuv420p10le",
        color_space="bt2020nc",
        color_transfer="smpte2084",
        profile="Main 10",
    )
    cards = {c.key: c for c in present_media(result)}
    file = dict(cards["file"].rows)
    assert file["类型"] == "MP4 文件"
    assert file["文件大小"] == "1 MiB"
    assert file["文件位置"] == "C:\\Videos"
    assert file["修改日期"] == "2026-09-20 17:38:00"
    assert file["时长"] == "23:04.500"
    assert file["总平均码率（估算）"] == "3 Mbit/s"
    assert cards["content"].hero == "Title"
    assert dict(cards["content"].rows)["年份"] == "2026"
    assert dict(cards["content"].rows)["作者 / 艺术家"] == "Doe, Jane · ESPN"
    assert dict(cards["source"].rows)["备注 / 来源链接"] == "https://example.com/VideoA"
    video = dict(cards["video:0"].rows)
    assert video["帧宽度"] == "1920 px" and video["帧高度"] == "1080 px"
    assert video["平均帧率"] == "59.94 fps"
    assert video["视频码率"] == "11.48 Mbit/s"
    assert video["位深"] == "10 bit" and video["动态范围"] == "HDR · PQ"
    assert "C:\\Videos" not in cards["file"].summary(include_path=False)
