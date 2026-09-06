"""`TaskDBWriter` 的批量合并语义。

关注两条会在生产里咬人的性质：
1. 同一 `(op, db_id)` 只写最后一条 —— 5Hz 的进度更新要塌成 1 条 UPDATE；
2. 合并后**不同 op 的相对先后不能乱** —— `metadata` 与 `result` 都写 output_path，
   顺序反过来会让已完成任务的最终路径被解析期的空路径覆盖。

第 2 条是纯逻辑却极易在「顺手用 dict 去重」时写错（dict 重新赋值保留的是**旧**位置），
所以单列一个用例钉住。不起线程、不碰用户数据。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 导入 storage 会在模块级建 TaskDB 单例与 TaskDBWriter 线程，先把数据根指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-dbwriter-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)

from fluentytdl.storage import db_writer as db_writer_mod  # noqa: E402
from fluentytdl.storage.task_db import TaskDB  # noqa: E402

TaskDBWriter = db_writer_mod.TaskDBWriter


def test_coalesce_keeps_only_last_per_key():
    items = [
        ("status", 1, "downloading", 0.1, "10%"),
        ("status", 1, "downloading", 0.5, "50%"),
        ("status", 2, "downloading", 0.2, "20%"),
        ("status", 1, "completed", 1.0, "done"),
    ]
    merged = TaskDBWriter._coalesce(items)
    assert len(merged) == 2
    by_id = {item[1]: item for item in merged}
    assert by_id[1] == ("status", 1, "completed", 1.0, "done")
    assert by_id[2] == ("status", 2, "downloading", 0.2, "20%")


def test_coalesce_keeps_distinct_ops_for_same_row():
    items = [
        ("metadata", 7, "title", "thumb"),
        ("status", 7, "downloading", 0.3, "30%"),
        ("result", 7, "C:/out.mp4", 1024),
        ("quality", 7, 720, 1080, "lower"),
    ]
    merged = TaskDBWriter._coalesce(items)
    assert [item[0] for item in merged] == ["metadata", "status", "result", "quality"]


def test_coalesce_preserves_order_of_last_occurrence():
    """幸存者之间的先后必须跟原队列一致。

    `merged[key] = item` 单独用会把 result 留在 metadata **之前**（dict 重新赋值
    不移动位置），于是 metadata 的空 output_path 反过来覆盖最终路径。
    """
    items = [
        ("result", 3, "C:/first.mp4", 10),
        ("metadata", 3, "title", "thumb"),
        ("result", 3, "C:/final.mp4", 99),
    ]
    merged = TaskDBWriter._coalesce(items)
    assert [item[0] for item in merged] == ["metadata", "result"]
    assert merged[-1] == ("result", 3, "C:/final.mp4", 99)


@pytest.fixture
def writer_db(tmp_path, monkeypatch):
    """把 db_writer 模块里的 task_db 换成临时库，并给出一个不起线程的 writer。"""
    db = TaskDB(db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(db_writer_mod, "task_db", db)
    writer = TaskDBWriter.__new__(TaskDBWriter)  # 不跑 __init__，不起后台线程
    return writer, db


def test_process_batch_applies_every_distinct_write(writer_db):
    writer, db = writer_db
    a = db.insert_task("https://example.com/a", {})
    b = db.insert_task("https://example.com/b", {})

    writer._process_batch(
        [
            ("status", a, "downloading", 0.4, "40%"),
            ("status", a, "completed", 1.0, "完成"),
            ("metadata", a, "标题 A", "thumb-a"),
            ("result", a, "C:/a.mp4", 2048),
            ("status", b, "error", 0.0, "失败"),
        ]
    )

    row_a = db.get_task(a)
    assert row_a is not None
    assert row_a["state"] == "completed"
    assert row_a["progress"] == 1.0
    assert row_a["status_text"] == "完成"
    assert row_a["title"] == "标题 A"
    assert row_a["output_path"] == "C:/a.mp4"
    assert row_a["file_size"] == 2048

    row_b = db.get_task(b)
    assert row_b is not None
    assert row_b["state"] == "error"

    assert db.count_tasks_by_state() == {"completed": 1, "error": 1}


def test_process_batch_single_item_bypasses_transaction(writer_db):
    writer, db = writer_db
    task_id = db.insert_task("https://example.com/x", {})
    writer._process_batch([("status", task_id, "paused", 0.5, "暂停")])
    row = db.get_task(task_id)
    assert row is not None
    assert row["state"] == "paused"


def test_process_batch_empty_is_noop(writer_db):
    writer, db = writer_db
    writer._process_batch([])
    assert db.count_tasks_by_state() == {}


def test_unknown_op_does_not_abort_the_rest(writer_db):
    """一条坏数据不许连带丢掉同批的状态更新。"""
    writer, db = writer_db
    task_id = db.insert_task("https://example.com/y", {})
    writer._process_batch(
        [
            ("bogus", task_id, "???"),
            ("status", task_id, "completed", 1.0, "完成"),
        ]
    )
    row = db.get_task(task_id)
    assert row is not None
    assert row["state"] == "completed"


def test_collect_stops_at_poison_pill(writer_db):
    writer, _ = writer_db
    from queue import Queue

    writer._queue = Queue()
    writer._queue.put(("status", 2, "downloading", 0.2, ""))
    writer._queue.put(None)
    writer._queue.put(("status", 3, "downloading", 0.3, ""))

    batch, poisoned = writer._collect(("status", 1, "downloading", 0.1, ""))
    assert poisoned is True
    assert [item[1] for item in batch] == [1, 2]


def test_collect_honours_batch_max(writer_db):
    writer, _ = writer_db
    from queue import Queue

    writer._queue = Queue()
    for i in range(TaskDBWriter._BATCH_MAX + 50):
        writer._queue.put(("status", i, "downloading", 0.0, ""))

    batch, poisoned = writer._collect(("status", -1, "downloading", 0.0, ""))
    assert poisoned is False
    assert len(batch) == TaskDBWriter._BATCH_MAX
    # first 是参数带进来的，所以只从队列里取了 _BATCH_MAX - 1 条
    assert writer._queue.qsize() == 51
