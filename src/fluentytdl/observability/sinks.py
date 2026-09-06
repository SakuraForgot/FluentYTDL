"""JSONL sink、raw dump sink、同步 ERROR sink、trace 目录清扫。

## 设计决策

- **JSONL 以 `flow_id` 为路由键**，`task_id` 只是字段。一个 flow 天然包含解析段
  + 下载段（Playlist 甚至是一对多），按 task 路由会让同一条链物理裂成两个文件，
  导出 bug 包时必然漏掉最前面的解析段。无 flow 时才退回 `task-<id>.jsonl`。

- **开句柄 LRU**（上限 8）**+ 单文件体积上限**（512 MB）。开太多句柄会撑高 fd 数；
  单文件不封顶的话，活跃 Playlist 任务能把一个 jsonl 写到几百 MB，`bundle.py` 打
  zip 就会卡住。

- **`enqueue=True` 做写入串行化**，单消费者线程，不需要自己加锁。

- **同步 ERROR sink**：`enqueue=True` 的写入线程在进程被强杀时来不及 flush。补一个
  `enqueue=False` 的同步 ERROR/CRITICAL sink，保证崩溃前最后一条**已产生**的错误
  一定落盘。它的体积很小（只有 ERROR+，不是 DEBUG 全量），不会把磁盘写穿。

- **raw dump 跳过进度行**：`--newline` 下进度行数千条，若不过滤则 2000 行全是噪音；
  `should_keep_raw_line()` 只做一次 `startswith` 检查，代价极低。

- **raw dump 触发条件**：`failed` / `degraded` / `recovered`。正常 `success` 彻底
  不留。`recovered` 也留 —— "rc=1 但判定文件可用"本身就是值得保留的异常成功。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger

from ..utils.paths import user_data_dir
from .events import emit_event

# ── 目录 ────────────────────────────────────────────────────

_TRACE_DIR: Path | None = None
_TRACE_DIR_LOCK = threading.Lock()


def get_trace_dir() -> Path:
    global _TRACE_DIR
    if _TRACE_DIR is not None:
        return _TRACE_DIR
    with _TRACE_DIR_LOCK:
        if _TRACE_DIR is None:
            d = user_data_dir() / "logs" / "traces"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                d = Path(os.environ.get("TEMP", "/tmp")) / "FluentYTDL_traces"
                d.mkdir(parents=True, exist_ok=True)
            _TRACE_DIR = d
    return _TRACE_DIR


# ── JSONL sink ───────────────────────────────────────────────

_MAX_OPEN_FILES = 8
_MAX_JSONL_BYTES = 512 * 1024 * 1024  # 512 MB

#: {path: (file_object, size_bytes)}，按最近访问排序（OrderedDict 模拟 LRU）。
_open_files: OrderedDict[Path, tuple[Any, int]] = OrderedDict()
_open_files_lock = threading.Lock()


def _lru_get(path: Path) -> Any:
    """返回打开的文件句柄，必要时淘汰最旧的。调用方需持有 `_open_files_lock`。"""
    if path in _open_files:
        _open_files.move_to_end(path)
        return _open_files[path][0]
    while len(_open_files) >= _MAX_OPEN_FILES:
        _, evicted = _open_files.popitem(last=False)
        try:
            evicted[0].close()
        except Exception:
            pass
    size = path.stat().st_size if path.exists() else 0
    fh = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - 长期持有
    _open_files[path] = (fh, size)
    return fh


def _jsonl_path(record: dict) -> Path | None:
    """按 flow_id 路由，无 flow 才退回 task。"""
    extra = record.get("extra") or {}
    fytdl = extra.get("fytdl") or {}
    flow = fytdl.get("flow") or "-"
    task = fytdl.get("task") or "-"
    if flow and flow != "-":
        name = f"flow-{flow}.jsonl"
    elif task and task != "-":
        name = f"task-{task}.jsonl"
    else:
        return None
    return get_trace_dir() / name


def jsonl_sink(message: Any) -> None:
    """loguru sink：把带 `fytdl` extra 的记录写成一行 JSON。

    任何异常都静默 —— 这里出错不能影响下载（硬规则 5）。
    """
    try:
        record = message.record
        if "fytdl" not in record.get("extra", {}):
            return
        path = _jsonl_path(record)
        if path is None:
            return
        payload = dict(record["extra"]["fytdl"])
        payload["_ts"] = record["time"].timestamp()
        # `_level` 与 `_ts` 同性质：loguru 记录的元数据，不是事件字段（封闭集合见
        # `events.py`），所以带下划线前缀。缺了它，JSONL 读回来的历史事件分不出
        # WARNING 和 INFO —— 日志查看器的时间线页回填历史时正是这么读的，
        # 导 bug 包后 `jq 'select(._level=="ERROR")'` 也才有得筛。
        payload["_level"] = record["level"].name
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        with _open_files_lock:
            fh = _lru_get(path)
            old_size = _open_files[path][1]
            if old_size >= _MAX_JSONL_BYTES:
                # 旋转：关旧、重命名（加时间戳后缀），开新
                try:
                    fh.close()
                except Exception:
                    pass
                try:
                    rotated = path.with_suffix(f".{int(time.time())}.jsonl")
                    path.rename(rotated)
                except Exception:
                    pass
                _open_files.pop(path, None)
                fh = _lru_get(path)
            fh.write(line)
            fh.flush()
            new_size = old_size + len(encoded)
            _open_files[path] = (fh, new_size)
    except Exception:
        pass


# ── raw dump ─────────────────────────────────────────────────

#: 结构化进度行的专属前缀（`--progress-template` 里我们自己定的），一定跳过。
_RAW_SKIP_PREFIX = "FLUENTYTDL|"

#: `[download]  45.2% of ...` 这类百分比进度行。**只挡带百分比的那种** ——
#: `[download] Destination: ...`、`[download] ... has already been downloaded`、
#: `[download] ... Skipping ...` 同样是 `[download] ` 前缀，却正是 raw dump 存在的理由
#: （前者说明文件落在哪，后两者是"字幕/分片为什么没下"的唯一原文）。
#: 原先整个前缀一刀切，把它们一起扔了。
_RAW_PROGRESS_RE = re.compile(r"^\[download\]\s+\d{1,3}(?:\.\d+)?%")


def should_keep_raw_line(line: str) -> bool:
    """是否把这一行纳入 raw dump（进度噪音要过滤掉）。

    这是**独立可用**的判据：调用方手里没有 `YtDlpOutputParser` 时靠它兜住进度洪水。
    `download/executor.py` 另有一道更准的前置闸门（`parsed.type not in
    ("progress", "ffmpeg_progress")`，与 `diag_lines` 同一个条件）—— 两道各自成立，
    叠在一起不会互相掩盖。
    """
    if not line:
        return False
    if line.startswith(_RAW_SKIP_PREFIX):
        return False
    return not _RAW_PROGRESS_RE.match(line)


def write_raw_dump(
    flow_id: str,
    task_id: str | int,
    run_id: str,
    lines: list[str],
) -> Path | None:
    """把 raw yt-dlp 输出写到 trace 目录，返回写入路径；出错返回 None。

    文件名格式：``flow-{flow}/task-{task}-run-{run}.ytdlp.log``
    （或 ``no-flow/task-{task}-run-{run}.ytdlp.log``）。
    """
    if not lines:
        return None
    try:
        flow_part = str(flow_id) if flow_id and str(flow_id) != "-" else "no-flow"
        task_part = str(task_id) if task_id and str(task_id) != "-" else "unknown"
        run_part = str(run_id) if run_id and str(run_id) != "-" else "unknown"
        dest_dir = get_trace_dir() / flow_part
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"task-{task_part}-run-{run_part}.ytdlp.log"
        with open(dest, "w", encoding="utf-8", errors="replace") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")
        return dest
    except Exception:
        return None


#: 哪些终态值得留原始输出。`cancelled` / `paused` 不在内 —— 用户自己按的停止不是异常；
#: `interrupted` 也不在内，进程被强杀时这段代码压根没机会跑（真正的检测在下次启动的
#: 恢复审计里，那时早就没有 raw 行了）。
_RAW_WORTHY_OUTCOMES = frozenset({"failed"})


def dump_raw_for_outcome(
    trace: Any,
    lines: Iterable[str],
    *,
    outcome: str,
    always: bool = False,
) -> Path | None:
    """在 run 的终态边界上决定要不要留 raw 输出，要留就写盘并落一条 `signal`。

    **留的条件和 outcome 的裁决在同一个地方**，这是刻意的：条件散开写就会出现
    "失败路径记了、降级路径忘了"，而"降级成功"恰好是最需要原文的一类
    —— 任务绿着完成、字幕却没有，结构化事件只说得出 `missing=[...]`，
    说不出 yt-dlp 当时那句 `WARNING:` 的原文是什么。

    四个 `reason` 分得清，因为它们回答的是不同的问题：

    - ``outcome_failed`` —— 失败，原文是复现材料。
    - ``degraded`` —— 成功但缺产物（硬规则 3 的 `expected − actual`）。
    - ``recovered`` —— rc≠0 却判定文件可用。这是**异常的成功**，体积启发式给不出
      完整性证明（见 `executor` 里 `confidence=low` 那段），原文是唯一的复核依据。
    - ``always_on`` —— 用户把 `log_raw_ytdlp` 打开了，全程记录。

    干净的 `success` 什么都不留 —— 那是绝大多数下载，留下来只会把 trace 目录写满。

    `trace` 只用 `getattr` 取标识，不要求具体类型：观测永远 best-effort（硬规则 5），
    传进来一个没有 run 的 `FlowTrace` 也只是让文件名落到 `run-unknown`，不该抛异常。
    """
    try:
        if trace is None:
            # 没有标识就没有落点：`task-unknown-run-unknown.ytdlp.log` 会被下一次
            # 无标识 dump 直接覆盖，且 `bundle.py` 按 task 反查永远找不到它。
            return None
        recovered = bool(getattr(trace, "recovered", False))
        degraded = bool(getattr(trace, "degraded", False))
        if outcome in _RAW_WORTHY_OUTCOMES:
            reason = "outcome_failed"
        elif degraded:
            reason = "degraded"
        elif recovered:
            reason = "recovered"
        elif always:
            reason = "always_on"
        else:
            return None

        materialized = [ln for ln in lines if ln]
        if not materialized:
            return None
        dest = write_raw_dump(
            getattr(trace, "flow_id", "") or "",
            getattr(trace, "task_id", "") or "",
            getattr(trace, "run_id", "") or "",
            materialized,
        )
        if dest is None:
            return None

        # 用户得知道这份文件存在，否则它只会静静躺在 trace 目录里等清扫。
        # `always_on` 走 DEBUG：那是用户自己开的全程记录，每次下载都往控制台喊一句
        # 就成了噪音。
        emit_event(
            "signal",
            trace=trace,
            level="DEBUG" if reason == "always_on" else "INFO",
            stage="finalize",
            code="raw_output_retained",
            reason=reason,
            lines=len(materialized),
            # 只记文件名：目录是固定的 trace 目录，而完整路径含 Windows 用户名。
            file=dest.name,
        )
        return dest
    except Exception:
        return None  # 硬规则 5


# ── 目录清扫 ─────────────────────────────────────────────────

_RETENTION_DAYS = 14
_MAX_TRACE_BYTES = 200 * 1024 * 1024  # 200 MB，先到者为准


def sweep_trace_dir() -> None:
    """清扫 trace 目录：保留最近 14 天 / 200 MB，两个上限先到先清。

    由启动流程在后台调用，任何异常都静默。
    """
    try:
        trace_dir = get_trace_dir()
        cutoff = time.time() - _RETENTION_DAYS * 86400
        files: list[tuple[float, Path]] = []
        for p in trace_dir.rglob("*"):
            if p.is_file():
                try:
                    files.append((p.stat().st_mtime, p))
                except Exception:
                    pass
        files.sort(key=lambda t: t[0])  # 最旧在前
        total = sum(p.stat().st_size for _, p in files if p.exists())
        for mtime, p in files:
            if not p.exists():
                continue
            too_old = mtime < cutoff
            too_big = total > _MAX_TRACE_BYTES
            if too_old or too_big:
                try:
                    size = p.stat().st_size
                    p.unlink()
                    total -= size
                    # 删完空目录
                    try:
                        p.parent.rmdir()
                    except Exception:
                        pass
                except Exception:
                    pass
    except Exception:
        pass


# ── loguru 接线 ──────────────────────────────────────────────

_sinks_installed = False
_sinks_lock = threading.Lock()


def install_sinks(log_dir: str | Path | None = None) -> None:
    """把 JSONL sink 和同步 ERROR sink 接进 loguru。

    设计为幂等：重复调用安全。由 `utils/logger.py` 的下游（或 `startup_info`）
    在日志目录确定后调用一次，不能由 `paths.py` 触发（循环依赖）。
    """
    global _sinks_installed
    with _sinks_lock:
        if _sinks_installed:
            return
        _sinks_installed = True
    try:
        # JSONL：只收带 fytdl extra 的记录，enqueue=True 做写入串行化。
        logger.add(
            jsonl_sink,
            level="DEBUG",
            filter=lambda record: "fytdl" in record.get("extra", {}),
            enqueue=True,
            format="{message}",  # 内容由 jsonl_sink 自己构造，format 无关紧要
        )
        # 同步 ERROR sink：崩溃前最后一条错误一定落盘。
        if log_dir is None:
            log_dir = user_data_dir() / "logs"
        sync_path = Path(log_dir) / "errors_sync.log"
        logger.add(
            str(sync_path),
            level="ERROR",
            enqueue=False,  # 同步写，进程强杀时不会丢
            rotation="10 MB",
            retention="7 days",
            compression="zip",
            encoding="utf-8",
        )
    except Exception as exc:
        try:
            stream = getattr(sys, "__stderr__", None) or sys.stderr
            if stream is not None:
                stream.write(f"[Observability] install_sinks failed: {exc}\n")
        except Exception:
            pass
