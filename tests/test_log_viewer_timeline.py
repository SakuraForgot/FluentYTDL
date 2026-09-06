"""日志查看器的第二页（`EventTimelineView`）+ 事件信号 + 压缩历史回填。

这三块是 Observability Event 层唯一的**用户可见出口**。前面所有 emit 点都正确，
但如果时间线分组把事件挂错地方、或者历史日志读不出真实级别，用户看到的还是一团
读不懂的东西 —— 而"打开日志查看器"的典型时机恰恰是刚刚失败完。

钉住四件在 GUI 里不可能稳定复现的性质：

1. **占位符层级不建壳** —— 解析段事件 `task=-` `run=-`，硬套五层会白挂空节点；
2. **过滤保留祖先链** —— 筛 ERROR 时那条 `flow → task → run` 必须还在，
   "哪个任务出的错"全靠它读出来；
3. **淘汰不留脏索引** —— `_groups` 里残留一个已被 Qt 回收的节点，之后同一个 flow
   的新事件会静默消失（`add_event()` 整段吞异常，连报错都看不到）；
4. **历史日志按 loguru 默认格式解析** —— 时间和级别不是装饰，级别筛选靠它。

需要 QApplication（构造的是真实 QTreeWidget / 无边框窗口），走 offscreen。
"""

import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-logview-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from fluentytdl.observability import (  # noqa: E402
    NO_ID,
    FlowTrace,
    TaskTrace,
    build_event,
    emit_event,
)
from fluentytdl.ui.components.common import event_timeline as timeline_mod  # noqa: E402
from fluentytdl.ui.components.common.event_timeline import EventTimelineView  # noqa: E402
from fluentytdl.ui.components.dialogs import log_viewer_window as window_mod  # noqa: E402
from fluentytdl.ui.components.dialogs.log_viewer_window import LogViewerWindow  # noqa: E402
from fluentytdl.utils.log_signal_handler import log_signal_handler  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qt_app):
    v = EventTimelineView()
    yield v
    v.deleteLater()


def _event(
    kind: str,
    *,
    level: str = "INFO",
    stage: str = "download",
    trace=None,
    extra: dict | None = None,
    **fields,
):
    """按真实形状造一条事件 dict。

    刻意走 `build_event().as_dict()` 而不是手写字典：标识键名、`attempt` 的类型、
    业务字段与标识同名时改写成 `<key>_` 这些规则都在那边，手写会和真实数据漂移。

    `extra` 是给 `from` 这类 Python 关键字字段用的（`transition` 的 headline 字段
    恰好就叫 `from`，真实 emit 点也是靠显式 `fields={}` 传的）。
    """
    merged = dict(extra or {})
    merged.update(fields)
    payload = build_event(kind, trace=trace, stage=stage, fields=merged).as_dict()
    payload["_level"] = level
    payload["_time"] = "12:00:00"
    return payload


def _tops(view):
    return [view.topLevelItem(i) for i in range(view.topLevelItemCount())]


def _leaf(view, index: int = 0):
    """第 index 条事件的叶子节点。

    不靠 `.child(0).child(0)...` 数层数：分组层数随事件带不带 task/run 变化，
    数错一层就静默断言到分组节点上（它每一列都是空串，断言反而"通过"了）。
    """
    return list(view._leaves)[index]


def _labels(item, depth=0):
    """(缩进深度, 第一列文本) 的先序列表 —— 树形状的可读快照。"""
    out = [(depth, item.text(0))]
    for i in range(item.childCount()):
        out.extend(_labels(item.child(i), depth + 1))
    return out


def _visible_labels(item, depth=0):
    if item.isHidden():
        return []
    out = [(depth, item.text(0))]
    for i in range(item.childCount()):
        out.extend(_visible_labels(item.child(i), depth + 1))
    return out


# ── 分组 ────────────────────────────────────────────────────


