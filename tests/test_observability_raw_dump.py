"""raw yt-dlp 原文留档的触发矩阵与过滤判据。

这套东西有个讨厌的失效模式：**该留的时候没留**，而"没留"在当时毫无迹象 —— 等用户
报了个"字幕没下到"的 bug，去 trace 目录一看空的，才知道条件写错了。所以四个 reason
（`outcome_failed` / `degraded` / `recovered` / `always_on`）和"干净成功不留"这五种
情形各配一条断言。

过滤判据单独测，因为它踩过一次坑：原先按 `[download] ` 整个前缀一刀切，把
`Destination:` / `has already been downloaded` / `Skipping` 这三类**恰好是 raw dump
存在理由**的行一起扔了。
"""

import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-raw-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from fluentytdl.observability import (  # noqa: E402
    TaskTrace,
    dump_raw_for_outcome,
    should_keep_raw_line,
    write_raw_dump,
)
from fluentytdl.observability import sinks as sinks_mod  # noqa: E402


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    """把 trace 目录钉到 tmp_path —— `_TRACE_DIR` 是模块级缓存，直接改它。"""
    d = tmp_path / "traces"
    d.mkdir()
    monkeypatch.setattr(sinks_mod, "_TRACE_DIR", d)
    return d


@pytest.fixture
def events():
    """`_level` 是本夹具加的，不是事件字段 —— 级别决定这条会不会冒到控制台。"""
    captured: list[dict] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"]["fytdl"], _level=m.record["level"].name)),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


LINES = ["[youtube] Extracting URL", "WARNING: nsig extraction failed", "[Merger] Merging formats"]


# ── 过滤判据 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "[download] Destination: Title.f399.mp4",
        "[download] Title.mp4 has already been downloaded",
        "[download] Skipping fragment 3",
        "[info] There are no subtitles for the requested languages",
        "WARNING: Falling back on generic information extractor",
        "ERROR: HTTP Error 429: Too Many Requests",
        '[Merger] Merging formats into "Title.mkv"',
    ],
)
def test_keeps_the_lines_the_dump_exists_for(line):
    """`[download] ` 前缀下藏着三类必须留的行 —— 它们是"文件落在哪 / 为什么没下"的唯一原文。"""
    assert should_keep_raw_line(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "[download]  45.2% of ~12.34MiB at 1.23MiB/s ETA 00:09",
        "[download] 100% of 12.34MiB in 00:10",
        "[download]   0.0% of 1.00GiB at Unknown B/s ETA Unknown",
        "FLUENTYTDL|downloading|45.2|12345|",
        "",
    ],
)
def test_drops_progress_noise(line):
    """`--newline` 下进度行上万条，2000 行的缓冲挡不住它们的洪水。"""
    assert should_keep_raw_line(line) is False


# ── 触发矩阵 ─────────────────────────────────────────────────


def test_clean_success_leaves_nothing(trace_dir, events):
    trace = TaskTrace(flow_id="fl1", task_id="42")
    assert dump_raw_for_outcome(trace, LINES, outcome="success") is None
    assert list(trace_dir.rglob("*.ytdlp.log")) == []
    assert not [e for e in events if e.get("code") == "raw_output_retained"]


def test_failure_leaves_a_file(trace_dir, events):
    trace = TaskTrace(flow_id="fl1", task_id="42")
    dest = dump_raw_for_outcome(trace, LINES, outcome="failed")
    assert dest is not None
    assert dest.parent.name == "fl1", "按 flow 分子目录，bundle.py 就是照这个路径捞的"
    assert dest.name == f"task-42-run-{trace.run_id}.ytdlp.log"
    assert dest.read_text(encoding="utf-8").splitlines() == LINES

    signal = [e for e in events if e.get("code") == "raw_output_retained"]
    assert len(signal) == 1, "留了一份就该有一条事件，否则用户永远不知道这文件存在"
    assert signal[0]["kind"] == "signal", "这是观察到的事实，不是失败裁决（硬规则 2）"
    assert signal[0]["reason"] == "outcome_failed"
    assert signal[0]["lines"] == len(LINES)
    assert signal[0]["file"] == dest.name
    assert str(trace_dir) not in str(signal[0]["file"]), "只记文件名：完整路径含 Windows 用户名"


def test_degraded_success_leaves_a_file(trace_dir, events):
    """任务绿着完成、字幕却没有 —— 结构化事件只说得出 `missing=[...]`，说不出原话。"""
    trace = TaskTrace(flow_id="fl1", task_id="7")
    trace.expected_artifacts.update({"video", "subtitle:zh-Hans"})
    trace.actual_artifacts.add("video")
    assert trace.degraded is True

    dest = dump_raw_for_outcome(trace, LINES, outcome="success")
    assert dest is not None and dest.exists()
    assert [e for e in events if e.get("code") == "raw_output_retained"][0]["reason"] == "degraded"


