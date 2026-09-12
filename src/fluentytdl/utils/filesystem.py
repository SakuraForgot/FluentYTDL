"""
FluentYTDL 文件系统安全模块

提供文件名清洗、路径安全检查等功能，解决 Windows 文件系统限制问题：
- 非法字符替换 (<>:"/\\|?*)
- 长路径截断 (MAX_PATH 260 限制)
- 保留名称检测 (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from fluentytdl.utils.message_catalog import english

# Windows 非法文件名字符
ILLEGAL_CHARS = r'[<>:"/\\|?*]'
ILLEGAL_CHARS_PATTERN = re.compile(ILLEGAL_CHARS)

# Windows 保留名称 (不能用作文件名)
RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

# 控制字符范围 (0x00-0x1F)
CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f]")

# 默认最大文件名长度 (保留一些余量给路径)
DEFAULT_MAX_FILENAME_LENGTH = 200

# Windows MAX_PATH 限制
WINDOWS_MAX_PATH = 260


def sanitize_filename(
    name: str,
    replacement: str = "_",
    max_length: int = DEFAULT_MAX_FILENAME_LENGTH,
    preserve_extension: bool = True,
) -> str:
    """
    清洗文件名，使其在 Windows 文件系统中安全使用。

    Args:
        name: 原始文件名
        replacement: 非法字符的替换字符
        max_length: 最大文件名长度
        preserve_extension: 截断时是否保留扩展名

    Returns:
        安全的文件名

    Examples:
        >>> sanitize_filename('video: test?.mp4')
        'video_ test_.mp4'
        >>> sanitize_filename('a' * 300 + '.mp4')
        'aaa...aaa.mp4'  # 截断到 max_length
    """
    if not name:
        return "unnamed"

    # 1. 规范化 Unicode (NFC 形式)
    name = unicodedata.normalize("NFC", name)

    # 2. 移除控制字符
    name = CONTROL_CHARS_PATTERN.sub("", name)

    # 3. 替换非法字符
    name = ILLEGAL_CHARS_PATTERN.sub(replacement, name)

    # 4. 处理开头和结尾的点和空格 (Windows 不允许)
    name = name.strip(". ")

    # 5. 检查保留名称
    base_name = name.rsplit(".", 1)[0].upper() if "." in name else name.upper()
    if base_name in RESERVED_NAMES:
        name = f"_{name}"

    # 6. 截断超长文件名
    if len(name) > max_length:
        name = _truncate_filename(name, max_length, preserve_extension)

    # 7. 确保不为空
    if not name:
        return "unnamed"

    return name


def _truncate_filename(
    name: str,
    max_length: int,
    preserve_extension: bool = True,
) -> str:
    """
    智能截断文件名，保留扩展名。

    Args:
        name: 文件名
        max_length: 最大长度
        preserve_extension: 是否保留扩展名

    Returns:
        截断后的文件名
    """
    if len(name) <= max_length:
        return name

    if preserve_extension and "." in name:
        # 分离扩展名
        base, ext = name.rsplit(".", 1)
        ext = f".{ext}"

        # 确保扩展名合理长度
        if len(ext) > 10:
            # 扩展名太长，可能不是真正的扩展名
            return name[:max_length]

        # 截断基础名称
        available = max_length - len(ext)
        if available < 1:
            return name[:max_length]

        return base[:available] + ext
    else:
        return name[:max_length]


def sanitize_path(
    path: str | Path,
    max_total_length: int = WINDOWS_MAX_PATH - 10,
) -> Path:
    """
    清洗完整路径，确保符合 Windows 限制。

    Args:
        path: 原始路径
        max_total_length: 最大总路径长度

    Returns:
        安全的路径
    """
    path = Path(path)

    # 清洗每个路径组件
    parts = list(path.parts)
    sanitized_parts = []

    for i, part in enumerate(parts):
        if i == 0 and (part.endswith(":") or part == "/" or part == "\\"):
            # 保留驱动器号或根目录
            sanitized_parts.append(part)
        else:
            sanitized_parts.append(sanitize_filename(part))

    result = Path(*sanitized_parts) if sanitized_parts else path

    # 检查总长度
    if len(str(result)) > max_total_length:
        # 尝试截断文件名部分
        *dirs, filename = sanitized_parts
        dir_path = str(Path(*dirs)) if dirs else ""
        available = max_total_length - len(dir_path) - 1

        if available > 10:
            filename = _truncate_filename(filename, available)
            result = Path(*dirs, filename) if dirs else Path(filename)

    return result


def is_path_too_long(path: str | Path, limit: int = WINDOWS_MAX_PATH) -> bool:
    """
    检查路径是否超过 Windows MAX_PATH 限制。

    Args:
        path: 路径
        limit: 长度限制

    Returns:
        True 如果超长
    """
    return len(str(path)) >= limit


def suggest_shorter_path(
    directory: str | Path,
    filename: str,
    target_length: int = WINDOWS_MAX_PATH - 20,
) -> Path:
    """
    为给定目录和文件名建议一个不超长的路径。

    Args:
        directory: 目标目录
        filename: 原始文件名
        target_length: 目标最大长度

    Returns:
        建议的完整路径
    """
    directory = Path(directory)
    dir_len = len(str(directory))

    # 计算文件名可用长度
    available = target_length - dir_len - 1  # -1 for separator

    if available < 20:
        # 目录太长，无法容纳合理的文件名
        available = 50  # 最小文件名长度

    safe_filename = sanitize_filename(filename, max_length=available)
    return directory / safe_filename


def get_unique_filename(directory: str | Path, filename: str) -> Path:
    """
    获取唯一文件名，如果存在则添加序号。

    Args:
        directory: 目标目录
        filename: 期望的文件名

    Returns:
        不冲突的完整路径
    """
    directory = Path(directory)
    safe_filename = sanitize_filename(filename)
    target = directory / safe_filename

    if not target.exists():
        return target

    # 添加序号
    base, ext = safe_filename.rsplit(".", 1) if "." in safe_filename else (safe_filename, "")
    ext = f".{ext}" if ext else ""

    counter = 1
    while True:
        new_name = f"{base} ({counter}){ext}"
        target = directory / new_name
        if not target.exists():
            return target
        counter += 1
        if counter > 9999:
            raise RuntimeError(english("无法生成唯一文件名"))
