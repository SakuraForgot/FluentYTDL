"""「文件还在不在」的后台探测。

融合前这件事是在 GUI 线程里逐行做的：`history_service.validated_records()` 对每条
历史记录同步调一次 `os.path.exists()`，`total_size()` 再来一遍。100 条记录在本地 SSD 上
只是「有点卡」，但产物落在网络盘 / 移动硬盘上时，一次 `stat` 就能阻塞几百毫秒 ——
乘以行数，表现是「打开历史页整个窗口白屏」。

融合后行数只会更多（活任务 + 全部历史行都在同一个列表里），所以这里改成：

* `stat` 全部在一个**专属**线程池里做（不抢 `AsyncExtractManager` 的池，也不占
  `globalInstance()` —— 一个挂死的网络路径不该拖住别人）；
* 池只开 1 条线程：`stat` 是 IO，机械盘上并发只会让磁头来回寻道；
* 请求带 60ms 合并窗口 + 单批上限，滚动时补进来的一页页历史行会塌成少数几批。

结论本身**不存在这里** —— 它写在 `TaskRow.file_exists` 上（`None` = 还没查过）。
这个类只管「把路径送出去、把结果送回来」，重下时结论作废由 `TaskRow.attach_worker`
负责。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal


def probe_paths(items: Iterable[tuple[int, str]]) -> dict[int, bool]:
    """`(db_id, path)` 序列 → `{db_id: 文件是否存在}`。**纯函数，不碰 Qt。**

    抽成模块级函数是为了能不起线程直接测；`OSError` 一律算「不存在」——
    路径过长、盘符已拔出、权限不足都属于「用户点了打不开」，和文件被删等价。
    """
    out: dict[int, bool] = {}
    for db_id, path in items:
        if not path:
            out[db_id] = False
            continue
        try:
            out[db_id] = os.path.exists(path)
        except OSError:
            out[db_id] = False
    return out


class _ProbeSignals(QObject):
    """结果回传通道。

    单独一个 QObject 而不是让 `QRunnable` 继承 QObject：runnable 是 `setAutoDelete(True)`
    的，Qt 在 run() 返回后立刻析构它，而信号可能还在事件队列里排着。
    """

    # **必须是 `Signal(object)`，不能写成 `Signal(dict)`。** PySide6 把 `dict` 映射成
    # `QVariantMap`（即 `QMap<QString, QVariant>`），键只能是字符串；而这里的键是
    # `db_id`（int），过 `QVariantMap` 时会被**静默丢成空 dict** —— 槽照样被调用，
    # 收到的却是 `{}`，只在 stderr 留一句
    # `Shiboken::Conversions::_pythonToCppCopy: Cannot copy-convert ... (dict) to C++.`。
    # 实测直连和跨线程排队投递都一样，且那句提示按 dict 对象去重，成批发射时只出现一次，
    # 很容易被当成无害噪音。`object` 映射成 `PyObject`，原样透传（连对象身份都保留），
    # 跨线程排队投递同样正常。
    done = Signal(object)


class _ProbeRunnable(QRunnable):
    def __init__(self, items: list[tuple[int, str]], signals: _ProbeSignals) -> None:
        super().__init__()
        self._items = items
        self._signals = signals

    def run(self) -> None:  # pragma: no cover - 线程体，逻辑在 probe_paths 里测
        self._signals.done.emit(probe_paths(self._items))


class FileExistenceProbe(QObject):
    """把成批的路径丢到后台线程 stat，结果按 `db_id` 回传。

    `checked` 携带 `{db_id: bool}`。跨线程的那一跳是 `_ProbeSignals.done`（排队投递），
    所以 `_on_done` 已经在 GUI 线程里了，`checked` 只是一次普通的同线程转发。
    """

    # 键是 int，同样只能用 `Signal(object)` —— 理由见 `_ProbeSignals.done`
    checked = Signal(object)

    # 合并窗口。滚动到底触发 fetchMore 会一次补 100 行，逐行 request 会开 100 个批次。
    _COALESCE_MS = 60
    # 单批上限。全表重扫（「文件丢失」筛选）时不要把几万条塞进一个 runnable ——
    # 那会让第一批结果等到全部扫完才回来，用户看到的是「筛选按钮点了没反应」。
    _MAX_BATCH = 300

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._signals = _ProbeSignals(self)
        self._signals.done.connect(self._on_done)

        # 待查：db_id -> path。dict 而不是 list —— 同一行被反复请求（重扫 + 分页撞上）
        # 只该 stat 一次。
        self._queued: dict[int, str] = {}
        # 已下发、还没回来的。不去重会让重扫期间的每次滚动都把同一行再排一遍。
        self._inflight: set[int] = set()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._COALESCE_MS)
        self._timer.timeout.connect(self._flush)

    # === 对外接口 ===

    def request(self, items: Iterable[tuple[int, str]]) -> None:
        """排队检查。`db_id <= 0` 的行（还没落库）直接忽略。"""
        added = False
        for db_id, path in items:
            key = int(db_id or 0)
            if key <= 0 or key in self._inflight or key in self._queued:
                continue
            self._queued[key] = str(path or "")
            added = True
        if added and not self._timer.isActive():
            self._timer.start()

    def forget(self, db_id: int) -> None:
        """撤销某一行的待查（行被删掉了，或者它开始重新下载）。

        已下发的那一批拦不住，但结果由调用方按当前状态决定要不要采纳
        （见 `DownloadListModel._apply_existence`）。
        """
        self._queued.pop(int(db_id or 0), None)

    def clear(self) -> None:
        self._queued.clear()
        self._timer.stop()

    def pending_count(self) -> int:
        return len(self._queued) + len(self._inflight)

    # === 内部 ===

    def _flush(self) -> None:
        if not self._queued:
            return
        batch: list[tuple[int, str]] = []
        for db_id in list(self._queued)[: self._MAX_BATCH]:
            batch.append((db_id, self._queued.pop(db_id)))
            self._inflight.add(db_id)

        runnable = _ProbeRunnable(batch, self._signals)
        runnable.setAutoDelete(True)
        self._pool.start(runnable)
        # 队列里还有剩的不在这里续排：等这一批回来（`_on_done`）再排下一批，
        # 免得几万条一次全压进池子的等待队列，第一批结果反而要等到最后才回来。

    def _on_done(self, result: dict) -> None:
        for db_id in result:
            self._inflight.discard(int(db_id))
        self.checked.emit(result)
        if self._queued and not self._timer.isActive():
            self._timer.start()
