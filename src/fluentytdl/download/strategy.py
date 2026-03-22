"""
下载策略模块

定义单一默认的下载策略配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DownloadStrategy:
    """
    不可变的下载策略配置 (Native Only)。
    """

    label: str = "默认"

    # ── yt-dlp native 参数 ──
    socket_timeout: int = 15
    retries: str | int = 10  # infinite or int
    fragment_retries: str | int = 10
    sleep_interval: int = 0
    max_sleep_interval: int = 0
    force_ipv4: bool = False

    # ── IO 优化参数 ──
    buffer_size: str | None = None
    http_chunk_size: str | None = None
    resize_buffer: bool = False
    skip_unavailable_fragments: bool = True

    # ── 元信息 ──
    risk_level: str = "low"  # low / medium / high

    def apply_to_ydl_opts(self, ydl_opts: dict[str, Any]) -> None:
        """将策略参数注入 ydl_opts。"""
        ydl_opts["socket_timeout"] = self.socket_timeout
        ydl_opts["retries"] = self.retries
        ydl_opts["fragment_retries"] = self.fragment_retries

        if self.sleep_interval > 0:
            ydl_opts["sleep_interval"] = self.sleep_interval
        if self.max_sleep_interval > 0:
            ydl_opts["max_sleep_interval"] = self.max_sleep_interval

        if self.force_ipv4:
            ydl_opts["source_address"] = "0.0.0.0"

        # IO Optimization
        if self.buffer_size:
            ydl_opts["buffersize"] = self.buffer_size
        if self.http_chunk_size:
            ydl_opts["http_chunk_size"] = self.http_chunk_size
        if self.resize_buffer:
            ydl_opts["resize_buffer"] = True

        if self.skip_unavailable_fragments:
            ydl_opts["skip_unavailable_fragments"] = True


# 全局默认策略
DEFAULT_STRATEGY = DownloadStrategy()