def test_placeholder_identity_levels_are_not_nested(view):
    """解析段没有 task / run，不许为它们建空壳层级。"""
    flow = FlowTrace(flow_id="k72f")
    view.add_event(_event("stage", stage="parse", trace=flow, worker="InfoExtractWorker"))

    assert _labels(view.topLevelItem(0)) == [
        (0, "flow k72f"),
        (1, "[parse]"),
        (2, "stage"),
    ]
    assert view.event_count() == 1


def test_task_events_nest_through_run_and_attempt(view):
    """`flow → task → run → attempt → stage → 事件`，重试只多一个 attempt 兄弟。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("diagnosis", level="ERROR", trace=trace, code="rate_limited_429"))
    view.add_event(_event("retry", stage="retry", trace=trace, code="rate_limited_429"))
    trace.next_attempt()
    view.add_event(_event("outcome", stage="finalize", trace=trace, outcome="success"))

    assert _labels(view.topLevelItem(0)) == [
        (0, "flow k72f"),
        (1, "task 42"),
        (2, "run a91c"),
        (3, "attempt 0"),
        (4, "[download]"),
        (5, "diagnosis"),
        (4, "[retry]"),
        (5, "retry"),
        (3, "attempt 1"),
        (4, "[finalize]"),
        (5, "outcome"),
    ]


def test_attempt_level_exists_even_at_attempt_zero(view):
    """attempt 0 也建层。

    "只在 >0 时建"会让重试一发生，attempt 0 的事件挂在 run 上、attempt 1 的事件缩进
    一级 —— 同一轮的东西看着像两回事。
    """
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("stage", trace=trace))
    assert (3, "attempt 0") in _labels(view.topLevelItem(0))


def test_events_without_a_flow_land_under_one_placeholder_group(view):
    """没有 flow 的事件（如 startup）也得有落点，不能散在顶层。"""
    view.add_event(_event("config", stage="startup", scope="snapshot"))
    view.add_event(_event("argv", stage="startup", component="ytdlp"))
    assert view.topLevelItemCount() == 1
    assert view.topLevelItem(0).text(0) != f"flow {NO_ID}"
    assert view.event_count() == 2


def test_same_flow_and_task_reuse_one_group(view):
    """同 flow 同 task 的事件必须归到同一棵子树，不能每条各建一份。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    for i in range(5):
        view.add_event(_event("signal", trace=trace, code=f"s{i}"))
    assert view.topLevelItemCount() == 1
    labels = _labels(view.topLevelItem(0))
    assert labels.count((1, "task 42")) == 1
    assert labels.count((4, "[download]")) == 1
    assert view.event_count() == 5


# ── 渲染 ────────────────────────────────────────────────────


def test_detail_column_puts_headline_fields_first(view):
    """详情列先给"这条事件存在的理由"，其余字段跟在后面。

    列宽有限，`code` 被挤到 `extractor=youtube` 后面就等于没记。
    """
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(
        _event(
            "diagnosis",
            level="ERROR",
            trace=trace,
            extractor="youtube",
            severity="fatal",
            code="rate_limited_429",
            category="network",
        )
    )
    detail = _leaf(view).text(3)
    assert detail.startswith("code=rate_limited_429 category=network severity=fatal")
    assert "extractor=youtube" in detail


