from __future__ import annotations

import json
import os
import time
from collections import deque
from functools import partial
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..core.config_manager import config_manager
from ..observability import NO_ID, FlowTrace, emit_event, new_flow
from ..storage.db_writer import db_writer
from ..storage.task_db import UNFINISHED_STATES, task_db
from ..utils.logger import logger
from .workers import DownloadWorker

#: 从这两个状态迁出是非法的：任务已经收尾，不该再冒出新状态。
#:
#: **刻意不复用 `task_db.TERMINAL_STATES`** —— 那个集合含 `error`，而 `workers.py` 的
#: 两条规则驱动重试路径都在 `force_update("error", ...)` 之后紧跟
#: `force_update("parsing", ...)`，`error → parsing` 在同一个 worker 上完全合法。
#:
#: 也刻意做成"只封这两个"的黑名单，而不是穷举白名单：过严的迁移表会刷出一片假告警，
#: 而假告警只会训练读日志的人忽略告警 —— 那比没有这张表更糟。未知状态一律放行。
_SEALED_TRANSITION_SOURCES = frozenset({"completed", "cancelled"})


def _emit_transition(worker: DownloadWorker, to_state: str, pct: float) -> bool:
    """状态迁移的**唯一**权威产生点（硬规则 1）。

    为什么是这里：它同时具备三个条件 —— 有 `db_id`、下一句就要写 `task_db`、
    处在"UI 状态 → 持久状态"的边界。落在这里的一条 `transition` 表达的是
    **"这个状态已被系统接受"**，而不是"某个组件想让 UI 显示什么"。

    **必须按状态变化去重。** `unified_status` 每个进度 tick 都带着
    `state="downloading"` 触发本闭包，无条件 emit 会让 `count(kind=transition)`
    随进度刷新翻上十几倍，所有基于事件的统计当场失效。

    `from` 不能读 `worker._final_state`：`_on_clean_update()` 在 emit 信号**之前**
    就把它改成了新值，而本闭包是 `QueuedConnection`，跑到这里时看到的已经是新值。
    所以基线单独存一份 `_last_transition_state`。
    """
    run = getattr(getattr(worker, "trace", None), "run_id", None)
    if getattr(worker, "_transition_run", None) != run:
        worker._transition_run = run
        worker._last_transition_state = None
        worker._late_transition_reported = False
    prev = getattr(worker, "_last_transition_state", None)
    if to_state == prev:
        return True
    if prev in _SEALED_TRANSITION_SOURCES:
        if not getattr(worker, "_late_transition_reported", False):
            worker._late_transition_reported = True
            emit_event(
                "signal",
                trace=worker.trace,
                level="WARNING",
                code="late_status_ignored",
                previous=prev,
                requested=to_state,
            )
        return False
    worker._last_transition_state = to_state
    emit_event(
        "transition",
        trace=worker.trace,
        level="WARNING" if prev in _SEALED_TRANSITION_SOURCES else "INFO",
        # `from` 是 Python 关键字，只能走 `fields` 这条显式入口。
        fields={"from": prev or NO_ID, "to": to_state, "pct": round(pct, 1)},
    )
    return True


#: 上个会话遗留的 state → 本次启动该给那个 run 补记的 outcome。
#:
#: **为什么终态审计只能在启动时做。** 被任务管理器强杀 / 断电的进程不可能 emit 自己的
#: 终态 —— 再同步的 sink 也写不出一条从未产生的事件。唯一还补得上这一笔的地方就是下次
#: 启动的恢复审计，而 `load_unfinished_tasks()` 本来就在逐行核对 `tasks` 表，只是一声不响。
#:
#: 两组的语义分得很开，不能合并：上一组是**执行到一半被打断**（`processing` 在 FFmpeg
#: 合并期间发射，是发射次数最多的状态）；下一组是**从未真正开跑**，只是队列态被恢复 ——
#: `suspend_pending()` 只挂起 `not isRunning() and not isFinished()` 的 worker，所以
#: `quality_guard` 本质是 `queued` 的变体。
#:
#: `paused` 与 `error` 都不在表里，是刻意的：前者关机前本就是稳定态，后者的 run 早在上个
#: 会话由错误边界给过 `outcome=failed`，再补一条就成了同一个 run 两个终态（破硬规则 4）。
_INTERRUPTED_PRIOR_STATES = frozenset({"downloading", "parsing", "processing", "running"})
_RESTORED_PENDING_PRIOR_STATES = frozenset({"queued", "quality_guard"})


