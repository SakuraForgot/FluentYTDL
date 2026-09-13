"""
Disk space monitor for checking available storage.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger


class DiskMonitor(QObject):
    """
    磁盘空间监控器

    定期检查目标目录的可用空间，达到阈值时发出警告。
    """

    # 信号
    low_space_warning = Signal(int)  # 剩余 MB
    critical_space = Signal(int)  # 剩余 MB

    # 阈值 (MB)
    WARNING_THRESHOLD = 5000  # 5 GB
    CRITICAL_THRESHOLD = 1000  # 1 GB

    # 检查间隔 (秒)
    CHECK_INTERVAL = 30

    def __init__(self, target_path: Path, parent=None):
        super().__init__(parent)

        self._target_path = target_path
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_space)

        self._last_warning = False
        self._last_critical = False

    @property
    def target_path(self) -> Path:
        """目标路径"""
        return self._target_path

    @target_path.setter
    def target_path(self, path: Path):
        """设置目标路径"""
        self._target_path = path

    def start(self, interval: int | None = None):
        """开始监控"""
        if interval:
            self._timer.setInterval(interval * 1000)
        else:
            self._timer.setInterval(self.CHECK_INTERVAL * 1000)

        self._check_space()  # 立即检查一次
        self._timer.start()

    def stop(self):
        """停止监控"""
        self._timer.stop()

    def get_free_space_mb(self) -> int:
        """获取可用空间 (MB)"""
        try:
            # 确保路径存在
            check_path = self._target_path
            while not check_path.exists() and check_path.parent != check_path:
                check_path = check_path.parent

            usage = shutil.disk_usage(str(check_path))
            return int(usage.free / 1024 / 1024)

        except Exception as e:
            log_text(logger, "error", "获取磁盘空间失败: {0}", e)
            return -1

    def get_free_space_display(self) -> str:
        """获取格式化的可用空间显示"""
        mb = self.get_free_space_mb()
        if mb < 0:
            return tr_text("未知")
        elif mb < 1024:
            return f"{mb} MB"
        else:
            return f"{mb / 1024:.1f} GB"

    def _check_space(self):
        """检查磁盘空间"""
        free_mb = self.get_free_space_mb()

        if free_mb < 0:
            return

        # 严重不足
        if free_mb < self.CRITICAL_THRESHOLD:
            if not self._last_critical:
                log_text(logger, "error", "磁盘空间严重不足: {0} MB", free_mb)
                self.critical_space.emit(free_mb)
                self._last_critical = True
                self._last_warning = True

        # 空间不足
        elif free_mb < self.WARNING_THRESHOLD:
            if not self._last_warning:
                log_text(logger, "warning", "磁盘空间不足: {0} MB", free_mb)
                self.low_space_warning.emit(free_mb)
                self._last_warning = True
            self._last_critical = False

        else:
            self._last_warning = False
            self._last_critical = False

    def is_space_sufficient(self, required_mb: int = 500) -> bool:
        """
        检查是否有足够空间

        Args:
            required_mb: 需要的空间 (MB)

        Returns:
            是否有足够空间
        """
        free = self.get_free_space_mb()
        return free >= required_mb
