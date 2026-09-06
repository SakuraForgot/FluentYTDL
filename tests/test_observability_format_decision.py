"""画质/音轨选择的决策事件契约（`kind=decision subsystem=format`）。

用户最初的痛点就在这里："我设了日语音轨怎么还是英语"、"我选了 1080p 怎么下了 720p"。
命令行上这两种情况和正常情况长得一模一样 —— 只有把**要的**和**实际拿到的**并排记下来
才答得上，所以这几条断言盯的不是"有没有日志"，而是那几对字段是否真的成对出现。

三处专门锁死回归：

1. **重复取值不再 emit。** `get_selection_result()` 不是干净的收口点 ——
   `selection_dialog._open_subtitle_picker()` 只为读容器就会调它一次。没有指纹闸门，
   一次下载会落好几条一模一样的决策，`count(kind=decision)` 从此失去意义。
2. **`quality_score_deviation` 不许误报。** 旧代码拿 `_pick_best_video()` 的中间结果
   跟目标比，走整合流兜底时那条视频流根本没被采用，于是"实际下了 360p 整合流"被报成
   "144p 严重偏离 360p"。
3. **预览路径必须静默。** `resolve_global_format()` 在播放列表里被每行的画质预览调用，
   滚一次几十遍；只有真正拼 `row_opts` 的那次传 trace。

需要 QApplication（构造的是真实 QFrame），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-fmtdec-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.core.config_manager import config_manager  # noqa: E402
from fluentytdl.observability import new_flow  # noqa: E402
from fluentytdl.ui.components.platforms.youtube import (  # noqa: E402
    VideoFormatSelectorWidget,
    resolve_global_format,
)
from fluentytdl.utils.format_scorer import (  # noqa: E402
    ScoringContext,
    rank_audio_formats,
    score_audio_format,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def events():
    """收集本次测试期间产生的所有 fytdl 事件。"""
    captured: list[dict] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"]["fytdl"])),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _video(fid: str, ext: str, height: int, **kw) -> dict:
    row = {
        "format_id": fid,
        "ext": ext,
        "height": height,
        "vcodec": "avc1",
        "acodec": "none",
        "vbr": 100,
    }
    row.update(kw)
    return row


def _audio(fid: str, ext: str, lang: str, abr: int, **kw) -> dict:
    row = {
        "format_id": fid,
        "ext": ext,
        "vcodec": "none",
        "acodec": "mp4a",
        "abr": abr,
        "language": lang,
    }
    row.update(kw)
    return row


_MUXED_360 = {
    "format_id": "18",
    "ext": "mp4",
    "height": 360,
    "vcodec": "avc1",
    "acodec": "mp4a",
    "vbr": 50,
}

INFO_BILINGUAL = {
    "formats": [
        _video("137", "mp4", 1080),
        _video("136", "mp4", 720),
        _audio("140", "m4a", "en", 128, audio_track_type="original"),
        _audio("251-ja", "webm", "ja", 160, audio_track_type="dubbed"),
        _MUXED_360,
    ]
}

INFO_EN_ONLY = {
    "formats": [
        _video("248", "webm", 1080),
        _audio("251", "webm", "en", 160, audio_track_type="original"),
    ]
}

#: 只有 360p 整合流 + 144p 视频流 —— 旧代码在这套数据上误报偏差
INFO_TINY = {"formats": [_MUXED_360, _video("160", "mp4", 144)]}

#: 两条**同语言**日语音轨：人工配音 vs AI 配音。`avail_langs` 解释不了这种内斗
INFO_SAME_LANG = {
    "formats": [
        _video("137", "mp4", 1080),
        _audio("ja-dub", "m4a", "ja", 128, audio_track_type="dubbed"),
        _audio("ja-ai", "webm", "ja", 160, format_note="Japanese (auto-generated)"),
    ]
}


def _decisions(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "decision" and e.get("subsystem") == "format"]


def _score_of(ranked: list[str], fid: str) -> int:
    """从 `format_id:lang:score` 串里取回分数。"""
    for entry in ranked:
        parts = entry.split(":")
        if parts[0] == fid:
            return int(parts[-1])
    raise AssertionError(f"{fid} 不在排名里: {ranked}")


def _select(info: dict, *, langs: list[str], intent: dict | None = None):
    """构造控件、压一份预设意图、返回 (widget, result)。

    **一律显式给 intent，不按下标点预设按钮。** 预设阶梯是固定的 1080/720/480/360，
    与视频实际有什么无关，所以下标和"用户看到的选项"并不一一对应；`_compute_selection_result()`
    也是每次调用现读 `btn.property("intent")`，压属性和点按钮等效。
    """
    config_manager.set("preferred_audio_languages", langs)
    widget = VideoFormatSelectorWidget(info, trace=new_flow(stage="select"))
    btn = widget.simple_widget.btn_group.buttons()[-1]
    btn.setProperty("intent", intent or {"max_height": None, "type": "video"})
    btn.setChecked(True)
    return widget, widget.get_selection_result()


# ── 要的 vs 拿到的：音轨语言 ────────────────────────────────


def test_audio_language_hit_records_both_sides(qapp, events):
    """偏好命中时，`pref_langs` / `audio_lang` / `avail_langs` 三者同时在场。"""
    _, result = _select(INFO_BILINGUAL, langs=["ja", "en"])

    assert result["format"] == "137+251-ja"
    (decision,) = _decisions(events)
    assert decision["pref_langs"] == ["ja", "en"]
    assert decision["audio_lang"] == "ja"
    assert decision["lang_matched"] is True
    assert decision["avail_langs"] == ["en", "ja"]


def test_audio_language_miss_is_distinguishable_from_a_bug(qapp, events):
    """ "我设了日语怎么下了英语" —— `avail_langs` 里没有 ja，才说明不是 bug。

    `lang_matched=False` 单独看不够：它既可能是"视频没有日语音轨"，也可能是
    "有日语音轨却没被选中"（那是真 bug）。区分二者的唯一字段就是 `avail_langs`。
    """
    _, result = _select(INFO_EN_ONLY, langs=["ja"])

    assert result["format"] == "248+251"
    (decision,) = _decisions(events)
    assert decision["lang_matched"] is False
    assert decision["audio_lang"] == "en"
    assert "ja" not in decision["avail_langs"]


# ── audio_ranked：同语言内斗的唯一答案 ──────────────────────


def test_ranking_explains_a_same_language_upset(qapp, events):
    """两条日语音轨，挑了码率更低的那条 —— 因为另一条是 AI 配音。

    `avail_langs=["ja"]` + `lang_matched=True` 在这里什么都解释不了：语言明明命中了，
    用户的抱怨却是"为什么给我下了个机器音"。分差的来源是配音加权
    （人工 +10000 / AI −50000），而那套加权全靠 `format_note` —— 它此前在候选集
    构造器里被丢掉了，所以这条排名同时是那处修复的验收口。
    """
    _, result = _select(INFO_SAME_LANG, langs=["ja"], intent={"max_height": 1080, "type": "video"})

    assert result["format"] == "137+ja-dub"  # 128k 人工配音赢过 160k AI 配音
    (decision,) = _decisions(events)
    assert decision["audio_lang"] == "ja"
    assert decision["lang_matched"] is True
    ranked = decision["audio_ranked"]
    assert ranked[0].startswith("ja-dub:ja:")  # 排名第一 == 真正选中的
    assert _score_of(ranked, "ja-dub") - _score_of(ranked, "ja-ai") == 10000 + 50000 + 128 - 160


def test_ranking_is_scored_with_the_context_that_actually_picked(qapp, events):
    """排名必须用挑流时那份 ctx 重算，不能随手 `ScoringContext()`。

    `ScoringContext.prefer_ext` 的 dataclass 缺省值是 `"mp4"`，而实际传进来的
    `intent.get("prefer_ext")` 从来是 `None`。用错 ctx，m4a 会白拿 +2000 的容器亲和
    加分，排名和真实选择就对不上 —— **一份对不上的排名比没有排名更糟**，所以这里
    把分数钉死到个位。
    """
    _select(INFO_BILINGUAL, langs=["en"], intent={"max_height": 1080, "type": "video"})

    (decision,) = _decisions(events)
    # 命中偏好第 0 位(1e8) + 原音(50000) + abr(128)，**没有** mp4 亲和的 +2000
    assert _score_of(decision["audio_ranked"], "140") == 100_000_000 + 50_000 + 128


def test_ranking_is_absent_when_there_are_no_audio_rows(qapp, events):
    """只有整合流时不记排名 —— 空排名是噪音，不是信息。"""
    _select(INFO_TINY, langs=["en"], intent={"max_height": 360, "type": "video"})

    (decision,) = _decisions(events)
    assert decision["audio_ranked"] is None


# ── 要的 vs 拿到的：容器 ────────────────────────────────────


def test_forced_container_mismatch_is_visible(qapp, events):
    """用户在输出格式栏压了 mp4、流却是 webm —— 转封装的成因只有这一处看得见。

    "输出容器"从不参与挑流（`intent["prefer_ext"]` 全项目没有一处赋非 None 值），
    所以选 MP4 完全可能下到 VP9 再转一遍。`container_forced` 与 `container_auto`
    并排放才能看出这是用户压的、而非无损推断的结果。
    """
    widget = VideoFormatSelectorWidget(INFO_EN_ONLY, trace=new_flow(stage="select"))
    btn = widget.simple_widget.btn_group.buttons()[-1]
    btn.setProperty("intent", {"max_height": None, "type": "video"})
    btn.setChecked(True)
    combo = widget.format_bar.container_combo
    for i in range(combo.count()):
        if combo.itemText(i).lower() == "mp4":
            combo.setCurrentIndex(i)
            break
    widget.get_selection_result()

    (decision,) = _decisions(events)
    assert decision["container_forced"] == "mp4"
    assert decision["container"] == "mp4"
    assert decision["container_auto"] == "webm"  # 无损推断本来会选 webm
    assert decision["video_ext"] == "webm"


# ── route：整合流兜底是音轨问题的头号成因 ────────────────────


def test_muxed_fallback_is_named(qapp, events):
    """分离音轨一条都没匹配上、退回整合流，命令行上看不出，`route` 必须说明。"""
    _, result = _select(INFO_TINY, langs=["en"], intent={"max_height": 360, "type": "video"})

    assert result["format"] == "18"
    (decision,) = _decisions(events)
    assert decision["route"] == "muxed_fallback"
    assert decision["audio_lang"] is None


def test_unreachable_preset_degrades_to_the_bare_selector(qapp, events):
    """用户点 360p、可视频只有 1080p —— 控件直接交出 `best`，打分引擎彻底没参与。

    这是既有行为（`_pick_best_video()` 在候选池被 `max_height` 滤空时 `return None`，
    而 `resolve_global_format()` 的同名逻辑却退回整池），不在本次改动范围内。但它必须
    在日志里看得见：`route=bare_fallback` 是"这次下载的画质由 yt-dlp 自己定"的唯一凭据。
    """
    _, result = _select(INFO_EN_ONLY, langs=["en"], intent={"max_height": 360, "type": "video"})

    assert result["format"] == "best"
    (decision,) = _decisions(events)
    assert decision["route"] == "bare_fallback"
    assert decision["video_height"] is None
    assert decision["avail_height_max"] == 1080  # 有 1080p，只是没被采用


def test_audio_only_route(qapp, events):
    widget = VideoFormatSelectorWidget(INFO_BILINGUAL, trace=new_flow(stage="select"))
    widget.simple_widget._type_combo.setCurrentIndex(2)  # 仅音频
    buttons = widget.simple_widget.btn_group.buttons()
    if buttons:
        buttons[0].setChecked(True)
    widget.get_selection_result()

    (decision,) = _decisions(events)
    assert decision["route"] == "audio_only"


# ── quality_score_deviation：只报真偏差 ─────────────────────


def _signals(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "signal"]


def test_deviation_signal_fires_on_a_real_shortfall(qapp, events):
    """要 1080p 而全站最高 360p —— 落 signal，且带上 `avail_height_max` 供判断成因。"""
    _select(INFO_TINY, langs=["en"], intent={"max_height": 1080, "type": "video"})

    (signal,) = _signals(events)
    assert signal["code"] == "quality_score_deviation"
    assert signal["target_height"] == 1080
    assert signal["actual_height"] == 360
    # 成功路径的异常征兆只能是 signal，绝不是 diagnosis（硬规则 2）
    assert not [e for e in events if e["kind"] == "diagnosis"]
    # 同一条决策事件里的 avail_height_max 才能说明"这视频压根没有 1080p"
    (decision,) = _decisions(events)
    assert decision["avail_height_max"] == 360


def test_deviation_signal_does_not_false_alarm_on_muxed_fallback(qapp, events):
    """目标 360 且实际下的就是 360p 整合流 —— 不许报偏差。

    旧代码比的是 `_pick_best_video()` 挑出的 144p 视频流，可那条流在整合流兜底
    路线上根本没被采用，于是一次完全正常的下载稳定产出一条 WARNING。
    """
    _select(INFO_TINY, langs=["en"], intent={"max_height": 360, "type": "video"})

    (decision,) = _decisions(events)
    assert decision["target_height"] == 360
    assert decision["video_height"] == 360
    assert not _signals(events)


# ── 指纹闸门：UI 重复取值不是新决策 ─────────────────────────


def test_repeated_query_of_the_same_selection_is_silent(qapp, events):
    """`get_selection_result()` 被字幕选择器顺手调来读容器，那不是一次新决策。"""
    widget, _ = _select(INFO_BILINGUAL, langs=["en"])
    assert len(_decisions(events)) == 1

    widget.get_selection_result()
    widget.get_selection_result()
    assert len(_decisions(events)) == 1


def test_deviation_signal_is_deduped_too(qapp, events):
    """偏差警告跟着决策走，不会因为 UI 多问几次就喊几次。"""
    widget, _ = _select(INFO_TINY, langs=["en"], intent={"max_height": 1080, "type": "video"})
    assert len(_signals(events)) == 1

    widget.get_selection_result()
    assert len(_signals(events)) == 1


def test_changing_the_selection_emits_again(qapp, events):
    """闸门挡的是重复，不是变化 —— 换了画质必须落新的一条。"""
    widget, _ = _select(INFO_BILINGUAL, langs=["en"])
    buttons = widget.simple_widget.btn_group.buttons()
    buttons[-1].setProperty("intent", {"max_height": 720, "type": "video"})
    buttons[-1].setChecked(True)
    widget.get_selection_result()

    decisions = _decisions(events)
    assert len(decisions) == 2
    assert decisions[0]["format"] != decisions[1]["format"]


# ── 全局预设侧：字段同名同义 + 预览路径静默 ──────────────────


class _Override:
    """`PlaylistGlobalFormatOverride` 的最小替身。"""

    preset_intent = {"max_height": 1080, "prefer_ext": "mp4"}
    download_type = "video_audio"
    container_override = None
    audio_format_override = None


def test_global_preset_uses_the_same_field_names(qapp, events):
    """播放列表里"这一行怎么是 720p"应该和单视频用同一条查询筛出来。"""
    config_manager.set("preferred_audio_languages", ["en"])
    fmt, _ = resolve_global_format(INFO_BILINGUAL, _Override(), trace=new_flow(stage="select"))

    assert fmt == "137+140"
    (decision,) = _decisions(events)
    assert decision["mode"] == "global"
    assert decision["route"] == "video_audio"
    assert decision["target_height"] == 1080
    assert decision["video_height"] == 1080
    assert decision["avail_langs"] == ["en", "ja"]
    # 全局侧的 `prefer_ext` 是真的 "mp4"（预设意图里带），所以 m4a 这次**该**拿到
    # +2000 亲和加分 —— 同一个字段名，两边各自记的是各自那份 ctx 算出来的分。
    assert _score_of(decision["audio_ranked"], "140") == 100_000_000 + 50_000 + 2_000 + 128


def test_global_preset_without_formats_reports_the_bare_selector(qapp, events):
    """info 里没有 formats 时返回的是 yt-dlp 选择器串，`route` 必须说清没走打分引擎。"""
    resolve_global_format({}, _Override(), trace=new_flow(stage="select"))

    (decision,) = _decisions(events)
    assert decision["route"] == "no_formats"
    assert decision["format"].startswith("bv*[height<=1080]")


def test_playlist_preview_path_emits_nothing(qapp, events):
    """不传 trace 的调用点是每行的画质预览，滚一次几十遍 —— 一条都不许落。"""
    resolve_global_format(INFO_BILINGUAL, _Override())

    assert not events


# ── rank_audio_formats 与 max() 的等价性 ────────────────────


def test_ranking_ties_keep_the_original_order():
    """同分时取原始顺序里的第一条 —— 与 `max()` 完全一致。

    `_get_best_audio_id()` 从 `max(rows, key=score)` 换成了 `rank_audio_formats(...)[0]`，
    动机纯粹是观测（要拿到亚军和分差），**不许顺手改变任何选择结果**。同分是唯一可能
    分道扬镳的地方（`sorted` 稳定、`max` 取首个最大值），所以专门钉这一处。
    """
    ctx = ScoringContext(prefer_ext=None, preferred_audio_langs=["en"])
    rows = [_audio(f"a{i}", "m4a", "en", 128) for i in range(4)]

    ranked = rank_audio_formats(rows, ctx)
    assert len({s for _, s in ranked}) == 1  # 确实全同分
    assert [r["format_id"] for r, _ in ranked] == ["a0", "a1", "a2", "a3"]
    assert ranked[0][0] is max(rows, key=lambda r: score_audio_format(r, ctx))