def _emit_recovery_audit(row: dict[str, Any], flow: FlowTrace | None) -> None:
    """给上个会话没善终的 run 补一条迟到的 `outcome`。

    **这是全项目唯一不走 `TaskTrace.finish()` 的 outcome 产生点**，两个理由都是硬的：

    1. 没有活着的 trace 可以 `finish()`。run A 的 `TaskTrace` 随上个进程一起死了；
       这里是从库里的 `last_run_id` 把它的身份**重建**出来，不是给一个活对象收尾。
    2. `restored_pending` 必须能表达 `run=-`。那种任务从没 `start()` 过，
       `_on_run_started` 也就从没落过 `last_run_id`；而 `TaskTrace.__post_init__` 见到
       空 run_id 会**铸一个新的**，日志里于是凭空多出一个从未执行过的 run。`run=-` 才是实话。

    exactly-once 不受影响：这一条是 run A **唯一**的 outcome（它自己那次根本没写出来），
    随后恢复出的 run B 有它自己的终态边界。

    identity 里的 `run` 取落库值、`flow` 取本轮启动的 `flow`：事件因此读作
    `task=42 run=A outcome=interrupted`，指向刚死的那个 run，同时和随后在同一个 restore
    flow 下铸出的 run B 挂进时间线的同一条链，闭环。
    """
    state = str(row.get("state") or "")
    if state in _INTERRUPTED_PRIOR_STATES:
        outcome = "interrupted"
    elif state in _RESTORED_PENDING_PRIOR_STATES:
        outcome = "restored_pending"
    else:
        return

    audit = FlowTrace(
        flow_id=flow.flow_id if flow is not None else "",
        session_id=flow.session_id if flow is not None else "",
        stage="startup",
        task_id=str(row.get("id") or NO_ID),
        # `interrupted` 正常都拿得到 run A；拿不到只说明是加列之前的老库行 —— 那也照发，
        # 让"这个任务被打断过"这件事可见，比为了字段齐整而沉默有用。
        run_id=str(row.get("last_run_id") or "") or NO_ID,
    )
    emit_event(
        "outcome",
        trace=audit,
        level="WARNING" if outcome == "interrupted" else "INFO",
        outcome=outcome,
        # 字段形状与 `TaskTrace.finish()` 对齐，让 `kind=outcome` 在 JSONL 里 schema 恒定
        # （`jq 'select(.kind=="outcome") | .degraded'` 不该时有时无）。三个都空 ——
        # 被强杀的 run 没留下任何"已容忍"或"降级"的证据，凭空填就是编造。
        recovered=False,
        recovery=None,
        degraded=False,
        missing=[],
        previous_state=state,
        reason="unclean_shutdown" if outcome == "interrupted" else None,
        # 上个会话 / 操作链只当**字段**记，不占 identity：identity 必须与写这条日志的进程
        # 一致，否则 session Y 的日志里混进 session X 的事件，按 session 分组当场就乱。
        # 当字段一样够用 —— bug 包据此反查得到 run A 原来那条 flow 的 JSONL。
        previous_session=str(row.get("last_session_id") or "") or None,
        previous_flow=str(row.get("last_flow_id") or "") or None,
    )


