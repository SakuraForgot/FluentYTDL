"""Observability Event 层的契约测试。

这一层的每条硬规则都必须配一条断言 —— **规则不配测试等于没有规则**。日志代码有个
恶劣特性：坏了不会报错，只是从此少记一类事件，而少记什么恰恰要等到真出事那天才发现。

本文件按硬规则分段，五条规则全部有断言：

| 硬规则 | 断言 |
|---|---|
| 1. `transition` 只有一个权威产生点 | `test_transition_*` |
| 2. `diagnosis` 只属于失败边界 | `test_success_path_*` / `test_diagnosis_*` |
| 3. `degraded` 只能由 `expected − actual` 得出 | `test_degraded_*` |
| 4. 每个 run 恰好一个 `outcome` | `test_outcome_is_emitted_exactly_once_per_run` |
| 5. observability 永远 best-effort | `test_emit_event_never_raises_*` / `test_*json*` |

规则 1 和 2 各配了一条**静态**断言（扫源码树数产生点），规则 4 也是。它们看着不像
测试，却是这几条规则唯一挡得住的失效方式：不是某处逻辑写错，而是两个月后有人在另一个
收口点"顺手也记一条" —— 事件翻倍、统计失效，而所有动态测试照样全绿。

方案里那张**错误注入矩阵**也在这里落地，一律用 `Popen` + `BytesIO` 灌真实 yt-dlp
输出、驱动真实收口点，不手搓事件：

| 注入 | 断言 |
|---|---|
| 429 + rc≠0 | `test_rate_limited_429_retries_inside_the_same_run` |
| nsig 警告 + rc=0 | `test_executor_success_path_emits_signal_not_diagnosis` |
| 用户勾/没勾字幕 | `test_degraded_*` |
| rc=1 + 有效大文件 | `test_recovery_reports_a_fact_and_declares_no_outcome` |
| 带凭据的 argv | `test_argv_sanitization_*` |
"""

import ast
import json
import os
import sys
import tempfile
from enum import Enum
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-obs-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from fluentytdl.observability import (  # noqa: E402
    EVENT_KINDS,
    OUTCOMES,
    FlowTrace,
    TaskTrace,
    build_event,
    emit_event,
    emit_success_signals,
    json_safe,
    new_flow,
    render_argv,
    sanitize_argv,
    sanitize_path,
    sanitize_proxy,
    worst_outcome,
)

PKG = Path(__file__).resolve().parent.parent / "src" / "fluentytdl"


@pytest.fixture
def events():
    """收集本次测试期间产生的所有 fytdl 事件（as_dict 后的形态）。"""
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


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


# ── 硬规则 5：best-effort，绝不在错误现场自己炸 ──────────────


class _Color(Enum):
    RED = "red"


class _Exploding:
    """`repr()` 和 `str()` 都会炸的对象。normalizer 的最坏输入。"""

    def __repr__(self):
        raise RuntimeError("repr exploded")

    def __str__(self):
        raise RuntimeError("str exploded")


def test_emit_event_accepts_hostile_values_and_stays_json_safe(events):
    """硬规则 5：文本 sink 打得出来、JSONL sink 却 `TypeError` 是最坏的失败模式。

    `Path` / `Enum` / `set` / `tuple` / 异常对象在事件字段里都是家常便饭
    （`ydl_opts` 里全是 Path，字幕语言集合天然是 set）。
    """
    emit_event(
        "decision",
        path=Path("/tmp/a.mp4"),
        color=_Color.RED,
        langs={"zh-Hans", "en"},
        pair=(1, 2),
        err=ValueError("boom"),
        nan=float("nan"),
        raw=b"\xff\xfe bytes",
        hostile=_Exploding(),
        deep={"a": {"b": {"c": {"d": {"e": "太深了"}}}}},
    )
    assert len(events) == 1
    # 关键断言：能被 json.dumps 吃下去
    blob = json.dumps(events[0], ensure_ascii=False, allow_nan=False)
    assert blob

    ev = events[0]
    assert ev["color"] == "red", "Enum 应展开成 .value"
    assert ev["langs"] == ["en", "zh-Hans"], "set 应变成有序 list（否则日志行不可比对）"
    assert ev["pair"] == [1, 2], "tuple 应变成 list"
    assert ev["err"] == "ValueError: boom"
    assert ev["nan"] == "nan", "NaN 在严格 JSON 里非法，必须转字符串"


def test_emit_event_never_raises_on_bad_arguments():
    """非法 kind / 非法 level / 无 trace 都不得抛异常 —— 业务不该被日志绊倒。"""
    emit_event("nonexistent_kind", level="NOT_A_LEVEL", x=1)
    emit_event("stage", trace=None)
    emit_event("stage", trace=object())  # 没有任何标识属性的对象
    emit_event("stage", level=None)  # type: ignore[arg-type]


