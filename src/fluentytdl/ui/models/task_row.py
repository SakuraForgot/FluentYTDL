"""融合后列表的行结构。

下载列表与历史记录合成一条列表之后，行有两种来源：**有 worker 的活任务**，和
**只有 DB 快照的历史行**。旧模型的行是裸 dict（`{"worker", "title", "thumbnail"}`），
它表达不了后者 —— delegate 与 proxy 一旦 `data.get("worker")` 拿到 None 就整行放弃
（前者画成空白条，后者从所有非「全部」分类里消失）。

`TaskRow` 把两种来源收进同一个结构：

* 字段是**落库快照**，命名与 `tasks` 表列一一对应，所以 `from_db_row` 是机械映射；
* `effective_*` 属性是**读穿透** —— 有 worker 就问 worker（它比 DB 新，`db_writer`
  是异步落库的），没有就退回快照。

渲染方只许读 `effective_*`，不要再 `getattr(worker, ...)`：那正是历史行画不出来的原因。

`db_id` 是唯一去重键。每个 worker 创建时就已经有 DB 行了
（`download_manager.create_worker` → `task_db.insert_task`，恢复时走 `restore_db_id`），
所以活任务与历史行天然共享同一个 `tasks.id`；模型按它建索引，「同一个任务被列两次」
这个融合前的老问题就从根上消失。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...download.workers import DownloadWorker
from ...utils.formatters import derive_format_note


def _height_or_none(value: Any) -> int | None:
    """质量偏差的高度列：DB 默认值是 0，语义上等同「没记录」。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass
class TaskRow:
    """列表里的一行。字段是 DB 快照，`effective_*` 才是渲染值。"""

    db_id: int
    url: str = ""
    title: str = ""
    thumbnail: str = ""

    # ── DB 快照 ──
    # `state` 的取值是 `task_db.TERMINAL_STATES + UNFINISHED_STATES` 的全集，比
    # `worker.effective_state` 多出 `downloading` / `parsing`（CleanLogger 写进去的）。
    state: str = "queued"
    progress: float = 0.0
    status_text: str = ""
    output_path: str = ""
    file_size: int = 0
    duration: int = 0
    # 完成时间：历史行 meta 里那个「3 分钟前」的时间基准，对应 `tasks.updated_at`。
    updated_at: float = 0.0

    # 「1080p MP4」这类画质标签。历史行没有 worker，只能从落库的 ydl_opts 反推。
    format_note: str = ""

    # 质量守卫写入的偏差记录（0 / 空串一律归一成 None）
    actual_height: int | None = None
    target_height: int | None = None
    quality_deviation: str | None = None

    # 活任务有，历史行为 None。挂上它就等于「这一行升级成活任务」。
    worker: DownloadWorker | None = None

    # None = 尚未检查。存在性检查要走后台线程池，不许在 GUI 线程里逐行 os.path.exists。
    file_exists: bool | None = None

    # `format_note` 上次是按哪个输出路径算出来的。活任务建行时 output_path 还是空的
    # （「1080p」少了容器后缀），下载完成后才知道，靠这个字段判断要不要重算 —— 不能改成
    # 每次 paint 都算：一次 paint 一个正则 + 一次 splitext，滚动时是每帧每行。
    _note_src: str = field(default="", repr=False, compare=False)

    # ── 读穿透属性（渲染方只认这一组）──

    @property
    def is_live(self) -> bool:
        return self.worker is not None

    @property
    def effective_state(self) -> str:
        """有 worker 就用它的权威推断，否则用快照。

        `worker.effective_state` 内部会调 `QThread.isRunning()`，比读字段贵，但它是
        唯一能消除「`_final_state` 与线程实际状态不一致窗口」的来源，不能绕开。
        """
        if self.worker is not None:
            return self.worker.effective_state
        return self.state

    @property
    def effective_progress(self) -> float:
        # progress_val / status_text 是 `_on_clean_update` 第一次触发时才建的属性，
        # 新建的 worker 上并不存在 —— 一律 getattr 带默认值。
        if self.worker is not None:
            try:
                return float(getattr(self.worker, "progress_val", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return self.progress

    @property
    def effective_status_text(self) -> str:
        if self.worker is not None:
            return str(getattr(self.worker, "status_text", "") or "")
        return self.status_text

    @property
    def effective_output_path(self) -> str:
        """worker 的路径优先：它在下载途中就更新了，DB 那一列要等 `db_writer` 排到。"""
        if self.worker is not None:
            live = str(getattr(self.worker, "output_path", "") or "")
            if live:
                return live
        return self.output_path

    @property
    def effective_file_size(self) -> int:
        if self.worker is not None:
            try:
                n = int(getattr(self.worker, "total_bytes", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n
        return self.file_size

    @property
    def effective_title(self) -> str:
        # 解析完成后 worker 才拿到真标题，而建行时传进来的 title 常常还是空的。
        if self.worker is not None:
            t = str(getattr(self.worker, "v_title", "") or "")
            if t:
                return t
        return self.title

    @property
    def effective_thumbnail(self) -> str:
        if self.worker is not None:
            u = str(getattr(self.worker, "v_thumbnail", "") or "")
            if u:
                return u
        return self.thumbnail

    @property
    def effective_format_note(self) -> str:
        """画质标签。活任务在拿到输出路径后补上容器后缀，然后缓存。"""
        if self.worker is not None:
            out = self.effective_output_path
            if out and out != self._note_src:
                self._note_src = out
                self.format_note = derive_format_note(getattr(self.worker, "opts", None) or {}, out)
        return self.format_note

    # ── 构造与升级 ──

    def attach_worker(self, worker: DownloadWorker) -> None:
        """把 worker 挂到已有行上（**就地升级，不新增行**）。

        两种场景：分页先补进了这条历史行、随后用户重试它；或 `controller` 拿
        `restore_db_id` **重建**了 worker（已结束的 QThread 无法 restart）。
        """
        self.worker = worker
        # 要重新下载，之前算出来的「文件是否存在」结论作废；画质标签也要按新 worker 的
        # opts 重算（用户可能换了档位重试）。
        self.file_exists = None
        self._note_src = ""

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> TaskRow:
        """从 `tasks` 表的一行造历史行（无 worker）。"""
        opts: dict[str, Any] = {}
        try:
            parsed = json.loads(row.get("ydl_opts_json") or "{}")
            if isinstance(parsed, dict):
                opts = parsed
        except (TypeError, ValueError):
            # 用户机器上的历史数据，别假设它一定是合法 JSON。
            opts = {}

        out = str(row.get("output_path") or "")
        return cls(
            db_id=int(row.get("id") or 0),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            thumbnail=str(row.get("thumbnail_url") or ""),
            state=str(row.get("state") or "queued"),
            progress=float(row.get("progress") or 0.0),
            status_text=str(row.get("status_text") or ""),
            output_path=out,
            file_size=int(row.get("file_size") or 0),
            duration=int(row.get("duration") or 0),
            updated_at=float(row.get("updated_at") or 0.0),
            format_note=derive_format_note(opts, out),
            actual_height=_height_or_none(row.get("actual_height")),
            target_height=_height_or_none(row.get("target_height")),
            quality_deviation=str(row.get("quality_deviation") or "") or None,
        )

    @classmethod
    def from_worker(
        cls,
        worker: DownloadWorker,
        title: str = "",
        thumbnail: str = "",
    ) -> TaskRow:
        """从一个活 worker 造行。快照字段只做初值，之后一律走读穿透。"""
        opts = getattr(worker, "opts", None) or {}
        out = str(getattr(worker, "output_path", "") or "")
        return cls(
            db_id=int(getattr(worker, "db_id", 0) or 0),
            url=str(getattr(worker, "url", "") or ""),
            title=title or str(getattr(worker, "v_title", "") or ""),
            thumbnail=thumbnail or str(getattr(worker, "v_thumbnail", "") or ""),
            state=worker.effective_state,
            output_path=out,
            format_note=derive_format_note(opts, out),
            worker=worker,
        )
