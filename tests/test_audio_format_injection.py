"""语言 / 原音偏好在 `-f` 里的表达（Phase B 的回归锁）。

盯的是一个已核实的上游事实：**`-S lang:xx` 从来不表示"偏好某语言"。**
`FormatSorter.settings` 里 `lang` 是 `language_preference` 的**数值**别名，喂它一个
语言码会让 yt-dlp 把全局 `settings['lang']['convert']` 改成 `'string'`、拿 10/5/−1/−10
当字符串跟 `"ja"` 比；而且 `add_item` 有 `if field in self._order: return`，所以十几个
`lang:` 条目里**只有第一个会被接受**。项目原先在 `youtube_service` 里展开的那一整段
BCP-47 别名（十几个 `lang:` 条目）贡献为零。

唯一有效的表达是格式串里的过滤器，也就是这里测的东西：

- 语言用 `[language^=xx]`（startswith）—— 裸 `[language=en]` 匹配不到真实标注 `en-US`
- 原音用 `[language_preference>=10?]` —— `?` 是**必须的**，见 `_ORIGINAL_AUDIO_FILTER`
- 兜底永远是原始格式串 —— 少了它，没有原音标记的视频会一条格式都选不出来

不需要 QApplication：`_plan_language_injection()` 是纯函数。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-langinj-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.utils.format_scorer import (  # noqa: E402
    STRATEGY_LANGUAGE_FIRST,
    STRATEGY_ORIGINAL_FIRST,
    STRATEGY_ORIGINAL_ONLY,
)
from fluentytdl.youtube.yt_dlp_cli import (  # noqa: E402
    _ORIGINAL_AUDIO_FILTER,
    _plan_language_injection,
)

#: 简易模式真实产出的形状：合并式 + 已混流兜底。
FMT = "bv*[height<=1080]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba"


def _plan(langs, strategy, *, fmt=FMT, multistreams=False):
    return _plan_language_injection(fmt, langs, strategy, multistreams=multistreams)


# ── 原音：策略决定它在不在，以及排第几 ──────────────────────


def test_original_first_puts_the_original_filter_ahead_of_the_language_one():
    """`original_first` 的全部含义就是分支顺序：原音在语言前面。"""
    plan = _plan(["ja"], STRATEGY_ORIGINAL_FIRST)
    assert plan.injected
    assert plan.reason == "injected_original_and_language"
    assert plan.fmt.index(_ORIGINAL_AUDIO_FILTER) < plan.fmt.index("[language^=ja]")


def test_language_first_omits_the_original_filter():
    plan = _plan(["ja"], STRATEGY_LANGUAGE_FIRST)
    assert plan.injected
    assert plan.reason == "injected_language"
    assert _ORIGINAL_AUDIO_FILTER not in plan.fmt
    assert "[language^=ja]" in plan.fmt


def test_original_only_matches_original_first_on_this_path():
    """格式串表达不了"只要原音，没有就报错" —— 两个策略在这里产出相同的串。

    硬要表达就得去掉兜底，那会让没有原音标记的视频一条格式都选不出来（合并直接失败）。
    与 `format_scorer` 那边"原音命中即赢、无原音则退化为语言优先"是同一个取舍。
    """
    assert _plan(["ja"], STRATEGY_ORIGINAL_ONLY).fmt == _plan(["ja"], STRATEGY_ORIGINAL_FIRST).fmt


def test_original_filter_is_injected_without_any_language_preference():
    """没有语言偏好时"原音优先"仍然要生效 —— 旧实现在这里直接不注入。

    旧代码把 `orig` 当成"不是具体请求"显式跳过，于是原音偏好在 CLI 上没有任何表达，
    这正是用户说的"原音只能寻找到最佳音频"。
    """
    plan = _plan([], STRATEGY_ORIGINAL_FIRST)
    assert plan.injected
    assert plan.reason == "injected_original"
    assert plan.langs == []
    assert _ORIGINAL_AUDIO_FILTER in plan.fmt


def test_original_filter_is_none_inclusive():
    """`?` 不能掉：Twitter 等平台的格式里没有 `language_preference`。

    不带 `?` 时 `_build_format_filter` 对 `None` 返回假，会把那些平台的**所有**音轨
    过滤光 —— 一个纯粹为 YouTube 加的偏好会让别的平台整体下载失败。
    """
    assert _ORIGINAL_AUDIO_FILTER.endswith("?]")


# ── 语言过滤器的形状 ────────────────────────────────────────


def test_language_uses_startswith_so_a_bare_preference_hits_a_regional_tag():
    """`[language=en]` 匹配不到 `en-US`，`[language^=en]` 可以。"""
    plan = _plan(["en"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=en]" in plan.fmt
    assert "[language=en]" not in plan.fmt


def test_aliases_are_exact_not_prefix():
    """别名分支用 `=`：`[language^=zh]` 会把 `zh-Hant` 一起命中。

    别名表里有 `zh-hans → zh` 这条，用 `^=` 展开等于"简中偏好换来一条繁中音轨"。
    """
    plan = _plan(["zh-Hans"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=zh-Hans]" in plan.fmt
    assert "[language=zh-CN]" in plan.fmt
    assert "[language^=zh]" not in plan.fmt


def test_preference_order_is_preserved_in_branch_order():
    plan = _plan(["ja", "ko"], STRATEGY_LANGUAGE_FIRST)
    assert plan.fmt.index("[language^=ja]") < plan.fmt.index("[language^=ko]")


def test_user_written_tags_are_canonicalized():
    """设置页允许自定义标签，`pt-br` 要变成 YouTube 的写法 `pt-BR`。"""
    assert "[language^=pt-BR]" in _plan(["pt-br"], STRATEGY_LANGUAGE_FIRST).fmt


def test_unsafe_tags_never_reach_the_command_line():
    """过滤器是命令行的一部分 —— 带 `]` / 空格的标签必须被丢掉，不是被转义。"""
    plan = _plan(["ja]bad", "ko"], STRATEGY_LANGUAGE_FIRST)
    assert "bad" not in plan.fmt
    assert "[language^=ko]" in plan.fmt


def test_duplicate_preferences_do_not_duplicate_branches():
    plan = _plan(["ja", "ja"], STRATEGY_LANGUAGE_FIRST)
    assert plan.langs == ["ja"]
    assert plan.fmt.count("[language^=ja]") == len(FMT.split("/"))


# ── 过滤器贴在哪 ────────────────────────────────────────────


def test_filter_goes_on_the_audio_half_of_a_merge():
    """`bv*+ba` 里过滤器只贴 `ba` —— 贴到视频侧会把视频流也按语言过滤掉。"""
    plan = _plan(["ja"], STRATEGY_LANGUAGE_FIRST, fmt="bv*[height<=720]+ba")
    assert plan.fmt.startswith("bv*[height<=720]+ba[language^=ja]/")


def test_single_format_gets_the_filter_appended_whole():
    plan = _plan(["ja"], STRATEGY_LANGUAGE_FIRST, fmt="ba")
    assert plan.fmt == "ba[language^=ja]/ba"


def test_the_unfiltered_format_string_is_always_the_last_fallback():
    """少了兜底，一个没有日语音轨的视频会直接选不出格式。"""
    for strategy in (STRATEGY_ORIGINAL_FIRST, STRATEGY_LANGUAGE_FIRST, STRATEGY_ORIGINAL_ONLY):
        assert _plan(["ja"], strategy).fmt.endswith("/" + FMT)


# ── 五条不注入 / 注入路径全收口 ──────────────────────────────


def test_multistreams_is_deliberately_not_injected():
    """显式 `v+a1+a2` ID 已经点选完音轨，插过滤器只会把那串 ID 改坏。"""
    plan = _plan(["ja"], STRATEGY_ORIGINAL_FIRST, multistreams=True)
    assert not plan.injected
    assert plan.reason == "explicit_multistream_ids"
    assert plan.fmt == FMT


def test_language_first_with_no_preference_is_not_a_failure():
    plan = _plan([], STRATEGY_LANGUAGE_FIRST)
    assert not plan.injected
    assert plan.reason == "no_language_request"
    assert plan.fmt == FMT


def test_empty_format_string_short_circuits():
    plan = _plan(["ja"], STRATEGY_ORIGINAL_FIRST, fmt="")
    assert not plan.injected
    assert plan.reason == "no_format_string"


def test_unparsable_format_string_is_reported_as_such():
    plan = _plan(["ja"], STRATEGY_ORIGINAL_FIRST, fmt="///")
    assert not plan.injected
    assert plan.reason == "unparsable_format_string"


def test_unknown_strategy_falls_back_to_original_first():
    """配置里读到垃圾值时按默认策略走，而不是静默退化成"什么都不注入"。"""
    for value in (None, "", "nonsense"):
        plan = _plan(["ja"], value)
        assert plan.strategy == STRATEGY_ORIGINAL_FIRST
        assert _ORIGINAL_AUDIO_FILTER in plan.fmt


def test_strategy_is_reported_for_the_log():
    """`strategy` 进日志：`audio_kind` / `audio_ranked` 要和它一起读才有意义。"""
    assert _plan(["ja"], STRATEGY_LANGUAGE_FIRST).strategy == STRATEGY_LANGUAGE_FIRST