def test_business_field_may_be_named_kind(events):
    """业务字段叫 `kind` 不能变成 `TypeError`。

    参数绑定错误发生在函数体之前，`emit_event` 的 try/except 兜不住 ——
    所以 `kind` 必须是 positional-only，撞名的业务字段改名成 `kind_` 落库。
    """
    emit_event("signal", kind="cover", code="x")
    assert events[0]["kind"] == "signal"
    assert events[0]["kind_"] == "cover"


def test_identity_fields_cannot_be_shadowed(events):
    """标识键被业务字段覆盖，整条时间线就串不起来了。"""
    trace = TaskTrace(task_id="42")
    emit_event("signal", trace=trace, task="999", run="fake", attempt=7)
    ev = events[0]
    assert ev["task"] == "42" and ev["attempt"] == 0
    assert ev["task_"] == "999" and ev["run_"] == "fake" and ev["attempt_"] == 7


def test_json_safe_handles_unrepresentable_object():
    assert isinstance(json_safe(_Exploding()), str)


# ── 标识层次：五级 ──────────────────────────────────────────


def test_flow_has_no_task_or_run():
    """`parse` / `select` 天然没有 task —— 占位符要一眼可辨，不能是空串。"""
    flow = new_flow()
    ev = build_event("stage", trace=flow)
    assert ev.flow == flow.flow_id
    assert ev.task == "-" and ev.run == "-" and ev.attempt == 0


def test_child_task_inherits_flow_and_session():
    flow = new_flow()
    task = flow.child_task(42)
    assert task.flow_id == flow.flow_id
    assert task.session_id == flow.session_id
    assert task.task_id == "42"
    assert task.run_id not in ("", "-"), "TaskTrace 一诞生就该有 run"


def test_bind_task_id_emits_identity_linking_flow_to_task(events):
    """`create_worker()` 拿到 db_id 后补的这一条，是 bug 包能找回解析段的唯一线索。"""
    flow = new_flow()
    task = flow.child_task()
    assert task.task_id == "-"
    task.bind_task_id(42)
    assert task.task_id == "42"
    identity = [e for e in events if e["kind"] == "identity"]
    assert len(identity) == 1
    assert identity[0]["flow"] == flow.flow_id and identity[0]["task"] == "42"


def test_bind_task_id_is_idempotent(events):
    task = TaskTrace()
    task.bind_task_id(7)
    task.bind_task_id(7)
    assert len([e for e in events if e["kind"] == "identity"]) == 1


def test_auto_retry_keeps_run_id_and_increments_attempt():
    """**自动重试不换 `run_id`。**

    换了 `run_id` 就和 `attempt` 在表达同一件事，五级层次失去意义 ——
    也就没法回答"这个 run 一共试了几次"。
    """
    task = TaskTrace(task_id="42")
    run = task.run_id
    assert task.next_attempt() == 1
    assert task.next_attempt() == 2
    assert task.run_id == run


def test_new_run_mints_new_id_and_resets_attempt():
    """启动恢复才铸新 run：`task=42 run=A→interrupted`，随后 `run=B attempt=0`。"""
    task = TaskTrace(task_id="42")
    first = task.run_id
    task.next_attempt()
    task.mark_recovered("nonzero_exit_valid_output")
    task.expect_artifacts(["video"])

    second = task.new_run()
    assert second != first
    assert task.attempt == 0
    assert task.recovered is False and task.recovery == ""
    assert task.expected_artifacts == set(), "上一轮的累积状态必须清空"


def test_enter_updates_stage_and_emits(events):
    flow = new_flow()
    flow.enter("select", reason="user_confirmed")
    assert flow.stage == "select"
    assert _kinds(events) == ["stage"]
    assert events[0]["stage"] == "select"


# ── 源码树静态扫描（规则 1 / 2 的守卫）────────────────────────

#: 事件出口的三个名字。`emit`（`FlowTrace.emit` / `trace.emit`）也算 —— 它是纯转发。
_EMIT_FUNCS = frozenset({"emit_event", "emit_events", "emit"})


def _emit_sites(kind: str) -> list[str]:
    """扫源码树，列出所有把 `kind` 字面量传给事件出口的位置（``相对路径:行号``）。

    **用 AST 而不是 grep**：本项目每条硬规则都在注释与 docstring 里被解释过一遍，
    光数字符串出现次数会把"解释规则的那句话"也算成产生点。AST 只看真实调用。
    """
    sites: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _EMIT_FUNCS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == kind:
                sites.append(f"{path.relative_to(PKG).as_posix()}:{node.lineno}")
    return sites


