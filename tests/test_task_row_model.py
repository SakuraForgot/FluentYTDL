"""融合后的行结构（`TaskRow`）与 `DownloadListModel` 的无头测试。

阶段 3 把「下载列表」和「历史记录」收成一条列表，行因此有两种来源：**有 worker 的
活任务**与**只有 DB 快照的历史行**。这里钉住三件在 GUI 里不可能稳定复现的性质：

1. **`db_id` 去重** —— 本次会话下完的任务同时满足「活任务」和「终态历史行」两个身份，
   分页补进来时必须**就地给已有行挂 worker**、不能新增一行（融合前「同一个任务被列
   两次」就是这么来的）；
2. **读穿透** —— `effective_*` 有 worker 问 worker、没有就用快照，而 worker 上的
   `progress_val` / `total_bytes` 是**第一次进度回调才创建**的，读的时候可能还不存在；
3. **文件存在性只对 `completed` 生效** —— delegate 见到 `file_exists is False` 就整行
   降透明且**不看状态**，所以一旦把失败行也拿去 stat，每条失败记录都会变成半透明。

只需要 `QCoreApplication`（不需要显示），DB 一律指向 `tmp_path`，不碰用户数据。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 导入 storage / ui 会按真实规则解析用户数据目录，先把数据根指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-taskrow-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Signal  # noqa: E402

from fluentytdl.storage.task_db import TaskDB  # noqa: E402
from fluentytdl.ui.models import download_list_model as dlm_mod  # noqa: E402
from fluentytdl.ui.models.download_list_model import DownloadListModel  # noqa: E402
from fluentytdl.ui.models.file_probe import FileExistenceProbe, probe_paths  # noqa: E402
from fluentytdl.ui.models.task_row import TaskRow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    """`QTimer` / `QThreadPool` 需要一个 QCoreApplication；不需要 QApplication。"""
    return QCoreApplication.instance() or QCoreApplication([])


class FakeWorker(QObject):
    """最小 worker：够 `_bind_worker_signals` 连接、够 `TaskRow` 读穿透。

    **故意不预置** `progress_val` / `status_text` / `total_bytes` —— 真实
    `DownloadWorker` 也要等第一次 `_on_clean_update` 才建这些属性，所以读穿透必须
    `getattr` 带默认值，否则新建任务的第一帧就是 AttributeError。
    """

    progress = Signal(float)
    status_msg = Signal(str)
    unified_status = Signal(str, float, str)
    completed = Signal()
    error = Signal(dict)
    cancelled = Signal()

    def __init__(self, db_id=0, url="", state="running", opts=None, **attrs):
        super().__init__()
        self.db_id = int(db_id)
        self.url = url
        self.opts = opts or {}
        self.effective_state = state
        for name, value in attrs.items():
            setattr(self, name, value)


class RecordingProbe:
    """替掉真 `FileExistenceProbe`：只记请求，不起线程。"""

    def __init__(self):
        self.requested: list[list[tuple[int, str]]] = []
        self.forgotten: list[int] = []
        self.cleared = 0

    def request(self, items):
        self.requested.append(list(items))

    def forget(self, db_id):
        self.forgotten.append(int(db_id))

    def clear(self):
        self.cleared += 1

    def ids(self) -> set[int]:
        return {db_id for batch in self.requested for db_id, _ in batch}


@pytest.fixture
def db(tmp_path):
    """连临时文件的独立 TaskDB（`db_path=` 会绕过单例）。"""
    return TaskDB(db_path=tmp_path / "tasks.db")


@pytest.fixture
def model(db, monkeypatch):
    """模型 + 被替掉的 probe。`fetchMore` 读模块级 `task_db`，一并指向临时库。"""
    monkeypatch.setattr(dlm_mod, "task_db", db)
    m = DownloadListModel()
    m._probe = RecordingProbe()
    return m


def _seed_completed(db, count, out_dir="C:/out"):
    """建 `count` 条已完成行，返回 id 列表（按插入顺序，`updated_at` 递增）。"""
    ids = []
    for i in range(count):
        tid = db.insert_task(f"https://example.com/v{i}", {"format": "bv[height<=1080]+ba"})
        db.update_task_metadata(
            tid,
            f"标题 {i}",
            "",
            output_path=f"{out_dir}/v{i}.mp4",
            file_size=1024 * (i + 1),
            duration=60,
        )
        db.update_task_status(tid, "completed", 1.0, "✅ 完成")
        ids.append(tid)
    return ids


# === probe_paths ===


def test_probe_paths_maps_existence(tmp_path):
    present = tmp_path / "has.mp4"
    present.write_bytes(b"x")
    missing = tmp_path / "gone.mp4"
    # 空路径算「不存在」：老记录没落下 output_path，调用方靠 `_probe_rows` 提前跳过
    result = probe_paths([(1, str(present)), (2, str(missing)), (3, "")])
    assert result == {1: True, 2: False, 3: False}


def test_probe_paths_empty_input():
    assert probe_paths([]) == {}


def test_probe_delivers_int_keys_through_the_signal(tmp_path, qt_app):
    """真 `FileExistenceProbe` 端到端跑一遍 —— 上面两条只测了纯函数。

    钉的是**信号的载荷类型**，不是 stat 逻辑：结论的键是 `db_id`（int），而
    `Signal(dict)` 在 PySide6 里映射成 `QVariantMap`（`QMap<QString, QVariant>`），
    非字符串键会被**静默丢成空 dict** —— 槽照样被调用，`{}` 里没有任何行，于是
    `TaskRow.file_exists` 永远停在 `None`，「文件丢失」筛选永远筛不出东西，
    而 stderr 只有一句 `Cannot copy-convert ... (dict) to C++.`。
    模型的用例全都用 `RecordingProbe` 顶掉了真 probe，所以这条路只能在这里覆盖。
    """
    present = tmp_path / "has.mp4"
    present.write_bytes(b"x")
    missing = tmp_path / "gone.mp4"

    probe = FileExistenceProbe()
    received: list[dict] = []
    probe.checked.connect(received.append)
    probe.request([(11, str(present)), (22, str(missing))])

    loop = QEventLoop()
    probe.checked.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()

    assert received, "5 秒内没收到 checked —— 后台批次没回来"
    assert received[0] == {11: True, 22: False}
    assert [type(k) for k in received[0]] == [int, int], "键被转成了别的类型"
    # `_inflight` 靠回传的键来清；键丢了它就永远不空，那一行此后再也排不进检查
    assert probe.pending_count() == 0


# === TaskRow.from_db_row ===


def test_from_db_row_maps_columns(db):
    [tid] = _seed_completed(db, 1)
    row = TaskRow.from_db_row(db.get_task(tid))
    assert row.db_id == tid
    assert row.url == "https://example.com/v0"
    assert row.title == "标题 0"
    assert row.state == "completed"
    assert row.progress == 1.0
    assert row.output_path == "C:/out/v0.mp4"
    assert row.file_size == 1024
    assert row.duration == 60
    assert row.updated_at > 0
    assert row.worker is None
    assert row.file_exists is None


def test_from_db_row_derives_format_note_from_opts(db):
    """历史行没有 worker，画质标签只能从落库的 ydl_opts + 输出扩展名反推。"""
    [tid] = _seed_completed(db, 1)
    row = TaskRow.from_db_row(db.get_task(tid))
    assert row.format_note == "1080p MP4"


def test_from_db_row_tolerates_invalid_json():
    """`ydl_opts_json` 是用户机器上的历史数据，不能假设它一定是合法 JSON。"""
    row = TaskRow.from_db_row({"id": 7, "ydl_opts_json": "{not json", "output_path": "a.mkv"})
    assert row.db_id == 7
    assert row.format_note == "MKV"


def test_from_db_row_normalizes_zero_heights():
    """质量偏差的高度列 DB 默认值是 0，语义上等同「没记录」。"""
    row = TaskRow.from_db_row(
        {"id": 1, "actual_height": 0, "target_height": 1080, "quality_deviation": ""}
    )
    assert row.actual_height is None
    assert row.target_height == 1080
    assert row.quality_deviation is None


def test_from_db_row_defaults_for_empty_mapping():
    row = TaskRow.from_db_row({})
    assert row.db_id == 0
    assert row.state == "queued"
    assert row.format_note == ""


# === 读穿透（effective_*）===


def test_effective_uses_snapshot_without_worker(db):
    [tid] = _seed_completed(db, 1)
    row = TaskRow.from_db_row(db.get_task(tid))
    assert row.is_live is False
    assert row.effective_state == "completed"
    assert row.effective_progress == 1.0
    assert row.effective_output_path == "C:/out/v0.mp4"
    assert row.effective_file_size == 1024
    assert row.effective_title == "标题 0"


def test_effective_reads_through_worker(db):
    """worker 比 DB 新（`db_writer` 是异步落库的），有 worker 就以它为准。"""
    [tid] = _seed_completed(db, 1)
    row = TaskRow.from_db_row(db.get_task(tid))
    worker = FakeWorker(
        db_id=tid,
        state="running",
        progress_val=42.5,
        status_text="下载中 42%",
        total_bytes=4096,
        v_title="真标题",
        v_thumbnail="https://img/new.jpg",
        output_path="C:/live/v0.part",
    )
    row.attach_worker(worker)
    assert row.is_live is True
    assert row.effective_state == "running"
    assert row.effective_progress == 42.5
    assert row.effective_status_text == "下载中 42%"
    assert row.effective_file_size == 4096
    assert row.effective_title == "真标题"
    assert row.effective_thumbnail == "https://img/new.jpg"
    assert row.effective_output_path == "C:/live/v0.part"


def test_effective_survives_worker_without_lazy_attrs():
    """`progress_val` / `status_text` / `total_bytes` 要等第一次进度回调才存在。"""
    row = TaskRow(db_id=1, progress=0.5, file_size=99, title="快照", thumbnail="snap.jpg")
    row.attach_worker(FakeWorker(db_id=1, state="queued"))
    # 进度以 worker 为唯一权威：刚重建的 worker 就是从 0 开始的
    assert row.effective_progress == 0.0
    assert row.effective_status_text == ""
    # 大小 / 标题 / 封面则是「worker 给不出非空值就退回快照」，不能让 0 覆盖已知值
    assert row.effective_file_size == 99
    assert row.effective_title == "快照"
    assert row.effective_thumbnail == "snap.jpg"


def test_effective_format_note_recomputed_when_output_path_appears():
    """建行时输出路径还是空的（「1080p」少了容器后缀），拿到路径后要补上并缓存。"""
    worker = FakeWorker(db_id=1, opts={"format": "bv[height<=1080]+ba"})
    row = TaskRow.from_worker(worker)
    assert row.effective_format_note == "1080p"

    worker.output_path = "D:/dl/video.mp4"
    assert row.effective_format_note == "1080p MP4"
    assert row._note_src == "D:/dl/video.mp4"

    # 路径没变就不再重算 —— 否则滚动时每帧每行一个正则 + 一次 splitext
    worker.opts = {"format": "bv[height<=720]"}
    assert row.effective_format_note == "1080p MP4"


def test_attach_worker_resets_existence_and_note_cache():
    row = TaskRow(db_id=5, state="completed", output_path="D:/old.mp4", format_note="720p MP4")
    row.file_exists = False
    row._note_src = "D:/old.mp4"
    row.attach_worker(FakeWorker(db_id=5, state="running", opts={"format": "bv[height<=2160]"}))
    # 要重下：旧的「文件已丢失」结论作废，画质标签按新 opts 重算（用户可能换了档位）
    assert row.file_exists is None
    assert row.effective_format_note == "2160p MP4"


# === DownloadListModel：db_id 去重与就地升级 ===


def test_add_task_inserts_at_top(model):
    model.add_task(FakeWorker(db_id=1, url="u1"), "第一个", "")
    model.add_task(FakeWorker(db_id=2, url="u2"), "第二个", "")
    assert model.rowCount() == 2
    assert model.get_task(0).db_id == 2  # 新的在最上面
    assert model.row_of_db_id(1) == 1


def test_add_task_upgrades_history_row_in_place(model, db):
    """融合的核心：分页先补进了历史行，用户随后重试它 —— 挂 worker，不新增行。"""
    ids = _seed_completed(db, 3)
    model.fetchMore()
    assert model.rowCount() == 3
    before = model.get_task(model.row_of_db_id(ids[1]))
    assert before.worker is None

    worker = FakeWorker(db_id=ids[1], state="running")
    model.add_task(worker, "重试标题", "")

    assert model.rowCount() == 3
    row_obj = model.get_task(model.row_of_db_id(ids[1]))
    assert row_obj is before
    assert row_obj.worker is worker
    assert row_obj.title == "重试标题"
    # 重下了，已排队的存在性检查要撤销
    assert ids[1] in model._probe.forgotten


def test_fetch_more_skips_rows_already_live(model, db):
    """本次会话下完的任务此刻两边都算得上，以已有的活任务行为准。"""
    ids = _seed_completed(db, 3)
    live = FakeWorker(db_id=ids[1], state="running")
    model.add_task(live, "活的", "")
    model.fetchMore()

    assert model.rowCount() == 3
    assert sorted(model.get_task(r).db_id for r in range(3)) == sorted(ids)
    assert model.get_task(model.row_of_db_id(ids[1])).worker is live
    assert model.canFetchMore() is False


def test_fetch_more_ignores_unfinished_states(model, db):
    """未完成态归 `load_unfinished_tasks()` 注入，分页只许取终态 —— 否则同一个任务
    会既是活任务又是历史行。"""
    ids = _seed_completed(db, 2)
    paused = db.insert_task("https://example.com/paused", {})
    db.update_task_status(paused, "paused", 0.3, "⏸️ 已暂停")
    model.fetchMore()

    listed = sorted(model.get_task(r).db_id for r in range(model.rowCount()))
    assert listed == sorted(ids)
    assert model.row_of_db_id(paused) == -1


def test_fetch_more_scans_forward_when_a_whole_page_is_already_live(model, db, monkeypatch):
    """一整页全是已在列的活任务时插入 0 行，视图就不会再触发 fetchMore —— 必须就地续查。"""
    ids = _seed_completed(db, 4)
    monkeypatch.setattr(DownloadListModel, "_PAGE_SIZE", 2)
    # 最新的两条（正好是分页第一页）先作为活任务入列
    for db_id in ids[-2:]:
        model.add_task(FakeWorker(db_id=db_id, state="running"), "", "")

    model.fetchMore()

    assert model.rowCount() == 4
    assert model.row_of_db_id(ids[0]) >= 0
    assert model.row_of_db_id(ids[1]) >= 0


def test_rebind_worker_relocates_index_on_new_db_id(model):
    """已结束的 QThread 不能重启，controller 会重建 worker；索引要跟着搬。"""
    model.add_task(FakeWorker(db_id=4, state="error"), "失败的", "")
    rebuilt = FakeWorker(db_id=9, state="running")
    model.rebind_worker(0, rebuilt)

    row_obj = model.get_task(0)
    assert row_obj.worker is rebuilt
    assert row_obj.db_id == 9
    assert model.row_of_db_id(9) == 0
    assert model.row_of_db_id(4) == -1


def test_rebind_worker_ignores_out_of_range_row(model):
    model.rebind_worker(3, FakeWorker(db_id=1))
    assert model.rowCount() == 0


def test_remove_task_drops_all_indexes(model, db):
    ids = _seed_completed(db, 2)
    model.fetchMore()
    model.remove_task(model.row_of_db_id(ids[0]))

    assert model.rowCount() == 1
    assert model.row_of_db_id(ids[0]) == -1
    assert ids[0] in model._probe.forgotten


def test_rows_for_thumbnail_returns_every_owner(model):
    """同一封面被多行共用时每一行都要刷新 —— 旧实现只刷第一行就 break。"""
    model.add_task(FakeWorker(db_id=1), "a", "https://img/same.jpg")
    model.add_task(FakeWorker(db_id=2), "b", "https://img/same.jpg")
    model.add_task(FakeWorker(db_id=3), "c", "https://img/other.jpg")

    assert sorted(model.rows_for_thumbnail("https://img/same.jpg")) == [1, 2]
    assert model.rows_for_thumbnail("https://img/other.jpg") == [0]
    assert model.rows_for_thumbnail("https://img/none.jpg") == []


def test_get_counts_by_state_includes_history_rows(model, db):
    """历史行没有 worker，只有快照状态；计数徽标必须把它们算进去。"""
    _seed_completed(db, 2)
    model.fetchMore()
    model.add_task(FakeWorker(db_id=999, state="running"), "活的", "")

    counts = model.get_counts_by_state()
    assert counts["all"] == 3
    assert counts["completed"] == 2
    assert counts["running"] == 1


def test_clear_resets_pagination_cursor(model, db):
    _seed_completed(db, 2)
    model.fetchMore()
    assert model.canFetchMore() is False

    model.clear()

    assert model.rowCount() == 0
    assert model._by_db_id == {}
    # 清空通常伴随 DB 行的删除，游标回到起点重新查一遍才是对的
    assert model.canFetchMore() is True
    assert model._probe.cleared == 1


# === 文件存在性：只对 completed 生效 ===


def test_probe_rows_only_targets_completed_with_path(model):
    """失败 / 取消行也拿去 stat 的话，delegate 会把每条失败记录都画成半透明。"""
    rows = [
        TaskRow(db_id=1, state="completed", output_path="C:/a.mp4"),
        TaskRow(db_id=2, state="error", output_path="C:/b.mp4"),
        TaskRow(db_id=3, state="cancelled", output_path="C:/c.mp4"),
        TaskRow(db_id=4, state="paused", output_path="C:/d.mp4"),
        TaskRow(db_id=5, state="completed", output_path=""),
        TaskRow(db_id=0, state="completed", output_path="C:/f.mp4"),
    ]
    model._probe_rows(rows)
    assert model._probe.ids() == {1}


def test_probe_rows_skips_settled_rows_unless_forced(model):
    """分页补进来的每一页只 stat 一次，滚动过程中不该反复查同一行。"""
    row = TaskRow(db_id=1, state="completed", output_path="C:/a.mp4")
    row.file_exists = True

    model._probe_rows([row])
    assert model._probe.requested == []

    model._probe_rows([row], force=True)
    assert model._probe.ids() == {1}


def test_fetch_more_probes_the_page_it_just_inserted(model, db):
    ids = _seed_completed(db, 3)
    model.fetchMore()
    assert model._probe.ids() == set(ids)


def test_rescan_existence_forces_all_completed_rows(model, db):
    ids = _seed_completed(db, 3)
    model.fetchMore()
    for db_id in ids:
        model._by_db_id[db_id].file_exists = True
    model._probe.requested.clear()

    model.rescan_existence()

    assert model._probe.ids() == set(ids)


def test_apply_existence_marks_dirty_only_on_false_flips(model, db):
    """`None → True` 在渲染上什么都不改，补完一页 100 行不该换来 100 行重绘。"""
    ids = _seed_completed(db, 2)
    model.fetchMore()
    emitted: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda a, b, roles: emitted.append((a.row(), b.row())))

    model._apply_existence({ids[0]: True})
    assert model._by_db_id[ids[0]].file_exists is True
    assert model._dirty_tasks == set()
    assert model._update_timer.isActive() is False

    # `None → False` 会让整行降透明，必须重绘（也是「文件丢失」筛选重新过滤的依据）
    model._apply_existence({ids[1]: False})
    assert model._by_db_id[ids[1]].file_exists is False
    assert len(model._dirty_tasks) == 1
    assert model._update_timer.isActive() is True

    # 走的是 150ms 聚合窗口，不是同步发射
    assert emitted == []


def test_apply_existence_ignores_rows_that_restarted(model, db):
    """结论回来时这一行可能已经在重下了；采纳旧结论会让它显示「已丢失」。"""
    [tid] = _seed_completed(db, 1)
    model.fetchMore()
    row_obj = model._by_db_id[tid]
    row_obj.attach_worker(FakeWorker(db_id=tid, state="running"))

    model._apply_existence({tid: False})

    assert row_obj.file_exists is None
    assert model._dirty_tasks == set()


def test_apply_existence_ignores_unknown_ids(model):
    model._apply_existence({4242: False})
    assert model._dirty_tasks == set()
