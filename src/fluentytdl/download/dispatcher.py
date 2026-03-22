"""
下载调度器

调度器是无状态的决策入口，输入上下文，输出具体策略。
现在只使用全局默认策略。
"""

from __future__ import annotations

from typing import Any

from .strategy import (
    DEFAULT_STRATEGY,
    DownloadStrategy,
)


class DownloadDispatcher:
    """下载模式路由决策器。

    Usage::

        dispatcher = DownloadDispatcher()
        strategy = dispatcher.resolve(ydl_opts)
    """

    def __init__(self) -> None:
        pass

    def report_result(self, success: bool) -> None:
        """上报一次下载结果（成功/失败）。为了向后兼容暂时保留，但不做熔断。"""
        pass

    def resolve(
        self,
        ydl_opts: dict[str, Any],
        *,
        running_tasks: int = 0,
    ) -> DownloadStrategy:
        """将请求解析为具体策略。一直返回默认策略。

        Args:
            ydl_opts: 当前任务的 yt-dlp 选项。
            running_tasks: 当前正在运行的下载任务数。

        Returns:
            最终确定的下载策略。
        """
        return DEFAULT_STRATEGY


# 全局单例
download_dispatcher = DownloadDispatcher()
