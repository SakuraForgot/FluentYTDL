"""BCP-47 字幕语言匹配矩阵。

这套断言直接来自用户反馈报告 `FluentYTDL-字幕下载问题排查报告.md`：3.6.9 下载
BBC 6 Minute English 时把 `--sub-langs zh-Hans,en` 原样交给 yt-dlp，而该视频的真实
字幕键是人工 `en-GB`、自动翻译 `zh-Hans-en-GB` / `en-en-GB`。
`--sub-langs` 的每一项被当成**锚定正则**，裸 `en` 匹配不到 `en-GB`，于是一个 `.vtt`
都没写出来，日志只留下一句「未找到字幕文件」。

在这次修复之前，`processing/` 下一个字幕测试都没有。

纯函数，不导入 Qt、不碰网络。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.utils import bcp47  # noqa: E402

# 报告里实测到的真实字幕键（按 quality_rank 排序：人工 → 自动生成 → 自动翻译）
BBC_AVAILABLE = ["en-GB", "en-en-GB", "zh-Hans-en-GB"]


@pytest.mark.parametrize(
    ("pref", "lang"),
    [
        # 报告里的三条核心用例
        ("en", "en-GB"),
        ("en", "en-en-GB"),
        ("zh-Hans", "zh-Hans-en-GB"),
        # 大小写不敏感
        ("EN", "en-gb"),
        ("zh-hans", "zh-Hans-en-GB"),
        # 完全相等
        ("en-GB", "en-GB"),
        # 别名表
        ("zh-Hans", "zh-CN"),
        ("zh-Hans", "zh-SG"),
        ("zh-Hant", "zh-TW"),
        ("en", "en-US"),
    ],
)
def test_matches_positive(pref: str, lang: str) -> None:
    assert bcp47.matches(pref, lang)


@pytest.mark.parametrize(
    ("pref", "lang"),
    [
        # 简繁绝不能混：这正是"按 `-` 切开比首段"那种双向前缀比较会犯的错
        ("zh-Hans", "zh-Hant"),
        ("zh-Hant", "zh-Hans"),
        ("zh-Hans", "zh-Hant-en-GB"),
        # 三字母 ISO-639-2 是**另一个语种**，不是 en 的变体。
        # 这条钉住 to_sub_langs_pattern 不能写成 `en.*`
        ("en", "eng"),
        ("en", "enm"),
        # 完全不相干
        ("ja", "en-GB"),
        ("en", "zh-Hans-en-GB"),
        # 空值
        ("", "en"),
        ("en", ""),
    ],
)
def test_matches_negative(pref: str, lang: str) -> None:
    assert not bcp47.matches(pref, lang)


def test_matches_is_one_way() -> None:
    """`en` 命中 `en-GB`，但 `en-GB` 不命中 `en`。

    单向是刻意的：用户点名要英国英语时，不该拿到一条不知道是哪个地区的 `en`。
    """
    assert bcp47.matches("en", "en-GB")
    assert not bcp47.matches("en-GB", "en")


def test_match_tier_orders_by_closeness() -> None:
    """紧密度决定多候选时挑谁：相等 < 前缀 < 别名。"""
    assert bcp47.match_tier("en", "en") == bcp47.TIER_EXACT
    assert bcp47.match_tier("en", "en-GB") == bcp47.TIER_PREFIX
    assert bcp47.match_tier("zh-Hans", "zh-CN") == bcp47.TIER_ALIAS
    assert bcp47.match_tier("en", "eng") is None


# ── to_sub_langs_pattern ──────────────────────────────────────


def test_pattern_matches_variants_but_not_other_languages() -> None:
    """回落正则要覆盖地区/翻译变体，且**不能**误伤三字母语种码。"""
    import re

    pattern = bcp47.to_sub_langs_pattern("en")
    assert pattern == "en(-.+)?"

    # yt-dlp 用锚定匹配（re.match + 末尾锚定），这里照同样方式验证
    def anchored(p: str, s: str) -> bool:
        return re.fullmatch(p, s) is not None

    assert anchored(pattern, "en")
    assert anchored(pattern, "en-GB")
    assert anchored(pattern, "en-en-GB")
    assert not anchored(pattern, "eng")
    assert not anchored(pattern, "enm")
    assert not anchored(pattern, "zh-Hans-en-GB")


def test_pattern_keeps_well_formed_tags_readable() -> None:
    """合法 BCP-47 tag 不做正则转义 —— 否则日志里会出现 `zh\\-Hans(-.+)?` 这种噪声。"""
    assert bcp47.to_sub_langs_pattern("zh-Hans") == "zh-Hans(-.+)?"
    assert bcp47.to_sub_langs_pattern(" EN ") == "en(-.+)?"


def test_pattern_uses_canonical_case_not_lowercase() -> None:
    """回落正则必须用规范大小写，**不能**图省事全转小写。

    无法确认 yt-dlp 编译 `--sub-langs` 的正则时有没有加 `re.IGNORECASE`
    （本机没有 yt-dlp 源码可查）。所以这里不去赌：规范大小写恰好就是 YouTube
    字幕键的写法，无论 yt-dlp 区不区分大小写都能命中。全小写的
    `zh-hans(-.+)?` 一旦遇上区分大小写的匹配，就配不上真实键 `zh-Hans-en-GB`,
    整条回落路径直接失效。
    """
    import re

    for written_by_user in ("zh-hans", "ZH-HANS", "zh-Hans"):
        pattern = bcp47.to_sub_langs_pattern(written_by_user)
        assert pattern == "zh-Hans(-.+)?"
        # 区分大小写地匹配真实键也必须成立
        assert re.fullmatch(pattern, "zh-Hans-en-GB") is not None

    assert re.fullmatch(bcp47.to_sub_langs_pattern("en-gb"), "en-GB") is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("zh-hans", "zh-Hans"),
        ("ZH-HANT", "zh-Hant"),
        ("en-gb", "en-GB"),
        ("pt-br", "pt-BR"),
        ("zh-hant-hk", "zh-Hant-HK"),
        ("es-419", "es-419"),
        (" en ", "en"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    """语种小写、文字首字母大写、地区大写 —— 与 YouTube 字幕键的写法一致。"""
    assert bcp47.canonicalize(raw) == expected


@pytest.mark.parametrize("raw", ["zh-Hans-en-GB", "en-en-GB", "", "en--gb", "a-b-c-d"])
def test_canonicalize_leaves_non_bcp47_alone(raw: str) -> None:
    """YouTube 的复合键（`目标-来源`）不是合法 BCP-47，原样返回。

    `zh-Hans-en-GB` 如果按 BCP-47 位置规则处理，`en` 会被当成地区段大写成
    `zh-Hans-EN-GB` —— 那就把真实键改坏了。这类键只会从 yt-dlp 读回来，
    不需要我们规范化。
    """
    assert bcp47.canonicalize(raw) == raw


def test_pattern_escapes_untrusted_input() -> None:
    """偏好来自配置文件，可能是手写的垃圾；不能让它变成能爆炸的正则。"""
    import re

    pattern = bcp47.to_sub_langs_pattern("en(")
    re.compile(pattern)  # 不抛异常即可


# ── resolve_requested ────────────────────────────────────────


def test_resolve_reproduces_report_case() -> None:
    """报告原案：`["zh-Hans", "en"]` 必须解析成真实键，而不是原样传出去。"""
    matched, missed = bcp47.resolve_requested(["zh-Hans", "en"], BBC_AVAILABLE)
    assert matched == ["zh-Hans-en-GB", "en-GB"]
    assert missed == []


def test_resolve_respects_pref_order() -> None:
    matched, _ = bcp47.resolve_requested(["en", "zh-Hans"], BBC_AVAILABLE)
    assert matched == ["en-GB", "zh-Hans-en-GB"]


def test_resolve_prefers_exact_over_prefix() -> None:
    """同时存在 `en` 和 `en-GB` 时，偏好 `en` 应拿到 `en`。"""
    matched, _ = bcp47.resolve_requested(["en"], ["en-GB", "en-en-GB", "en"])
    assert matched == ["en"]


def test_resolve_uses_caller_order_to_break_ties() -> None:
    """同紧密度时按调用方给的顺序决胜 —— 调用方按 quality_rank 排好序，
    于是人工 `en-GB` 胜过自动翻译 `en-en-GB`。"""
    manual_first, _ = bcp47.resolve_requested(["en"], ["en-GB", "en-en-GB"])
    auto_first, _ = bcp47.resolve_requested(["en"], ["en-en-GB", "en-GB"])
    assert manual_first == ["en-GB"]
    assert auto_first == ["en-en-GB"]


def test_resolve_per_pref_limit_is_one_by_default() -> None:
    """默认每个偏好只取一条。

    这是硬约束：`container_compat.ensure_subtitle_compatible_container()` 按
    `len(subtitleslangs) > 1` 把 mp4 升成 mkv，一个 `en` 偏好展开成三条就会
    凭空触发容器升级。
    """
    matched, _ = bcp47.resolve_requested(["en"], ["en-GB", "en-US", "en-en-GB"])
    assert matched == ["en-GB"]

    more, _ = bcp47.resolve_requested(["en"], ["en-GB", "en-US", "en-en-GB"], per_pref_limit=2)
    assert more == ["en-GB", "en-US"]


def test_resolve_never_reuses_one_track_for_two_prefs() -> None:
    """`zh` 和 `zh-Hans` 都能命中 `zh-Hans-en-GB`，但不能重复请求同一条键。"""
    matched, missed = bcp47.resolve_requested(["zh-Hans", "zh"], ["zh-Hans-en-GB"])
    assert matched == ["zh-Hans-en-GB"]
    assert missed == ["zh"]


def test_resolve_reports_missed_prefs() -> None:
    matched, missed = bcp47.resolve_requested(["ja", "ko"], BBC_AVAILABLE)
    assert matched == []
    assert missed == ["ja", "ko"]


def test_resolve_partial_hit() -> None:
    matched, missed = bcp47.resolve_requested(["en", "ja"], BBC_AVAILABLE)
    assert matched == ["en-GB"]
    assert missed == ["ja"]


def test_resolve_empty_inputs() -> None:
    assert bcp47.resolve_requested([], BBC_AVAILABLE) == ([], [])
    assert bcp47.resolve_requested(["en"], []) == ([], ["en"])


# ── 别名表本身（音频侧的格式过滤器直接读它） ────────────────────


def test_alias_table_covers_the_simplified_chinese_spellings() -> None:
    """`yt_dlp_cli._language_filters()` 直接遍历 `BCP47_ALIASES` 生成过滤器分支。

    这里原先测的是 `expand_for_sort("zh-Hans")` 展开成的 `lang:` 条目 —— 那个函数
    已经删了（`-S lang:xx` 从来不生效，见 `docs/YTDLP_KNOWLEDGE.md` §2.1），但**别名表
    本身仍是有效的**，只是现在展开成 `[language=zh-CN]` 这样的过滤器。表的内容照旧要锁。
    """
    assert bcp47.BCP47_ALIASES[bcp47.normalize("zh-Hans")] == {
        "zh-cn",
        "zh-sg",
        "zh-simplified",
        "zh",
    }
    # 简繁绝不能互相出现在对方的别名里
    assert "zh-tw" not in bcp47.BCP47_ALIASES[bcp47.normalize("zh-Hans")]
    assert "zh-cn" not in bcp47.BCP47_ALIASES[bcp47.normalize("zh-Hant")]


# ── split_translated_key（复合键拆解） ──────────────────────────


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # 报告实测到的三个真实键
        ("en-GB", ("en-GB", None)),
        ("en-en-GB", ("en", "en-GB")),
        ("zh-Hans-en-GB", ("zh-Hans", "en-GB")),
        # 人工字幕键**绝不能**被拆开：`GB` 是地区，不是来源语种。
        # 形状和 `en-en` 一模一样，只有大小写能区分 —— 这是整个函数的关键前提
        ("pt-BR", ("pt-BR", None)),
        ("en-US", ("en-US", None)),
        ("es-419", ("es-419", None)),  # 数字地区码
        ("zh-Hans", ("zh-Hans", None)),  # 4 字母文字段
        ("en", ("en", None)),
        # yt-dlp 的原声标记不是复合键
        ("en-orig", ("en-orig", None)),
        # 简中原声 → 繁中机翻
        ("zh-Hant-zh-Hans", ("zh-Hant", "zh-Hans")),
        # 边界：拆不开就原样返回，调用方永远拿到能直接展示的东西
        ("", ("", None)),
        ("-", ("-", None)),
        ("x", ("x", None)),
    ],
)
def test_split_translated_key(key: str, expected: tuple[str, str | None]) -> None:
    assert bcp47.split_translated_key(key) == expected


# ── preference_rank（UI 排序与预勾选） ─────────────────────────


def test_preference_rank_orders_by_user_prefs() -> None:
    """命中越靠前的偏好、匹配越贴合，排得越前。"""
    prefs = ["zh-Hans", "en"]

    assert bcp47.preference_rank(prefs, "zh-Hans-en-GB") == (0, bcp47.TIER_PREFIX)
    assert bcp47.preference_rank(prefs, "zh-Hans") == (0, bcp47.TIER_EXACT)
    assert bcp47.preference_rank(prefs, "en-GB") == (1, bcp47.TIER_PREFIX)


def test_preference_rank_puts_misses_last() -> None:
    """未命中的下标是 `len(prefs)`，必然排在所有命中项之后。

    这是取代 `priority.index(code)` + `ValueError` 兜底的那半边：老写法对
    `en-GB` 这类真实键**全部**抛 ValueError，于是"命中"和"没命中"挤在同一档。
    """
    prefs = ["zh-Hans", "en"]

    assert bcp47.preference_rank(prefs, "ja") == (2, bcp47.TIER_EXACT)
    assert bcp47.preference_rank([], "en-GB") == (0, bcp47.TIER_EXACT)

    ordered = sorted(
        ["ja", "en-GB", "zh-Hans-en-GB"], key=lambda c: bcp47.preference_rank(prefs, c)
    )
    assert ordered == ["zh-Hans-en-GB", "en-GB", "ja"]


def test_preference_rank_hit_test_matches_matches() -> None:
    """`rank[0] < len(prefs)` 必须与 `matches()` 给出同样的答案。

    UI 侧靠这个等价关系判断"要不要默认勾上"，两套判定分叉就会出现
    "排在最前面但没勾"的诡异状态。
    """
    prefs = ["zh-Hans", "en"]
    for lang in [*BBC_AVAILABLE, "ja", "cy-en-GB", "zh-Hant"]:
        hit = bcp47.preference_rank(prefs, lang)[0] < len(prefs)
        assert hit == any(bcp47.matches(p, lang) for p in prefs), lang