def test_recovered_success_leaves_a_file(trace_dir, events):
    """rc≠0 却判定文件可用：体积启发式给不出完整性证明，原文是唯一复核依据。"""
    trace = TaskTrace(flow_id="fl1", task_id="8")
    trace.recovered = True
    trace.recovery = "nonzero_exit_valid_output"

    dest = dump_raw_for_outcome(trace, LINES, outcome="success")
    assert dest is not None and dest.exists()
    assert [e for e in events if e.get("code") == "raw_output_retained"][0]["reason"] == "recovered"


def test_always_on_leaves_a_file_quietly(trace_dir, events):
    """用户自己开的全程记录：留文件，但事件走 DEBUG，别每次下载都往控制台喊。"""
    trace = TaskTrace(flow_id="fl1", task_id="9")
    dest = dump_raw_for_outcome(trace, LINES, outcome="success", always=True)
    assert dest is not None and dest.exists()
    signal = [e for e in events if e.get("code") == "raw_output_retained"][0]
    assert signal["reason"] == "always_on"
    assert signal["_level"] == "DEBUG"


@pytest.mark.parametrize("outcome", ["cancelled", "paused", "restored_pending"])
def test_user_initiated_stops_leave_nothing(trace_dir, outcome):
    """用户自己按的停止不是异常，没什么可复盘的。"""
    trace = TaskTrace(flow_id="fl1", task_id="10")
    assert dump_raw_for_outcome(trace, LINES, outcome=outcome) is None


def test_no_lines_no_file(trace_dir, events):
    """失败但一行原文都没收到（进程压根没起来）—— 不要留个空文件占位。"""
    trace = TaskTrace(flow_id="fl1", task_id="11")
    assert dump_raw_for_outcome(trace, [], outcome="failed") is None
    assert dump_raw_for_outcome(trace, ["", ""], outcome="failed") is None
    assert list(trace_dir.rglob("*.ytdlp.log")) == []


def test_dump_never_raises(trace_dir):
    """硬规则 5：留原文失败也不能在 run 收尾时把自己炸掉。"""

    class Hostile:
        @property
        def flow_id(self):
            raise RuntimeError("boom")

    assert dump_raw_for_outcome(Hostile(), LINES, outcome="failed") is None
    assert dump_raw_for_outcome(None, LINES, outcome="failed") is None
    assert write_raw_dump("fl1", "42", "r1", None) is None  # type: ignore[arg-type]


def test_flowless_trace_falls_back_to_no_flow(trace_dir):
    """没有 flow 的 trace 也得有个落点，`bundle.py` 会额外兜一遍 `no-flow`。"""
    trace = TaskTrace(task_id="12")
    trace.flow_id = ""
    dest = dump_raw_for_outcome(trace, LINES, outcome="failed")
    assert dest is not None
    assert dest.parent.name == "no-flow"


# ── 接线（真的跑一遍 executor / worker）─────────────────────


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_executor_collects_context_lines_but_no_progress_flood(mock_popen, _resolve):
    """raw 缓冲比 `diag_lines` 宽：命中不了规则的上下文行也要留，进度洪水一律不留。

    2000 行的缓冲只要漏进进度行就等于废掉 —— `--newline` 下它们上万条，会把开头那些
    "用了哪个 client、挑了哪个 format"全部挤出去。
    """
    from fluentytdl.download.executor import DownloadExecutor

    payload = b"\n".join(
        [
            b"[youtube] mock_id_456: Downloading webpage",
            b"[info] Downloading 1 format(s): 399+251",
            b"[download] Destination: v.f399.mp4",
            b"[download]   0.5% of ~10.00MiB at 1.00MiB/s ETA 00:09",
            b"[download]  45.2% of ~10.00MiB at 1.00MiB/s ETA 00:05",
            b"[download] 100% of 10.00MiB in 00:10",
            b"FLUENTYTDL|download|10485760|10485760|NA|1048576|0|avc1|mp4a|mp4|v.mp4",
            b"WARNING: [youtube] nsig extraction failed",
            b'[Merger] Merging formats into "v.mkv"',
        ]
    )
    process = MagicMock()
    process.stdout = BytesIO(payload)
    process.wait.return_value = 0
    process.returncode = 0
    mock_popen.return_value = process

    executor = DownloadExecutor()
    executor.execute(
        url="https://youtube.com/watch?v=mock_id_456",
        ydl_opts={"format": "best_mp4"},
        on_progress=lambda _e: None,
        on_status=lambda _s: None,
        on_path=lambda _p: None,
        cancel_check=lambda: False,
    )

    raw = list(executor.raw_lines)
    kept = "\n".join(raw)
    # 上下文行：`diag_lines` 一条都不会收（它们命中不了任何规则），却正是复现材料
    assert "Downloading 1 format(s): 399+251" in kept
    assert "[download] Destination: v.f399.mp4" in kept
    assert "nsig extraction failed" in kept
    assert "Merging formats" in kept
    # 噪音
    assert not [ln for ln in raw if "%" in ln and ln.startswith("[download]")], raw
    assert not [ln for ln in raw if ln.startswith("FLUENTYTDL|")], raw


