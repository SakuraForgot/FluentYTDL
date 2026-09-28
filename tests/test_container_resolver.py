"""单一权威 `resolve_output_container` 的阶梯全分支覆盖（Phase 3a）。

resolver 是纯函数，理应与今日 `decide_merge_container` + 两个 `ensure_*` 的**最终容器**
逐分支等价。本表锁死每一档 rung 的 (最终容器, reason, overridden)，任何人改阶梯顺序、
放松 ext 守卫、或把 both-true 的 reason 从 audio_multistream 换掉都会在这里炸。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-resolver-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.utils.container_compat import (  # noqa: E402
    ContainerResolution,
    resolve_output_container,
)

# (id, kwargs, container, reason, overridden)
_VIDEO_CASES = [
    ("mp4+m4a_plain", dict(video_ext="mp4", audio_ext="m4a"), "mp4", None, False),
    ("webm+webm_plain", dict(video_ext="webm", audio_ext="webm"), "webm", None, False),
    ("mixed_naive_mkv", dict(video_ext="mp4", audio_ext="webm"), "mkv", None, False),
    (
        "mp4_multiaudio_count",
        dict(video_ext="mp4", audio_ext="m4a", audio_track_count=2),
        "mkv",
        "audio_multistream",
        False,
    ),
    (
        "user_mp4_multiaudio_overrides",
        dict(user_container="mp4", video_ext="mp4", audio_ext="m4a", audio_track_count=2),
        "mkv",
        "audio_multistream",
        True,
    ),
    (
        "user_mkv_multiaudio_stays",
        dict(user_container="mkv", video_ext="mp4", audio_ext="m4a", audio_track_count=2),
        "mkv",
        None,
        False,
    ),
    (
        "multistreams_flag_single_count",
        dict(user_container="mp4", video_ext="mp4", audio_ext="m4a", audio_multistreams=True),
        "mkv",
        "audio_multistream",
        True,
    ),
    (
        "mp4_embed_two_langs",
        dict(video_ext="mp4", audio_ext="m4a", embed_subtitles=True, subtitle_lang_count=2),
        "mkv",
        "subtitle_multi_lang",
        False,
    ),
    (
        "user_mp4_embed_two_langs_overrides",
        dict(
            user_container="mp4",
            video_ext="mp4",
            audio_ext="m4a",
            embed_subtitles=True,
            subtitle_lang_count=2,
        ),
        "mkv",
        "subtitle_multi_lang",
        True,
    ),
    (
        "mp4_embed_one_lang_stays",
        dict(video_ext="mp4", audio_ext="m4a", embed_subtitles=True, subtitle_lang_count=1),
        "mp4",
        None,
        False,
    ),
    (
        "webm_embed_one_lang",
        dict(video_ext="webm", audio_ext="webm", embed_subtitles=True, subtitle_lang_count=1),
        "mkv",
        "subtitle_webm_incompatible",
        False,
    ),
    (
        "user_webm_embed_overrides",
        dict(user_container="webm", embed_subtitles=True, subtitle_lang_count=1),
        "mkv",
        "subtitle_webm_incompatible",
        True,
    ),
    (
        "webm_no_embed_stays",
        dict(video_ext="webm", audio_ext="webm", embed_subtitles=False),
        "webm",
        None,
        False,
    ),
    (
        "embed_unset_no_ext",
        dict(embed_subtitles=True, subtitle_lang_count=1),
        "mkv",
        "subtitle_container_unset",
        False,
    ),
    ("quick_deliberate_unset", dict(), None, None, False),
    (
        "mov_embed_two_langs_stays",
        dict(user_container="mov", embed_subtitles=True, subtitle_lang_count=2),
        "mov",
        None,
        False,
    ),
    (
        "both_true_audio_multistream_wins",
        dict(
            video_ext="mp4",
            audio_ext="m4a",
            audio_track_count=2,
            embed_subtitles=True,
            subtitle_lang_count=2,
        ),
        "mkv",
        "audio_multistream",
        False,
    ),
    (
        "rung5_partial_ext_fallback",
        dict(video_ext="mp4", audio_ext=None),
        "mkv",
        None,
        False,
    ),
    (
        "empty_user_container_is_unset",
        dict(user_container="", video_ext="mp4", audio_ext="m4a"),
        "mp4",
        None,
        False,
    ),
]


@pytest.mark.parametrize(
    "kwargs,container,reason,overridden",
    [case[1:] for case in _VIDEO_CASES],
    ids=[case[0] for case in _VIDEO_CASES],
)
def test_video_ladder(kwargs, container, reason, overridden):
    res = resolve_output_container(**kwargs)
    assert res.container == container
    assert res.reason == reason
    assert res.overridden is overridden
    # 视频路径：resolved 跟随 container，audio_format 恒 None。
    assert res.resolved == container
    assert res.audio_format is None


def test_overridden_never_true_without_explicit_user_container():
    # user_container=None 时，无论怎么升级 mkv，overridden 恒 False（锁死 provenance 语义）。
    for kwargs in (
        dict(video_ext="mp4", audio_ext="m4a", audio_track_count=3),
        dict(video_ext="webm", audio_ext="webm", embed_subtitles=True, subtitle_lang_count=2),
        dict(embed_subtitles=True),
    ):
        res = resolve_output_container(**kwargs)
        assert res.overridden is False
        assert res.user_requested is None


def test_needs_audio_multistreams_flag():
    assert (
        resolve_output_container(video_ext="mp4", audio_ext="m4a").needs_audio_multistreams is False
    )
    assert (
        resolve_output_container(
            video_ext="mp4", audio_ext="m4a", audio_track_count=2
        ).needs_audio_multistreams
        is True
    )
    assert (
        resolve_output_container(
            video_ext="mp4", audio_ext="m4a", audio_multistreams=True
        ).needs_audio_multistreams
        is True
    )


def test_audio_only_ignores_container_and_returns_format():
    res = resolve_output_container(
        audio_only=True,
        user_audio_format="mp3",
        # 下列视频维度全部应被忽略：
        user_container="mp4",
        embed_subtitles=True,
        subtitle_lang_count=3,
        audio_track_count=5,
    )
    assert isinstance(res, ContainerResolution)
    assert res.container is None
    assert res.audio_format == "mp3"
    assert res.resolved == "mp3"
    assert res.user_requested == "mp3"
    assert res.overridden is False
    assert res.reason is None
    assert res.needs_audio_multistreams is False


def test_audio_only_unset_format():
    res = resolve_output_container(audio_only=True)
    assert res.container is None
    assert res.audio_format is None
    assert res.resolved is None