def test_detail_column_never_repeats_identity_or_sink_metadata(view):
    """标识已经在树的层级里、时间已经在时间列里，详情列再抄一遍就没地方放真内容了。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("transition", trace=trace, extra={"from": "queued", "to": "downloading"}))
    detail = _leaf(view).text(3)
    assert detail == "from=queued to=downloading"
    for banned in ("flow=", "task=", "run=", "attempt=", "session=", "_ts", "_level", "_time"):
        assert banned not in detail


def test_leaf_columns_carry_level_and_time(view):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("actual", level="WARNING", stage="verify", trace=trace, matched=False))
    leaf = _leaf(view)
    assert leaf.text(0) == "actual"
    assert leaf.text(1) == "WARNING"
    assert leaf.text(2) == "12:00:00"


def test_add_event_swallows_malformed_input(view):
    """畸形事件绝不能炸掉整个日志窗口（硬规则 5 的 UI 侧延伸）。"""
    view.add_event(None)  # type: ignore[arg-type]
    view.add_event({})
    view.add_event({"kind": "stage", "flow": object()})
    assert view.event_count() == 2  # None 那条被吞掉，另两条仍挂得上


# ── 过滤 ────────────────────────────────────────────────────


def _error_only(level: str, _text: str) -> bool:
    return level == "ERROR"


def test_filter_hides_leaves_but_keeps_the_ancestor_chain(view):
    """筛 ERROR 时那条 `flow → task → run → attempt → stage` 必须还在。

    只留一行 `diagnosis` 而砍掉分组节点，就再也读不出"是哪个任务、第几次尝试出的错"。
    """
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("stage", trace=trace))
    view.add_event(_event("diagnosis", level="ERROR", trace=trace, code="rate_limited_429"))
    other = TaskTrace(flow_id="zzz9", task_id="7", run_id="b000")
    view.add_event(_event("stage", trace=other))

    view.apply_filter(_error_only)

    visible = [lbl for item in _tops(view) for _, lbl in _visible_labels(item)]
    assert visible == ["flow k72f", "task 42", "run a91c", "attempt 0", "[download]", "diagnosis"]


def test_filter_lets_later_matching_events_show_through(view):
    """过滤生效期间进来的新事件，祖先链要跟着重新露出来。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("stage", trace=trace))
    view.apply_filter(_error_only)
    assert not [lbl for item in _tops(view) for _, lbl in _visible_labels(item)]

    view.add_event(_event("diagnosis", level="ERROR", trace=trace, code="boom"))
    visible = [lbl for item in _tops(view) for _, lbl in _visible_labels(item)]
    assert visible[-1] == "diagnosis"
    assert visible[0] == "flow k72f"


