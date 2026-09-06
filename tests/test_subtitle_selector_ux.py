"""字幕选择器的排序与默认勾选。

报告 `FluentYTDL-字幕下载问题排查报告.md` 里那条 BBC 视频，用户点开选择器看到的是
**一个都没勾**：老代码的预勾条件是"排序后第一行 + 代码以 `zh` 开头 + 人工字幕"，
而该视频排完序首行是 `en-GB` 人工字幕，条件恒假。排序本身也是坏的 ——
`priority.index(t.lang_code)` 拿精确代码比对硬编码表，`en-GB` / `zh-Hans-en-GB`
一条都 index 不到，整批落进兜底档，那张表对真实数据整体失效。

需要 QApplication（构造的是真实 QFrame），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subsel-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.core.config_manager import config_manager  # noqa: E402
from fluentytdl.processing.subtitle_manager import SubtitleSourceType  # noqa: E402
from fluentytdl.ui.components.platforms.subtitle import SubtitleSelectorWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _sub_entry(ext: str = "vtt", name: str = "") -> list[dict]:
    return [{"ext": ext, "url": f"https://example.invalid/{ext}", "name": name}]


# 报告实测到的 payload 形状（`--list-subs` 的真实键）
BBC_INFO = {
    "id": "MSJMJxd1udk",
    "language": "en-GB",
    "subtitles": {"en-GB": _sub_entry(name="English (United Kingdom)")},
    "automatic_captions": {
        "cy-en-GB": _sub_entry(name="Welsh from English (United Kingdom)"),
        "en-en-GB": _sub_entry(name="English (United Kingdom) from English (United Kingdom)"),
        "ja-en-GB": _sub_entry(name="Japanese from English (United Kingdom)"),
        "zh-Hans-en-GB": _sub_entry(name="Chinese (Simplified) from English (United Kingdom)"),
    },
}


@pytest.fixture
def selector(qapp, request):
    """按 `request.param` 里的偏好建一个选择器；用完恢复原配置。"""
    prefs = getattr(request, "param", ["zh-Hans", "en"])
    original = config_manager.get("subtitle_default_languages")
    config_manager.set("subtitle_default_languages", prefs)
    try:
        widget = SubtitleSelectorWidget(BBC_INFO)
        yield widget
        widget.deleteLater()
    finally:
        config_manager.set("subtitle_default_languages", original)


def _codes(widget: SubtitleSelectorWidget) -> list[str]:
    return [t.lang_code for t in widget._tracks]


def _checked_codes(widget: SubtitleSelectorWidget) -> list[str]:
    return [cb.property("track_data").lang_code for cb in widget.checkboxes if cb.isChecked()]


# ── 排序 ──────────────────────────────────────────────────────


def test_sorts_by_user_prefs_not_hardcoded_table(selector) -> None:
    """命中偏好的真实键排在最前，未命中的沉到后面。"""
    codes = _codes(selector)
    assert codes[0] == "zh-Hans-en-GB"
    assert codes[1] == "en-GB"
    # 未命中偏好的两条一定在命中的之后
    assert codes.index("cy-en-GB") > codes.index("en-en-GB")
    assert codes.index("ja-en-GB") > codes.index("en-en-GB")


@pytest.mark.parametrize("selector", [["en", "zh-Hans"]], indirect=True)
def test_sort_follows_pref_order(selector) -> None:
    """偏好顺序变了，表头也要跟着变 —— 否则用户改设置看不到任何反馈。"""
    codes = _codes(selector)
    assert codes[0] == "en-GB"
    assert codes.index("en-en-GB") < codes.index("zh-Hans-en-GB")


def test_manual_wins_within_same_pref(selector) -> None:
    """同一偏好内部仍是人工优先：`en` 命中的两条里 `en-GB`（人工）在前。"""
    codes = _codes(selector)
    assert codes.index("en-GB") < codes.index("en-en-GB")
    by_code = {t.lang_code: t for t in selector._tracks}
    assert by_code["en-GB"].source_type == SubtitleSourceType.MANUAL


@pytest.mark.parametrize("selector", [[]], indirect=True)
def test_empty_prefs_falls_back_to_quality_order(selector) -> None:
    """一条偏好都没配时不能崩，退化成人工 > 自动生成 > 自动翻译。"""
    ranks = [t.quality_rank for t in selector._tracks]
    assert ranks == sorted(ranks)
    assert _codes(selector)[0] == "en-GB"


# ── 默认勾选 ──────────────────────────────────────────────────


def test_preselects_one_row_per_pref(selector) -> None:
    """报告原案：以前一个都不勾，现在每个偏好各勾一条真实键。"""
    assert _checked_codes(selector) == ["zh-Hans-en-GB", "en-GB"]


def test_preselect_count_never_exceeds_pref_count(selector) -> None:
    """勾上的总数不得超过偏好数。

    这与 `bcp47.resolve_requested(per_pref_limit=1)` 是同一条硬约束：
    `container_compat.ensure_subtitle_compatible_container()` 按
    `len(subtitleslangs) > 1` 把 mp4 升成 mkv，多勾一条就可能凭空触发容器升级。
    """
    prefs = selector._preferred_languages()
    assert 0 < len(_checked_codes(selector)) <= len(prefs)


@pytest.mark.parametrize("selector", [["ja"]], indirect=True)
def test_preselects_auto_translated_when_thats_all_there_is(selector) -> None:
    """偏好只有自动翻译轨可选时也要勾上 —— 老逻辑只认 MANUAL，会一个不勾。"""
    assert _checked_codes(selector) == ["ja-en-GB"]


@pytest.mark.parametrize("selector", [["ko", "th"]], indirect=True)
def test_preselects_nothing_when_no_pref_matches(selector) -> None:
    """一条偏好都没命中时不许乱勾 —— 那会下到用户没要过的语言。"""
    assert _checked_codes(selector) == []
    assert len(selector.checkboxes) == len(BBC_INFO["automatic_captions"]) + 1


@pytest.mark.parametrize("selector", [["zh-Hans", "zh"]], indirect=True)
def test_two_prefs_hitting_one_track_check_it_once(selector) -> None:
    """`zh-Hans` 和 `zh` 都能命中 `zh-Hans-en-GB`，但只有一行、只勾一次。"""
    assert _checked_codes(selector) == ["zh-Hans-en-GB"]


# ── 交给下游的东西 ────────────────────────────────────────────


def test_selected_codes_are_real_keys(selector) -> None:
    """选择器交出去的必须是**真实字幕键**，不是用户偏好。

    这是整条修复链的终点：`en-GB` 才是 `--sub-langs` 能命中的东西，裸 `en` 不是。
    """
    languages, has_manual, has_auto = selector.get_selected_language_codes()
    assert languages == ["zh-Hans-en-GB", "en-GB"]
    assert has_manual is True
    assert has_auto is True


def test_opts_carry_write_flags_matching_selection(selector) -> None:
    opts = selector.get_opts()
    assert set(opts["subtitleslangs"]) == {"zh-Hans-en-GB", "en-GB"}
    assert opts["writesubtitles"] is True
    assert opts["writeautomaticsub"] is True


def test_row_label_shows_both_name_and_real_code(selector) -> None:
    """标签同时给出可读名与真实代码 —— 用户报 issue 时需要那串代码。"""
    from qfluentwidgets import BodyLabel

    texts = [
        w.text()
        for w in selector.scroll_content.findChildren(BodyLabel)
        if "(" in w.text() and ")" in w.text()
    ]
    assert "中文(简体)（由 en-GB 自动翻译） (zh-Hans-en-GB)" in texts
    assert "英语 (en-GB)" in texts


def test_set_initial_state_restores_previous_choice(selector) -> None:
    """恢复历史选择用精确真实键比对，覆盖默认预勾。"""
    selector.set_initial_state(["en-en-GB"])
    assert _checked_codes(selector) == ["en-en-GB"]


def test_no_subtitles_shows_placeholder(qapp) -> None:
    """无字幕时列表让位给提示语。

    用 `isHidden()` 而不是 `isVisible()`：widget 从未 show()，`isVisible()` 对两者
    都是 False，断不出任何东西。`isHidden()` 问的是"有没有被显式 hide 过"。
    """
    widget = SubtitleSelectorWidget({"id": "x", "subtitles": {}, "automatic_captions": {}})
    try:
        assert widget._tracks == []
        assert widget.scroll_area.isHidden()
        assert not widget.noSubtitleLabel.isHidden()
    finally:
        widget.deleteLater()
