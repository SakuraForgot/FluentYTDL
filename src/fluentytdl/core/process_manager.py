"""
FluentYTDL 进程管理模块

解决 GUI 下载器常见的僵尸进程问题:
- ffmpeg.exe、yt-dlp.exe 残留
- 主程序退出后子进程仍在运行
- 文件锁定导致无法删除
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
from collections.abc import Callable

from fluentytdl.utils.localized_log import log_text

from ..utils.logger import logger

# 尝试导入 psutil，如果不可用则使用降级方案
try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False
    log_text(logger, "warning", "psutil 未安装，进程清理功能受限")


# 需要监控的子进程名称
TARGET_PROCESS_NAMES = frozenset(
    {
        "ffmpeg.exe",
        "ffprobe.exe",
        "yt-dlp.exe",
        "deno.exe",
    }
)


class ProcessManager:
    """
    子进程生命周期管理器

    确保主程序退出时清理所有子进程，防止僵尸进程。
    """

    _instance: ProcessManager | None = None

    def __new__(cls) -> ProcessManager:
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._child_pids: set[int] = set()
        self._on_cleanup_callbacks: list[Callable[[], None]] = []

        # 注册程序退出清理
        atexit.register(self._on_exit)

        # 注册信号处理 (仅非 Windows 或 Windows 上支持的信号)
        try:
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, self._signal_handler)
            if hasattr(signal, "SIGINT"):
                signal.signal(signal.SIGINT, self._signal_handler)
        except Exception as e:
            log_text(logger, "debug", "信号处理注册失败 (可忽略): {0}", e)

        self._initialized = True
        log_text(logger, "debug", "ProcessManager 初始化完成")

    def register(self, pid: int) -> None:
        """
        注册子进程 PID

        Args:
            pid: 进程 ID
        """
        self._child_pids.add(pid)
        log_text(logger, "debug", "注册子进程 PID {0}", pid)

    def unregister(self, pid: int) -> None:
        """
        注销子进程 PID

        Args:
            pid: 进程 ID
        """
        self._child_pids.discard(pid)
        log_text(logger, "debug", "注销子进程 PID {0}", pid)

    def on_cleanup(self, callback: Callable[[], None]) -> None:
        """注册清理前回调"""
        self._on_cleanup_callbacks.append(callback)

    def cleanup(self) -> int:
        """
        清理所有注册的子进程

        Returns:
            成功终止的进程数
        """
        killed = 0

        # 执行清理前回调
        for callback in self._on_cleanup_callbacks:
            try:
                callback()
            except Exception as e:
                log_text(logger, "warning", "清理回调失败: {0}", e)

        # 方法 1: 终止已注册的进程
        for pid in list(self._child_pids):
            if self._kill_pid(pid):
                killed += 1

        # 方法 2: 扫描并终止同名子进程 (需要 psutil)
        if HAS_PSUTIL:
            killed += self._cleanup_by_name()

        if killed > 0:
            log_text(logger, "info", "已清理 {0} 个子进程", killed)

        return killed

    def cleanup_by_pid(self, pid: int) -> bool:
        """
        清理指定 PID 的进程

        Args:
            pid: 进程 ID

        Returns:
            是否成功终止
        """
        result = self._kill_pid(pid)
        self._child_pids.discard(pid)
        return result

    def _kill_pid(self, pid: int) -> bool:
        """终止指定 PID 的进程"""
        if not HAS_PSUTIL:
            # 降级方案：使用 os.kill
            try:
                os.kill(pid, signal.SIGTERM)
                self._child_pids.discard(pid)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                self._child_pids.discard(pid)
                return False

        import psutil as psutil_mod

        try:
            proc = psutil_mod.Process(pid)

            # 先尝试优雅终止
            proc.terminate()

            try:
                proc.wait(timeout=3)
            except psutil_mod.TimeoutExpired:
                # 超时则强制杀死
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except psutil_mod.TimeoutExpired:
                    pass

            self._child_pids.discard(pid)
            return True

        except psutil_mod.NoSuchProcess:
            # 进程已不存在
            self._child_pids.discard(pid)
            return False
        except psutil_mod.AccessDenied:
            log_text(logger, "warning", "无权限终止进程 {0}", pid)
            return False
        except Exception as e:
            log_text(logger, "warning", "终止进程 {0} 失败: {1}", pid, e)
            return False

    def _cleanup_by_name(self) -> int:
        """按进程名清理由本程序启动的子进程"""
        if not HAS_PSUTIL:
            return 0

        import psutil as psutil_mod

        killed = 0
        my_pid = os.getpid()

        try:
            for proc in psutil_mod.process_iter(["pid", "name", "ppid"]):
                try:
                    info = proc.info
                    name = info.get("name", "").lower()
                    ppid = info.get("ppid", 0)
                    pid = info.get("pid", 0)

                    # 只处理目标进程名且是本程序的子进程
                    if name in TARGET_PROCESS_NAMES or name.lower() in {
                        n.lower() for n in TARGET_PROCESS_NAMES
                    }:
                        if ppid == my_pid:
                            if self._kill_pid(pid):
                                killed += 1

                except (psutil_mod.NoSuchProcess, psutil_mod.AccessDenied):
                    continue

        except Exception as e:
            log_text(logger, "warning", "进程扫描失败: {0}", e)

        return killed

    def get_active_children(self) -> list[dict]:
        """
        获取活跃的子进程信息

        Returns:
            子进程信息列表 [{"pid": int, "name": str, "status": str}, ...]
        """
        if not HAS_PSUTIL:
            return [
                {"pid": pid, "name": "unknown", "status": "unknown"} for pid in self._child_pids
            ]

        import psutil as psutil_mod

        result = []
        for pid in list(self._child_pids):
            try:
                proc = psutil_mod.Process(pid)
                result.append(
                    {
                        "pid": pid,
                        "name": proc.name(),
                        "status": proc.status(),
                    }
                )
            except (psutil_mod.NoSuchProcess, psutil_mod.AccessDenied):
                self._child_pids.discard(pid)

        return result

    def _on_exit(self) -> None:
        """程序退出时的清理"""
        self.cleanup()

    def _signal_handler(self, signum: int, frame) -> None:
        """信号处理函数"""
        self.cleanup()
        sys.exit(0)

    @property
    def registered_count(self) -> int:
        """已注册的子进程数"""
        return len(self._child_pids)


# 全局单例
process_manager = ProcessManager()
