"""音轨类型判定与三种选择策略（Phase A 的回归锁）。

这套测试盯的是三个已核实的实现缺陷，每一条都对应一个用户能看见的症状：

1. **`audio_track_type` 在 yt-dlp 里根本不存在。** 项目曾在 6 处读它，永远是 `None`，
   于是音轨弹窗的"类型"列恒显示"配音"、原音识别退化成靠 `format_note` 猜子串。
   替代品是 `language_preference` —— yt-dlp 的 YouTube extractor 已经算好、`-J` 输出里
   已经有、项目却从未读过的字段。
2. **`(default)` 不是原音。** 旧代码 `"default" in format_note` 把 `audioIsDefault`
   （账号/地区默认音轨）判成原音，而上游明确把这两者分开（`ORIGINAL_LANG_VALUE = 10`
   vs `DEFAULT_LANG_VALUE = 5`）。所以下面专门有一条断言钉住"5 不是 original"。
3. **原音以前只是语言列表里的一个条目。** 现在是独立策略，三种策略在同一份数据上
   必须选出不同的轨 —— 否则"策略"就只是个不起作用的下拉框。

不需要 QApplication：这里全是纯函数与 config 迁移，不构造任何 widget。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-audiosel-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.core.config_manager import ConfigManager  # noqa: E402
from fluentytdl.utils.format_scorer import (  # noqa: E402
    STRATEGY_LANGUAGE_FIRST,
    STRATEGY_ORIGINAL_FIRST,
    STRATEGY_ORIGINAL_ONLY,
    ScoringContext,
    audio_track_kind,
    rank_audio_formats,
)


def _audio(fid: str, lang: str, abr: int, **kw) -> dict:
    row = {
        "format_id": fid,
        "ext": "m4a",
        "vcodec": "none",
        "acodec": "mp4a",
        "abr": abr,
        "language": lang,
    }
    row.update(kw)
    return row


def _ctx(strategy: str, langs: list[str], *, allow_descriptive: bool = False) -> ScoringContext:
    """打分上下文。`prefer_ext=None` 是刻意的 —— dataclass 缺省是 `"mp4"`，
    而控件侧真实传进来的 `intent.get("prefer_ext")` 从来是 `None`。"""
    return ScoringContext(
        prefer_ext=None,
        preferred_audio_langs=langs,
        audio_strategy=strategy,
        allow_descriptive=allow_descriptive,
    )


def _winner(rows: list[dict], ctx: ScoringContext) -> str:
    return rank_audio_formats(rows, ctx)[0][0]["format_id"]


# ── audio_track_kind：四个取值 + 缺失回落 ───────────────────


def test_kind_reads_language_preference():
    """10 / 5 / −1 / −10 是 yt-dlp 的四个取值，一一对应四种类型。"""
    assert audio_track_kind(_audio("a", "en", 128, language_preference=10)) == "original"
    assert audio_track_kind(_audio("a", "en", 128, language_preference=5)) == "default"
    assert audio_track_kind(_audio("a", "en", 128, language_preference=-1)) == "dub"
    assert audio_track_kind(_audio("a", "en-desc", 128, language_preference=-10)) == "descriptive"


def test_default_is_not_original():
    """`(default)` 是账号/地区默认，不是原音 —— 缺陷 2 的直接回归锁。

    旧代码 `"default" in format_note` 会把这一条判成原音，于是"我的默认音轨是英语"的
    用户在任何视频上都拿不到真正的原音。
    """
    row = _audio("a", "en", 128, language_preference=5, format_note="English (default)")
    assert audio_track_kind(row) == "default"


def test_kind_falls_back_to_format_note_when_field_missing():
    """非 YouTube extractor（Twitter 等）没有 `language_preference`，只能看 note/lang。"""
    assert audio_track_kind(_audio("a", "en", 128, format_note="English (original)")) == "original"
    assert audio_track_kind(_audio("a", "en", 128, format_note="English (default)")) == "default"
    assert audio_track_kind(_audio("a", "en-desc", 128)) == "descriptive"
    # 什么线索都没有 → unknown，**不是** dub。旧实现在这里默认"配音"，
    # 于是所有 Twitter 音轨都被打上配音标签。
    assert audio_track_kind(_audio("a", "en", 128)) == "unknown"


def test_bool_is_not_treated_as_a_preference_value():
    """`bool` 是 `int` 的子类 —— `True` 不许被当成 1（那会落进 unknown 而不是回落路径）。"""
    row = _audio("a", "en", 128, language_preference=True, format_note="English (original)")
    assert audio_track_kind(row) == "original"


# ── 三种策略在同一份数据上选出不同的轨 ──────────────────────

#: en 原音 + ja 配音 + ja 默认。用户偏好 `["ja"]`：
#:   original_first → en 原音（类型档为主键）
#:   language_first → ja 里最好的那条（语言名次为主键，ja-default 档高于 ja-dub）
#:   original_only  → en 原音（有原音就赢）
MULTI = [
    _audio("en-orig", "en", 128, language_preference=10),
    _audio("ja-dub", "ja", 160, language_preference=-1),
    _audio("ja-default", "ja", 130, language_preference=5),
]


def test_original_first_picks_the_original_over_a_preferred_language():
    assert _winner(MULTI, _ctx(STRATEGY_ORIGINAL_FIRST, ["ja"])) == "en-orig"


def test_language_first_picks_the_preferred_language_over_the_original():
    """语言优先时 ja 里档最高的那条赢 —— 类型档退成次键，但仍在 ja 内部决胜。"""
    assert _winner(MULTI, _ctx(STRATEGY_LANGUAGE_FIRST, ["ja"])) == "ja-default"


def test_original_only_picks_the_original():
    assert _winner(MULTI, _ctx(STRATEGY_ORIGINAL_ONLY, ["ja"])) == "en-orig"


def test_original_only_falls_back_when_there_is_no_original():
    """ "仅原音"在没有原音的视频上必须退化成"语言优先"，不能交出空手。

    硬过滤会让这类视频一条音轨都拿不到（合并直接失败）。所以 original_only 的实现是
    "原音命中即赢"的加分，而不是过滤器。
    """
    rows = [
        _audio("ja-dub", "ja", 160, language_preference=-1),
        _audio("en-dub", "en", 128, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_ORIGINAL_ONLY, ["en"])) == "en-dub"


def test_preference_order_matters_within_language_first():
    """偏好列表的先后必须真的起作用 —— 名次是 `len(prefs) - i`，不是"命中就一样"。"""
    rows = [
        _audio("ja", "ja", 128, language_preference=-1),
        _audio("ko", "ko", 128, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["ko", "ja"])) == "ko"
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["ja", "ko"])) == "ja"


def test_bcp47_alias_matches_a_regional_key():
    """偏好 `zh-Hans` 要能命中真实键 `zh-CN` —— 别名表走的是 `utils/bcp47.py`。"""
    rows = [
        _audio("en", "en", 160, language_preference=-1),
        _audio("zh", "zh-CN", 128, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["zh-Hans"])) == "zh"


# ── 同母语言软档（macro tier）：脚本/地区不同也算命中同一门语言 ──────


def test_language_first_falls_back_to_same_language_other_script():
    """用户的原始现场：中文原音被 YouTube 标成繁体（`zh-Hant`），偏好首项却是简体。

    偏好 `[zh-Hans, en, ko]` 下，`zh-Hant` 原音要靠"同母语言软档"命中中文，
    而不是让"中文"整体落空、沿回退链掉到英文。这是本次修复的核心断言。
    """
    rows = [
        _audio("zh-orig", "zh-Hant", 128, language_preference=10),
        _audio("en-dub", "en", 160, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["zh-Hans", "en", "ko"])) == "zh-orig"


def test_exact_script_still_beats_the_other_script():
    """软档不吞掉简/繁切换：两个脚本都在时，用户点名的那个脚本胜出。

    这是用户的硬约束——"要保留繁中简中的切换"。即便另一脚本码率更高，
    精确脚本（同偏好里的 2n）仍压过软档（2n−1）。
    """
    rows = [
        _audio("hans", "zh-Hans", 128, language_preference=-1),
        _audio("hant", "zh-Hant", 160, language_preference=-1),
        _audio("en", "en", 160, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["zh-Hans", "en"])) == "hans"
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["zh-Hant", "en"])) == "hant"


def test_macro_is_general_across_languages():
    """软档是通用规则，不写死中文：偏好 `pt-BR` 没命中时软命中 `pt-PT`，仍胜过英文。"""
    rows = [
        _audio("pt", "pt-PT", 128, language_preference=-1),
        _audio("en", "en", 160, language_preference=-1),
    ]
    assert _winner(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["pt-BR"])) == "pt"


# ── 描述性音轨：默认排除但不硬过滤 ──────────────────────────


def test_descriptive_ranks_last_by_default():
    """音频描述轨默认排最后，即使它码率最高、语言正好命中。"""
    rows = [
        _audio("desc", "en-desc", 320, language_preference=-10),
        _audio("dub", "en", 96, language_preference=-1),
    ]
    ranked = rank_audio_formats(rows, _ctx(STRATEGY_LANGUAGE_FIRST, ["en"]))
    assert [r["format_id"] for r, _ in ranked] == ["dub", "desc"]


def test_descriptive_is_still_selectable_when_it_is_the_only_track():
    """只有描述轨的视频仍要拿到音频 —— 地板分是"垫底"，不是"淘汰"。"""
    rows = [_audio("desc", "en-desc", 128, language_preference=-10)]
    assert _winner(rows, _ctx(STRATEGY_ORIGINAL_FIRST, ["en"])) == "desc"


def test_allow_descriptive_lifts_it_back_into_normal_ranking():
    """打开开关后，描述轨按普通候选参与排序（这里靠码率赢）。"""
    rows = [
        _audio("desc", "en", 320, language_preference=-10),
        _audio("dub", "en", 96, language_preference=-1),
    ]
    ctx = _ctx(STRATEGY_LANGUAGE_FIRST, ["en"], allow_descriptive=True)
    # 同语言命中、类型档 descriptive(0) < dub(1)，所以 language_first 下 dub 仍在前；
    # 关键是 desc 不再是地板分 —— 两者分差不该是 1e12 那个量级。
    ranked = dict((r["format_id"], s) for r, s in rank_audio_formats(rows, ctx))
    assert ranked["dub"] - ranked["desc"] < 10**9


# ── 配置迁移：orig 条目 → 独立策略 ──────────────────────────


def _migrate(data: dict) -> dict:
    """跑一遍迁移，返回被写入的合并结果。

    直接调 staticmethod，不碰真实 config 文件 —— `_load_config()` 外面套着 except，
    在这里踩到异常会静默退回默认值，测试就什么都测不到了。
    """
    merged = {
        "audio_track_strategy": "original_first",
        "preferred_audio_languages": ["zh-Hans", "en"],
        "audio_strategy_migrated": False,
    }
    merged.update(data)
    ConfigManager._migrate_audio_orig_to_strategy(data, merged)
    return merged


def test_migration_orig_at_head_becomes_original_first():
    """`orig` 在首位 = "先试原音" → 原音优先。"""
    merged = _migrate({"preferred_audio_languages": ["orig", "zh-Hans"]})
    assert merged["audio_track_strategy"] == STRATEGY_ORIGINAL_FIRST
    assert merged["preferred_audio_languages"] == ["zh-Hans"]
    assert merged["audio_strategy_migrated"] is True


def test_migration_orig_in_the_middle_becomes_language_first():
    """`orig` 排在语言后面 = "先按语言，其次原音" → 语言优先。"""
    merged = _migrate({"preferred_audio_languages": ["zh-Hans", "orig", "en"]})
    assert merged["audio_track_strategy"] == STRATEGY_LANGUAGE_FIRST
    assert merged["preferred_audio_languages"] == ["zh-Hans", "en"]


def test_migration_only_orig_falls_back_to_the_default_list():
    """列表里只有 `orig` 时不能留下空列表 —— 空列表等于"没有语言偏好"。"""
    merged = _migrate({"preferred_audio_languages": ["orig"]})
    assert merged["audio_track_strategy"] == STRATEGY_ORIGINAL_FIRST
    assert merged["preferred_audio_languages"] == ["zh-Hans", "en"]


def test_migration_is_not_repeated_once_marked():
    """标记位在场就不再迁移 —— 否则用户手动加回 `orig` 会被反复吞掉。"""
    merged = _migrate(
        {"preferred_audio_languages": ["orig", "en"], "audio_strategy_migrated": True}
    )
    assert merged["preferred_audio_languages"] == ["orig", "en"]


def test_migration_marks_even_when_there_is_nothing_to_do():
    """没有 `orig` 的配置也要落标记位，否则每次启动都白跑一遍。"""
    merged = _migrate({"preferred_audio_languages": ["en"]})
    assert merged["audio_strategy_migrated"] is True
    assert merged["preferred_audio_languages"] == ["en"]
    assert merged["audio_track_strategy"] == STRATEGY_ORIGINAL_FIRST