def test_filter_matches_against_fields_not_just_kind(view):
    """搜 `429` 或 `subtitle` 都该命中 —— 用户记得的是错误内容，不是 kind 名。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("diagnosis", level="ERROR", trace=trace, code="rate_limited_429"))
    view.add_event(_event("signal", trace=trace, code="nsig_extraction_failed"))

    view.apply_filter(lambda _level, text: "429" in text)
    leaves = [lbl for item in _tops(view) for _, lbl in _visible_labels(item)]
    assert leaves[-1] == "diagnosis"


# ── 淘汰 ────────────────────────────────────────────────────


def test_eviction_caps_the_tree_and_prunes_empty_groups(view):
    """超上限按最旧淘汰，掏空的分组节点一起收掉。

    一次 Playlist 能产生上万条事件，全留着树会卡。
    """
    view.MAX_EVENTS = 3
    for i in range(6):
        trace = TaskTrace(flow_id="k72f", task_id=str(i), run_id="a91c")
        view.add_event(_event("signal", trace=trace, code=f"s{i}"))

    assert view.event_count() == 3
    labels = [lbl for item in _tops(view) for _, lbl in _labels(item)]
    assert [lbl for lbl in labels if lbl.startswith("task ")] == ["task 3", "task 4", "task 5"]


def test_eviction_clears_the_group_index(view):
    """脏索引会让同一个 task 之后的事件静默消失。

    `_groups` 里留着一个已从树上摘掉的节点，`add_event()` 会往那个孤儿上挂子节点 ——
    而它整段吞异常，连报错都看不到。这是本文件最重要的一条断言。
    """
    view.MAX_EVENTS = 1
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("signal", trace=trace, code="first"))
    view.add_event(_event("signal", trace=TaskTrace(flow_id="zzz9", task_id="7"), code="evictor"))
    # task 42 那棵子树已被淘汰清空
    assert not any(lbl == "task 42" for item in _tops(view) for _, lbl in _labels(item))
    assert not any("task:42" in key for key in view._groups)

    view.MAX_EVENTS = 50
    view.add_event(_event("signal", trace=trace, code="second"))
    labels = [lbl for item in _tops(view) for _, lbl in _labels(item)]
    assert "task 42" in labels, labels
    assert view.event_count() == 2


def test_clear_all_resets_every_index(view):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("signal", trace=trace, code="x"))
    view.clear_all()
    assert view.event_count() == 0
    assert view.topLevelItemCount() == 0
    assert not view._groups
    # 清空后还能继续用（同一个 flow 的分组要重新建得起来）
    view.add_event(_event("signal", trace=trace, code="y"))
    assert view.event_count() == 1


# ── 选中 / 回填 ──────────────────────────────────────────────


def test_selected_identity_walks_up_to_the_task(view):
    """导 bug 包要 `(task_id, flow_id)`，而用户点的往往是某条事件或某个 stage 分组。"""
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    view.add_event(_event("outcome", stage="finalize", trace=trace, outcome="success"))
    leaf = _leaf(view)

    view.setCurrentItem(leaf)
    assert view.selected_identity() == ("42", "k72f")

    view.setCurrentItem(leaf.parent())  # [finalize] 分组
    assert view.selected_identity() == ("42", "k72f")

    view.setCurrentItem(view.topLevelItem(0).child(0))  # task 42 分组本身
    assert view.selected_identity() == ("42", "k72f")


def test_selected_identity_is_none_for_the_parse_half(view):
    """解析段没有 task —— 这时"导出该任务"无从下手，必须返回 None 让 UI 提示用户。"""
    view.add_event(_event("stage", stage="parse", trace=FlowTrace(flow_id="k72f")))
    view.setCurrentItem(view.topLevelItem(0).child(0))  # [parse] 分组
    assert view.selected_identity() is None


def test_load_recent_backfills_jsonl_sorted_across_files(view, tmp_path, monkeypatch):
    """回填要按 `_ts` 全局排序，否则多 flow 会拼成"A 的收尾在 B 的开头之前"。"""
    monkeypatch.setattr(timeline_mod, "get_trace_dir", lambda: tmp_path)
    (tmp_path / "flow-aaa.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in (
                {"kind": "stage", "stage": "parse", "flow": "aaa", "_ts": 100.0},
                {"kind": "outcome", "stage": "finalize", "flow": "aaa", "_ts": 400.0},
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "flow-bbb.jsonl").write_text(
        json.dumps({"kind": "stage", "stage": "parse", "flow": "bbb", "_ts": 200.0}) + "\n",
        encoding="utf-8",
    )

    assert view.load_recent() == 3
    # 挂载顺序必须是全局 `_ts` 顺序（100 → 200 → 400），不是"先把 aaa 读完再读 bbb"。
    # 树的形状按 flow 分组，读不出这件事，所以看插入序。
    assert [leaf.text(0) for leaf in view._leaves] == ["stage", "stage", "outcome"]
    # `_level` 缺省补 INFO，`_time` 从 `_ts` 还原 —— 否则级别列空白、筛选筛不动
    leaf = _leaf(view)
    assert leaf.text(1) == "INFO"
    assert leaf.text(2) != ""


def test_load_recent_tolerates_broken_lines(view, tmp_path, monkeypatch):
    """trace 文件可能被强杀截断在半行 —— 一行坏的不该带走整份历史。"""
    monkeypatch.setattr(timeline_mod, "get_trace_dir", lambda: tmp_path)
    (tmp_path / "flow-aaa.jsonl").write_text(
        json.dumps({"kind": "stage", "stage": "parse", "flow": "aaa", "_ts": 1.0})
        + "\n{ this is not json\n"
        + json.dumps({"kind": "signal", "stage": "parse", "flow": "aaa", "_ts": 2.0})
        + "\n",
        encoding="utf-8",
    )
    assert view.load_recent() == 2


# ── log_signal_handler ──────────────────────────────────────


def _pump(app, predicate, timeout=5.0):
    """等异步 sink（`enqueue=True`）把记录送过来。

    loguru 的写入线程 + Qt 跨线程 queued connection 中间有两跳，
    一次 `processEvents()` 抓不到 —— 首个事件到了就以为全到了正是这么误判的。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


@pytest.fixture
def installed_handler(qt_app):
    """装上转发 sink，用完必须撤 —— 它是全局单例，留着会污染后面所有用例。"""
    was_installed = log_signal_handler.is_installed
    log_signal_handler.install()
    yield log_signal_handler
    if not was_installed:
        log_signal_handler.uninstall()


