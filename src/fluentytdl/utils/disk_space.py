"""
FluentYTDL 磁盘空间检测模块

下载前检查目标磁盘空间，避免下载到 99% 时报错磁盘已满。

**本模块只报告事实，从不阻止下载。** 唯一的消费者是 `download/executor.py` 在 `Popen`
之前落的那条 `kind=signal stage=preflight`：磁盘剩多少、够不够。之所以不把
`ensure_space_available()` 接上去当拦路虎，是因为预估体积在开跑前根本不可靠
（DASH 分流 + 合并产物 + 后处理临时文件都算不准），拿它拦下载只会误杀。
真正的判定留给 yt-dlp 自己的写盘错误，而这条 signal 负责让那个错误**有上下文**。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DiskSpaceInfo:
    """磁盘空间信息"""

    total: int  # 总空间 (bytes)
    used: int  # 已用空间 (bytes)
    free: int  # 可用空间 (bytes)

    @property
    def free_gb(self) -> float:
        """可用空间 (GB)"""
        return self.free / (1024**3)

    @property
    def total_gb(self) -> float:
        """总空间 (GB)"""
        return self.total / (1024**3)

    @property
    def used_percent(self) -> float:
        """使用率 (0-100)"""
        return (self.used / self.total * 100) if self.total > 0 else 0


@dataclass
class SpaceCheckResult:
    """空间检查结果"""

    sufficient: bool  # 空间是否足够
    required_bytes: int  # 需要的空间 (bytes)
    available_bytes: int  # 可用空间 (bytes)
    message: str  # 人类可读消息
    #: 探测本身失败时的原因（路径不存在 / 无权限）。空串表示探测成功。
    #:
    #: **"探不出来"和"确实不够"必须分得开。** 两种情况都会给出
    #: `sufficient=False, available_bytes=0`，可前者是我们瞎了，后者是磁盘真满了 ——
    #: 拿同一个 code 记进日志，"下载失败是不是因为磁盘满"就永远答不上来。
    error: str = ""

    @property
    def required_gb(self) -> float:
        return self.required_bytes / (1024**3)

    @property
    def available_gb(self) -> float:
        return self.available_bytes / (1024**3)

    @property
    def shortfall_bytes(self) -> int:
        """缺少的空间 (bytes)"""
        return max(0, self.required_bytes - self.available_bytes)

    @property
    def shortfall_gb(self) -> float:
        return self.shortfall_bytes / (1024**3)


def get_disk_space(path: str | Path) -> DiskSpaceInfo:
    """
    获取指定路径所在磁盘的空间信息

    Args:
        path: 目标路径 (可以是文件或目录)

    Returns:
        磁盘空间信息

    Raises:
        FileNotFoundError: 路径不存在
        PermissionError: 无权限访问
    """
    path = Path(path)

    # 如果路径不存在，尝试找到存在的父目录
    check_path = path
    while not check_path.exists():
        if check_path.parent == check_path:
            raise FileNotFoundError(f"无法确定磁盘: {path}")
        check_path = check_path.parent

    usage = shutil.disk_usage(check_path)
    return DiskSpaceInfo(
        total=usage.total,
        used=usage.used,
        free=usage.free,
    )


def check_disk_space(
    path: str | Path,
    required_bytes: int,
    safety_margin: float = 0.1,
) -> SpaceCheckResult:
    """
    检查磁盘空间是否足够

    Args:
        path: 目标路径
        required_bytes: 需要的空间 (bytes)
        safety_margin: 安全余量 (0.1 = 10%)

    Returns:
        检查结果
    """
    try:
        info = get_disk_space(path)
    except (FileNotFoundError, PermissionError) as e:
        return SpaceCheckResult(
            sufficient=False,
            required_bytes=required_bytes,
            available_bytes=0,
            message=f"无法检查磁盘空间: {e}",
            error=type(e).__name__,
        )

    # 加上安全余量
    total_required = int(required_bytes * (1 + safety_margin))

    if info.free >= total_required:
        return SpaceCheckResult(
            sufficient=True,
            required_bytes=required_bytes,
            available_bytes=info.free,
            message=f"空间充足 (需要 {_format_size(required_bytes)}，可用 {_format_size(info.free)})",
        )
    else:
        shortfall = total_required - info.free
        return SpaceCheckResult(
            sufficient=False,
            required_bytes=required_bytes,
            available_bytes=info.free,
            message=(
                f"磁盘空间不足！\n"
                f"需要: {_format_size(total_required)}\n"
                f"可用: {_format_size(info.free)}\n"
                f"缺少: {_format_size(shortfall)}"
            ),
        )


def check_space_for_download(
    output_dir: str | Path,
    estimated_size: int,
    min_free_gb: float = 1.0,
) -> SpaceCheckResult:
    """
    检查是否有足够空间进行下载

    Args:
        output_dir: 输出目录
        estimated_size: 预估文件大小 (bytes)
        min_free_gb: 最小保留空间 (GB)

    Returns:
        检查结果
    """
    min_free_bytes = int(min_free_gb * 1024**3)
    required = max(estimated_size, min_free_bytes)

    return check_disk_space(output_dir, required)


def ensure_space_available(
    output_dir: str | Path,
    required_bytes: int,
) -> None:
    """
    确保有足够空间，否则抛出异常

    Args:
        output_dir: 输出目录
        required_bytes: 需要的空间

    Raises:
        IOError: 空间不足
    """
    result = check_disk_space(output_dir, required_bytes)
    if not result.sufficient:
        raise OSError(result.message)


def _format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.1f} MB"
    else:
        return f"{size_bytes / 1024**3:.2f} GB"


def format_size(size_bytes: int) -> str:
    """格式化文件大小 (公开接口)"""
    return _format_size(size_bytes)
