"""Regression: pro-mode stream tables keep their selection on empty-space clicks and drags.

Two selection defects surfaced in the parse-window professional mode, both mirroring the
download-list fix: clicking the empty viewport below the rows cleared everything, and an
accidental drag in the multi-select audio table wiped the whole batch ("连续选择突然全部取消").
`_StreamTable.selectionCommand` guards both — see the memory note
`fluent-table-empty-click-clears-selection`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QPoint,
    QPointF,
    Qt,
)
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

from fluentytdl.models.download_plan import (  # noqa: E402
    KIND_AUDIO_ONLY,
    KIND_VIDEO_ONLY,
    SubtitlePlan,
    build_download_plan,
)
from fluentytdl.ui.components.platforms.youtube import (  # noqa: E402
    VideoFormatSelectorWidget,
    _StreamTable,
)


def _no_sub():
    return SubtitlePlan(
        enabled=False, embed=False, keep_external=False, languages=(), output_format="srt"
    )


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _info():
    return {
        "id": "sel",
        "title": "sel",
        "duration": 60,
        "extractor_key": "Youtube",
        "formats": [
            {
                "format_id": f"v{i}",
                "height": 1080,
                "width": 1920,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "fps": 30,
                "url": "https://e.invalid/v",
            }
            for i in range(4)
        ]
        + [
            {
                "format_id": f"a{i}",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a",
                "abr": 128 + i,
                "url": "https://e.invalid/a",
            }
            for i in range(6)
        ],
    }


def _mouse(etype, mods=Qt.KeyboardModifier.NoModifier, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(etype, QPointF(5, 5), button, button, mods)


def test_stream_table_command_guards_move_and_empty_click(app):
    """`selectionCommand` 拦下两类误触，合法按下 / 程序化事件照常交回基类。"""
    t = _StreamTable()
    t.setColumnCount(3)
    t.setRowCount(4)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
    no_update = QItemSelectionModel.SelectionFlag.NoUpdate
    invalid = t.model().index(-1, -1)
    valid = t.model().index(0, 0)

    # 拖拽移动一律不改选择（不管 index 是否有效）——杜绝框选清空整批勾选。
    assert t.selectionCommand(valid, _mouse(QEvent.Type.MouseMove)) == no_update
    assert t.selectionCommand(invalid, _mouse(QEvent.Type.MouseMove)) == no_update
    # 点空白（无效 index + 无修饰键）：按下 / 松开都拦成 NoUpdate，不清空。
    assert t.selectionCommand(invalid, _mouse(QEvent.Type.MouseButtonPress)) == no_update
    assert t.selectionCommand(invalid, _mouse(QEvent.Type.MouseButtonRelease)) == no_update
    # 合法行的普通按下：交回基类切换单行（绝不能是 NoUpdate）。
    assert t.selectionCommand(valid, _mouse(QEvent.Type.MouseButtonPress)) != no_update
    # event=None（键盘 / 程序化）：不拦截，交回基类。
    t.selectionCommand(invalid, None)


def test_audio_drag_does_not_clear_multiselection(app):
    """已选好几条音轨后，误触拖拽绝不能把整批勾选清空（回归 bug 时 after 会变空集）。"""
    sel = VideoFormatSelectorWidget(_info())
    sel._on_mode_changed("advanced")
    sel.resize(900, 500)
    sel.show()
    app.processEvents()
    at = sel.audio_table
    assert at.rowCount() >= 6

    # 程序化选中 4 行，模拟用户已逐条点好（走原生选择模型，触发正常的高亮同步）。
    model = at.model()
    seeded = QItemSelection()
    for r in (0, 1, 2, 3):
        seeded.select(model.index(r, 0), model.index(r, at.columnCount() - 1))
    at.selectionModel().select(
        seeded,
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    app.processEvents()
    assert {i.row() for i in at.selectionModel().selectedRows()} == {0, 1, 2, 3}

    # 在未选中的行上做一次带位移的拖拽（press → move → release）。
    r = 5
    x = at.columnViewportPosition(1) + 10
    y = at.rowViewportPosition(r) + at.rowHeight(r) // 2
    start, end = QPoint(x, y), QPoint(x + 4, y - 40)
    QTest.mousePress(
        at.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
    )
    QTest.mouseMove(at.viewport(), end)
    QTest.mouseRelease(
        at.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end
    )
    app.processEvents()

    # 拖拽前选好的行必须原样保留：MouseMove 被 NoUpdate 拦掉，没有框选清空。
    after = {i.row() for i in at.selectionModel().selectedRows()}
    assert {0, 1, 2, 3}.issubset(after), after
    sel.close()


# ── 装配预览三连修：模式切换刷新 / 简易仅视频 / 专业单表仅音频 ─────────────────


def test_mode_switch_emits_selection_changed(app):
    """顶层标准↔专业切换必须发 `selectionChanged`，否则宿主装配预览停在旧模式结果。

    标准/专业算的是两套完全不同的 format（`_compute_selection_result` 第一行就按
    `_current_mode` 分流），所以模式一换、选择结果就变了，必须发信号让宿主重算——这正是
    「专业模式选了质量，切到标准模式不重新识别」那个 bug 的根因。
    """
    sel = VideoFormatSelectorWidget(_info())
    fired: list[int] = []
    sel.selectionChanged.connect(lambda: fired.append(1))

    sel._on_mode_changed("advanced")
    assert fired, "切到专业模式应触发 selectionChanged"
    fired.clear()

    sel._on_mode_changed("simple")
    assert fired, "切回标准模式应触发 selectionChanged"
    sel.close()


def test_simple_video_only_intent_drops_audio(app):
    """标准模式「仅视频」意图必须短路成纯视频流，绝不无条件配一条音频。

    回归 bug 时 video_only 没有独立支路，会掉进「含视频模式」被配上音频，于是实际下成
    视频+音频、预览也误判为 KIND_VIDEO_AUDIO。这里 stub 掉预设选择返回 video_only 意图，
    直接验证 `_compute_selection_result` 走新的短路分支。
    """
    sel = VideoFormatSelectorWidget(_info())
    sel._current_mode = "simple"
    sel.simple_widget.get_current_selection = lambda: {
        "id": "video_only_best",
        "intent": {"type": "video_only", "max_height": None},
    }

    result = sel._compute_selection_result()
    fmt = result.get("format", "")
    vid_ids = {r["format_id"] for r in sel._rows if r["kind"] == "video"}
    # 只挑视频流：不含 `+`（无音轨），且是一条真正的 video-kind 流。
    assert "+" not in fmt
    assert fmt in vid_ids, fmt
    # 仅视频绝不能带 extract_audio（那是「仅音频」路径）。
    assert not (result.get("extra_opts") or {}).get("extract_audio")

    # 反推路线必须与实际装配一致：KIND_VIDEO_ONLY，而非 VIDEO_AUDIO。
    plan = build_download_plan(result=result, rows=sel._rows, resolution=None, subtitle=_no_sub())
    assert plan.download_kind == KIND_VIDEO_ONLY
    assert plan.audio_tracks == ()
    sel.close()


def test_advanced_single_table_audio_only_not_empty(app):
    """专业模式「仅音频」单表点击只写单数 `_selected_audio_id`，结果不能被算成空。

    回归 bug 时 advanced 分支只读复数 `_selected_audio_ids`（单表点击从不写它）→ v/m/a_ids
    全空 → `return {}` → 预览类型恒显示「自动（yt-dlp 兜底）」、实际下载也失效。复数→单数
    兜底修好后，单条音轨照常成为 KIND_AUDIO_ONLY，且输出格式回落到流的原生 ext。
    """
    sel = VideoFormatSelectorWidget(_info())
    sel._on_mode_changed("advanced")
    aud_ids = [r["format_id"] for r in sel._rows if r["kind"] == "audio"]
    assert aud_ids

    # 精确模拟单表点击：只写单数，复数与 video/muxed 全部留空。
    sel._selected_video_id = None
    sel._selected_muxed_id = None
    sel._selected_audio_ids = []
    sel._selected_audio_id = aud_ids[0]

    result = sel._compute_selection_result()
    # 单数被兜底采纳 → format 正是那条音轨，绝不再是空结果 {}。
    assert result.get("format") == aud_ids[0], result

    plan = build_download_plan(result=result, rows=sel._rows, resolution=None, subtitle=_no_sub())
    assert plan.download_kind == KIND_AUDIO_ONLY
    assert len(plan.audio_tracks) == 1
    # 无 extract_audio 时输出格式回落到流原生 ext（`_info()` 音轨是 m4a）。
    assert plan.audio_format == "m4a"
    assert plan.output_label == "M4A"
    sel.close()


# ── 专业模式「模式感知」：切到仅视频/仅音频后，上一模式遗留的复数音轨选择绝不能漏进来 ──────


def test_advanced_video_only_ignores_stale_plural_audio(app):
    """专业「仅视频」：哪怕复数 `_selected_audio_ids` 残留整批音轨，也绝不配音。

    回归 bug 时 advanced 分支不看 `mode_combo`，`v and a_ids` 会把残留音轨拼成 `v+整批` →
    类型算成「视频 + 音频」、预览列出全部音轨（正是截图 1）。修复后 compute 按模式分流，模式 2
    只取视频流。这里故意灌满复数 + 单数（模拟从分屏切过来、`_refresh_table` 从不清复数）。
    """
    sel = VideoFormatSelectorWidget(_info())
    sel._on_mode_changed("advanced")
    sel.mode_combo.setCurrentIndex(2)  # 仅视频
    vid_ids = [r["format_id"] for r in sel._rows if r["kind"] == "video"]
    aud_ids = [r["format_id"] for r in sel._rows if r["kind"] == "audio"]
    assert vid_ids and len(aud_ids) >= 2

    sel._selected_video_id = vid_ids[0]
    sel._selected_audio_ids = list(aud_ids)  # 遗留：整批音轨
    sel._selected_audio_id = aud_ids[0]

    result = sel._compute_selection_result()
    fmt = result.get("format", "")
    assert fmt == vid_ids[0], fmt  # 只有视频，绝不拼 `+音轨`
    assert "+" not in fmt

    plan = build_download_plan(result=result, rows=sel._rows, resolution=None, subtitle=_no_sub())
    assert plan.download_kind == KIND_VIDEO_ONLY
    assert plan.audio_tracks == ()
    sel.close()


def _info_mixed_audio():
    """视频照旧，音轨换成两条异 ext：排序首条 m4a(129k) vs 用户点选的 opus/webm(126k)。

    只有当反推真的落到用户点选那条、而非排序首条 `aud_rows[0]` 时，格式才会显示 WEBM——据此
    复现截图 2 的「选的是 Opus，输出格式却显示 M4A」。
    """
    info = _info()
    info["formats"] = [f for f in info["formats"] if f["vcodec"] != "none"] + [
        {
            "format_id": "am4a",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a",
            "abr": 129,
            "url": "https://e.invalid/a",
        },
        {
            "format_id": "aopus",
            "ext": "webm",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 126,
            "url": "https://e.invalid/a",
        },
    ]
    return info


def test_advanced_audio_only_single_click_beats_stale_plural(app):
    """专业「仅音频」：单表点击（只写单数）必须胜过遗留的复数整批。

    回归 bug 时复数非空就短路掉单数 → format 变成整批音轨、`audio_format` 取排序首条 m4a，于是
    列出全部音轨、格式恒显示 M4A。修复后模式 3 只认单数：结果只含点选那条，格式回落其原生 ext。
    """
    sel = VideoFormatSelectorWidget(_info_mixed_audio())
    sel._on_mode_changed("advanced")
    sel.mode_combo.setCurrentIndex(3)  # 仅音频
    aud_ids = [r["format_id"] for r in sel._rows if r["kind"] == "audio"]
    assert {"am4a", "aopus"}.issubset(set(aud_ids))

    sel._selected_audio_ids = list(aud_ids)  # 遗留：整批（含排序首条 m4a）
    sel._selected_audio_id = "aopus"  # 单表点击只写单数

    result = sel._compute_selection_result()
    assert result.get("format") == "aopus", result  # 单数胜出，绝不是整批

    plan = build_download_plan(result=result, rows=sel._rows, resolution=None, subtitle=_no_sub())
    assert plan.download_kind == KIND_AUDIO_ONLY
    assert len(plan.audio_tracks) == 1
    assert plan.audio_format == "webm"  # 取点选那条的原生 ext，而非排序首条 m4a
    assert plan.output_label == "WEBM"
    sel.close()
