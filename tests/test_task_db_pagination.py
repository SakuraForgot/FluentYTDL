"""`TaskDB.query_tasks` 的 keyset 分页与 `count_tasks_by_state` 的一致性。

这是阶段 3「行来源统一」的地基：任务列表要靠 `fetchMore()` 一页页把历史行补进来，
分页一旦漏行或重复行，用户看到的就是「历史少了一条」或「同一条出现两次」——
两种都不可能在 GUI 里被稳定复现，所以必须在这一层锁死。

**每个用例都连临时 db 文件**（`TaskDB(db_path=...)` 会绕过单例），不碰用户数据。
不需要 QApplication，可在无头 CI 跑。
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 导入 storage.task_db 会在模块级执行 `task_db = TaskDB()`，那会按真实规则解析出
# 用户的数据目录。先把数据根指到临时目录，导入副作用就落不到用户机器上。
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-taskdb-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)

from fluentytdl.storage.task_db import (  # noqa: E402
    TERMINAL_STATES,
    UNFINISHED_STATES,
    TaskDB,
)


@pytest.fixture
def db(tmp_path):
    """连到临时文件的独立 TaskDB 实例。"""
    return TaskDB(db_path=tmp_path / "tasks.db")


def _seed(db, rows):
    """按 `(state, updated_at)` 序列建表内容，返回 id 列表（插入顺序）。

    `insert_task` 只写 url/state=queued/时间戳，状态靠 `update_task_status` 补 ——
    那也是生产里唯一改这两列的路径。`updated_at` 则要**可控且故意制造并列**，
    而生产接口一律写 `time.time()`，所以用例自己开一条 sqlite 连接改这一列：
    比让测试去碰 `TaskDB._conn` 干净（私有连接对新代码是禁区）。
    """
    ids = []
    for i, (state, updated_at) in enumerate(rows):
        task_id = db.insert_task(f"https://example.com/v{i}", {"format": "best"})
        db.update_task_status(task_id, state, 0.0, "")
        ids.append((task_id, updated_at))

    conn = sqlite3.connect(str(db.db_path))
    try:
        conn.executemany(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            [(ts, tid) for tid, ts in ids],
        )
        conn.commit()
    finally:
        conn.close()
    return [tid for tid, _ in ids]


def _page_all(db, states, page_size):
    """按 page_size 一页页翻完，返回 (全部行的 id 列表, 页数)。"""
    seen: list[int] = []
    key = None
    pages = 0
    while True:
        rows = db.query_tasks(states=states, limit=page_size, before_key=key)
        if not rows:
            break
        pages += 1
        seen.extend(int(r["id"]) for r in rows)
        key = TaskDB.page_key(rows[-1])
        if len(rows) < page_size:
            break
        # 防呆：分页写错时不要把测试跑成死循环
        assert pages < 100, "分页没有收敛"
    return seen, pages


def test_empty_db_yields_nothing(db):
    assert db.query_tasks(states=TERMINAL_STATES) == []
    assert db.count_tasks_by_state() == {}


def test_orders_by_updated_at_desc(db):
    ids = _seed(db, [("completed", 100.0), ("completed", 300.0), ("completed", 200.0)])
    rows = db.query_tasks(states=("completed",), limit=10)
    assert [int(r["id"]) for r in rows] == [ids[1], ids[2], ids[0]]


def test_paging_covers_every_row_exactly_once(db):
    """翻页无重复、无遗漏 —— 分页的核心契约。"""
    ids = _seed(db, [("completed", float(1000 - i)) for i in range(25)])
    for page_size in (1, 2, 7, 25, 100):
        seen, _ = _page_all(db, ("completed",), page_size)
        assert seen == ids, f"page_size={page_size} 顺序或内容不符"
        assert len(set(seen)) == len(seen), f"page_size={page_size} 出现重复行"


def test_tied_updated_at_does_not_lose_rows(db):
    """`updated_at` 完全并列时也不许漏行。

    批量恢复 / 批量删除会让十几行共享同一个 `time.time()`，只按 updated_at 做游标
    会在并列处整段跳过。这正是排序键必须带上 id 的原因。
    """
    ids = _seed(db, [("completed", 500.0) for _ in range(10)])
    seen, pages = _page_all(db, ("completed",), 3)
    assert sorted(seen) == sorted(ids)
    assert len(set(seen)) == 10
    assert pages == 4  # 3 + 3 + 3 + 1


def test_page_boundary_exact_multiple(db):
    """行数刚好是页大小整数倍时，最后一次查询返回空而不是重复上一页。"""
    ids = _seed(db, [("completed", float(100 - i)) for i in range(6)])
    first = db.query_tasks(states=("completed",), limit=3)
    second = db.query_tasks(states=("completed",), limit=3, before_key=TaskDB.page_key(first[-1]))
    third = db.query_tasks(states=("completed",), limit=3, before_key=TaskDB.page_key(second[-1]))
    assert [int(r["id"]) for r in first + second] == ids
    assert third == []


def test_state_filter_is_exclusive(db):
    _seed(
        db,
        [
            ("completed", 300.0),
            ("error", 250.0),
            ("cancelled", 200.0),
            ("paused", 150.0),
            ("queued", 100.0),
        ],
    )
    terminal = db.query_tasks(states=TERMINAL_STATES, limit=50)
    assert {r["state"] for r in terminal} == {"completed", "error", "cancelled"}

    unfinished = db.query_tasks(states=UNFINISHED_STATES, limit=50)
    assert {r["state"] for r in unfinished} == {"paused", "queued"}

    # 两组互不相交且合起来就是全表 —— 阶段 3 的行来源所有权划分依赖这一点
    assert not (set(TERMINAL_STATES) & set(UNFINISHED_STATES))
    assert len(terminal) + len(unfinished) == 5


def test_states_none_means_unrestricted_but_empty_means_empty(db):
    _seed(db, [("completed", 200.0), ("paused", 100.0)])
    assert len(db.query_tasks(states=None, limit=50)) == 2
    # 空序列是「没有任何状态被选中」，不是「不限状态」
    assert db.query_tasks(states=(), limit=50) == []
    assert db.query_tasks(states=[], limit=50) == []


def test_limit_zero_and_negative_are_harmless(db):
    _seed(db, [("completed", 100.0)])
    assert db.query_tasks(states=("completed",), limit=0) == []
    assert db.query_tasks(states=("completed",), limit=-5) == []


def test_count_matches_row_by_row_tally(db):
    rows = (
        [("completed", float(500 - i)) for i in range(7)]
        + [("error", float(400 - i)) for i in range(3)]
        + [("paused", float(300 - i)) for i in range(2)]
    )
    _seed(db, rows)

    expected: dict[str, int] = {}
    for state, _ in rows:
        expected[state] = expected.get(state, 0) + 1

    assert db.count_tasks_by_state() == expected
    # 与逐行统计一致（用不同的取数路径交叉验证）
    tally: dict[str, int] = {}
    for row in db.get_all_tasks():
        tally[row["state"]] = tally.get(row["state"], 0) + 1
    assert db.count_tasks_by_state() == tally


def test_count_follows_deletion(db):
    ids = _seed(db, [("completed", 200.0), ("completed", 100.0), ("error", 50.0)])
    db.delete_task(ids[0])
    assert db.count_tasks_by_state() == {"completed": 1, "error": 1}
    assert db.delete_tasks(ids[1:]) == 2
    assert db.count_tasks_by_state() == {}


def test_delete_tasks_empty_is_noop(db):
    _seed(db, [("completed", 100.0)])
    assert db.delete_tasks([]) == 0
    assert db.count_tasks_by_state() == {"completed": 1}


def test_batch_commits_once_and_rolls_back_on_error(db):
    ids = _seed(db, [("queued", 100.0), ("queued", 90.0)])

    with db.batch():
        db.update_task_status(ids[0], "completed", 1.0, "ok")
        db.update_task_status(ids[1], "completed", 1.0, "ok")
    assert db.count_tasks_by_state() == {"completed": 2}

    with pytest.raises(RuntimeError):
        with db.batch():
            db.update_task_status(ids[0], "error", 0.0, "boom")
            raise RuntimeError("boom")
    # 整批回滚：第一条的 error 不许留下
    assert db.count_tasks_by_state() == {"completed": 2}


def test_nested_batch_does_not_deadlock(db):
    """`batch()` 里再调用一个自带 `batch()` 的方法必须穿透，不能自死锁。

    `_write_lock` 是不可重入的 `threading.Lock` —— 少了穿透判断，这个用例会挂死
    而不是失败，所以它同时也是「别把穿透逻辑删掉」的守卫。
    """
    ids = _seed(db, [("queued", 100.0), ("queued", 90.0)])
    with db.batch():
        db.update_task_status(ids[0], "completed", 1.0, "ok")
        db.delete_tasks([ids[1]])  # 内部也用 batch()
    assert db.count_tasks_by_state() == {"completed": 1}
