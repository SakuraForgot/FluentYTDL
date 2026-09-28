"""Phase 4 装配预览 view-model（`models/download_plan.py`）的表驱动覆盖。

view-model 是纯函数、不持久化的「配方」快照。它的**唯一不变量**是「预览==实际装配」：
容器绝不在这里重算，而是从 `_compute_selection_result()` 已算好的 `{format, extra_opts}`
反推 —— `format` 串按 `/`→`+` 归一后拆出的 id 回查 `rows`，容器读 `merge_output_format`，
音频格式读 `audio_format`（仅 `extract_audio` 时）。本表锁死分类五档、反推选流、容器/
格式取值、`override_note`/`output_label`/`summary_lines` 文案，以及 `SubtitlePlan` 四态与
`Stream`/`AudioSummary` 的展示串。任何人改反推逻辑或文案都会在这里炸。

`tr_text` 在无 `.qm` 的测试环境里原样返回中文源串（`{}` 仍会 `.format`），故断言直接比
中文字面值。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-plan-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.models.download_plan import (  # noqa: E402
    KIND_AUDIO_ONLY,
    KIND_FALLBACK,
    KIND_MUXED,
    KIND_VIDEO_AUDIO,
    KIND_VIDEO_ONLY,
    AudioSummary,
    ResolvedDownloadPlan,
    StreamSummary,
    SubtitlePlan,
    build_download_plan,
)
from fluentytdl.models.subtitle_config import SubtitleConfig  # noqa: E402
from fluentytdl.utils.container_compat import resolve_output_container  # noqa: E402

# ── 行/计划构造助手 ──────────────────────────────────────────────


def _vid(fid="137", ext="mp4", height=1080, vcodec="avc1.640028", filesize=52428800):
    return {
        "kind": "video",
        "format_id": fid,
        "ext": ext,
        "height": height,
        "vcodec": vcodec,
        "filesize": filesize,
    }


def _aud(fid="140", ext="m4a", abr=128, acodec="mp4a.40.2", language="en", language_preference=10):
    return {
        "kind": "audio",
        "format_id": fid,
        "ext": ext,
        "abr": abr,
        "acodec": acodec,
        "language": language,
        "language_preference": language_preference,
    }


def _mux(fid="22", ext="mp4", height=720, vcodec="avc1.4d401f"):
    return {"kind": "muxed", "format_id": fid, "ext": ext, "height": height, "vcodec": vcodec}


def _no_sub():
    return SubtitlePlan(
        enabled=False, embed=False, keep_external=False, languages=(), output_format="srt"
    )


def _plan(**overrides):
    base = dict(
        download_kind=KIND_VIDEO_AUDIO,
        format_expr="137+140",
        container="mp4",
        audio_format=None,
        resolution=None,
        video=None,
        audio_tracks=(),
        subtitle=_no_sub(),
        output_dir="",
    )
    base.update(overrides)
    return ResolvedDownloadPlan(**base)


# ── 分类五档 + 反推选流 ──────────────────────────────────────────

# (id, result, rows, expected_kind)
_CLASSIFY_CASES = [
    (
        "video_audio",
        {"format": "137+140", "extra_opts": {"merge_output_format": "mp4"}},
        [_vid(), _aud()],
        KIND_VIDEO_AUDIO,
    ),
    (
        "video_only",
        {"format": "137", "extra_opts": {"merge_output_format": "mp4"}},
        [_vid()],
        KIND_VIDEO_ONLY,
    ),
    (
        "audio_only",
        {"format": "140", "extra_opts": {"extract_audio": True, "audio_format": "mp3"}},
        [_aud()],
        KIND_AUDIO_ONLY,
    ),
    (
        # 专业模式手选音频流、不转码（无 extract_audio）：仍应归「仅音频」，而非兜底。
        "audio_only_raw_no_extract",
        {"format": "140", "extra_opts": {}},
        [_aud()],
        KIND_AUDIO_ONLY,
    ),
    (
        "muxed",
        {"format": "22", "extra_opts": {}},
        [_mux()],
        KIND_MUXED,
    ),
    (
        "fallback_best",
        {"format": "best", "extra_opts": {}},
        [],
        KIND_FALLBACK,
    ),
    (
        "fallback_empty",
        {"format": "", "extra_opts": {}},
        [],
        KIND_FALLBACK,
    ),
    (
        "fallback_no_result",
        {},
        [],
        KIND_FALLBACK,
    ),
    (
        "slash_normalized",
        {"format": "137+140/best", "extra_opts": {"merge_output_format": "mkv"}},
        [_vid(), _aud()],
        KIND_VIDEO_AUDIO,
    ),
]


@pytest.mark.parametrize(
    "result,rows,expected_kind",
    [case[1:] for case in _CLASSIFY_CASES],
    ids=[case[0] for case in _CLASSIFY_CASES],
)
def test_classify_kind(result, rows, expected_kind):
    plan = build_download_plan(result=result, rows=rows, resolution=None, subtitle=_no_sub())
    assert plan.download_kind == expected_kind


def test_reverse_derivation_picks_rows_across_slash():
    # `/` 归一成 `+`，"best" 不在 rows 里被忽略；vid+aud 命中 → video_audio。
    plan = build_download_plan(
        result={"format": "137+140/best", "extra_opts": {"merge_output_format": "mkv"}},
        rows=[_vid(), _aud()],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_VIDEO_AUDIO
    assert plan.video is not None and plan.video.height == 1080
    assert len(plan.audio_tracks) == 1
    assert plan.container == "mkv"
    assert plan.audio_format is None


def test_multi_audio_tracks_all_derived():
    plan = build_download_plan(
        result={"format": "137+140+251", "extra_opts": {"merge_output_format": "mkv"}},
        rows=[
            _vid(),
            _aud("140", language="en", language_preference=10),
            _aud("251", ext="webm", acodec="opus", language="ja", language_preference=-1),
        ],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_VIDEO_AUDIO
    assert len(plan.audio_tracks) == 2


def test_muxed_stream_summary_from_mux_row():
    plan = build_download_plan(
        result={"format": "22", "extra_opts": {}},
        rows=[_mux()],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_MUXED
    assert plan.video is not None and plan.video.kind == "muxed"
    assert plan.audio_tracks == ()


# ── 容器 / 音频格式取值 ──────────────────────────────────────────


def test_container_read_from_merge_output_format():
    plan = build_download_plan(
        result={"format": "137+140", "extra_opts": {"merge_output_format": "mp4"}},
        rows=[_vid(), _aud()],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.container == "mp4"
    assert plan.audio_format is None


def test_audio_format_ignored_without_extract_audio():
    # audio_format 键在，但没有 extract_audio → 反推为视频+音频，audio_format 恒 None。
    plan = build_download_plan(
        result={
            "format": "137+140",
            "extra_opts": {"merge_output_format": "mp4", "audio_format": "mp3"},
        },
        rows=[_vid(), _aud()],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_VIDEO_AUDIO
    assert plan.audio_format is None


def test_audio_only_honors_audio_format():
    plan = build_download_plan(
        result={"format": "140", "extra_opts": {"extract_audio": True, "audio_format": "mp3"}},
        rows=[_aud()],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_AUDIO_ONLY
    assert plan.audio_format == "mp3"
    assert plan.output_label == "MP3"


def test_audio_only_raw_falls_back_to_stream_ext():
    # 专业模式手选音频流、不转码（无 extract_audio / audio_format）：输出格式回落到流的原生
    # ext，预览显示 M4A 而非「自动」；同时归类为「仅音频」而非兜底。
    plan = build_download_plan(
        result={"format": "140", "extra_opts": {}},
        rows=[_aud(ext="m4a")],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_AUDIO_ONLY
    assert plan.audio_format == "m4a"
    assert plan.output_label == "M4A"
    assert len(plan.audio_tracks) == 1


def test_video_only_falls_back_to_stream_ext_for_container():
    # 仅视频（无 merge_output_format）：容器回落到视频流的原生 ext，输出容器显示 WEBM。
    plan = build_download_plan(
        result={"format": "137", "extra_opts": {}},
        rows=[_vid(ext="webm")],
        resolution=None,
        subtitle=_no_sub(),
    )
    assert plan.download_kind == KIND_VIDEO_ONLY
    assert plan.container == "webm"
    assert plan.output_label == "WEBM"
    assert plan.audio_tracks == ()


# ── output_label ────────────────────────────────────────────────


def test_output_label_video_uses_container():
    assert _plan(download_kind=KIND_VIDEO_AUDIO, container="mkv").output_label == "MKV"


def test_output_label_audio_uses_format():
    assert (
        _plan(download_kind=KIND_AUDIO_ONLY, audio_format="flac", container=None).output_label
        == "FLAC"
    )


def test_output_label_auto_when_missing():
    assert _plan(download_kind=KIND_FALLBACK, container=None).output_label == "自动"


# ── override_note ───────────────────────────────────────────────


def test_override_note_none_without_resolution():
    assert _plan(resolution=None).override_note is None


def test_override_note_none_when_not_overridden():
    res = resolve_output_container(video_ext="mp4", audio_ext="m4a")
    assert res.overridden is False
    assert _plan(resolution=res).override_note is None


def test_override_note_present_when_overridden():
    res = resolve_output_container(
        user_container="mp4", video_ext="mp4", audio_ext="m4a", audio_track_count=2
    )
    assert res.overridden is True
    assert _plan(resolution=res).override_note == "已从 MP4 升级为 MKV：保留多条音轨"


# ── SubtitlePlan 四态 + from_config ──────────────────────────────

# (id, kwargs, expected_describe)
_SUB_CASES = [
    (
        "disabled",
        dict(enabled=False, embed=False, keep_external=False, languages=(), output_format="srt"),
        "不下载",
    ),
    (
        "enabled_but_neither",
        dict(
            enabled=True, embed=False, keep_external=False, languages=("en",), output_format="srt"
        ),
        "不下载",
    ),
    (
        "embed_only",
        dict(enabled=True, embed=True, keep_external=False, languages=("en",), output_format="srt"),
        "内嵌 · en · SRT",
    ),
    (
        "external_only",
        dict(
            enabled=True,
            embed=False,
            keep_external=True,
            languages=("en", "zh-Hans"),
            output_format="ass",
        ),
        "外挂文件 · en/zh-Hans · ASS",
    ),
    (
        "both",
        dict(enabled=True, embed=True, keep_external=True, languages=("en",), output_format="vtt"),
        "内嵌 + 外挂文件 · en · VTT",
    ),
]


@pytest.mark.parametrize(
    "kwargs,expected",
    [case[1:] for case in _SUB_CASES],
    ids=[case[0] for case in _SUB_CASES],
)
def test_subtitle_plan_describe(kwargs, expected):
    assert SubtitlePlan(**kwargs).describe() == expected


def test_subtitle_plan_from_config_truncates_languages():
    cfg = SubtitleConfig(
        enabled=True,
        embed=True,
        keep_external=True,
        default_languages=["zh-Hans", "en", "ja"],
        max_languages=2,
        output_format="srt",
    )
    plan = SubtitlePlan.from_config(cfg)
    assert plan.enabled is True
    assert plan.embed is True
    assert plan.keep_external is True
    assert plan.languages == ("zh-Hans", "en")
    assert plan.active is True


def test_subtitle_plan_from_config_disabled_zeroes_delivery():
    cfg = SubtitleConfig(enabled=False, embed=True, keep_external=True, default_languages=["en"])
    plan = SubtitlePlan.from_config(cfg)
    assert plan.enabled is False
    assert plan.embed is False
    assert plan.keep_external is False
    assert plan.languages == ()
    assert plan.active is False


# ── StreamSummary / AudioSummary 展示串 ──────────────────────────


def test_stream_summary_describe_full():
    s = StreamSummary(kind="video", height=1080, ext="mp4", vcodec="avc1.640028", filesize=52428800)
    assert s.describe() == "1080p · H.264 · MP4 · 50.0 MB"


def test_stream_summary_describe_no_size():
    s = StreamSummary(kind="video", height=720, ext="webm", vcodec="vp9", filesize=None)
    assert s.describe() == "720p · VP9 · WEBM"


def test_stream_summary_unknown_resolution():
    s = StreamSummary(kind="muxed", height=0, ext="mp4", vcodec="", filesize=None)
    assert s.describe() == "未知分辨率 · MP4"


def test_audio_summary_describe_full():
    a = AudioSummary(language="en", kind="original", abr=128, ext="m4a", acodec="mp4a.40.2")
    assert a.describe() == "en · 原声 · 128k · AAC"


def test_audio_summary_describe_minimal():
    a = AudioSummary(language=None, kind="unknown", abr=0, ext="", acodec="")
    assert a.describe() == "音频"


# ── summary_lines 整行组合 ───────────────────────────────────────


def test_summary_lines_full_video_audio():
    res = resolve_output_container(
        user_container="mp4", video_ext="mp4", audio_ext="m4a", audio_track_count=2
    )
    plan = ResolvedDownloadPlan(
        download_kind=KIND_VIDEO_AUDIO,
        format_expr="137+140+251",
        container="mkv",
        audio_format=None,
        resolution=res,
        video=StreamSummary(
            kind="video", height=1080, ext="mp4", vcodec="avc1.640028", filesize=52428800
        ),
        audio_tracks=(
            AudioSummary(language="en", kind="original", abr=128, ext="m4a", acodec="mp4a.40.2"),
            AudioSummary(language="ja", kind="dub", abr=128, ext="m4a", acodec="mp4a.40.2"),
        ),
        subtitle=SubtitlePlan(
            enabled=True, embed=True, keep_external=False, languages=("en",), output_format="srt"
        ),
        output_dir="D:/Videos",
    )
    assert plan.summary_lines() == [
        "类型：视频 + 音频",
        "输出容器：MKV",
        "  ↳ 已从 MP4 升级为 MKV：保留多条音轨",
        "视频：1080p · H.264 · MP4 · 50.0 MB",
        "音频：en · 原声 · 128k · AAC",
        "音频：ja · 配音 · 128k · AAC",
        "字幕：内嵌 · en · SRT",
        "封面：不下载",
    ]


def test_summary_lines_audio_only_omits_empty_dir():
    plan = build_download_plan(
        result={"format": "140", "extra_opts": {"extract_audio": True, "audio_format": "mp3"}},
        rows=[_aud()],
        resolution=None,
        subtitle=_no_sub(),
        output_dir="",
    )
    lines = plan.summary_lines()
    assert lines[0] == "类型：仅音频"
    assert lines[1] == "输出格式：MP3"
    assert "字幕：不下载" in lines
    assert not any(line.startswith("保存到") for line in lines)