def _calls_named(path: Path, name: str) -> list[int]:
    """`path` 里调用 `name(...)` 的行号。注释和 docstring 里提到它的地方不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called == name:
            hits.append(node.lineno)
    return hits


# ── 硬规则 1：transition 只有一个权威产生点 ──────────────────


def test_transition_has_exactly_one_producer_in_the_source_tree():
    """产生点必须**恰好一个**，且是那个"UI 状态 → 持久状态"的边界。

    这条规则的失效方式不是逻辑写错，是有人在另一个收口点顺手也记一条：
    `count(kind=transition)` 当场翻倍，所有基于事件的统计一起失效，而动态测试全绿
    —— 每条事件单独看都是对的。所以守卫只能是静态的。
    """
    sites = _emit_sites("transition")
    assert len(sites) == 1, f"transition 产生点不止一个：{sites}"
    assert sites[0].startswith("download/download_manager.py:"), sites


def test_progress_channel_produces_no_event_at_all(events):
    """`clean_logger._emit()` 是进度通道 —— 刷一百次也不许落一条事件。

    它每次 UI 刷新（41.2% → 41.8% → 42.5%…）都会跑，接上事件层就等于把 JSONL 变成
    进度数据库 —— 而进度**刻意**不设 `ui_progress` kind。这里带着 trace 构造，正是
    为了证明"有 trace 可用"也不会顺手记一条。
    """
    from fluentytdl.utils.clean_logger import CleanLogger

    cl = CleanLogger(lambda *_a: None, trace=TaskTrace(task_id="42"))
    for pct in (0.0, 12.5, 41.2, 41.8, 42.5, 99.9):
        cl.force_update("downloading", pct, f"{pct}%")

    assert events == [], _kinds(events)


def test_progress_channel_may_only_emit_the_phase_switch(events):
    """唯一的例外：多流阶段切换（视频流 → 音轨流）落 `kind=stage`。

    "音轨阶段到底有没有开始过"是排查"多音轨视频只下到视频没有声音"的头号问题，而
    `detect_phase()` 是全项目唯一看得见 vcodec/acodec 切换的地方。它是**语义变化**，
    不是 UI 刷新 —— 这条界线就是"进度不进事件层"的全部内容。
    """
    from fluentytdl.utils.clean_logger import CleanLogger

    cl = CleanLogger(lambda *_a: None, trace=TaskTrace(task_id="42"))
    cl._emit_phase_stage(("audio",))

    assert _kinds(events) == ["stage"]
    assert events[0]["phase"] == "audio"
    assert events[0]["stage"] == "download"


def test_progress_channel_never_emits_a_transition():
    """`transition` 表达"这个状态已被系统接受"，而进度通道只知道"想让 UI 显示什么"。"""
    assert not [s for s in _emit_sites("transition") if s.startswith("utils/clean_logger.py")]


def test_transition_is_deduped_across_progress_ticks(events):
    """`unified_status` 每个进度 tick 都带着 `state="downloading"` 触发产生点。

    无条件 emit 会让一次下载刷出几十条 `to=downloading` —— 那不是"状态变化"，
    是进度刷新。去重的判据必须是"状态变了"，不是"信号来了"。
    """
    from types import SimpleNamespace

    from fluentytdl.download.download_manager import _emit_transition

    worker = SimpleNamespace(trace=TaskTrace(task_id="42"))
    for pct in (0.0, 12.5, 41.2, 41.8, 99.9):
        _emit_transition(worker, "downloading", pct)
    _emit_transition(worker, "processing", 100.0)
    _emit_transition(worker, "completed", 100.0)

    moves = [(e["from"], e["to"]) for e in events if e["kind"] == "transition"]
    assert moves == [
        ("-", "downloading"),  # 首次迁入：上一个状态本来就不存在，不是 bug
        ("downloading", "processing"),
        ("processing", "completed"),
    ], moves


def test_transition_out_of_a_sealed_state_is_a_warning():
    """`completed → downloading` 是任务已收尾之后又冒出新状态，值得一条 WARNING。

    迁移表刻意只封 `completed` / `cancelled` 两个源：过严的白名单会刷出一片假告警，
    而假告警只会训练读日志的人忽略告警。
    """
    from types import SimpleNamespace

    from fluentytdl.download.download_manager import _emit_transition

    seen: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["extra"]["fytdl"]["to"])),
        level="TRACE",
        filter=lambda r: r.get("extra", {}).get("fytdl", {}).get("kind") == "transition",
    )
    try:
        worker = SimpleNamespace(trace=TaskTrace(task_id="42"))
        _emit_transition(worker, "completed", 100.0)
        _emit_transition(worker, "downloading", 0.0)
    finally:
        logger.remove(sink_id)

    assert seen == [("INFO", "completed"), ("WARNING", "downloading")], seen


# ── 硬规则 2：diagnosis 只属于失败边界 ───────────────────────

#: 允许 emit `kind=diagnosis` 的文件。**往这张表里加一行是一次架构决定**，不是维护
#: 动作：能进来的只有"自己**产生**了那个 `Diagnosis` 的 `except` 块"。展示层重算
#: （`diagnose(1, raw_error)` 那几处）永远不许 emit —— 否则同一次失败两条判定。
_DIAGNOSIS_PRODUCERS = ("download/workers.py",)

#: 展示层重算 `Diagnosis` 的地方：只为了拿本地化文案渲染错误面板，不是错误边界。
_DISPLAY_LAYER_RECOMPUTERS = (
    "ui/components/dialogs/download_config_window.py",
    "ui/components/dialogs/selection_dialog.py",
)


def test_diagnosis_is_only_emitted_from_failure_boundaries():
    sites = _emit_sites("diagnosis")
    assert sites, "一条都没有说明失败边界断了"
    stray = [s for s in sites if not s.startswith(_DIAGNOSIS_PRODUCERS)]
    assert not stray, f"这些位置不是失败边界，不该 emit diagnosis：{stray}"


def test_display_layer_recomputation_never_emits():
    """展示层为了拿 `user_title` 重跑一次 `diagnose()` 是合理的，emit 不是。

    exactly-once 靠这条约定（架构）保证，而不是在 `Diagnosis` 上挂个 `_logged`
    布尔补丁 —— 那种标记挡不住重新构造 / copy / 跨线程 / 同一段 stderr 二次诊断。
    """
    for rel in _DISPLAY_LAYER_RECOMPUTERS:
        path = PKG / rel
        assert _calls_named(path, "diagnose"), f"{rel} 不再重算 Diagnosis，这张表该更新了"
        assert not [s for s in _emit_sites("diagnosis") if s.startswith(rel)], rel


def test_the_event_layer_never_arbitrates_a_root_cause():
    """`observability` 整个包都不许调 `diagnose()`。

    它是**主因仲裁**，只属于失败边界。事件层调它意味着成功路径也会产出主因，
    于是日志里会出现 `kind=diagnosis severity=fatal` 却 `rc=0` 的诡异语义。
    """
    offenders = [
        f"{p.relative_to(PKG).as_posix()}:{line}"
        for p in sorted((PKG / "observability").rglob("*.py"))
        for line in _calls_named(p, "diagnose")
    ]
    assert offenders == [], offenders


def test_success_path_yields_signals_and_never_a_diagnosis(events):
    """rc=0 带警告 → `count(signal) > 0` 且 `count(diagnosis) == 0`。

    `operation_succeeded=true` 是刻意冗余的字段：光看 `code=nsig_extraction_failed`
    分不清这次到底成没成，而那正是"用户说下载好了但字幕没有"这类工单的关键上下文。
    """
    emitted = emit_success_signals(
        "WARNING: [youtube] nsig extraction failed, some formats may be missing\n"
        "[info] There are no subtitles for the requested languages\n",
        trace=TaskTrace(task_id="42"),
        stage="download",
        operation="download",
    )
    assert emitted >= 2
    assert set(_kinds(events)) == {"signal"}, _kinds(events)
    assert all(e["operation_succeeded"] is True for e in events)
    codes = {e["code"] for e in events}
    assert {"nsig_extraction_failed", "subtitles_no_language_match"} <= codes, codes
    # 语言中立：只记 code / severity_hint，绝不记会随界面语言变化的本地化文案
    assert all("user_title" not in e and "user_message" not in e for e in events)


def test_success_signals_dedup_by_code(events):
    """同一 code 在一次操作里只落第一条 —— 播放列表会对每个条目各刷一遍同样的警告。"""
    emit_success_signals(
        "\n".join(
            [
                "WARNING: [youtube] nsig extraction failed, some formats may be missing",
                "WARNING: [youtube] nsig extraction failed, some formats may be missing (2)",
            ]
        ),
        trace=TaskTrace(task_id="42"),
    )
    assert [e["code"] for e in events].count("nsig_extraction_failed") == 1


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_executor_success_path_emits_signal_not_diagnosis(mock_popen, _resolve, events):
    """端到端灌一段 rc=0 但带警告的 yt-dlp 输出（错误注入矩阵第二行）。

    "成功但结果不对"是这个项目最难查的一类问题：以前这段输出只有失败时才有人读，
    rc=0 时整段丢掉，日志里一个字都没有。
    """
    from fluentytdl.download.executor import DownloadExecutor

    payload = b"\n".join(
        [
            b"WARNING: [youtube] nsig extraction failed, some formats may be missing",
            b"[info] There are no subtitles for the requested languages",
            b"FLUENTYTDL|download|10485760|10485760|NA|1048576|0|avc1|mp4a|mp4|v.mp4",
        ]
    )
    process = MagicMock()
    process.stdout = BytesIO(payload)
    process.wait.return_value = 0
    process.returncode = 0
    mock_popen.return_value = process

    DownloadExecutor().execute(
        url="https://youtube.com/watch?v=mock_id_456",
        ydl_opts={"format": "best_mp4"},
        on_progress=lambda _e: None,
        on_status=lambda _s: None,
        on_path=lambda _p: None,
        cancel_check=lambda: False,
    )

    kinds = _kinds(events)
    assert kinds.count("diagnosis") == 0, [e for e in events if e["kind"] == "diagnosis"]
    assert kinds.count("signal") >= 1, kinds
    codes = {e.get("code") for e in events if e["kind"] == "signal"}
    assert "nsig_extraction_failed" in codes, codes


def test_failure_boundary_does_not_re_arbitrate(events):
    """失败边界只**落**已有结论，不重跑 `diagnose()`。

    哨兵 code 是这条断言的关键：真去重跑一次仲裁，那段 429 原文会把 code 变成
    `rate_limited_429`。同一次失败两次仲裁未必给出同一个主因，而日志里出现两条
    `diagnosis` 之后，`count(kind=diagnosis)` 就不再等于"真正发生了错误"。
    """
    from fluentytdl.download.workers import _emit_failure_diagnosis

    _emit_failure_diagnosis(
        TaskTrace(task_id="42"),
        {
            "code": "sentinel_only_from_err_dict",
            "category": "network",
            "severity": "fatal",
            "retry": {"policy": "backoff"},
            "events": ["nsig_extraction_failed"],
            # 本地化文案就摆在入参里 —— 断言它们没被带进事件
            "user_title": "网络错误",
            "user_message": "请稍后重试",
        },
        stage="parse",
        operation="extract_video_info",
        exc=RuntimeError("ERROR: unable to download video data: HTTP Error 429"),
    )

    diags = [e for e in events if e["kind"] == "diagnosis"]
    assert len(diags) == 1, diags
    diag = diags[0]
    assert diag["code"] == "sentinel_only_from_err_dict"
    assert diag["retry_policy"] == "backoff"
    assert diag["events"] == ["nsig_extraction_failed"]
    assert "user_title" not in diag and "user_message" not in diag
    # 规则没命中时异常原文是唯一线索，但要脱敏后再落
    assert "429" in json.dumps(diag, ensure_ascii=False)


def test_rate_limited_429_retries_inside_the_same_run(events, tmp_path):
    """错误注入矩阵第一行：429 → `diagnosis` → `retry attempt=1`，且 **`run_id` 不变**。

    两个半边各自测过（`diagnose()` 认得 429；`next_attempt()` 不换 run），真正会坏的
    是中间那根线。所以这里直接驱动 `DownloadWorker.run()` 的失败收口块：确认它调的是
    `next_attempt()` 而不是 `new_run()` —— 换了 `run_id`，"这个 run 一共试了几次"就
    再也答不出来，而两条事件单独看都完全正常。
    """
    from fluentytdl.diagnostics.models import RetryPolicy
    from fluentytdl.download import workers as worker_mod
    from fluentytdl.models.errors import YtDlpExecutionError

    calls = {"n": 0}

    class _FakeExecutor:
        """第一次撞 429，第二次直接取消 —— 只为让 `run()` 走完，不涉及下载后半程。"""

        def execute(self, url, ydl_opts, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise YtDlpExecutionError(
                    exit_code=1,
                    stderr="ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
                )
            raise worker_mod.DownloadCancelled()

    with (
        patch.object(worker_mod, "DownloadExecutor", _FakeExecutor),
        patch.object(worker_mod.youtube_service, "build_ydl_options", return_value={}),
        # backoff 首档是 30 秒真等待，和这条断言要验的东西无关
        patch.object(RetryPolicy, "delay_for", lambda self, attempt: 0.0),
    ):
        worker = worker_mod.DownloadWorker(
            "https://youtube.com/watch?v=mock_id_456",
            {"format": "bv+ba", "paths": {"home": str(tmp_path)}},
        )
        worker.features = []  # Feature 链与本断言无关
        worker.db_id = 42
        worker.trace.bind_task_id(42)
        worker.run()

    assert calls["n"] == 2, "429 是 backoff 策略，必须真的再试一次"

    diag = next(e for e in events if e["kind"] == "diagnosis")
    retry = next(e for e in events if e["kind"] == "retry")
    assert diag["code"] == "rate_limited_429"
    assert diag["retry_policy"] == "backoff"
    assert retry["code"] == "rate_limited_429" and retry["trigger"] == "automatic"
    assert retry["run"] == diag["run"], "自动重试不换 run_id"
    assert (diag["attempt"], retry["attempt"]) == (0, 1)
    # 重试段自成一段时间线，不混在 download 里
    assert retry["stage"] == "retry"
    assert _kinds(events).count("outcome") == 1, "一个 run 恰好一个 outcome"


# ── 硬规则 3：degraded 只能由 expected − actual 得出 ─────────


def test_degraded_is_false_when_user_did_not_request_the_artifact():
    """用户没勾封面而封面不存在，**不是**降级。

    这条最容易写错成"系统支持的 − actual"，那样每个任务都会被标成 degraded，
    标记就此失去信号价值。
    """
    task = TaskTrace(task_id="42")
    task.expect_artifacts(["video"])
    task.record_artifacts(["video"])
    assert task.missing == []
    assert task.degraded is False


def test_degraded_is_true_only_for_requested_but_absent_artifacts():
    task = TaskTrace(task_id="42")
    task.expect_artifacts(["video", "subtitle:zh-Hans", "thumbnail"])
    task.record_artifacts(["video", "thumbnail"])
    assert task.missing == ["subtitle:zh-Hans"]
    assert task.degraded is True


def test_degraded_has_no_setter():
    """没有 setter 就没有"把系统能力集塞进来"那条路。"""
    task = TaskTrace(task_id="42")
    with pytest.raises(AttributeError):
        task.degraded = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        task.missing = ["whatever"]  # type: ignore[misc]


def test_extra_actual_artifacts_do_not_make_it_degraded():
    """多下到东西不算降级（差集是单向的）。"""
    task = TaskTrace(task_id="42")
    task.expect_artifacts(["video"])
    task.record_artifacts(["video", "thumbnail"])
    assert task.degraded is False


# ── 硬规则 4：每个 run 恰好一个 outcome ─────────────────────

#: 允许 emit `kind=outcome` 的文件。**只有两处，且各有不可替代的理由**：
#:
#: - `observability/trace.py` —— `TaskTrace.finish()`，run 终态的唯一正门；
#: - `download/download_manager.py` —— 启动时的恢复审计。被强杀的进程不可能 emit
#:   自己的终态（同步 sink 也写不出一条从未产生的事件），那条 `outcome=interrupted`
#:   只能由下次启动代为补记，而此时原 run 的 `TaskTrace` 早已随进程消失，走不了
#:   `finish()`。
#:
#: 子系统一律不许进这张表：executor 判"rc≠0 但产物有效"之后还有 postprocess /
#: verify / finalize，它宣布 success 就会让一个 run 出两个 outcome。
_OUTCOME_PRODUCERS = ("observability/trace.py", "download/download_manager.py")


def test_outcome_has_no_producer_outside_the_two_terminal_boundaries():
    """静态守卫：新增一个 outcome 产生点是架构决定，不是维护动作。

    这条规则的失效方式是**加**而不是错：某个子系统"顺手也宣布一下结果"，每条事件
    单看都对，所有动态测试照样全绿，而 `count(kind=outcome)` 从此不再等于 run 数。
    """
    sites = _emit_sites("outcome")
    assert sites, "一个 outcome 产生点都没有 = 事件层没接上"
    for site in sites:
        assert site.startswith(_OUTCOME_PRODUCERS), f"{site} 不是 run 的终态边界"


def test_outcome_is_emitted_exactly_once_per_run(events):
    """子系统只报告事实，不宣布结果；重复宣布要被拒。"""
    task = TaskTrace(task_id="42")
    assert task.finish("success") is True
    assert task.finish("failed") is False
    assert task.finish("cancelled") is False

    outcomes = [e for e in events if e["kind"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "success"


def test_duplicate_finish_is_not_silent(events):
    """重复宣布终态是契约违规，本身就该在日志里看得见。"""
    task = TaskTrace(task_id="42")
    task.finish("success")
    task.finish("failed")
    signals = [e for e in events if e["kind"] == "signal"]
    assert [s["code"] for s in signals] == ["duplicate_outcome_suppressed"]
    assert signals[0]["attempted_outcome"] == "failed"


def test_new_run_allows_a_fresh_outcome(events):
    """Outcome 属于 run，不属于 task —— `run=A→paused, run=B→success` 完全合理。"""
    task = TaskTrace(task_id="42")
    task.finish("paused")
    task.new_run()
    assert task.finish("success") is True
    outcomes = [e for e in events if e["kind"] == "outcome"]
    assert [o["outcome"] for o in outcomes] == ["paused", "success"]
    assert outcomes[0]["run"] != outcomes[1]["run"]


def test_outcome_carries_recovered_and_degraded_orthogonally(events):
    """ "rc≠0 恢复成功 + 字幕又没下到"就是两者同真。"""
    task = TaskTrace(task_id="42")
    task.mark_recovered("nonzero_exit_valid_output")
    task.expect_artifacts(["video", "subtitle:zh-Hans"])
    task.record_artifacts(["video"])
    task.finish("success")

    ev = next(e for e in events if e["kind"] == "outcome")
    assert ev["outcome"] == "success"
    assert ev["recovered"] is True
    assert ev["recovery"] == "nonzero_exit_valid_output"
    assert ev["degraded"] is True
    assert ev["missing"] == ["subtitle:zh-Hans"]


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_recovery_reports_a_fact_and_declares_no_outcome(mock_popen, _resolve, events, tmp_path):
    """错误注入矩阵第六行：rc=1 + 产物有效 → `kind=recovery confidence=low`，**此处不出现 outcome**。

    Windows 上 `.part-Frag` 删不掉会让 yt-dlp 以 rc=1 收场，而产物其实是完整的 ——
    executor 放过它是对的。但 executor 返回之后还有 postprocess / verify / finalize，
    任何一步都可能再失败：它要是顺手宣布 `outcome=success`，一个 run 就出两个 outcome。
    终态只属于 `DownloadWorker.run()` 的收口（硬规则 4）。

    `confidence=low` 一并锁死：判据只是"文件存在且体积不离谱"，一个体积启发式，
    不是完整性证明。记成高置信度等于教读日志的人相信一条它给不出的保证。
    """
    from fluentytdl.download.executor import DownloadExecutor

    merged = tmp_path / "video.mp4"
    merged.write_bytes(b"\0" * 1_050_000)  # 两条流之和的 95%，容器开销范围内
    video = tmp_path / "video.f315.mp4"
    audio = tmp_path / "video.f251.m4a"

    process = MagicMock()
    process.stdout = BytesIO(
        b"\n".join(
            [
                f"FLUENTYTDL|download|1000000|1000000|NA|1048576|0|avc1|mp4a|mp4|{video}".encode(),
                f"FLUENTYTDL|download|100000|100000|NA|1048576|0|avc1|mp4a|m4a|{audio}".encode(),
                f'[Merger] Merging formats into "{merged}"'.encode(),
                b"ERROR: Unable to delete file video.f315.mp4.part-Frag1",
            ]
        )
    )
    process.wait.return_value = 1
    process.returncode = 1
    mock_popen.return_value = process

    result = DownloadExecutor().execute(
        url="https://youtube.com/watch?v=mock_id_456",
        ydl_opts={"format": "bv+ba", "paths": {"home": str(tmp_path)}},
        on_progress=lambda _e: None,
        on_status=lambda _s: None,
        on_path=lambda _p: None,
        cancel_check=lambda: False,
    )

    assert result is not None and result.endswith("video.mp4"), "产物有效就不该抛"

    recoveries = [e for e in events if e["kind"] == "recovery"]
    assert len(recoveries) == 1, _kinds(events)
    rec = recoveries[0]
    assert rec["reason"] == "nonzero_exit_valid_output"
    assert rec["verification"] == "size_heuristic"
    assert rec["confidence"] == "low"
    assert rec["exit_code"] == 1
    assert _kinds(events).count("outcome") == 0, "子系统只报告事实，不宣布终态"


def test_caller_cannot_override_derived_outcome_fields(events):
    """`degraded` / `missing` 是派生量，不接受外部裁决。"""
    task = TaskTrace(task_id="42")
    task.expect_artifacts(["video", "subtitle:en"])
    task.record_artifacts(["video"])
    task.finish("success", degraded=False, missing=[])
    ev = next(e for e in events if e["kind"] == "outcome")
    assert ev["degraded"] is True and ev["missing"] == ["subtitle:en"]


def test_unknown_outcome_degrades_to_failed(events):
    """未知终态宁可报失败 —— 把它记成 success 会让统计说谎。"""
    TaskTrace(task_id="42").finish("weird_state")
    assert next(e for e in events if e["kind"] == "outcome")["outcome"] == "failed"


def test_worst_outcome_priority():
    assert worst_outcome("success", "failed") == "failed"
    assert worst_outcome("paused", "cancelled") == "cancelled"
    assert worst_outcome("success", "restored_pending") == "restored_pending"
    assert worst_outcome("interrupted", "paused") == "interrupted"
    assert worst_outcome("garbage") == "failed"


# ── 封闭集合 ────────────────────────────────────────────────


def test_event_kind_set_is_closed():
    """刻意不设 `mismatch`（用 `actual matched=false` 表达）与 `ui_progress`
    （进度维持 Qt Signal 通道，不进事件层，否则 JSONL 会变成进度数据库）。"""
    assert EVENT_KINDS == {
        "stage",
        "decision",
        "expect",
        "actual",
        "signal",
        "diagnosis",
        "recovery",
        "retry",
        "transition",
        "outcome",
        "config",
        "argv",
        "identity",
    }
    assert "mismatch" not in EVENT_KINDS
    assert "ui_progress" not in EVENT_KINDS


def test_outcome_set_is_closed():
    assert OUTCOMES == {
        "success",
        "failed",
        "cancelled",
        "paused",
        "interrupted",
        "restored_pending",
    }
    assert "queued" not in OUTCOMES, "队列恢复的语义是 restored_pending"


# ── 脱敏 ────────────────────────────────────────────────────


def test_argv_sanitization_drops_credentials_but_keeps_diagnostics():
    """`executor` 那句 `cmd={}` 会把整条 argv 原样落盘。

    真实泄漏面：代理账密、cookie 路径、含 Windows 用户名的输出路径、PO Token。
    但视频 URL 和代理主机端口**必须留下** —— 涂掉它们等于让日志失去排查价值，
    换不到任何安全收益（它们本来就显示在界面上）。
    """
    cmd = [
        "yt-dlp.exe",
        "--proxy",
        "socks5://alice:s3cr3t@127.0.0.1:1080",
        "--cookies",
        r"C:\Users\alice\FluentYTDL\bin\cookies.txt",
        "--extractor-args",
        "youtube:po_token=web+SECRETTOKEN;player_client=default",
        "--add-header",
        "Authorization: Bearer abcdef123",
        "--username",
        "alice@example.com",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    rendered = render_argv(cmd)
    for leak in ("alice", "s3cr3t", "SECRETTOKEN", "abcdef123", "example.com"):
        assert leak not in rendered, f"泄漏了 {leak}"
    assert "127.0.0.1:1080" in rendered
    assert "dQw4w9WgXcQ" in rendered
    assert "cookies.txt" in rendered, "cookie 文件名要留，用于分辨用的是哪一份"
    assert "player_client=default" in rendered, "非敏感的 extractor-args 要留"


def test_argv_sanitization_handles_equals_form():
    """yt-dlp 两种写法都接受，脱敏不能只认空格分隔那种。"""
    assert "s3cr3t" not in render_argv(["--proxy=http://u:s3cr3t@h:8080"])


def test_sanitize_proxy_keeps_host_and_port():
    assert sanitize_proxy("socks5://u:p@1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert sanitize_proxy("") == ""


def test_sanitize_path_folds_home_only():
    """折用户名，不折结构 —— 哪个盘、哪个子目录、什么文件名都还看得见。"""
    assert sanitize_path(Path.home() / "Videos" / "a.mp4").startswith("~")
    assert sanitize_path(r"D:\Media\a.mp4") == r"D:\Media\a.mp4"
    assert "bob" not in sanitize_path(r"C:\Users\bob\a.mp4")


def test_sanitize_argv_tolerates_empty():
    assert sanitize_argv(None) == []
    assert sanitize_argv([]) == []


def test_path_fields_are_sanitized_in_events(events):
    """日志是用户直接贴到 Issue 里的东西 —— Path 字段不能带出 Windows 用户名。"""
    emit_event("actual", path=Path.home() / "Videos" / "out.mp4")
    assert events[0]["path"].startswith("~")


# ── 转发层不该污染调用点定位 ────────────────────────────────


def test_trace_emit_reports_the_business_call_site():
    """每条事件都显示 `trace.py:emit` 的话，`{name}:{function}:{line}` 就白记了。"""
    seen: list[str] = []
    sink_id = logger.add(
        lambda m: seen.append(m.record["function"]),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        FlowTrace().emit("signal", code="x")
        FlowTrace().enter("parse")
        TaskTrace().finish("success")
    finally:
        logger.remove(sink_id)
    assert seen and all(fn == "test_trace_emit_reports_the_business_call_site" for fn in seen), seen
