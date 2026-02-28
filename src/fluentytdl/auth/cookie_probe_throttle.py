"""
Cookie 探测节流器

事件驱动 + 节流策略，避免过度频繁探测触发 YouTube 风控。
规则：
- 最小间隔 30 分钟
- 每日最多 5 次自动探测
- 风控退避 2 小时
- 手动探测（force=True）不受日限约束
"""

from __future__ import annotations

import threading
import time
from datetime import date

from ..utils.logger import logger


class CookieProbeThrottle:
    """Cookie + IP 探测节流器（全局单例）"""

    MIN_INTERVAL = 1800  # 30 分钟最小间隔
    MAX_DAILY_PROBES = 5  # 每日自动探测上限
    BACKOFF_ON_RISK = 7200  # 风控退避 2 小时

    _instance = None

    def __new__(cls) -> CookieProbeThrottle:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._lock = threading.Lock()
        self._last_probe_time: float = 0.0
        self._last_result: dict | None = None
        self._last_risk_detected: bool = False
        self._daily_date: date = date.today()
        self._daily_count: int = 0
        self._consecutive_cookie_failures: int = 0

    # ──────────────── 节流判断 ────────────────

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0

    def should_probe(self) -> bool:
        """检查是否允许探测（自动模式）"""
        # 读取开关，如果用户关闭了自动探测，直接拦截
        from ..core.config_manager import config_manager

        if not config_manager.get("auto_risk_probe", False):
            return False

        with self._lock:
            self._reset_daily_if_needed()

            # 日限
            if self._daily_count >= self.MAX_DAILY_PROBES:
                return False

            now = time.time()
            elapsed = now - self._last_probe_time

            # 风控退避
            if self._last_risk_detected and elapsed < self.BACKOFF_ON_RISK:
                return False

            # 最小间隔
            if elapsed < self.MIN_INTERVAL:
                return False

            return True

    # ──────────────── 探测执行 ────────────────

    def probe_if_allowed(self, force: bool = False) -> dict | None:
        """
        带节流的探测。

        Args:
            force: True = 用户手动触发，忽略日限但保留最小间隔

        Returns:
            探测结果 dict，或 None（被节流跳过，使用 get_cached_result）
        """
        with self._lock:
            self._reset_daily_if_needed()
            now = time.time()
            elapsed = now - self._last_probe_time

            if not force:
                # 再次校验开关
                from ..core.config_manager import config_manager

                if not config_manager.get("auto_risk_probe", False):
                    logger.debug("[ProbeThrottle] 自动探测已在设置中关闭，跳过")
                    return None

                if self._daily_count >= self.MAX_DAILY_PROBES:
                    logger.debug("[ProbeThrottle] 达到日限，使用缓存")
                    return None
                if self._last_risk_detected and elapsed < self.BACKOFF_ON_RISK:
                    logger.debug("[ProbeThrottle] 风控退避中，使用缓存")
                    return None
                if elapsed < self.MIN_INTERVAL:
                    logger.debug("[ProbeThrottle] 间隔不足，使用缓存")
                    return None
            else:
                # 手动模式仍保留最短 60 秒间隔防刷
                if elapsed < 60:
                    logger.debug("[ProbeThrottle] 手动模式最短间隔，使用缓存")
                    return None

            # 标记开始探测
            self._last_probe_time = now
            self._daily_count += 1

        # 在锁外执行耗时操作
        try:
            from ..utils.error_parser import probe_cookie_and_ip
            from .cookie_sentinel import cookie_sentinel

            cookie_file = None
            if cookie_sentinel.exists:
                cookie_file = cookie_sentinel.get_cookie_file_path()

            result = probe_cookie_and_ip(cookie_file=cookie_file, timeout=15.0)

            with self._lock:
                self._last_result = result
                self._last_risk_detected = not result.get("ip_ok", True)

            logger.info(
                f"[ProbeThrottle] 探测完成: cookie_ok={result.get('cookie_ok')}, "
                f"ip_ok={result.get('ip_ok')}, {result.get('detail', '')}"
            )
            return result

        except Exception as e:
            logger.warning(f"[ProbeThrottle] 探测异常: {e}")
            return None

    # ──────────────── 缓存与状态 ────────────────

    def get_cached_result(self) -> dict | None:
        """获取上次探测的缓存结果"""
        with self._lock:
            return self._last_result

    def cache_age_seconds(self) -> float:
        """缓存结果的年龄（秒），无缓存返回 inf"""
        with self._lock:
            if self._last_probe_time == 0:
                return float("inf")
            return time.time() - self._last_probe_time

    # ──────────────── 下载失败计数 ────────────────

    def record_download_success(self) -> None:
        """下载成功，重置失败计数"""
        with self._lock:
            self._consecutive_cookie_failures = 0

    def record_download_failure(self, category: str) -> None:
        """
        下载失败，按类别计数。

        Args:
            category: "cookie" / "network" / "ambiguous" / "other"
        """
        with self._lock:
            if category in ("cookie", "ambiguous"):
                self._consecutive_cookie_failures += 1
                logger.info(f"[ProbeThrottle] Cookie 连续失败: {self._consecutive_cookie_failures}")

    @property
    def consecutive_failures(self) -> int:
        """连续 Cookie 失败次数"""
        with self._lock:
            return self._consecutive_cookie_failures

    @property
    def should_suggest_alternative(self) -> bool:
        """是否应建议切换到 DLE/Firefox（连续 ≥ 3 次失败）"""
        return self.consecutive_failures >= 3


# 全局单例
cookie_probe_throttle = CookieProbeThrottle()
