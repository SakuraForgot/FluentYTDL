"""一键导出 bug 包。

**必须走 `identity` 事件反查 flow**，不能只按 task 收文件。任务的解析段发生在
`create_worker()` 之前，那时还没有 `db_id`，事件记的是 `flow=k72f task=-` ——
只按 task 收，bug 包就会缺掉"用户到底解析了什么、选了什么"这一整段，而那正是
字幕/音轨类问题的现场。
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from ..utils.paths import user_data_dir
from .events import SESSION_ID
from .sinks import get_trace_dir

#: app 主日志最多带走多少字节（取尾部）。整份 7 天日志可能上百 MB。
_MAX_APP_LOG_BYTES = 2 * 1024 * 1024


def resolve_flow_for_task(task_id: str | int) -> str:
    """扫 trace 目录里的 `identity` 事件，找出该 task 属于哪个 flow。

    找不到返回空串 —— 那通常意味着该任务是旧版本创建的（没有 flow 标识），
    此时只能退回按 task 收文件。
    """
    target = str(task_id)
    trace_dir = get_trace_dir()
    try:
        candidates = sorted(
            trace_dir.glob("flow-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except Exception:
        return ""
    for path in candidates:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"identity"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("kind") == "identity" and str(event.get("task")) == target:
                        flow = str(event.get("flow") or "")
                        if flow and flow != "-":
                            return flow
        except Exception:
            continue
    return ""


def _collect_files(task_id: str | int, flow_id: str) -> list[tuple[Path, str]]:
    """返回 [(源路径, zip 内相对路径)]。"""
    task = str(task_id)
    trace_dir = get_trace_dir()
    picked: list[tuple[Path, str]] = []

    def add(path: Path, arcname: str) -> None:
        if path.exists() and path.is_file():
            picked.append((path, arcname))

    if flow_id:
        add(trace_dir / f"flow-{flow_id}.jsonl", f"traces/flow-{flow_id}.jsonl")
        # 体积旋转产生的历史分片
        for extra in sorted(trace_dir.glob(f"flow-{flow_id}.*.jsonl")):
            add(extra, f"traces/{extra.name}")
    # 独立 task jsonl（无 flow 时的退回路由）
    add(trace_dir / f"task-{task}.jsonl", f"traces/task-{task}.jsonl")

    # raw yt-dlp 输出：flow 子目录优先，另外兜一遍 no-flow
    for sub in filter(None, [flow_id, "no-flow"]):
        sub_dir = trace_dir / sub
        if not sub_dir.is_dir():
            continue
        for raw in sorted(sub_dir.glob(f"task-{task}-run-*.ytdlp.log")):
            add(raw, f"raw/{sub}/{raw.name}")

    # app 主日志（人读的那份时间线）与同步 ERROR 日志
    log_dir = user_data_dir() / "logs"
    try:
        app_logs = sorted(log_dir.glob("app_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        app_logs = []
    if app_logs:
        add(app_logs[0], f"logs/{app_logs[0].name}")
    add(log_dir / "errors_sync.log", "logs/errors_sync.log")
    return picked


def _meta(task_id: str | int, flow_id: str) -> dict[str, Any]:
    try:
        from .. import __version__ as app_version
    except Exception:
        app_version = "unknown"
    return {
        "app_version": app_version,
        "session": SESSION_ID,
        "task": str(task_id),
        "flow": flow_id or None,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }


def _write_tail(zf: zipfile.ZipFile, src: Path, arcname: str, limit: int) -> None:
    """大文件只带尾部，避免 bug 包动辄上百 MB。"""
    size = src.stat().st_size
    with open(src, "rb") as f:
        if size > limit:
            f.seek(size - limit)
            # 丢掉可能被切断的半行
            f.readline()
        data = f.read()
    zf.writestr(arcname, data)


def export_bug_bundle(
    task_id: str | int,
    dest: str | Path | None = None,
    *,
    flow_id: str | None = None,
) -> Path | None:
    """把一个任务的完整现场打成 zip，返回路径；失败返回 None。

    Args:
        task_id: `task_db` 自增主键（= `worker.db_id`）。
        dest: 目标 zip 路径或目录。缺省写到 `logs/bundles/`。
        flow_id: 已知 flow 时直接给，省掉一次全目录扫描。
    """
    try:
        resolved_flow = flow_id if flow_id is not None else resolve_flow_for_task(task_id)
        files = _collect_files(task_id, resolved_flow)

        if dest is None:
            out_dir = user_data_dir() / "logs" / "bundles"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"fluentytdl-bug-task{task_id}.zip"
        else:
            dest = Path(dest)
            if dest.is_dir() or dest.suffix.lower() != ".zip":
                dest.mkdir(parents=True, exist_ok=True)
                out_path = dest / f"fluentytdl-bug-task{task_id}.zip"
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                out_path = dest

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "meta.json",
                json.dumps(_meta(task_id, resolved_flow), ensure_ascii=False, indent=2),
            )
            for src, arcname in files:
                try:
                    if arcname.startswith("logs/") and src.stat().st_size > _MAX_APP_LOG_BYTES:
                        _write_tail(zf, src, arcname, _MAX_APP_LOG_BYTES)
                    else:
                        zf.write(src, arcname)
                except Exception:
                    continue
        logger.info(
            "[Bundle] 已导出 task={} flow={} 文件数={} → {}",
            task_id,
            resolved_flow or "-",
            len(files),
            out_path.name,
        )
        return out_path
    except Exception:
        logger.exception("[Bundle] 导出 bug 包失败 task={}", task_id)
        return None
