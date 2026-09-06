"""音轨弹窗的语言/编码筛选。

`AudioPickerDialog._on_filter_changed()` 在这次修复之前是 `pass` + 一句 TODO ——
语言 `SegmentedWidget` 和编码 `ComboBox` 都摆在界面上、都能点，点了什么都不发生。
四态类型列上线后表格行数变多（同一语言的原音轨与配音轨现在都留着，见
`extract_audio_tracks()` 的 `(kind, lang, codec)` 去重键），一个不工作的筛选器更显眼。

这里锁住的关键不变量是**下标对齐**：`_checkboxes[i]` 必须始终对应 `_all_tracks[i]`，
因为 `_get_selected_tracks()` 就是靠这个对应关系取值。所以筛选只能隐藏行，
绝不能重建表格 —— 重建一次就把用户已经勾好的选择丢了。

需要 QApplication（构造的是真实 MessageBoxBase），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-picker-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from fluentytdl.core.config_manager import config_manager  # noqa: E402
from fluentytdl.ui.dialogs.audio_picker_dialog import AudioPickerDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def pinned_audio_config():
    """把打分相关的三个配置键钉成已知值。

    `_build_scoring_ctx()` 直接读 `config_manager`，而 `FLUENTYTDL_DATA_DIR_OVERRIDE`
    只隔离**落盘位置**，不隔离内容 —— 开发机上那份 `config.json` 会被读进来。
    实测就撞上过：本机存的是 `language_first` + `['zh-Hans','en','ko']`，
    于是英语配音反而排在日语原音前面，tab 顺序断言随之翻转。
    钉住之后本文件的期望才只取决于 fixture 数据。
    """
    keys = ("audio_track_strategy", "preferred_audio_languages", "audio_allow_descriptive")
    saved = {k: config_manager.get(k) for k in keys}
    config_manager.config["audio_track_strategy"] = "original_first"
    config_manager.config["preferred_audio_languages"] = ["zh-Hans", "en"]
    config_manager.config["audio_allow_descriptive"] = False
    yield
    config_manager.config.update(saved)


def _audio(fid: str, lang: str, pref: int, acodec: str, abr: int) -> dict:
    return {
        "format_id": fid,
        "vcodec": "none",
        "acodec": acodec,
        "language": lang,
        "language_preference": pref,
        "abr": abr,
        "ext": "m4a" if "mp4a" in acodec else "webm",
        "format_note": "medium",
    }


# 日语原音 + 英语配音 + 英语音频描述，两种编码各一份 —— 真实多语言视频的形状
MULTI_LANG = {
    "formats": [
        _audio("140", "ja", 10, "mp4a.40.2", 128),
        _audio("251", "ja", 10, "opus", 160),
        _audio("140-1", "en", -1, "mp4a.40.2", 128),
        _audio("251-1", "en", -1, "opus", 160),
        _audio("251-2", "en", -10, "opus", 160),
    ]
}


@pytest.fixture
def host(qapp):
    """`MaskDialogBase.__init__` 会读 `parent.width()`，parent=None 直接 AttributeError。"""
    w = QWidget()
    w.resize(900, 600)
    yield w


def _dialog(info: dict, host: QWidget, container: str | None = "mp4") -> AudioPickerDialog:
    return AudioPickerDialog(info, container=container, parent=host)


def _visible(d: AudioPickerDialog) -> list[str]:
    return [
        d._all_tracks[i].format_id for i in range(len(d._all_tracks)) if not d.table.isRowHidden(i)
    ]


def _notice(d: AudioPickerDialog) -> str | None:
    """筛选提示的文案，藏着时返回 None。

    读 `isHidden()` 而**不是** `isVisible()`：弹窗从未 `show()` 过，所有子控件的
    `isVisible()` 恒为 False，那样断言就永远测不到显示逻辑。`isHidden()` 只反映
    显式的 `hide()` / `show()` 调用，正是这里要看的东西。
    """
    return None if d._filter_label.isHidden() else d._filter_label.text()


def _select_codec(d: AudioPickerDialog, codec: str | None) -> None:
    """按 userData 选编码项。`None` = 「全部」那一项（第 0 项，没有 userData）。"""
    for i in range(d.codec_combo.count()):
        if d.codec_combo.itemData(i) == codec:
            d.codec_combo.setCurrentIndex(i)
            return
    raise AssertionError(f"编码项 {codec!r} 不在下拉框里")


def test_no_filter_shows_everything(host) -> None:
    d = _dialog(MULTI_LANG, host)
    assert len(d._all_tracks) == 5
    assert len(_visible(d)) == 5
    assert _notice(d) is None


def test_language_tabs_follow_recommendation_order(host) -> None:
    """tab 顺序 = `_all_tracks` 顺序 = 得分降序，所以第一个语言 tab 是赢家的语言。

    用 dict 当有序集合而不是 `set` 就是为了这条 —— `set` 会让 tab 顺序每次运行都变。
    默认策略 `original_first` 下日语原音赢，所以 `ja` 在 `en` 前面。
    """
    d = _dialog(MULTI_LANG, host)
    keys = list(d.lang_segment.items.keys())
    assert keys == ["all", "ja", "en"]
    assert d.lang_segment.currentRouteKey() == "all"


def test_single_language_gets_no_tabs(host) -> None:
    """只有一种语言时不加 tab：单个「全部语言」的分段控件点不动，纯占地方。"""
    d = _dialog({"formats": [_audio("140", "ja", 10, "mp4a.40.2", 128)]}, host)
    assert list(d.lang_segment.items.keys()) == ["all"]


def test_language_filter_hides_other_languages(host) -> None:
    d = _dialog(MULTI_LANG, host)
    d.lang_segment.setCurrentItem("en")
    assert set(_visible(d)) == {"140-1", "251-1", "251-2"}


def test_codec_filter_hides_other_codecs(host) -> None:
    d = _dialog(MULTI_LANG, host)
    _select_codec(d, "opus")
    assert set(_visible(d)) == {"251", "251-1", "251-2"}


def test_two_filters_compose(host) -> None:
    d = _dialog(MULTI_LANG, host)
    d.lang_segment.setCurrentItem("en")
    _select_codec(d, "opus")
    assert set(_visible(d)) == {"251-1", "251-2"}


def test_clearing_filters_restores_all_rows(host) -> None:
    d = _dialog(MULTI_LANG, host)
    d.lang_segment.setCurrentItem("en")
    _select_codec(d, "opus")
    d.lang_segment.setCurrentItem("all")
    _select_codec(d, None)
    assert len(_visible(d)) == 5
    assert _notice(d) is None


def test_filtering_never_touches_the_selection(host) -> None:
    """筛选只影响显示。下标对齐一旦断掉，这条就会挂 —— 那正是重建表格会犯的错。"""
    d = _dialog(MULTI_LANG, host)
    before = [t.format_id for t in d._get_selected_tracks()]
    assert before, "默认应当勾上得分最高的那条"

    d.lang_segment.setCurrentItem("en")
    _select_codec(d, "opus")
    assert [t.format_id for t in d._get_selected_tracks()] == before

    d.lang_segment.setCurrentItem("all")
    _select_codec(d, None)
    assert [t.format_id for t in d._get_selected_tracks()] == before


def test_notice_warns_when_a_hidden_row_is_still_checked(host) -> None:
    """默认勾选的是日语原音；切到 `en` 就把它藏起来了，必须说一句。

    不说的话，用户看到的就是"我明明只勾了这一条，怎么下载了别的音轨"。
    """
    d = _dialog(MULTI_LANG, host)
    checked = [t.format_id for t in d._get_selected_tracks()]
    assert checked == ["140"]  # ja 原音 + mp4 亲和加分

    d.lang_segment.setCurrentItem("en")
    assert "勾选" in (_notice(d) or "")


def test_notice_counts_hidden_rows_when_nothing_hidden_is_checked(host) -> None:
    d = _dialog(MULTI_LANG, host)
    # 先把默认勾选清掉，这样被隐藏的行里就没有勾上的了
    for cb in d._checkboxes:
        cb.setChecked(False)

    d.lang_segment.setCurrentItem("en")
    assert "2" in (_notice(d) or "")  # ja 的两条被藏起来


def test_notice_updates_when_the_checkbox_changes_under_a_filter(host) -> None:
    """勾选变化也要重跑提示 —— 勾一条再把它筛掉，提示得跟着变。"""
    d = _dialog(MULTI_LANG, host)
    for cb in d._checkboxes:
        cb.setChecked(False)

    d.lang_segment.setCurrentItem("en")
    assert "勾选" not in (_notice(d) or "")

    # 勾上一条被隐藏的 ja 轨
    ja_row = next(i for i, t in enumerate(d._all_tracks) if t.language == "ja")
    d._checkboxes[ja_row].setChecked(True)
    assert "勾选" in (_notice(d) or "")


def test_tracks_without_language_get_their_own_tab(host) -> None:
    """没有 `language` 的音轨（非 YouTube extractor 常见）不能和别人挤在一个键上。

    键取 `"unknown"` 而**不是** `"all"` —— 后者是「全部语言」这一项的 routeKey，
    撞上就等于把那一项劫持成了"只看无语言标注的轨"。
    """
    info = {
        "formats": [
            _audio("140", "ja", 10, "mp4a.40.2", 128),
            {**_audio("251", "", 0, "opus", 160), "language": None},
        ]
    }
    d = _dialog(info, host)
    assert set(d.lang_segment.items.keys()) == {"all", "ja", "unknown"}

    d.lang_segment.setCurrentItem("unknown")
    assert _visible(d) == ["251"]


def test_type_column_shows_all_four_kinds(host) -> None:
    """四态类型列：`audio_track_type` 恒为 None 的年代这一列永远显示"配音"。"""
    info = {
        "formats": [
            _audio("140", "ja", 10, "mp4a.40.2", 128),
            _audio("141", "en", 5, "mp4a.40.2", 128),
            _audio("142", "fr", -1, "mp4a.40.2", 128),
            _audio("143", "de", -10, "mp4a.40.2", 128),
        ]
    }
    d = _dialog(info, host)
    labels = {d._all_tracks[i].kind: d.table.item(i, 2).text() for i in range(len(d._all_tracks))}
    assert set(labels) == {"original", "default", "dub", "descriptive"}
    assert len(set(labels.values())) == 4, "四种类型必须显示成四种不同的文案"
