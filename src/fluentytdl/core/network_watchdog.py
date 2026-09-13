"""
Network watchdog for monitoring connectivity status.
"""

from __future__ import annotations

import requests
from PySide6.QtCore import QObject, QTimer, Signal

from fluentytdl.utils.localized_log import log_text

from ..utils.logger import logger


class NetworkWatchdog(QObject):
    """
    网络状态监控器

    定期检测网络连接状态，尤其是 YouTube 可达性。
    """

    # 信号
    status_changed = Signal(str)  # "stable", "unstable", "disconnected"

    # 配置
    CHECK_INTERVAL = 10  # 秒
    FAILURE_THRESHOLD = 3  # 连续失败次数阈值

    # 测试 URL
    TEST_URLS = [
        "https://www.youtube.com",
        "https://www.google.com",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_network)

        self._failure_count = 0
        self._current_status = "unknown"
        self._is_running = False

    @property
    def current_status(self) -> str:
        """当前网络状态"""
        return self._current_status

    @property
    def is_running(self) -> bool:
        """是否正在监控"""
        return self._is_running

    def start(self, interval: int | None = None):
        """开始监控"""
        if interval:
            self._timer.setInterval(interval * 1000)
        else:
            self._timer.setInterval(self.CHECK_INTERVAL * 1000)

        self._is_running = True
        self._check_network()  # 立即检查一次
        self._timer.start()
        log_text(logger, "debug", "网络监控已启动")

    def stop(self):
        """停止监控"""
        self._timer.stop()
        self._is_running = False
        log_text(logger, "debug", "网络监控已停止")

    def _check_network(self):
        """检查网络状态"""
        reachable = False

        for url in self.TEST_URLS:
            try:
                response = requests.head(url, timeout=5)
                if response.status_code < 400:
                    reachable = True
                    break
            except requests.exceptions.ConnectionError:
                continue
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                log_text(logger, "debug", "网络检测异常: {0}", e)
                continue

        if reachable:
            self._on_success()
        else:
            self._on_failure()

    def _on_success(self):
        """网络检测成功"""
        old_status = self._current_status
        self._failure_count = 0
        self._current_status = "stable"

        if old_status != "stable":
            log_text(logger, "info", "网络状态: 稳定")
            self.status_changed.emit("stable")

    def _on_failure(self):
        """网络检测失败"""
        self._failure_count += 1

        old_status = self._current_status

        if self._failure_count >= self.FAILURE_THRESHOLD:
            self._current_status = "disconnected"
            if old_status != "disconnected":
                log_text(logger, "warning", "网络状态: 断开")
                self.status_changed.emit("disconnected")
        else:
            self._current_status = "unstable"
            if old_status == "stable":
                log_text(logger, "warning", "网络状态: 不稳定")
                self.status_changed.emit("unstable")

    def check_once(self) -> str:
        """执行一次检查并返回状态"""
        self._check_network()
        return self._current_status


# 全局单例
network_watchdog = NetworkWatchdog()
