"""列表行 meta 那一行的渲染（`DownloadItemDelegate`）。

融合后一行有两种来源：**有 worker 的活任务**，和**只有 DB 快照的历史行**。meta 行的
分支表就是按这两种来源写的，而中途状态（`downloading` / `parsing` / `processing`）
**只可能来自快照** —— `DownloadWorker.effective_state` 从不返回它们。也就是说：

    落在中途状态 + 没有 worker  ==  上个会话跑到一半，进程没了

这一支原先不存在，那类行的 meta 是空字符串：重启后看到一根停在 45% 的进度条，
底下一个字都没有。这里钉住三件事：

1. 三种中途状态各有各的文案（`processing` 指的是 FFmpeg 合并现场，和「下载才走到
   45%」完全不是一回事，半成品还躺在 sandbox 临时目录里）；
2. **不显示落库的 `status_text`** —— 那是 CleanLogger 上次会话的瞬时值
   （「45.2% · 3.2MB/s · 剩余 00:12」），摆在一行早就不动了的任务下面看着像还在下；
3. 已废弃的 `running` 快照（3.5.5 之前的老库里可能有）走「已中断」而不是实况支，
   否则会拿上个会话的速度冒充当前速度。

测试直接驱动 `paint()`：meta 行的取值分支就在那里面，单测 `_interrupted_label()`
只能证明文案表对，证不到「哪个状态真的走进了这一支」。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 导入 ui 会按真实规则解析用户数据目录，先把数据根指到临时目录
os.environ.setdefault(
    "FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fluentytdl-delegate-test-")
)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from fluentytdl.ui.delegates.download_item_delegate import (  # noqa: E402
    _INTERRUPTED_STATES,
    DownloadItemDelegate,
)
from fluentytdl.ui.models.task_row import TaskRow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    """delegate 里有 `QFont` / `QPainterPath` / `themeColor()`，需要 QApplication。"""
    return QApplication.instance() or QApplication([])


class _TextCapture(QPainter):
    """记下 delegate 画了哪些文字。

    覆写能生效是因为 `paint()` 里那句 `painter.drawText(...)` 是**从 Python 调的** ——
    走的是属性查找，不依赖 C++ 侧的虚函数（`QPainter::drawText` 并不虚）。
    """

    def __init__(self, device):
        super().__init__(device)
        self.texts: list[str] = []

    def drawText(self, *args):  # noqa: N802  Qt 命名
        if args and isinstance(args[-1], str):
            self.texts.append(args[-1])
        return super().drawText(*args)


class _OneRowModel(QAbstractListModel):
    """只放一行的模型。delegate 只从 `UserRole` 取 `TaskRow`，别的 role 一律不给。"""

    def __init__(self, row_obj: TaskRow):
        super().__init__()
        self._row = row_obj

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008  Qt 签名
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.UserRole:
            return self._row
        return None


class _FakeWorker:
    """`TaskRow.worker` 的替身。

    `TaskRow` 是 dataclass，运行时不校验类型；delegate 只经 `effective_*` 读它，
    真起一个 `DownloadWorker` 会连带拉起线程和 sandbox。
    """

    def __init__(self, state: str, progress: float = 0.0, status_text: str = ""):
        self.effective_state = state
        self.progress_val = progress
        self.status_text = status_text


def _meta_of(delegate: DownloadItemDelegate, row_obj: TaskRow) -> str:
    """跑一遍真实 `paint()`，返回 meta 那一行的文字。

    `drawText` 在 paint 里恰好两次：标题、meta（按钮走 `drawPixmap`）。取第 2 次
    而不是最后一次 —— 以后加了别的文字，这里要立刻断言失败而不是静默取错行。
    """
    image = QImage(600, 100, QImage.Format.Format_ARGB32)
    painter = _TextCapture(image)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 600, 100)
        model = _OneRowModel(row_obj)
        delegate.paint(painter, option, model.index(0, 0))
    finally:
        painter.end()
    assert len(painter.texts) == 2, painter.texts
    return painter.texts[1]


@pytest.fixture
def delegate(qt_app):
    return DownloadItemDelegate()


def _snapshot(state: str, **kwargs) -> TaskRow:
    """一条历史行（没有 worker）。"""
    return TaskRow(db_id=1, title="视频", state=state, **kwargs)


# ── 中途状态：被打断 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("downloading", "下载已中断"),
        ("parsing", "解析已中断"),
        ("processing", "后处理已中断"),
        ("running", "下载已中断"),  # 老库里的废弃状态
    ],
)
def test_interrupted_snapshot_gets_a_meta_line(delegate, state, expected):
    """没有 worker 的中途状态行必须有文案 —— 原先这里是空字符串。"""
    meta = _meta_of(delegate, _snapshot(state, progress=45.2))
    assert meta.startswith(expected), meta


def test_interrupted_meta_carries_the_time(delegate):
    """得说清「几时停的」，否则历史行读不出新旧。"""
    row_obj = _snapshot("processing", updated_at=time.time() - 180)
    assert _meta_of(delegate, row_obj) == "后处理已中断 · 3 分钟前"


def test_interrupted_meta_without_a_timestamp_is_just_the_label(delegate):
    """`updated_at` 为 0（老行/没落库）时不能拼出「· 」这种断尾。"""
    assert _meta_of(delegate, _snapshot("downloading")) == "下载已中断"


def test_interrupted_meta_hides_the_stale_progress_text(delegate):
    """落库的 `status_text` 是上次会话的瞬时值，摆出来像还在下载。

    百分比已经由进度条表达，这一行要回答的是「它停了」。
    """
    row_obj = _snapshot("downloading", status_text="45.2% · 3.2MB/s · 剩余 00:12", progress=45.2)
    meta = _meta_of(delegate, row_obj)
    assert meta == "下载已中断"
    assert "MB/s" not in meta


def test_legacy_running_snapshot_is_not_treated_as_live(delegate):
    """`running` 是唯一活/死两边都可能出现的值，靠 `is_live` 区分。

    不加这道判断，老库里的 `running` 行会走实况支，把上个会话的速度当成当前速度。
    """
    row_obj = _snapshot("running", status_text="88.0% · 9.9MB/s", progress=88.0)
    assert _meta_of(delegate, row_obj) == "下载已中断"


# ── 有 worker 的活任务不受影响 ───────────────────────────────


def test_live_running_row_still_shows_the_status_text(delegate):
    """活任务照旧用 CleanLogger 那个全能字符串。"""
    row_obj = _snapshot("queued")
    row_obj.worker = _FakeWorker("running", progress=45.2, status_text="45.2% · 3.2MB/s")
    assert _meta_of(delegate, row_obj) == "45.2% · 3.2MB/s"


def test_live_running_row_without_status_text_falls_back_to_percent(delegate):
    row_obj = _snapshot("queued")
    row_obj.worker = _FakeWorker("running", progress=45.2)
    assert _meta_of(delegate, row_obj) == "下载: 45.2%"


def test_live_running_row_at_zero_progress_says_preparing(delegate):
    row_obj = _snapshot("queued")
    row_obj.worker = _FakeWorker("running")
    assert _meta_of(delegate, row_obj) == "准备下载..."


# ── 其余分支没被这次改动碰到 ─────────────────────────────────


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("queued", "等待下载..."),
        ("paused", "已暂停"),
        ("quality_guard", "已被质量守卫挂起"),
    ],
)
def test_untouched_states_keep_their_meta(delegate, state, expected):
    assert _meta_of(delegate, _snapshot(state)) == expected


def test_error_row_keeps_time_suffix(delegate):
    row_obj = _snapshot("error", updated_at=time.time() - 7200)
    assert _meta_of(delegate, row_obj) == "下载失败 · 2 小时前"


def test_cancelled_row_keeps_time_suffix(delegate):
    row_obj = _snapshot("cancelled", updated_at=time.time() - 86400 * 3)
    assert _meta_of(delegate, row_obj) == "已取消 · 3 天前"


def test_quality_guard_prefers_the_persisted_reason(delegate):
    """挂起原因是 download_manager 写进 `status_text` 的，比通用文案有信息量。"""
    row_obj = _snapshot("quality_guard", status_text="风控防御挂起")
    assert _meta_of(delegate, row_obj) == "风控防御挂起"


def test_completed_row_is_unaffected(delegate):
    row_obj = _snapshot("completed", file_size=1024 * 1024, updated_at=time.time() - 120)
    meta = _meta_of(delegate, row_obj)
    assert meta.startswith("1.0 MB") and meta.endswith("2 分钟前"), meta


# ── 状态集合本身 ────────────────────────────────────────────


def test_interrupted_states_exclude_terminal_and_queue_states():
    """这个集合只该收「跑到一半」的状态。

    误收 `queued` / `quality_guard` 会把「从没开始跑」说成「被打断」，
    误收终态则会盖掉「已完成 / 下载失败」的文案。
    """
    assert _INTERRUPTED_STATES.isdisjoint(
        {"completed", "error", "cancelled", "paused", "queued", "quality_guard"}
    )


def test_interrupted_states_cover_every_mid_flight_db_state():
    """`UNFINISHED_STATES` 里所有中途状态都要有文案，漏一个就又是一行空 meta。

    从 `task_db` 反查而不是手写清单：以后往那边加状态，这里会立刻红。
    """
    from fluentytdl.storage.task_db import UNFINISHED_STATES

    mid_flight = set(UNFINISHED_STATES) - {"queued", "paused", "quality_guard"}
    assert mid_flight <= _INTERRUPTED_STATES, mid_flight - _INTERRUPTED_STATES