class DownloadManager(QObject):
    # 通知 UI：任务列表/状态变化（新增/结束/删除/暂停等）
    task_updated = Signal()
    status_msg = Signal(str)
    completed = Signal(str)
    worker_error = Signal(dict)
    #: 任务**成功**了但有该说的话（目前只有字幕三码）。载荷同 `worker_error`，都是
    #: `Diagnosis.to_dict()`；分成两个信号是因为去向不同 —— 错误弹模态框，这个只弹
    #: InfoBar，绝不能打断用户。
    worker_warning = Signal(dict)

    # 启动恢复的单页大小。未完成任务通常只有几十条，分页只是为了给「几千条卡在
    # paused」的极端库设一个每次查询的上界，不是为了惰性加载 —— 这里必须全取。
    _RESTORE_PAGE = 500

    def __init__(self) -> None:
        super().__init__()
        self.active_workers: list[DownloadWorker] = []
        self._pending_workers: deque[DownloadWorker] = deque()
        self.load_unfinished_tasks()

    def load_unfinished_tasks(self) -> None:
        """从 TaskDB 加载未能完成的会话（崩溃或退出留下的）。

        只查「未完成 + error」这几种状态，**不再 `get_all_tasks()`**：终态行归列表的
        `fetchMore()` 分页负责（见 `storage/task_db.py` 里的状态所有权划分），
        而 `get_all_tasks` 会把几万条已完成记录整表物化成 dict 再逐条 `continue` 掉
        —— 启动耗时和内存都白花在这上面。

        `error` 例外地也在这里恢复：它要在 UI 上以「可重试」的壳出现（下面会
        `restore_state("error")` 但既不入队也不 start），并且过期的 error 行在这里
        才有机会按 `failed_task_retention_days` 清掉。分页那边撞上同一个 `db_id`
        会被 `_by_db_id` 去重，不会列两次。
        """
        tasks: list[dict[str, Any]] = []
        page_key: tuple[float, int] | None = None
        while True:
            page = task_db.query_tasks(
                states=UNFINISHED_STATES + ("error",),
                limit=self._RESTORE_PAGE,
                before_key=page_key,
            )
            if not page:
                break
            tasks.extend(page)
            page_key = task_db.page_key(page[-1])
            if len(page) < self._RESTORE_PAGE:
                break

        # tasks 是按 updated_at 倒序的，反转以按先后顺序加载（`add_task` 插在 row 0，
        # 所以先加载的最终排在下面）。这和分页补进来的历史行同一个排序键，
        # 融合后整个列表才是单调的。
        #
        # 这一整轮恢复算**一个 flow**：它本身就是一次用户操作链（启动），而且 P1-B 的
        # 恢复审计要把 `outcome=interrupted` 挂在同一条链上，才能和随后铸出的新 run
        # 在时间线里串起来。
        restore_flow = new_flow(stage="startup") if tasks else None

        for row in reversed(tasks):
            state = row.get("state", "queued")
            if state in ("completed", "cancelled"):
                continue

            # error 状态的任务：检查过期时间，并在未过期时恢复到 UI 以便用户重试或清理
            if state == "error":
                retention_days = config_manager.get("failed_task_retention_days", 3)
                if retention_days > 0:
                    updated_at = row.get("updated_at", 0)
                    now = time.time()
                    if now - updated_at > retention_days * 86400:
                        task_db.delete_task(row["id"])
                        continue
                # 不自动重试，仅保持 error 状态展示在 UI
                pass  # 将在下方创建 Worker 壳

            # 恢复审计必须在**下面那次状态降级之前**：那里会把 running/downloading/
            # parsing/processing/queued 一律改写成 paused，改完就再也看不出上个会话
            # 究竟死在哪个阶段 —— 而"死在哪个阶段"正是 interrupted 与 restored_pending
            # 的分界。读的是 `row`（原始持久值），降级只动局部变量和库，不动它。
            _emit_recovery_audit(row, restore_flow)

            opts = json.loads(row.get("ydl_opts_json", "{}"))

            # skip_download 任务（纯字幕/封面提取）不应跨会话恢复：
            # 它们依赖的弹窗上下文（SubtitlePickerResult 等）已丢失，
            # 恢复后会以过时的参数自动重跑，产生幽灵任务。
            if opts.get("skip_download", False):
                task_db.update_task_status(
                    row["id"], "error", 0.0, tr_text("⚠️ 提取任务未能完成（应用已重启）")
                )
                continue

            # 如果重启前是运行、解析、后处理或排队状态，一律自动降级为暂停，不自动恢复下载。
            # `processing`（FFmpeg 合并 / 嵌字幕 / 封面）必须在列：它以前不在这个元组里，
            # 也不在 `UNFINISHED_STATES` 里，于是合并期间关软件的任务两条加载路径都捞不到，
            # 直接从 UI 消失。
            if state in ("running", "downloading", "parsing", "processing", "queued"):
                state = "paused"
                task_db.update_task_status(
                    row["id"], state, row.get("progress", 0.0), tr_text("⏸️ 下载已暂停 (应用重启)")
                )

            # error 状态不入队也不 start，仅创建 Worker 壳展示在 UI
            if state == "error":
                cached = {"title": row.get("title", ""), "thumbnail": row.get("thumbnail_url", "")}
                worker = self.create_worker(
                    row["url"], opts, cached_info=cached, restore_db_id=row["id"], flow=restore_flow
                )
                worker.restore_state("error")
                worker.progress_val = row.get("progress", 0.0)
                worker.status_text = row.get("status_text", "")
                worker.v_title = row.get("title", "")
                worker.v_thumbnail = row.get("thumbnail_url", "")
                worker.output_path = row.get("output_path", "")
                continue

            cached = {"title": row.get("title", ""), "thumbnail": row.get("thumbnail_url", "")}

            worker = self.create_worker(
                row["url"], opts, cached_info=cached, restore_db_id=row["id"], flow=restore_flow
            )

            # 手工同步 Worker 上下文使其与 DB 呈现一致
            worker.restore_state(state)
            worker.progress_val = row.get("progress", 0.0)
            worker.status_text = row.get("status_text", "")
            worker.v_title = row.get("title", "")
            worker.v_thumbnail = row.get("thumbnail_url", "")
            worker.output_path = row.get("output_path", "")
            worker.total_bytes = row.get("file_size", 0)

            if state == "queued":
                self._pending_workers.append(worker)
            elif state == "paused":
                # 对于 paused 状态，调用 pause 会设置红绿灯
                worker._cancel_event.clear()
                worker._pause_event.clear()

        # 任务恢复完成后回收孤儿沙盒（照 utils/paths.py:604-606 的「开工先清」写法）。
        # 存活判据是 staging_id，不是 task_key 也不是 run_id（见 staging.py 的 GC 一节）。
        # 取配置下载目录 + 所有已恢复任务各自的下载目录，各自独立调用 gc_orphans —— 不同
        # 下载目录各有自己的 .fluent_temp，合并成一个集合扫不出来。
        try:
            from .staging import gc_orphans

            live_ids: set[str] = set()
            gc_dirs: set[str] = set()

            for w in (*self.active_workers, *self._pending_workers):
                if getattr(w, "staging", None) is not None:
                    sid = getattr(w.staging, "staging_id", None)
                    if sid:
                        live_ids.add(sid)
                d = getattr(w, "download_dir", None)
                if d:
                    gc_dirs.add(str(d))

            cfg_dir = config_manager.get("download_dir", "")
            if cfg_dir:
                gc_dirs.add(str(cfg_dir))

            for d in gc_dirs:
                try:
                    gc_orphans(d, live_ids)
                except Exception:
                    log_text(logger, "exception", "gc_orphans 失败: {}", d)
        except Exception:
            log_text(logger, "exception", "启动 GC 失败")

        # cookie 运行副本的启动兜底：`cookie_runfile()` 的 `finally` 已在子进程结束时确定性
        # 删除副本，这里只回收「崩溃 / TerminateProcess / IDE 强杀 / 断电」导致 `finally`
        # 没跑到的 `fluentytdl_ck_*` 漏网文件（沿用上面 gc_orphans 的「开工先清」思路）。
        try:
            from ..auth.cookie_runfile import sweep_stale_cookie_runfiles

            sweep_stale_cookie_runfiles()
        except Exception:
            log_text(logger, "exception", "cookie 运行副本启动清理失败")

        # 最后不 pump()，要等 UI 初始化完后再由其他流程触发或用户手动恢复

    def _max_concurrent(self) -> int:
        try:
            n = int(config_manager.get("max_concurrent_downloads", 3) or 3)
        except Exception:
            logger.debug("max_concurrent_downloads parse error, using default")
            n = 3
        # Use 32-bit int max to avoid overflow in UI / Qt validators.
        return max(1, min(2_147_483_647, n))

    def _running_count(self) -> int:
        return sum(1 for w in self.active_workers if w.isRunning())

    @staticmethod
    def _is_precise_section_worker(worker: DownloadWorker) -> bool:
        return getattr(worker, "opts", {}).get("__fluentytdl_section_cut_mode") == "precise"

    def _has_running_precise_section(self) -> bool:
        return any(
            worker.isRunning() and self._is_precise_section_worker(worker)
            for worker in self.active_workers
        )

    def running_count(self) -> int:
        return self._running_count()

    def pending_count(self) -> int:
        return len(self._pending_workers)

    def has_active_tasks(self) -> bool:
        return self.running_count() > 0 or self.pending_count() > 0

    def _remove_from_pending(self, worker: DownloadWorker) -> None:
        if not self._pending_workers:
            return
        try:
            self._pending_workers = deque([w for w in self._pending_workers if w is not worker])
        except Exception:
            logger.debug("_remove_from_pending failed")

    def is_queued(self, worker: DownloadWorker) -> bool:
        return any(w is worker for w in self._pending_workers)

    def pump(self) -> None:
        """Start queued downloads until reaching the concurrency limit."""

        limit = self._max_concurrent()
        while self._pending_workers and self._running_count() < limit:
            precise_running = self._has_running_precise_section()
            candidate_index = next(
                (
                    index
                    for index, worker in enumerate(self._pending_workers)
                    if not (precise_running and self._is_precise_section_worker(worker))
                ),
                None,
            )
            if candidate_index is None:
                break
            self._pending_workers.rotate(-candidate_index)
            w = self._pending_workers.popleft()
            self._pending_workers.rotate(candidate_index)
            try:
                if w.isRunning() or w.isFinished():
                    continue
                w.start()
            except Exception:
                continue
        self.task_updated.emit()

    def create_worker(
        self,
        url: str,
        opts: dict[str, Any],
        cached_info: dict[str, Any] | None = None,
        restore_db_id: int = 0,
        *,
        flow: FlowTrace | None = None,
    ) -> DownloadWorker:
        from ..core.config_manager import config_manager
        from ..utils.youtube_request import COOKIE_MODE

        opts = dict(opts)
        from ..utils.url_router import UrlRouter

        if UrlRouter.detect_platform(url) == "youtube" and COOKIE_MODE not in opts:
            opts[COOKIE_MODE] = (cached_info or {}).get(
                COOKIE_MODE, bool(config_manager.get("youtube_cookies_enabled", True))
            )
            if restore_db_id > 0:
                task_db.update_task_opts(restore_db_id, opts)
        worker = DownloadWorker(url, opts, cached_info=cached_info, flow=flow)

        # 1. 登记入库，建立 Worker 的持久化主键
        if restore_db_id > 0:
            worker.db_id = restore_db_id
        else:
            db_id = task_db.insert_task(url, opts)
            worker.db_id = db_id
            # 只有新创建的才录入缓存元数据，恢复的不用覆盖
            if cached_info:
                t_title = cached_info.get("title", "")
                t_thumb = cached_info.get("thumbnail", "")
                db_writer.enqueue_metadata(db_id, t_title, str(t_thumb) if t_thumb else "")

        # 2. 把 flow → task 钉进日志。解析/选择阶段的事件只带 `flow=k72f task=-`，
        # 这条 identity 是**事后**从 task 反查回解析段的唯一线索（bug 包据此收集）。
        # 必须在 db_id 赋值之后：在此之前 trace 的 task_id 还是占位符。
        worker.trace.bind_task_id(worker.db_id)

        # 3. 建立单写者“过桥”连接 (强制在 QObject 的宿主线程即主线程执行写操作)
        def _on_run_started():
            # `QThread.started` 每次 `start()` 恰好一次 —— 也就是一个 run 恰好一次。
            # 挂在这里而不是 worker 内部：DB 写入归本层，`workers.py` 不碰 storage。
            # 只有真正跑起来的 run 才落库，恢复出来当壳用的 worker（error 行、排队行）
            # 不会污染 `last_run_id`，否则下次启动的恢复审计会给一个从未执行的 run
            # 记 `outcome=interrupted`。
            trace = worker.trace
            db_writer.enqueue_run_identity(
                worker.db_id, trace.session_id, trace.run_id, trace.flow_id
            )

        def _on_unified_status(state: str, pct: float, msg: str):
            # 先落 transition 再写库：两句都在主线程、同一个 tick 内完成，顺序不影响
            # 结果，但"日志里看到已接受 → 库里才有"读起来才是因果顺序。
            if not _emit_transition(worker, state, pct):
                return
            db_writer.enqueue_status(worker.db_id, state, pct, msg)

        def _on_output_ready(path: str):
            fsize = 0
            if path and os.path.exists(path):
                fsize = os.path.getsize(path)
            if fsize == 0:
                fsize = getattr(worker, "total_bytes", 0)
            db_writer.enqueue_result(worker.db_id, path, fsize)

        def _on_completed():
            path = getattr(worker, "output_path", "")
            fsize = 0
            if path and os.path.exists(path):
                fsize = os.path.getsize(path)
            if fsize == 0:
                fsize = getattr(worker, "total_bytes", 0)
            if path:
                db_writer.enqueue_result(worker.db_id, path, fsize)
            self.task_updated.emit()

        def _on_error(err: dict):
            self.task_updated.emit()
            self.worker_error.emit(err)

        worker.started.connect(_on_run_started, Qt.ConnectionType.QueuedConnection)
        worker.unified_status.connect(_on_unified_status, Qt.ConnectionType.QueuedConnection)
        worker.output_path_ready.connect(_on_output_ready, Qt.ConnectionType.QueuedConnection)

        self.active_workers.append(worker)

        # When a worker ends, free a slot and pump queued tasks.
        worker.finished.connect(partial(self._on_worker_finished, worker))
        worker.completed.connect(_on_completed)
        worker.cancelled.connect(self.task_updated.emit)
        worker.error.connect(_on_error)
        # 只转发，不 task_updated：任务状态没变（还是 completed），刷列表纯属白刷。
        worker.task_warning.connect(self.worker_warning.emit)
        return worker

    def _on_worker_finished(self, worker: DownloadWorker) -> None:
        self._remove_from_pending(worker)
        # 清理已完成的 Worker，防止 active_workers 无限增长
        if worker in self.active_workers and not worker.isRunning():
            self.active_workers.remove(worker)
        self.pump()

    def start_worker(self, worker: DownloadWorker) -> bool:
        """Start worker if a slot is available; otherwise queue it."""

        self._remove_from_pending(worker)

        if worker.isRunning():
            return True
        if worker.isFinished():
            return False

        can_start_precise = not (
            self._is_precise_section_worker(worker) and self._has_running_precise_section()
        )
        if self._running_count() < self._max_concurrent() and can_start_precise:
            try:
                worker.start()
                self.task_updated.emit()
                return True
            except Exception:
                return False

        self._pending_workers.append(worker)
        self.task_updated.emit()
        return False

    def stop_all(self) -> None:
        self._pending_workers.clear()
        for worker in list(self.active_workers):
            if worker.isRunning():
                worker.cancel()

    def suspend_pending(self) -> int:
        """挂起所有处于排队状态的任务。返回被挂起的任务数量"""
        count = 0
        pending_list = list(self._pending_workers)
        self._pending_workers.clear()

        for worker in pending_list:
            if not worker.isRunning() and not worker.isFinished():
                worker._final_state = "quality_guard"
                db_writer.enqueue_status(
                    worker.db_id,
                    "quality_guard",
                    0.0,
                    tr_text("连续画质未达标，排队任务已暂停"),
                )
                if hasattr(worker, "unified_status"):
                    worker.unified_status.emit("quality_guard", 0.0, tr_text("画质检查暂停"))
                count += 1

        if count > 0:
            self.task_updated.emit()

        return count

    def shutdown(self, grace_ms: int = 2000) -> bool:
        """Stop all workers and wait for them to exit.

        Returns True if all workers have stopped within the grace period.
        """

        self.stop_all()

        all_stopped = True
        for worker in list(self.active_workers):
            if not worker.isRunning():
                continue
            try:
                # Try graceful wait first.
                if not worker.wait(grace_ms):
                    all_stopped = False
                    # Last resort: terminate the thread.
                    try:
                        worker.terminate()
                    except Exception:
                        logger.debug("Failed to terminate worker thread")
                    try:
                        worker.wait(500)
                    except Exception:
                        logger.debug("Failed to wait for worker thread after terminate")
            except Exception:
                all_stopped = False

        # 确保所有待写数据落盘后再退出
        db_writer.flush_and_stop(timeout=3.0)

        return all_stopped

    def start_all(self) -> None:
        # QThread 结束后不能重用；“继续/重试”应由 UI 重建 worker。
        self.pump()

    def remove_worker(self, worker: DownloadWorker) -> None:
        self._remove_from_pending(worker)
        if worker in self.active_workers:
            if worker.isRunning():
                worker.cancel()
                # 移除阻塞式等待，防 UI 卡死
            self.active_workers.remove(worker)
            self.task_updated.emit()


download_manager = DownloadManager()
