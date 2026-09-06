"""线程边界的异常观测（P1-E）：`threading.excepthook` + `observe_future` + 同步 ERROR sink。

三个机制补的是三个**不同**的洞，混为一谈就会漏：

| 机制 | 覆盖什么 | 不覆盖什么 |
|---|---|---|
| `sys.excepthook` | 主线程未捕获异常 | 任何子线程 |
| `threading.excepthook` | 裸 `Thread.run()` 抛出的异常 | 线程池（异常进 `Future`，线程正常结束） |
| `observe_future()` | `ThreadPoolExecutor` 里泄漏的异常 | —— |

同步 ERROR sink 是第四件事：主文件 sink 是 `enqueue=True`，进程被强杀时队列里没落盘的
那几行一起消失，而最后一条 ERROR 恰恰是最想看的。它在**子进程**里测 ——
`install_sinks()` 往全局 logger 上加 sink 且刻意幂等，在本进程调一次会污染后面所有用例。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-thr-"))

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from fluentytdl.observability import TaskTrace, observe_future, observe_futures  # noqa: E402


@pytest.fixture
def events():
    """只收带 `fytdl` extra 的记录 —— 也就是 Observability Event，不含纯文本日志行。"""
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


# ── observe_future ──────────────────────────────────────────


def _settled(exc: BaseException | None = None, result=None) -> Future:
    """一个已经结束的 Future —— `add_done_callback` 会同步跑在当前线程上。"""
    fut: Future = Future()
    fut.set_running_or_notify_cancel()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
    return fut


def test_leaked_exception_becomes_a_signal(events):
    """池内异常必须留下痕迹，而且是 `signal` 不是 `diagnosis`。

    `observe_future` 只**观测**，调用方照旧可以 `fut.result()` 再按业务逻辑分类。
    两边都发 diagnosis 就成了一次失败两条判定，`count(kind=diagnosis)` 从此不再
    等于"真正发生了错误"（硬规则 2）。
    """
    trace = TaskTrace(task_id="7")
    observe_future(_settled(RuntimeError("boom")), trace, label="shorts", stage="parse")

    sig = next(e for e in events if e["kind"] == "signal")
    assert sig["code"] == "future_exception"
    assert sig["label"] == "shorts"
    assert sig["stage"] == "parse"
    assert sig["task"] == "7"


def test_leaked_exception_is_logged_at_error():
    """落 DEBUG 等于没修 —— 那正是"静默消失"的另一种写法。"""
    levels: list[str] = []
    sink_id = logger.add(
        lambda m: levels.append(m.record["level"].name),
        level="TRACE",
        filter=lambda r: r.get("extra", {}).get("fytdl", {}).get("code") == "future_exception",
    )
    try:
        observe_future(_settled(RuntimeError("boom")), label="x")
    finally:
        logger.remove(sink_id)
    assert levels == ["ERROR"]


def test_successful_future_emits_nothing(events):
    observe_future(_settled(result={"ok": True}), label="videos")
    assert events == []


def test_cancelled_future_is_not_an_error(events):
    """取消是用户意图。记一条 DEBUG 便于对时间线，但绝不能是 ERROR。"""
    fut: Future = Future()
    assert fut.cancel()
    observe_future(fut, label="videos")

    assert [e.get("code") for e in events] == ["future_cancelled"]


def test_observe_does_not_swallow_the_exception():
    """观测不得改变 future 的行为 —— 调用方仍要能拿到原异常并自己分类。"""
    fut = observe_future(_settled(RuntimeError("boom")), label="x")
    with pytest.raises(RuntimeError, match="boom"):
        fut.result()


def test_observe_future_returns_the_same_future():
    fut = _settled(result=1)
    assert observe_future(fut) is fut


def test_observe_futures_dict_form_labels_each_tab(events):
    """`{pool.submit(...): tab}` 的写法：value 就是 label。

    没有 label 的话，三个标签页并发时日志里只剩三条"有个 future 炸了"，
    定位不到是哪个标签页 —— 这正是 `workers.py` 从 list 改成 dict 的原因。
    """
    submitted = {
        _settled(RuntimeError("a")): "videos",
        _settled(RuntimeError("b")): "shorts",
    }
    observe_futures(submitted, TaskTrace(task_id="7"), stage="parse")

    labels = sorted(e["label"] for e in events if e["kind"] == "signal")
    assert labels == ["shorts", "videos"]


def test_observe_futures_list_form_works(events):
    observe_futures([_settled(RuntimeError("a"))], stage="parse")
    assert [e["code"] for e in events] == ["future_exception"]


def test_real_pool_exception_is_observed(events):
    """真起一个池：回调跑在工作线程上（loguru 是线程安全的），不是在这里。"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = observe_future(pool.submit(lambda: 1 / 0), label="divide", stage="download")
        with pytest.raises(ZeroDivisionError):
            fut.result()

    assert "future_exception" in [e.get("code") for e in events]


def test_observe_survives_a_hostile_future():
    """硬规则 5：观测本身绝不抛，坏掉的 future 也一样。"""

    class _Hostile:
        def add_done_callback(self, _cb):
            raise RuntimeError("nope")

    hostile = _Hostile()
    assert observe_future(hostile) is hostile  # type: ignore[arg-type]


# ── threading.excepthook ────────────────────────────────────


def test_thread_excepthook_is_installed():
    """裸线程的异常以前只往 stderr 打，而发布版是 windowed 进程 —— stderr 没有归宿。"""
    from fluentytdl.utils import logger as logger_mod

    assert threading.excepthook is logger_mod.handle_thread_exception


