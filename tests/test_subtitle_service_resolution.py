"""策略层把用户偏好解析成**真实字幕键**。

这是报告 `FluentYTDL-字幕下载问题排查报告.md` 的核心回归：三个策略以前各写一套
精确字符串比较 —— `t.lang_code == self.language`、`lang in available_dict`、
硬编码的中文阶梯 —— 对真实 YouTube 字幕键**全部失效**，且失配时 `return {}`
让调用方的 `row_opts.update({})` 变成空操作，字幕被无声无息地关掉。

这里钉住两件事：
1. `zh-Hans` / `en` 必须解析成 `zh-Hans-en-GB` / `en-GB`；
2. 一条都没命中时**不能**返回 `{}`，必须带上"为什么"。

用真实 `--list-subs` 形状的 fixture，不碰网络。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subtest-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.models.subtitle_config import (  # noqa: E402
    SUBTITLE_PREFS_KEY,
    SUBTITLE_RESOLUTION_KEY,
    SubtitleConfig,
    SubtitleTypePreference,
)
from fluentytdl.processing.subtitle_manager import (  # noqa: E402
    SubtitleSourceType,
    extract_subtitle_tracks,
)
from fluentytdl.processing.subtitle_service import (  # noqa: E402
    MultiLanguageStrategy,
    SingleLanguageStrategy,
    SmartStrategy,
    SubtitleRequest,
    deduplicate_by_quality,
    resolve_tracks_for_prefs,
)


def _sub_entry(ext: str = "vtt", name: str = "") -> list[dict]:
    return [{"ext": ext, "url": f"https://example.invalid/{ext}", "name": name}]


# 报告作者用内置 yt-dlp `--list-subs` 实测到的形状（BBC 6 Minute English）：
# 人工字幕只有 en-GB；自动生成是 en-en-GB；自动翻译成简中是 zh-Hans-en-GB。
BBC_INFO = {
    "id": "MSJMJxd1udk",
    "language": "en-GB",
    "subtitles": {"en-GB": _sub_entry(name="English (United Kingdom)")},
    "automatic_captions": {
        "en-en-GB": _sub_entry(name="English (United Kingdom) from English (United Kingdom)"),
        "zh-Hans-en-GB": _sub_entry(name="Chinese (Simplified) from English (United Kingdom)"),
    },
}


def _config(**kwargs) -> SubtitleConfig:
    """默认走 ALL，好让自动翻译轨道进入候选（见 test_type_preference_gates_translated）。"""
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("type_preference", SubtitleTypePreference.ALL)
    return SubtitleConfig(**kwargs)


def _request(info: dict, config: SubtitleConfig) -> SubtitleRequest:
    return SubtitleRequest(video_id=info.get("id", "x"), video_info=info, user_config=config)


# ── fixture 自检：轨道类型判定必须如预期 ───────────────────────


def test_fixture_yields_expected_track_types() -> None:
    """后面所有断言都建立在这三条轨道的类型判定上，先把它钉住。"""
    tracks = {t.lang_code: t for t in extract_subtitle_tracks(BBC_INFO)}
    assert tracks["en-GB"].source_type == SubtitleSourceType.MANUAL
    assert tracks["en-en-GB"].source_type == SubtitleSourceType.AUTO_GENERATED
    assert tracks["zh-Hans-en-GB"].source_type == SubtitleSourceType.AUTO_TRANSLATED


# ── resolve_tracks_for_prefs ─────────────────────────────────


def test_resolve_maps_prefs_to_real_codes() -> None:
    """报告原案：`["zh-Hans", "en"]` → `["zh-Hans-en-GB", "en-GB"]`。"""
    tracks = extract_subtitle_tracks(BBC_INFO)
    selected, missed = resolve_tracks_for_prefs(["zh-Hans", "en"], tracks)
    assert [t.lang_code for t in selected] == ["zh-Hans-en-GB", "en-GB"]
    assert missed == []


def test_resolve_prefers_manual_over_auto() -> None:
    """`en` 同时命中人工 `en-GB` 和自动 `en-en-GB`，必须拿人工那条。"""
    tracks = extract_subtitle_tracks(BBC_INFO)
    selected, _ = resolve_tracks_for_prefs(["en"], tracks)
    assert [t.lang_code for t in selected] == ["en-GB"]
    assert selected[0].source_type == SubtitleSourceType.MANUAL


def test_resolve_honours_max_languages() -> None:
    tracks = extract_subtitle_tracks(BBC_INFO)
    selected, _ = resolve_tracks_for_prefs(["zh-Hans", "en"], tracks, max_languages=1)
    assert [t.lang_code for t in selected] == ["zh-Hans-en-GB"]


def test_dedup_keeps_distinct_real_codes_apart() -> None:
    """`en-GB` 与 `en-en-GB` 是两条不同字幕，绝不能被归并成一个 `en` 桶。

    旧实现按 `lang_code.split("-")[0]` 分桶（`zh` 开头例外），于是这两条会互相
    覆盖，而 `zh-Hans-en-GB` 又因为那个 `zh` 特例自成一桶、永远归不到 `zh-Hans`。
    """
    kept = {t.lang_code for t in deduplicate_by_quality(extract_subtitle_tracks(BBC_INFO))}
    assert kept == {"en-GB", "en-en-GB", "zh-Hans-en-GB"}


# ── 三个策略 ─────────────────────────────────────────────────


def test_multi_language_strategy_emits_real_codes() -> None:
    """这条断言就是报告的验收点：**绝不再出现裸 `zh-Hans,en`**。"""
    opts = MultiLanguageStrategy(["zh-Hans", "en"]).apply(_request(BBC_INFO, _config()))

    assert opts["subtitleslangs"] == ["zh-Hans-en-GB", "en-GB"]
    assert opts["subtitleslangs"] != ["zh-Hans", "en"]
    # 选了人工 + 自动翻译，两个开关都得开
    assert opts["writesubtitles"] is True
    assert opts["writeautomaticsub"] is True
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "exact"
    assert opts[SUBTITLE_RESOLUTION_KEY]["matched"] == ["zh-Hans-en-GB", "en-GB"]


def test_single_language_strategy_matches_region_variant() -> None:
    """偏好 `en`、真实键 `en-GB` —— 旧实现在这里 `return {}`。"""
    opts = SingleLanguageStrategy("en").apply(_request(BBC_INFO, _config()))
    assert opts["subtitleslangs"] == ["en-GB"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "exact"


def test_smart_strategy_finds_translated_chinese() -> None:
    """智能策略的硬编码阶梯 `["zh-Hans","zh-Hant","zh","zh-CN","zh-TW"]` 对
    `zh-Hans-en-GB` 全不命中；换成 bcp47 之后中英各拿一条。"""
    opts = SmartStrategy().apply(_request(BBC_INFO, _config()))
    assert opts["subtitleslangs"] == ["zh-Hans-en-GB", "en-GB"]


def test_type_preference_gates_translated_tracks() -> None:
    """默认的 `MANUAL_AND_ASR` 会过滤掉自动翻译轨道 —— 于是 `zh-Hans` 命中不了。

    这不是 bug，是用户设置的直接后果，但必须被记为 `missed` 而不是静默丢弃：
    想要 `zh-Hans-en-GB` 这种自动翻译字幕，`type_preference` 得设成 `ALL`。
    """
    config = _config(type_preference=SubtitleTypePreference.MANUAL_AND_ASR)
    opts = MultiLanguageStrategy(["zh-Hans", "en"]).apply(_request(BBC_INFO, config))

    assert opts["subtitleslangs"] == ["en-GB"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["missed"] == ["zh-Hans"]


# ── 失配不再静默 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "strategy_factory",
    [
        lambda: MultiLanguageStrategy(["ja", "ko"]),
        lambda: SingleLanguageStrategy("ja"),
    ],
)
def test_no_match_never_returns_empty_dict(strategy_factory) -> None:
    """一条都没命中时**必须**给出原因。

    旧实现 `return {}`，调用方 `row_opts.update({})` 是空操作：字幕被无声关掉，
    日志一行没有，用户只看到"未找到字幕文件"。
    """
    opts = strategy_factory().apply(_request(BBC_INFO, _config()))

    assert opts != {}
    # 显式关掉，免得 yt-dlp 按自己的默认去下一堆没人要的语言
    assert opts["writesubtitles"] is False
    assert opts["writeautomaticsub"] is False
    assert opts["embedsubtitles"] is False

    meta = opts[SUBTITLE_RESOLUTION_KEY]
    assert meta["mode"] == "no_match"
    assert meta["missed"]
    assert set(meta["available"]) == {"en-GB", "en-en-GB", "zh-Hans-en-GB"}


def test_flat_info_defers_resolution_instead_of_giving_up() -> None:
    """播放列表未逐行解析时 info 是 flat 的，压根没有 `subtitles` 字段。

    这跟"视频确实没有这些语言"是两件事：必须留下偏好交给 `workers.py` 迟解析，
    **不能**当 no_match 处理，否则播放列表批量下载永远拿不到字幕 —— 那正是报告
    里四条任务字幕全空的路径。
    """
    flat_info = {"id": "MSJMJxd1udk", "title": "...", "_type": "url"}
    opts = MultiLanguageStrategy(["zh-Hans", "en"]).apply(_request(flat_info, _config()))

    assert opts[SUBTITLE_PREFS_KEY] == ["zh-Hans", "en"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "deferred"
    # 意图已声明，但真实键还没定 —— 此时不该有 subtitleslangs
    assert "subtitleslangs" not in opts
