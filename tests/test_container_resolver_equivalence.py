"""锁死 Phase 3b 迁移的**行为等价**：`resolve_output_container` 的最终容器必须与今日
`decide_merge_container` + 运行时 `ensure_*` + `SubtitleFeature` 兜底组成的旧管线逐格一致。

这不是重复 `test_container_resolver.py`（那张表锁的是 resolver 自身的 (容器,reason,overridden)
契约）。这里锁的是**跨实现的等价**：把 youtube.py 各视频计算点从旧 `decide + 运行时兜底`
换成 resolver 之前，先证明"换了也不改用户拿到的容器"。任一格不一致就说明迁移会静默改行为，
必须在这里炸而不是在用户机器上炸。

两条路径分别锁：
- **auto（user_container=None）**：旧管线是确定的 `decide → ensure_subtitle → ensure_audio →
  SubtitleFeature`，与 `resolve_output_container(...).container` 逐格比。
- **override（user_container 显式）**：旧的 UI 直接写 `override`、只有运行时 `ensure_*` 才升级
  （单视频 override 分支今天走交互式 `_handle_container_conflict`，无静态终值）。这里锁的是
  resolver 对 override 的升级 == 运行时 `ensure_*` 对同一 override 的升级 —— 这正是迁移后
  `ensure_*` 能安全退化成幂等空操作的不变量，并附带校验 `overridden` 标志。

只覆盖有真实流 ext 的 youtube 简易/专业路径（quick 的"蓄意 unset"是独立管线，不过 decide，
由 resolver 自己的 Rung5 ext 守卫测试覆盖）。reason 顺序漂移（both-true 走 audio_multistream、
user webm+多语言走 subtitle_multi_lang 而非 webm_incompatible）只影响 reason 不影响终值，
故此处只比最终容器；reason 契约由 test_observability_container_disk.py 单独锁。
"""

import itertools
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-equiv-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.utils.container_compat import (  # noqa: E402
    ensure_audio_multistream_compatible_container,
    ensure_subtitle_compatible_container,
    resolve_output_container,
)
from fluentytdl.utils.format_scorer import ScoringContext, decide_merge_container  # noqa: E402

# (video_ext, audio_ext) —— 覆盖 naive 推断的每一支：mp4 家族、webm 纯净、mixed→mkv。
_EXT_PAIRS = [
    ("mp4", "m4a"),
    ("mp4", "aac"),
    ("m4v", "m4a"),
    ("webm", "webm"),
    ("mp4", "webm"),
    ("mkv", "mka"),
]


def _norm(v):
    s = str(v or "").strip().lower()
    return s or None


def _legacy_auto_final(v, a, embed, lc, tc, ms):
    """auto 路径旧管线终值：decide → ensure_subtitle → ensure_audio → SubtitleFeature 兜底。"""
    ctx = ScoringContext(embed_subtitles=embed, subtitle_lang_count=lc, audio_track_count=tc)
    merge_fmt = decide_merge_container(v, a, ctx)  # 旧 UI：user_container=None 时直接用它
    opts = {"merge_output_format": merge_fmt}
    if embed:
        opts["embedsubtitles"] = True
        opts["subtitleslangs"] = [str(i) for i in range(lc)]
    if ms:
        opts["audio_multistreams"] = True
    ensure_subtitle_compatible_container(opts)
    ensure_audio_multistream_compatible_container(opts, tc)
    _apply_subtitle_feature_safety(opts)
    return _norm(opts.get("merge_output_format"))


def _legacy_override_final(uc, embed, lc, tc, ms):
    """override 路径：运行时 `ensure_*` 施加到用户 override 上的终值（迁移后应等于 resolver）。"""
    opts = {"merge_output_format": uc}
    if embed:
        opts["embedsubtitles"] = True
        opts["subtitleslangs"] = [str(i) for i in range(lc)]
    if ms:
        opts["audio_multistreams"] = True
    ensure_subtitle_compatible_container(opts)
    ensure_audio_multistream_compatible_container(opts, tc)
    _apply_subtitle_feature_safety(opts)
    return _norm(opts.get("merge_output_format"))


def _apply_subtitle_feature_safety(opts):
    """复刻 SubtitleFeature.on_download_start：embed 时 webm/未设 → mkv（下载启动最终兜底）。"""
    if opts.get("embedsubtitles"):
        f = (opts.get("merge_output_format") or "").lower()
        if f == "webm" or not f:
            opts["merge_output_format"] = "mkv"


_FEATURE_DIMS = list(
    itertools.product(
        [False, True],  # embed
        [0, 1, 2],  # subtitle_lang_count
        [1, 2],  # audio_track_count
        [False, True],  # audio_multistreams
    )
)


@pytest.mark.parametrize("v,a", _EXT_PAIRS)
@pytest.mark.parametrize("embed,lc,tc,ms", _FEATURE_DIMS)
def test_auto_path_equivalent(v, a, embed, lc, tc, ms):
    res = resolve_output_container(
        user_container=None,
        video_ext=v,
        audio_ext=a,
        embed_subtitles=embed,
        subtitle_lang_count=lc,
        audio_track_count=tc,
        audio_multistreams=ms,
    )
    assert res.container == _legacy_auto_final(v, a, embed, lc, tc, ms)
    assert res.overridden is False


@pytest.mark.parametrize("uc", ["mp4", "mkv", "webm", "mov"])
@pytest.mark.parametrize("embed,lc,tc,ms", _FEATURE_DIMS)
def test_override_matches_runtime_ensure(uc, embed, lc, tc, ms):
    res = resolve_output_container(
        user_container=uc,
        video_ext="mp4",
        audio_ext="m4a",
        embed_subtitles=embed,
        subtitle_lang_count=lc,
        audio_track_count=tc,
        audio_multistreams=ms,
    )
    assert res.container == _legacy_override_final(uc, embed, lc, tc, ms)
    assert res.overridden is (res.container != _norm(uc))