def test_worker_dump_covers_every_attempt_of_the_run(trace_dir, tmp_path):
    """一个 run 里每个 attempt 各有一个新 executor —— 收不上来就只剩最后一次的原文。

    "429 → 重试 → 挂了"恰恰是最需要完整复盘的 run：第一次的原文里才有 429 的上下文，
    最后一次可能只是另一个症状。文件名以 `run_id` 为键，内容就必须是整个 run 的。
    """
    from fluentytdl.diagnostics.models import RetryPolicy
    from fluentytdl.download import workers as worker_mod
    from fluentytdl.models.errors import YtDlpExecutionError

    calls = {"n": 0}

    class _FakeExecutor:
        def __init__(self):
            self.raw_lines = []

        def execute(self, url, ydl_opts, **kwargs):
            calls["n"] += 1
            self.raw_lines.append(f"[attempt-{calls['n']}] yt-dlp said something")
            if calls["n"] == 1:
                raise YtDlpExecutionError(
                    exit_code=1,
                    stderr="ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
                )
            # 第二次换个管线内异常，走 run() 的通用失败边界 → outcome=failed
            raise RuntimeError("boom in pipeline")

    with (
        patch.object(worker_mod, "DownloadExecutor", _FakeExecutor),
        patch.object(worker_mod.youtube_service, "build_ydl_options", return_value={}),
        patch.object(RetryPolicy, "delay_for", lambda self, attempt: 0.0),
    ):
        worker = worker_mod.DownloadWorker(
            "https://youtube.com/watch?v=mock_id_456",
            {"format": "bv+ba", "paths": {"home": str(tmp_path)}},
        )
        worker.features = []
        worker.db_id = 42
        worker.trace.bind_task_id(42)
        run_id = worker.trace.run_id
        worker.run()

    assert calls["n"] == 2, "429 是 backoff 策略，必须真的再试一次"
    dumps = list(trace_dir.rglob(f"task-42-run-{run_id}.ytdlp.log"))
    assert len(dumps) == 1, list(trace_dir.rglob("*"))
    body = dumps[0].read_text(encoding="utf-8")
    assert "[attempt-1] yt-dlp said something" in body, "第一次尝试的原文丢了"
    assert "[attempt-2] yt-dlp said something" in body
    # 分隔行：没有它，三次尝试的输出连成一片看不出边界
    assert body.count("===== FluentYTDL: attempt ") == 2
    assert "attempt 0 =====" in body and "attempt 1 =====" in body


def test_worker_clean_success_leaves_no_dump(trace_dir, tmp_path):
    """干净成功的下载彻底不留 —— 那是绝大多数任务，留下来只会把 trace 目录写满。"""
    from fluentytdl.diagnostics import DiagnosticLineCollector
    from fluentytdl.download import workers as worker_mod

    class _FakeExecutor:
        def __init__(self):
            self.raw_lines = ["[youtube] all good"]
            # 成功路径会扫 `diag_lines` 找"成功但结果不对"的征兆
            self.diag_lines = DiagnosticLineCollector()

        def execute(self, url, ydl_opts, **kwargs):
            out = tmp_path / "v.mp4"
            out.write_bytes(b"0" * 4096)
            kwargs["on_path"](str(out))
            return str(out)

    with (
        patch.object(worker_mod, "DownloadExecutor", _FakeExecutor),
        patch.object(worker_mod.youtube_service, "build_ydl_options", return_value={}),
    ):
        worker = worker_mod.DownloadWorker(
            "https://youtube.com/watch?v=mock_id_456",
            {"format": "bv+ba", "paths": {"home": str(tmp_path)}},
        )
        worker.features = []
        worker.db_id = 43
        worker.trace.bind_task_id(43)
        worker.run()

    assert worker._run_outcome == "success", "夹具本身要先真的走成功路径"
    assert list(trace_dir.rglob("*.ytdlp.log")) == []