def test_event_received_forwards_the_flat_dict(qt_app, installed_handler):
    """结构化事件走 `event_received`，纯文本日志只走 `log_received`。

    两条信号分开，是因为文本页要按时间看全部日志、时间线页要按 flow/task 分组，
    同一份文本满足不了两种需求。
    """
    events: list[dict] = []
    texts: list[tuple[str, str, str, str]] = []

    def on_text(*args):
        texts.append(args)

    # 具名而非 lambda：lambda 拿不到引用就断不开，而 handler 是全局单例 ——
    # 留着的连接会跟着后面每个用例一起收日志。
    installed_handler.event_received.connect(events.append)
    installed_handler.log_received.connect(on_text)
    try:
        trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
        emit_event("diagnosis", trace=trace, level="ERROR", code="rate_limited_429")
        logger.info("一条没有 fytdl extra 的普通日志 marker_plain")

        assert _pump(qt_app, lambda: len(events) >= 1 and len(texts) >= 2)
    finally:
        installed_handler.event_received.disconnect(events.append)
        installed_handler.log_received.disconnect(on_text)

    payload = next(e for e in events if e.get("code") == "rate_limited_429")
    assert (payload["flow"], payload["task"], payload["run"]) == ("k72f", "42", "a91c")
    assert payload["kind"] == "diagnosis" and payload["stage"] == "download"
    # `_time` / `_level` 是转发时补的展示字段，不是事件字段本身
    assert payload["_level"] == "ERROR"
    assert len(payload["_time"].split(":")) == 3

    assert any("marker_plain" in msg for *_, msg in texts)
    assert not any("marker_plain" in str(e) for e in events)


# ── 历史日志（文本页）───────────────────────────────────────


def test_parse_lines_reads_the_loguru_default_format():
    """时间和级别不是装饰：级别下拉框筛 ERROR 时，历史里的错误全靠它们筛得出来。"""
    parsed = LogViewerWindow._parse_lines(
        [
            "2026-08-25 14:03:12.345 | INFO     | fluentytdl.download.workers:run:812 - 开始下载\n",
            "2026-08-25 14:03:13.001 | ERROR    | fluentytdl.download.executor:run:474 - 炸了\n",
        ]
    )
    assert [(t, lvl) for t, lvl, _m, _msg in parsed] == [
        ("14:03:12", "INFO"),
        ("14:03:13", "ERROR"),
    ]
    assert parsed[0][2] == "fluentytdl.download.workers:run:812"
    assert parsed[1][3] == "炸了"


def test_parse_lines_keeps_tracebacks_with_their_error_header():
    """解析不出来的续行继承上一条的时间与级别。

    否则筛 ERROR 时只剩一行 "Uncaught exception"，底下的堆栈因为被当成 INFO 全被滤掉
    —— 而堆栈才是要看的东西。
    """
    parsed = LogViewerWindow._parse_lines(
        [
            "2026-08-25 14:03:13.001 | ERROR    | fluentytdl.main:hook:42 - Uncaught exception\n",
            "Traceback (most recent call last):\n",
            '  File "x.py", line 1, in <module>\n',
            "ValueError: boom\n",
        ]
    )
    assert len(parsed) == 4
    assert all(lvl == "ERROR" for _t, lvl, _m, _msg in parsed)
    assert all(t == "14:03:13" for t, _lvl, _m, _msg in parsed)
    assert parsed[-1][3] == "ValueError: boom"


def test_parse_lines_skips_blank_lines():
    assert LogViewerWindow._parse_lines(["\n", "   \n"]) == []


def test_history_files_are_ordered_by_the_date_in_the_name(tmp_path, monkeypatch):
    """不按 mtime 排：zip 的 mtime 是压缩那一瞬，同一批补压缩会挤在同一秒。"""
    monkeypatch.setattr(window_mod, "LOG_DIR", str(tmp_path))
    names = ["app_2026-08-25.log", "app_2026-08-23.log.zip", "app_2026-08-24.log.zip"]
    for name in reversed(names):  # 故意让 mtime 顺序与日期顺序相反
        (tmp_path / name).write_bytes(b"x")
        time.sleep(0.01)

    assert [p.name for p in LogViewerWindow._history_files()] == [
        "app_2026-08-23.log.zip",
        "app_2026-08-24.log.zip",
        "app_2026-08-25.log",
    ]


