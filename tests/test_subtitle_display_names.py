"""字幕轨道的显示名与来源类型分类。

报告 `FluentYTDL-字幕下载问题排查报告.md` 里那条 BBC 视频，选择器表格长这样：

| 真实键 | 以前显示 | 现在显示 |
| --- | --- | --- |
| `en-GB` | `English (United Kingdom)` | `英语` |
| `en-en-GB` | `English (United Kingdom) from English (United Kingdom) [自动生成]` | `英语（由 en-GB 自动生成）` |
| `zh-Hans-en-GB` | `Chinese (Simplified) from English (United Kingdom) [自动翻译]` | `中文(简体)（由 en-GB 自动翻译）` |

病根和 `--sub-langs` 那条是同一个：`LANGUAGE_NAMES` 的键是裸语种码，而真实字幕键
带地区或来源，`LANGUAGE_NAMES.get(lang_code)` **必然 miss**，于是同一张表里中英文
混排、来源语种还被重复念两遍。

不需要 QApplication：`QCoreApplication.translate()` 在没有实例时原样返回源字符串，
而测试环境不装任何 QTranslator，所以断言里写的就是源文案。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subname-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.processing.subtitle_manager import (  # noqa: E402
    SubtitleSourceType,
    SubtitleTrack,
    extract_subtitle_tracks,
    get_subtitle_languages,
)


def _sub_entry(ext: str = "vtt", name: str = "") -> list[dict]:
    return [{"ext": ext, "url": f"https://example.invalid/{ext}", "name": name}]


# 报告实测到的 payload 形状（`--list-subs` 的真实键）
BBC_INFO = {
    "id": "MSJMJxd1udk",
    "language": "en-GB",
    "subtitles": {"en-GB": _sub_entry(name="English (United Kingdom)")},
    "automatic_captions": {
        "en-en-GB": _sub_entry(name="English (United Kingdom) from English (United Kingdom)"),
        "zh-Hans-en-GB": _sub_entry(name="Chinese (Simplified) from English (United Kingdom)"),
        "cy-en-GB": _sub_entry(name="Welsh from English (United Kingdom)"),
    },
}


def _tracks_by_code(info: dict) -> dict[str, SubtitleTrack]:
    return {t.lang_code: t for t in extract_subtitle_tracks(info)}


# ── display_name ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # 人工字幕：地区段不是来源，不许拼后缀
        ("en-GB", "英语"),
        # 自动生成/翻译：来源语种显式写出来。自动翻译的质量完全取决于源轨道，
        # `中文(简体)（由 en-GB 自动翻译）` 比笼统的 `[自动翻译]` 有用得多 ——
        # 用户能据此判断"要不要干脆直接下英文人工字幕"
        ("en-en-GB", "英语（由 en-GB 自动生成）"),
        ("zh-Hans-en-GB", "中文(简体)（由 en-GB 自动翻译）"),
    ],
)
def test_display_name_on_real_youtube_keys(code: str, expected: str) -> None:
    assert _tracks_by_code(BBC_INFO)[code].display_name == expected


def test_display_name_does_not_repeat_source_for_table_misses() -> None:
    """`LANGUAGE_NAMES` 表外的语种，来源只能出现一次。

    yt-dlp 给复合键的 `name` 是 YouTube 拼好的 `Welsh from English (United Kingdom)`,
    来源已经在里面了；不切掉就会变成
    `Welsh from English (United Kingdom)（由 en-GB 自动翻译）`，同一件事说两遍。
    """
    name = _tracks_by_code(BBC_INFO)["cy-en-GB"].display_name
    assert name == "Welsh（由 en-GB 自动翻译）"
    assert name.count("en-GB") == 1
    assert " from " not in name


def test_display_name_keeps_from_when_not_a_compound_key() -> None:
    """只有真的拆出了来源才动 `name` —— 否则不该擅自截断 yt-dlp 给的文本。"""
    track = SubtitleTrack(
        lang_code="cy",
        lang_name="Welsh from somewhere",
        source_type=SubtitleSourceType.MANUAL,
        ext="vtt",
    )
    assert track.display_name == "Welsh from somewhere"


def test_display_name_looks_up_table_by_truncation() -> None:
    """`en-GB` 查不到就退一级查 `en`；大小写不参与判定。"""
    for code in ("en-GB", "en-gb", "EN-GB", "en-US"):
        track = SubtitleTrack(
            lang_code=code,
            lang_name="ignored",
            source_type=SubtitleSourceType.MANUAL,
            ext="vtt",
        )
        assert track.display_name == "英语", code


def test_display_name_falls_back_to_code_when_nothing_known() -> None:
    """既不在表里、yt-dlp 也没给 name —— 至少不能是空字符串。"""
    track = SubtitleTrack(
        lang_code="xyz",
        lang_name="",
        source_type=SubtitleSourceType.MANUAL,
        ext="vtt",
    )
    assert track.display_name == "xyz"


def test_display_name_keeps_bracket_wording_for_simple_auto_keys() -> None:
    """拆不出来源的自动字幕（裸 `en`）保持原有的 `[自动生成]` 措辞。"""
    gen = SubtitleTrack("en", "", SubtitleSourceType.AUTO_GENERATED, "vtt")
    trans = SubtitleTrack("en", "", SubtitleSourceType.AUTO_TRANSLATED, "vtt")
    assert gen.display_name == "英语 [自动生成]"
    assert trans.display_name == "英语 [自动翻译]"


# ── 来源类型分类（_is_asr_track） ──────────────────────────────


def test_asr_classification_on_real_youtube_keys() -> None:
    """复合键自己就带着答案：目标 == 来源即原声转写，否则是机翻。"""
    tracks = _tracks_by_code(BBC_INFO)
    assert tracks["en-GB"].source_type == SubtitleSourceType.MANUAL
    assert tracks["en-en-GB"].source_type == SubtitleSourceType.AUTO_GENERATED
    assert tracks["zh-Hans-en-GB"].source_type == SubtitleSourceType.AUTO_TRANSLATED
    assert tracks["cy-en-GB"].source_type == SubtitleSourceType.AUTO_TRANSLATED


def test_asr_classification_does_not_merge_hans_and_hant() -> None:
    """简中原声的视频里，繁中机翻轨**不能**被标成"自动生成"。

    这是旧 `_lang_matches()`（按 `-` 切开只比首段）的必然错误：它眼里 `zh-Hans`
    和 `zh-Hant` 是同一种语言，于是原声一旦是中文，每条 `zh-*-zh-Hans` 机翻轨
    都会被误标成原声转写 —— 用户以为拿到的是人念的，实际是机器转译的。
    """
    info = {
        "language": "zh-Hans",
        "subtitles": {},
        "automatic_captions": {
            "zh-Hans-zh-Hans": _sub_entry(),  # 原声转写
            "zh-Hant-zh-Hans": _sub_entry(),  # 简 → 繁 机翻
            "en-zh-Hans": _sub_entry(),  # 简 → 英 机翻
        },
    }
    tracks = _tracks_by_code(info)
    assert tracks["zh-Hans-zh-Hans"].source_type == SubtitleSourceType.AUTO_GENERATED
    assert tracks["zh-Hant-zh-Hans"].source_type == SubtitleSourceType.AUTO_TRANSLATED
    assert tracks["en-zh-Hans"].source_type == SubtitleSourceType.AUTO_TRANSLATED


def test_asr_classification_honours_orig_marker() -> None:
    """yt-dlp 的 `-orig` 后缀是最直接的原声标记。"""
    info = {
        "subtitles": {},
        "automatic_captions": {"en-orig": _sub_entry(), "ja-en": _sub_entry()},
    }
    tracks = _tracks_by_code(info)
    assert tracks["en-orig"].source_type == SubtitleSourceType.AUTO_GENERATED
    assert tracks["ja-en"].source_type == SubtitleSourceType.AUTO_TRANSLATED


def test_asr_classification_uses_original_lang_for_simple_keys() -> None:
    """非复合键（裸 `en`、`zh-Hans`）才需要拿视频原声语种来判。"""
    info = {
        "language": "en",
        "subtitles": {},
        "automatic_captions": {"en": _sub_entry(), "ja": _sub_entry()},
    }
    tracks = _tracks_by_code(info)
    assert tracks["en"].source_type == SubtitleSourceType.AUTO_GENERATED
    assert tracks["ja"].source_type == SubtitleSourceType.AUTO_TRANSLATED


def test_asr_classification_reads_yt_dlp_name_hint() -> None:
    """键上什么都看不出来时，退到 yt-dlp 自己的 `name` 里找 asr 标记。"""
    info = {
        "subtitles": {},
        "automatic_captions": {"fi": _sub_entry(name="Finnish (auto-generated)")},
    }
    assert _tracks_by_code(info)["fi"].source_type == SubtitleSourceType.AUTO_GENERATED


# ── get_subtitle_languages（排序） ────────────────────────────


def test_get_subtitle_languages_orders_by_prefs() -> None:
    """按偏好命中度排，而**不是**精确代码比对。

    老写法 `priority.index("en-GB")` 抛 ValueError，真实键会整批落进兜底档，
    排出来的顺序和偏好毫无关系。
    """
    codes = [item["code"] for item in get_subtitle_languages(BBC_INFO, ["zh-Hans", "en"])]
    assert codes[:3] == ["zh-Hans-en-GB", "en-GB", "en-en-GB"]
    assert codes[-1] == "cy-en-GB"  # 未命中偏好，排在最后


def test_get_subtitle_languages_respects_pref_order() -> None:
    """把 `en` 提到前面，英文轨就该浮到表头。"""
    codes = [item["code"] for item in get_subtitle_languages(BBC_INFO, ["en", "zh-Hans"])]
    assert codes[0] == "en-GB"
    assert codes.index("en-en-GB") < codes.index("zh-Hans-en-GB")


def test_get_subtitle_languages_defaults_are_deterministic() -> None:
    """不传偏好时走内置回落表，顺序仍然稳定（UI 首次打开就靠这条）。"""
    codes = [item["code"] for item in get_subtitle_languages(BBC_INFO)]
    assert codes == ["zh-Hans-en-GB", "en-GB", "en-en-GB", "cy-en-GB"]


def test_get_subtitle_languages_carries_display_name() -> None:
    """UI 直接拿 `name` 渲染，不能回退成裸代码。"""
    by_code = {item["code"]: item for item in get_subtitle_languages(BBC_INFO, ["en"])}
    assert by_code["en-GB"]["name"] == "英语"
    assert by_code["en-GB"]["auto"] is False
    assert by_code["zh-Hans-en-GB"]["auto"] is True


def test_get_subtitle_languages_prefers_manual_on_duplicate_codes() -> None:
    """同一个键同时出现在人工与自动里时，保留人工那条。"""
    info = {
        "language": "en",
        "subtitles": {"en": _sub_entry(name="English")},
        "automatic_captions": {"en": _sub_entry(name="English (auto-generated)")},
    }
    items = get_subtitle_languages(info, ["en"])
    assert len(items) == 1
    assert items[0]["auto"] is False


def test_get_subtitle_languages_handles_empty_info() -> None:
    assert get_subtitle_languages({}, ["en"]) == []
