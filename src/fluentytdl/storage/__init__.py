"""FluentYTDL 存储功能域

这里曾经再导出 `HistoryService` / `HistoryRecord` / `history_service`。那三个名字随
「任务列表 × 历史记录」融合一起删掉了：`HistoryService` 本质只是
`SELECT * FROM tasks WHERE state IN ('completed','error')` 的只读适配器，融合后历史行
由 `TaskDB.query_tasks()` 直接分页取出，不再需要中间那层记录对象。

本包不再对外再导出任何东西 —— 用 `from ..storage.task_db import task_db` 这样的直接导入。
"""