def test_tail_lines_reads_zip_members(tmp_path):
    """`retention="7 days" + compression="zip"` 意味着往日日志全在 zip 里。"""
    archive = tmp_path / "app_2026-08-24.log.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app_2026-08-24.log", "line1\nline2\nline3\n")

    assert LogViewerWindow._tail_lines(archive, 10) == ["line1\n", "line2\n", "line3\n"]
    assert LogViewerWindow._tail_lines(archive, 2) == ["line2\n", "line3\n"]


def test_tail_lines_takes_the_end_of_a_plain_log(tmp_path):
    path = tmp_path / "app_2026-08-25.log"
    path.write_text("".join(f"line{i}\n" for i in range(200)), encoding="utf-8")
    tail = LogViewerWindow._tail_lines(path, 3)
    assert tail == ["line197\n", "line198\n", "line199\n"]


def test_tail_lines_never_raises_on_a_bad_file(tmp_path):
    assert LogViewerWindow._tail_lines(tmp_path / "missing.log", 5) == []
    broken = tmp_path / "app_2026-08-22.log.zip"
    broken.write_bytes(b"not a zip at all")
    assert LogViewerWindow._tail_lines(broken, 5) == []


# ── 窗口整体 ────────────────────────────────────────────────


@pytest.fixture
def host(qt_app):
    """当"主窗口"用：日志窗口以 `parent=None` 构造，`host` 只作为初始几何的参照系。"""
    w = QWidget()
    w.resize(1000, 700)
    yield w
    w.deleteLater()