def test_crashing_thread_is_logged_as_critical():
    """必须带上线程名：项目里 6 处裸线程，不知道是哪一个等于没记。"""
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])),
        level="TRACE",
        filter=lambda r: "uncaught exception in thread" in r["message"].lower(),
    )
    try:
        t = threading.Thread(target=lambda: 1 / 0, name="CrashMe")
        t.start()
        t.join(5)
    finally:
        logger.remove(sink_id)

    assert any(lvl == "CRITICAL" and "CrashMe" in msg for lvl, msg in records), records


def test_thread_systemexit_is_not_a_crash():
    """`SystemExit` 在线程里是正常收场手段（CPython 的默认钩子也放过它），报 CRITICAL 只是噪音。"""
    records: list[str] = []
    sink_id = logger.add(
        lambda m: records.append(m.record["message"]),
        level="TRACE",
        filter=lambda r: "uncaught exception in thread" in r["message"].lower(),
    )
    try:
        t = threading.Thread(target=lambda: sys.exit(3), name="QuietExit")
        t.start()
        t.join(5)
    finally:
        logger.remove(sink_id)

    assert not any("QuietExit" in m for m in records), records


# ── install_sinks（子进程）──────────────────────────────────

#: 在干净的子进程里跑一遍"装 sink → 发事件 → 退出"。
#:
#: **为什么不能在本进程测**：`install_sinks()` 加的是全局 logger 的 sink，且用模块级
#: `_sinks_installed` 做幂等 —— 调一次之后既撤不回，也没法在同一进程里再测一次，
#: 还会让后面每个用例都往这份 `errors_sync.log` 里写。
_SINK_PROBE = r"""
import os, sys
os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = sys.argv[1]
sys.path.insert(0, sys.argv[2])

from fluentytdl.observability import TaskTrace, emit_event, install_sinks

install_sinks()
install_sinks()          # 幂等：重复调用不得叠出第二份 sink

trace = TaskTrace(task_id="42", flow_id="k72f")
emit_event("diagnosis", trace=trace, level="ERROR", code="rate_limited_429")
emit_event("stage", trace=trace, code="just_info")

from loguru import logger
logger.remove()          # 关掉所有 sink，顺带 flush enqueue=True 的 JSONL
print("done")
"""


@pytest.fixture(scope="module")
def sink_probe(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("sinkprobe")
    script = data_dir / "probe.py"
    script.write_text(_SINK_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(data_dir), str(SRC)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "done" in proc.stdout, proc.stderr
    return data_dir


def test_sync_error_sink_captures_errors(sink_probe):
    """崩溃前最后一条 ERROR 必须已经落盘。

    主文件 sink 是 `enqueue=True`，进程被强杀时队列里的行一起消失；这个 sink
    (`enqueue=False`) 写完才返回。
    """
    log_dir = sink_probe / "logs"
    sync_log = log_dir / "errors_sync.log"
    assert sync_log.exists(), sorted(p.name for p in log_dir.glob("*"))

    text = sync_log.read_text(encoding="utf-8")
    assert "rate_limited_429" in text
    # 只收 ERROR 及以上：INFO 混进来，这个文件就失去"只看错误"的价值
    assert "just_info" not in text
    # 幂等：两次 install_sinks() 不能让同一条 ERROR 落两遍
    assert text.count("rate_limited_429") == 1, text


def test_jsonl_sink_routes_by_flow(sink_probe):
    """JSONL 以 flow 为路由键，task 只是字段。

    按 task 路由会把同一条链物理裂开（Playlist 甚至一对多），导出 bug 包必然漏掉
    最前面的解析段。
    """
    trace_dir = sink_probe / "logs" / "traces"
    trace_files = list(trace_dir.rglob("*.jsonl"))
    assert trace_files, sorted(p.name for p in (sink_probe / "logs").rglob("*"))
    assert [p.name for p in trace_files] == ["flow-k72f.jsonl"], trace_files

    entries = [
        json.loads(line)
        for p in trace_files
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [e.get("kind") for e in entries]
    assert kinds == ["diagnosis", "stage"], entries
    assert all(e["task"] == "42" and e["flow"] == "k72f" for e in entries)


def test_jsonl_entries_are_json_and_carry_a_timestamp(sink_probe):
    """`_ts` 是时间线排序的唯一依据 —— 文本时间戳被 `format` 决定，JSONL 不能依赖它。"""
    entry = json.loads(
        next(
            line
            for line in (sink_probe / "logs" / "traces" / "flow-k72f.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    )
    assert isinstance(entry["_ts"], float)
    assert entry["session"] and entry["run"] != "-"


def test_jsonl_entries_carry_the_log_level(sink_probe):
    """`_level` 必须落盘 —— 否则 JSONL 读回来的历史事件分不出 ERROR 和 INFO。

    级别不是事件字段（`EventKind` 是封闭集合），它是 loguru 的记录元数据，所以和
    `_ts` 一样带下划线前缀。日志查看器的时间线页回填历史时按它上色和筛级别，
    bug 包也才有得 `jq 'select(._level=="ERROR")'`。
    """
    entries = [
        json.loads(line)
        for line in (sink_probe / "logs" / "traces" / "flow-k72f.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    by_kind = {e["kind"]: e for e in entries}
    assert by_kind["diagnosis"]["_level"] == "ERROR"
    assert by_kind["stage"]["_level"] == "INFO"
