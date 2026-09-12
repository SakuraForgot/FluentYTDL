"""语言 / 原音偏好在 `-f` 里的表达（Phase B 的回归锁）。

盯的是一个已核实的上游事实：**`-S lang:xx` 从来不表示"偏好某语言"。**
`FormatSorter.settings` 里 `lang` 是 `language_preference` 的**数值**别名，喂它一个
语言码会让 yt-dlp 把全局 `settings['lang']['convert']` 改成 `'string'`、拿 10/5/−1/−10
当字符串跟 `"ja"` 比；而且 `add_item` 有 `if field in self._order: return`，所以十几个
`lang:` 条目里**只有第一个会被接受**。项目原先在 `youtube_service` 里展开的那一整段
BCP-47 别名（十几个 `lang:` 条目）贡献为零。

唯一有效的表达是格式串里的过滤器，也就是这里测的东西：

- 语言用 `[language^=xx]`（startswith）—— 裸 `[language=en]` 匹配不到真实标注 `en-US`
- 原音用 `[language_preference>=?10]` —— `?` 是**必须的**，且**必须紧跟运算符**，
  见 `_ORIGINAL_AUDIO_FILTER`
- 兜底永远是原始格式串 —— 少了它，没有原音标记的视频会一条格式都选不出来

不需要 QApplication：`_plan_language_injection()` 是纯函数。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-langinj-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

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
    """`?` 不能掉，而且**位置只能在运算符后面**。

    两件事各有代价，都实测过（yt-dlp 2026.08.30，真实多语言视频）：

    1. 掉了 `?`：Twitter 等平台的格式里没有 `language_preference`，
       `_build_format_filter` 对 `None` 返回假，那些平台的**所有**音轨被过滤光，
       只能靠格式串末尾的无过滤兜底救回来。
    2. 写成 `>=10?`（`?` 跟在值后面）：yt-dlp 的过滤器正则把 none-inclusive 标记
       放在 operator 与 value 之间，`10?` 解析不成数字 ——
       `SyntaxError: Invalid filter specification`，整个下载起不来。
       这条断言原先只写了 `endswith("?]")`，正好放过了这种写法。
    """
    assert _ORIGINAL_AUDIO_FILTER == "[language_preference>=?10]"


# ── 语言过滤器的形状 ────────────────────────────────────────


def test_language_uses_startswith_so_a_bare_preference_hits_a_regional_tag():
    """`[language=en]` 匹配不到 `en-US`，`[language^=en]` 可以。"""
    plan = _plan(["en"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=en]" in plan.fmt
    assert "[language=en]" not in plan.fmt


def test_aliases_are_exact_not_prefix():
    """别名分支用 `=`：`[language^=zh]` 会把 `zh-Hant` 一起命中。

    别名表里有 `zh-hans → zh` 这条，用 `^=` 展开等于"简中偏好换来一条繁中音轨"。

    末尾追加的同母语言软档写成 `[language^=zh-]`（见 `test_macro_group_follows_exact_and_alias`），
    它**不含**裸子串 `[language^=zh]`，所以这条"别名不是前缀"的断言仍然成立。
    """
    plan = _plan(["zh-Hans"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=zh-Hans]" in plan.fmt
    assert "[language=zh-CN]" in plan.fmt
    assert "[language^=zh]" not in plan.fmt


def test_macro_group_follows_exact_and_alias():
    """同母语言软档追加在精确/别名之后：只在无精确脚本时兜底，不改变首选。

    偏好 `zh-Hans` 末尾多一组 `[language^=zh-]`（表达 primary==zh），它排在
    `[language^=zh-Hans]`（精确）与 `[language=zh-CN]`（别名）之后 —— 简中在场时仍先命中
    简中，简/繁切换不受影响；带多门语言时，软档也早于回退链里的下一门语言。
    """
    plan = _plan(["zh-Hans"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=zh-]" in plan.fmt
    assert plan.fmt.index("[language^=zh-]") > plan.fmt.index("[language^=zh-Hans]")
    assert plan.fmt.index("[language^=zh-]") > plan.fmt.index("[language=zh-CN]")
    # 软档仍早于末尾的原始兜底
    assert plan.fmt.index("[language^=zh-]") < plan.fmt.index("/" + FMT)

    # 多门语言：简中的软档要早于下一门语言 en，否则回退链顺序被打乱
    multi = _plan(["zh-Hans", "en"], STRATEGY_LANGUAGE_FIRST)
    assert multi.fmt.index("[language^=zh-]") < multi.fmt.index("[language^=en]")


def test_macro_is_precise_not_bare_prefix():
    """软档写成 `[language=zh]` + `[language^=zh-]`，**不是**裸 `[language^=zh]`。

    裸 `^=zh` 会连三字母近亲 Zhuang(`zha`) 一起命中，也与打分器的 `primary_subtag`
    相等语义分叉。两条精确表达式恰好等价于"primary subtag == zh"，两条路径语义一致。
    """
    plan = _plan(["zh-Hans"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=zh]" not in plan.fmt

    # 裸偏好（无脚本/地区后缀）不追加软档：`^=ja` 已覆盖其 primary 空间，追加是冗余
    bare = _plan(["ja"], STRATEGY_LANGUAGE_FIRST)
    assert "[language^=ja-]" not in bare.fmt


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


# ── 真实 yt-dlp 的语法校验 ──────────────────────────────────


def _ytdlp_binary() -> str | None:
    """Use the CI snapshot explicitly; retain the developer's existing tool fallback."""
    if configured := os.environ.get("FLUENTYTDL_TEST_YTDLP"):
        return configured if Path(configured).is_file() else None
    exe = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "bin"
        / "yt-dlp"
        / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    )
    return str(exe) if exe.exists() else None


@pytest.mark.parametrize(
    "strategy", [STRATEGY_ORIGINAL_FIRST, STRATEGY_LANGUAGE_FIRST, STRATEGY_ORIGINAL_ONLY]
)
def test_real_ytdlp_accepts_the_generated_format_string(strategy: str) -> None:
    """让**真的** yt-dlp 解析我们产出的格式串。

    上面那些断言全是字符串形状 —— 形状对、语法错的过滤器能整套通过，然后在用户机器上
    以 `SyntaxError: Invalid filter specification` 炸掉。实际发生过：
    `[language_preference>=10?]` 满足当时的 `endswith("?]")` 断言，真实 yt-dlp 直接拒收。

    `--simulate --skip-download` + 一个不存在的 URL：格式串的解析发生在取 URL **之前**，
    所以拿不到网络也能测到语法。判定只看有没有 `Invalid filter specification`，
    网络类报错一概不算失败。
    """
    exe = _ytdlp_binary()
    if not exe:
        if os.environ.get("FLUENTYTDL_REQUIRE_BUILD_TESTS"):
            pytest.fail("Required yt-dlp test executable was not prepared")
        pytest.skip("yt-dlp 未准备，跳过真实语法校验")

    fmt = _plan(["ja", "zh-Hans"], strategy).fmt
    proc = subprocess.run(  # noqa: S603
        [exe, "--simulate", "--no-warnings", "-f", fmt, "https://example.invalid/watch?v=x"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "Invalid filter specification" not in combined, combined[:600]