@pytest.fixture
def log_dirs(tmp_path, monkeypatch):
    """日志目录与 trace 目录都指到 tmp_path，别去读开发机上真实的日志。"""
    monkeypatch.setattr(window_mod, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(timeline_mod, "get_trace_dir", lambda: tmp_path / "traces")
    (tmp_path / "traces").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def window(host, log_dirs):
    was_installed = log_signal_handler.is_installed
    d = LogViewerWindow(host)
    yield d
    d._stop_log_capture()
    if not was_installed:
        log_signal_handler.uninstall()
    d.deleteLater()


def test_window_loads_both_zipped_and_plain_history(host, tmp_path, monkeypatch):
    """压缩历史必须也读 —— 刚过零点或刚重启时，"昨晚那次失败"全在 zip 里。"""
    monkeypatch.setattr(window_mod, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(timeline_mod, "get_trace_dir", lambda: tmp_path / "traces")
    (tmp_path / "traces").mkdir()
    with zipfile.ZipFile(tmp_path / "app_2026-08-24.log.zip", "w") as zf:
        zf.writestr(
            "app_2026-08-24.log",
            "2026-08-24 23:59:01.000 | ERROR    | fluentytdl.a:f:1 - 昨晚那次失败\n",
        )
    (tmp_path / "app_2026-08-25.log").write_text(
        "2026-08-25 00:00:02.000 | INFO     | fluentytdl.b:g:2 - 今天第一条\n",
        encoding="utf-8",
    )

    was_installed = log_signal_handler.is_installed
    d = LogViewerWindow(host)
    try:
        text = d.logView.toPlainText()
        assert "昨晚那次失败" in text
        assert "今天第一条" in text
        # 旧文件排在前面：budget 是从新往旧凑的，拼回去时必须反过来
        assert text.index("昨晚那次失败") < text.index("今天第一条")
        assert "[ERROR" in text and "23:59:01" in text
    finally:
        d._stop_log_capture()
        if not was_installed:
            log_signal_handler.uninstall()
        d.deleteLater()


def test_window_has_two_pages_and_switches_them(window):
    assert window.stack.count() == 2
    assert window.stack.currentIndex() == 0
    assert window.exportBtn.isHidden()  # 文本页没有"选中的任务"可导

    window.viewSwitcher.setCurrentItem("timeline")
    assert window.stack.currentIndex() == 1
    assert not window.exportBtn.isHidden()

    window.viewSwitcher.setCurrentItem("text")
    assert window.stack.currentIndex() == 0
    assert window.exportBtn.isHidden()


def test_window_routes_events_to_the_timeline(window):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    window._on_event_received(_event("stage", trace=trace))
    window._on_event_received(_event("diagnosis", level="ERROR", trace=trace, code="boom"))

    assert window.timelineView.event_count() == 2
    window.viewSwitcher.setCurrentItem("timeline")
    assert "2" in window.lineCountLabel.text()


def test_window_filter_applies_to_both_pages(window):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    window._on_log_received("12:00:00", "INFO", "fluentytdl.a", "普通信息")
    window._on_log_received("12:00:01", "ERROR", "fluentytdl.a", "出错了")
    window._on_event_received(_event("stage", trace=trace))
    window._on_event_received(_event("diagnosis", level="ERROR", trace=trace, code="boom"))

    window.levelCombo.setCurrentText("ERROR")
    assert "出错了" in window.logView.toPlainText()
    assert "普通信息" not in window.logView.toPlainText()
    visible = [lbl for item in _tops(window.timelineView) for _, lbl in _visible_labels(item)]
    assert visible[-1] == "diagnosis"
    assert "stage" not in visible


def test_window_clear_wipes_both_pages(window):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    window._on_log_received("12:00:00", "INFO", "fluentytdl.a", "一行")
    window._on_event_received(_event("stage", trace=trace))

    window._clear_log()
    assert window.logView.toPlainText() == ""
    assert window.timelineView.event_count() == 0


def test_window_export_without_a_selection_warns_instead_of_exporting(window, monkeypatch):
    """没选中任务时按导出：给提示，不许拿 `task=-` 去打一个空 zip。"""
    calls: list[str] = []
    monkeypatch.setattr(
        window_mod.InfoBar, "warning", classmethod(lambda cls, **kw: calls.append(kw["title"]))
    )
    from fluentytdl.observability import bundle as bundle_mod

    monkeypatch.setattr(
        bundle_mod,
        "export_bug_bundle",
        lambda *a, **kw: pytest.fail("没有选中任务却调用了导出"),
    )
    window._export_bundle()
    assert calls


def test_window_export_passes_the_selected_identity(window, monkeypatch, tmp_path):
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    window._on_event_received(_event("outcome", stage="finalize", trace=trace, outcome="success"))
    tv = window.timelineView
    tv.setCurrentItem(tv.topLevelItem(0).child(0))  # task 42 分组

    seen: dict = {}
    fake_zip = tmp_path / "fluentytdl-bug-task42.zip"
    fake_zip.write_bytes(b"PK")

    from fluentytdl.observability import bundle as bundle_mod

    def _fake_export(task_id, dest=None, *, flow_id=None):
        seen.update(task_id=task_id, flow_id=flow_id)
        return fake_zip

    monkeypatch.setattr(bundle_mod, "export_bug_bundle", _fake_export)
    monkeypatch.setattr(
        window_mod.InfoBar, "success", classmethod(lambda cls, **kw: seen.setdefault("ok", True))
    )

    window._export_bundle()
    assert seen == {"task_id": "42", "flow_id": "k72f", "ok": True}


def test_initial_size_follows_the_reference_window(window):
    """初始尺寸跟着主窗口走，别再钉死 900×600。

    900 宽的后果是这一页最值钱的「详情」列（`key=value` 串）在宽屏上被挤成
    `code=rate_limi…`，而主窗口的硬最小宽是 1150，本来能更宽。

    断言打在纯函数 `_initial_size()` 上而不是窗口实际几何：offscreen 平台的屏幕只有
    800×800 左右，`_apply_initial_geometry()` 会把结果夹回可用区，拿实际几何断言等于
    在测那块假屏幕有多大。
    """
    assert LogViewerWindow._initial_size(QSize(1400, 900)) == (
        int(1400 * LogViewerWindow.SIZE_RATIO),
        int(900 * LogViewerWindow.SIZE_RATIO),
    )


def test_initial_size_is_clamped_at_both_ends():
    """上限：4K 屏上铺满 86% 会让详情列长到要转头才读得完。下限：再小四列排不开。"""
    (w_min, w_max), (h_min, h_max) = LogViewerWindow.SIZE_BOUNDS

    assert LogViewerWindow._initial_size(QSize(4000, 3000)) == (w_max, h_max)
    assert LogViewerWindow._initial_size(QSize(400, 300)) == (w_min, h_min)


def test_window_opens_inside_the_screen(window):
    """开在屏幕里。

    非模态窗口没有遮罩兜着：主窗口可以横跨两块屏、也可以有一半拖到屏幕外，直接照它
    的中心摆会把日志窗口开到看不见的地方 —— 用户既看不见，也不知道为什么点了没反应。
    """
    avail = window.screen().availableGeometry()
    assert avail.contains(window.geometry())


def test_window_can_be_shrunk_to_its_own_minimum(window):
    """`MIN_SIZE` 比 `SIZE_BOUNDS` 的下限更小是故意的。

    后者是"我替你选的初始大小"，前者是"再小就没法用了" —— 用户想把窗口缩到屏幕角上
    只留时间线时，不该被初始尺寸的下限挡住。
    """
    assert window.minimumSize().toTuple() == LogViewerWindow.MIN_SIZE
    assert LogViewerWindow.MIN_SIZE[0] < LogViewerWindow.SIZE_BOUNDS[0][0]
    assert LogViewerWindow.MIN_SIZE[1] < LogViewerWindow.SIZE_BOUNDS[1][0]


def test_text_page_document_is_capped(window):
    """文档行数必须封顶。

    改成独立窗口之后它可以开着几个小时，而 `_log_buffer` 的 `maxlen` 只管缓冲区、
    管不到已经插进文档里的行 —— 少了这个上限，一次长时间的批量下载能把这个文本框
    喂到几十万行。
    """
    assert window.logView.document().maximumBlockCount() == LogViewerWindow.MAX_LINES


def test_closing_the_window_detaches_the_log_signals(window):
    """关窗必须先摘信号，再交给 `WA_DeleteOnClose` 销毁。

    loguru 的 sink 是 `enqueue=True`（跨线程），队列里可能还压着几条要投给已死控件的
    记录。这里刻意不 `processEvents()`：`close()` 是同步跑完 `closeEvent` 的，
    `deleteLater()` 只是排了队 —— 于是能在对象还活着的时候验证信号真的断开了，
    而不是被 C++ 对象销毁顺带断开的（那样这条断言就什么也没测）。
    """
    window.show()
    window.close()

    before = len(window._log_buffer)
    log_signal_handler.log_received.emit("12:00:02", "INFO", "fluentytdl.a", "关窗之后")
    assert len(window._log_buffer) == before


def test_initial_level_filter_reaches_the_timeline_page(window):
    """下拉框写的级别必须和树里看到的东西一致。

    原先构造时只 `load_recent()`、不 `apply_filter()`：下拉框显示一个级别、树里却是
    全量事件，用户得先随便动一下控件才会一致 —— 在此之前界面在说谎。
    """
    assert window.timelineView._predicate is not None


def test_default_level_keeps_the_debug_decision_chain_visible(window):
    """默认级别必须放过 DEBUG。

    决策链（`kind=decision`：bcp47 展开 / 格式打分 / 音轨注入）刻意打在 DEBUG ——
    它高频，不该盖住正常日志。可要是默认筛到 INFO，时间线页一打开就把这一整层滤掉，
    而"字幕为什么没下到"的答案恰恰只在那一层。
    """
    trace = TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")
    window._on_event_received(_event("decision", level="DEBUG", trace=trace, subsystem="bcp47"))

    visible = [lbl for item in _tops(window.timelineView) for _, lbl in _visible_labels(item)]
    assert "decision" in visible
