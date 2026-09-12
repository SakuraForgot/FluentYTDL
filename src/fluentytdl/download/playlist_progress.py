import re
from dataclasses import dataclass

from fluentytdl.utils.ui_text import tr_text


def _format_speed(speed_bytes: int) -> str:
    """格式化速率: 1234567 -> '1.2MB/s'"""
    if speed_bytes <= 0:
        return ""
    if speed_bytes < 1024:
        return f"{speed_bytes}B/s"
    elif speed_bytes < 1024**2:
        return f"{speed_bytes / 1024:.1f}KB/s"
    elif speed_bytes < 1024**3:
        return f"{speed_bytes / 1024**2:.1f}MB/s"
    else:
        return f"{speed_bytes / 1024**3:.2f}GB/s"


def _format_eta(seconds: int) -> str:
    """格式化剩余时间: 150 -> '02:30'"""
    if seconds <= 0:
        return ""
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_size_human(bytes_val: int) -> str:
    """格式化文件大小: 2415919104 -> '2.3GB'"""
    if bytes_val <= 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(bytes_val)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.1f}{u}" if u not in ("B", "KB") else f"{int(x)}{u}"
        x /= 1024
    return f"{bytes_val}B"


@dataclass
class PlaylistProgressTracker:
    """追踪单 Worker 播放列表下载进度。"""

    total_items: int = 0
    current_item: int = 0
    current_title: str = ""

    # 当前条目的实时下载数据（从 yt-dlp progress 行获取）
    item_speed: int = 0  # bytes/s
    item_eta: int | None = None  # seconds
    item_percent: float = 0.0  # 当前条目 0-100%
    item_downloaded: int = 0  # 当前条目已下载字节

    # 累计统计
    completed_items: int = 0
    failed_items: int = 0
    total_downloaded_bytes: int = 0  # 所有已完成条目的总下载量

    def parse_line(self, line: str) -> tuple[str, float, str] | None:
        """解析 yt-dlp 的播放列表进度行。

        返回 (state, percent, message) 或 None。
        """
        # 匹配 "[download] Downloading item N of M"
        m = re.match(r"\[download\]\s+Downloading\s+(?:item|video)\s+(\d+)\s+of\s+(\d+)", line)
        if m:
            self.current_item = int(m.group(1))
            self.total_items = int(m.group(2))
            return ("downloading", self.overall_percent, self.build_status_text())

        return None

    def update_item_progress(
        self, percent: float, speed: int | None, eta: int | None, downloaded: int
    ) -> None:
        """更新当前正在下载的条目的实时进度。"""
        self.item_percent = percent
        if speed is not None:
            self.item_speed = speed
        if eta is not None:
            self.item_eta = eta
        self.item_downloaded = downloaded

    def mark_item_completed(self) -> None:
        """标记当前条目完成，更新统计。"""
        self.completed_items += 1
        self.total_downloaded_bytes += self.item_downloaded
        self.item_percent = 100.0
        self.item_speed = 0
        self.item_eta = 0

    def mark_item_failed(self) -> None:
        self.failed_items += 1

    def build_status_text(self) -> str:
        """生成给 CleanLogger / DownloadItemDelegate 使用的进度文本。"""
        if self.total_items == 0:
            from PySide6.QtCore import QCoreApplication

            return QCoreApplication.translate("PlaylistProgress", "正在解析播放列表...")

        title_part = self.current_title or "..."
        # 截断过长标题
        if len(title_part) > 35:
            title_part = title_part[:32] + "..."

        progress_prefix = f"[{self.current_item}/{self.total_items}]"

        # 速率格式化
        speed_str = ""
        if self.item_speed and self.item_speed > 0:
            speed_str = f"⬇️ {_format_speed(self.item_speed)}"

        # ETA 格式化
        eta_str = ""
        if self.item_eta and self.item_eta > 0:
            eta_str = tr_text("⏳ 剩余: {0}", _format_eta(self.item_eta))

        # 下载量
        downloaded_str = ""
        if self.item_downloaded and self.item_downloaded > 0:
            downloaded_str = f"💾 {_format_size_human(self.item_downloaded)}"

        # 第一行：进度与标题
        line1 = f"{progress_prefix} {title_part}"

        # 第二行：详细信息
        detail_parts = []
        if speed_str:
            detail_parts.append(speed_str)
        if downloaded_str:
            detail_parts.append(downloaded_str)
        if eta_str:
            detail_parts.append(eta_str)

        if detail_parts:
            line2 = " | ".join(detail_parts)
        elif self.item_percent > 0:
            line2 = f"⚡ {self.item_percent:.0f}%"
        else:
            line2 = tr_text("⚡ 下载中...")

        return f"{line1}\n{line2}"

    def build_completed_text(self) -> str:
        """完成后的汇总文本。"""
        size_str = _format_size_human(self.total_downloaded_bytes)
        if self.failed_items > 0:
            return tr_text(
                "已完成 {0}/{1} · {2} 个失败 · 总计 {3}",
                self.completed_items,
                self.total_items,
                self.failed_items,
                size_str,
            )
        return tr_text("已完成 {0} 个视频 · 总计 {1}", self.total_items, size_str)

    @property
    def overall_percent(self) -> float:
        """整体进度百分比（用于进度条）。"""
        if self.total_items == 0:
            return 0.0
        base = (self.completed_items / self.total_items) * 100
        item_contrib = (self.item_percent / 100.0) / self.total_items * 100
        return min(base + item_contrib, 99.0)
