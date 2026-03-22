"""
FluentYTDL 下载功能域

包含下载管理、策略调度、执行器等功能。
"""

from .dispatcher import DownloadDispatcher, download_dispatcher
from .download_manager import DownloadManager, download_manager
from .executor import DownloadExecutor
from .resume_manager import ResumeManager
from .strategy import DownloadStrategy
from .task_queue import TaskQueue
from .workers import DownloadWorker

__all__ = [
    "DownloadManager",
    "download_manager",
    "DownloadDispatcher",
    "download_dispatcher",
    "DownloadExecutor",
    "DownloadStrategy",
    "DownloadWorker",
    "TaskQueue",
    "ResumeManager",
]
