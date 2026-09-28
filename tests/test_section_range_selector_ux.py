"""视频裁切选项区的展开/收起手感（Phase 2b）。

老实现在 `SmoothScrollArea` 里补间 `maximumHeight`（220ms InOutQuad），每一帧都让
滚动区按新的最小高度全量重排 —— 展开裁切面板肉眼掉帧。新实现改成「一次性放开高度
上限（滚动区只重排一次）+ 透明度补间淡入」：opacity 只触发重绘、不参与布局，补间期间
不再逐帧重排。本测试锁死这套可观测状态机，防止有人把逐帧高度补间改回来。

构造真实 QWidget（含自绘时间轴），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-section-ux-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.ui.components.dialogs.section_range_selector import (  # noqa: E402
    _QWIDGETSIZE_MAX,
    SectionRangeSelector,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _settle(ms: int) -> None:
    QTest.qWait(SectionRangeSelector.OPTIONS_ANIM_MS + ms)


def test_options_start_collapsed_and_transparent(qapp):
    selector = SectionRangeSelector(600.0)
    selector.show()
    assert selector.options.isHidden()
    assert selector.options.maximumHeight() == 0
    assert selector._options_opacity.opacity() == 0.0
    selector.close()
    selector.deleteLater()


def test_expand_releases_height_in_one_shot_then_fades_content_in(qapp):
    selector = SectionRangeSelector(600.0)
    selector.show()

    selector.enable_switch.setChecked(True)
    # 高度上限必须「立刻」放开到满 —— 不是逐帧补间到某个中间值，否则滚动区会逐帧重排。
    assert selector.options.maximumHeight() == _QWIDGETSIZE_MAX
    assert selector.options.isVisible()
    # 淡入尚未走完时内容不是全不透明，driver 是 opacity 而不是高度。
    assert selector._options_opacity.opacity() < 1.0

    _settle(80)
    assert selector._options_opacity.opacity() == 1.0
    assert selector.options.maximumHeight() == _QWIDGETSIZE_MAX
    assert selector.is_enabled()
    selector.close()
    selector.deleteLater()


def test_collapse_fades_out_before_reclaiming_layout_space(qapp):
    selector = SectionRangeSelector(600.0)
    selector.show()
    selector.enable_switch.setChecked(True)
    _settle(80)

    selector.enable_switch.setChecked(False)
    # 淡出期间高度仍占位（先淡后缩），避免收起时的高度骤缩打断淡出。
    assert not selector.options.isHidden()
    assert selector.options.maximumHeight() == _QWIDGETSIZE_MAX

    _settle(80)
    assert selector.options.isHidden()
    assert selector.options.maximumHeight() == 0
    assert selector._options_opacity.opacity() == 0.0
    assert not selector.is_enabled()
    selector.close()
    selector.deleteLater()


def test_rapid_retoggle_cancels_inflight_fade_without_stuck_state(qapp):
    selector = SectionRangeSelector(600.0)
    selector.show()

    # 展开途中立刻收起：正在跑的补间要被取消，且不能卡在半透明的中间态。
    selector.enable_switch.setChecked(True)
    QTest.qWait(SectionRangeSelector.OPTIONS_ANIM_MS // 3)
    selector.enable_switch.setChecked(False)
    _settle(80)
    assert selector.options.isHidden()
    assert selector.options.maximumHeight() == 0
    assert selector._options_opacity.opacity() == 0.0

    # 再展开一次，仍应回到全不透明的稳定展开态。
    selector.enable_switch.setChecked(True)
    _settle(80)
    assert selector.options.isVisible()
    assert selector._options_opacity.opacity() == 1.0
    selector.close()
    selector.deleteLater()
