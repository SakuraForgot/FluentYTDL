"""
封面嵌入支持模块

负责:
- 检测文件格式是否支持封面嵌入
- 提供封面嵌入格式兼容性信息
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.utils.ui_text import tr_text


class ThumbnailEmbedSupport(Enum):
    """封面嵌入支持级别"""

    FULL = "full"  # 完全支持（MP4, MKV, MP3, M4A 等）
    PARTIAL = "partial"  # 部分支持（可能有兼容性问题）
    NONE = "none"  # 不支持（WAV, AVI 等）


@dataclass
class FormatThumbnailInfo:
    """格式的封面嵌入信息"""

    extension: str
    support: ThumbnailEmbedSupport
    tool: str  # 使用的工具（ffmpeg / mutagen / atomicparsley）
    note: str  # 备注说明


# 格式封面嵌入支持表
FORMAT_THUMBNAIL_SUPPORT: dict[str, FormatThumbnailInfo] = {
    # === 完全支持（视频） ===
    "mp4": FormatThumbnailInfo(
        "mp4", ThumbnailEmbedSupport.FULL, "ffmpeg", QT_TRANSLATE_NOOP("RuntimeText", "最广泛支持")
    ),
    "m4v": FormatThumbnailInfo(
        "m4v", ThumbnailEmbedSupport.FULL, "ffmpeg", QT_TRANSLATE_NOOP("RuntimeText", "MPEG-4 视频")
    ),
    "mkv": FormatThumbnailInfo(
        "mkv",
        ThumbnailEmbedSupport.FULL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "作为附件嵌入"),
    ),
    "webm": FormatThumbnailInfo(
        "webm",
        ThumbnailEmbedSupport.FULL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "作为附件嵌入"),
    ),
    "mov": FormatThumbnailInfo(
        "mov",
        ThumbnailEmbedSupport.FULL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "QuickTime 格式"),
    ),
    # === 完全支持（音频） ===
    "mp3": FormatThumbnailInfo(
        "mp3",
        ThumbnailEmbedSupport.FULL,
        "mutagen",
        QT_TRANSLATE_NOOP("RuntimeText", "ID3 标签嵌入"),
    ),
    "m4a": FormatThumbnailInfo(
        "m4a",
        ThumbnailEmbedSupport.FULL,
        "ffmpeg/mutagen",
        QT_TRANSLATE_NOOP("RuntimeText", "AAC 音频容器"),
    ),
    "m4b": FormatThumbnailInfo(
        "m4b", ThumbnailEmbedSupport.FULL, "ffmpeg", QT_TRANSLATE_NOOP("RuntimeText", "有声书格式")
    ),
    "mka": FormatThumbnailInfo(
        "mka",
        ThumbnailEmbedSupport.FULL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "Matroska 音频"),
    ),
    "ogg": FormatThumbnailInfo(
        "ogg",
        ThumbnailEmbedSupport.FULL,
        "mutagen",
        QT_TRANSLATE_NOOP("RuntimeText", "Vorbis comment 嵌入"),
    ),
    "opus": FormatThumbnailInfo(
        "opus", ThumbnailEmbedSupport.FULL, "mutagen", QT_TRANSLATE_NOOP("RuntimeText", "Opus 音频")
    ),
    "flac": FormatThumbnailInfo(
        "flac", ThumbnailEmbedSupport.FULL, "mutagen", "FLAC metadata block"
    ),
    "aac": FormatThumbnailInfo(
        "aac",
        ThumbnailEmbedSupport.PARTIAL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "需要重新封装为 M4A"),
    ),
    # === 部分支持 ===
    "wma": FormatThumbnailInfo(
        "wma",
        ThumbnailEmbedSupport.PARTIAL,
        "ffmpeg",
        QT_TRANSLATE_NOOP("RuntimeText", "Windows Media，兼容性一般"),
    ),
    "asf": FormatThumbnailInfo(
        "asf", ThumbnailEmbedSupport.PARTIAL, "ffmpeg", QT_TRANSLATE_NOOP("RuntimeText", "ASF 容器")
    ),
    # === 不支持 ===
    "wav": FormatThumbnailInfo(
        "wav",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "格式不支持元数据/封面"),
    ),
    "aiff": FormatThumbnailInfo(
        "aiff", ThumbnailEmbedSupport.NONE, "", QT_TRANSLATE_NOOP("RuntimeText", "理论支持但未实现")
    ),
    "avi": FormatThumbnailInfo(
        "avi",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "老旧格式，无标准封面机制"),
    ),
    "flv": FormatThumbnailInfo(
        "flv",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "Flash 格式，无封面支持"),
    ),
    "3gp": FormatThumbnailInfo(
        "3gp",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "移动端老格式，支持有限"),
    ),
    "ts": FormatThumbnailInfo(
        "ts", ThumbnailEmbedSupport.NONE, "", QT_TRANSLATE_NOOP("RuntimeText", "传输流，不支持封面")
    ),
    "m2ts": FormatThumbnailInfo(
        "m2ts",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "蓝光传输流，不支持封面"),
    ),
    "vob": FormatThumbnailInfo(
        "vob",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "DVD 格式，不支持封面"),
    ),
    "wmv": FormatThumbnailInfo(
        "wmv",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "老旧格式，封面支持差"),
    ),
    "rm": FormatThumbnailInfo(
        "rm", ThumbnailEmbedSupport.NONE, "", QT_TRANSLATE_NOOP("RuntimeText", "RealMedia，不支持")
    ),
    "rmvb": FormatThumbnailInfo(
        "rmvb",
        ThumbnailEmbedSupport.NONE,
        "",
        QT_TRANSLATE_NOOP("RuntimeText", "RealMedia VBR，不支持"),
    ),
}


def get_thumbnail_support(extension: str) -> FormatThumbnailInfo:
    """获取指定扩展名的封面嵌入支持信息

    Args:
        extension: 文件扩展名（不含点，如 "mp4"）

    Returns:
        FormatThumbnailInfo 对象
    """
    ext = extension.lower().lstrip(".")

    if ext in FORMAT_THUMBNAIL_SUPPORT:
        info = FORMAT_THUMBNAIL_SUPPORT[ext]
        return replace(info, note=tr_text(info.note))

    # 未知格式默认为不支持
    return FormatThumbnailInfo(
        extension=ext,
        support=ThumbnailEmbedSupport.PARTIAL,
        tool="ffmpeg",
        note=tr_text("未知格式，尝试使用 ffmpeg 嵌入"),
    )


def can_embed_thumbnail(extension: str) -> bool:
    """检查指定格式是否支持封面嵌入

    Args:
        extension: 文件扩展名（不含点）

    Returns:
        True 如果支持或部分支持，False 如果不支持
    """
    info = get_thumbnail_support(extension)
    return info.support != ThumbnailEmbedSupport.NONE


def get_unsupported_formats_warning(extension: str) -> str | None:
    """获取不支持封面嵌入格式的警告信息

    Args:
        extension: 文件扩展名

    Returns:
        警告消息字符串，如果格式支持则返回 None
    """
    info = get_thumbnail_support(extension)

    if info.support == ThumbnailEmbedSupport.NONE:
        return tr_text(
            "⚠️ {0} 格式不支持封面嵌入\n原因：{1}\n下载将继续，但不会嵌入封面图片。",
            extension.upper(),
            info.note,
        )
    elif info.support == ThumbnailEmbedSupport.PARTIAL:
        return tr_text(
            "⚠️ {0} 格式封面嵌入支持有限\n备注：{1}\n封面可能无法正确显示在某些播放器中。",
            extension.upper(),
            info.note,
        )

    return None


def get_supported_formats_list() -> list[str]:
    """获取完全支持封面嵌入的格式列表"""
    return [
        ext
        for ext, info in FORMAT_THUMBNAIL_SUPPORT.items()
        if info.support == ThumbnailEmbedSupport.FULL
    ]


def get_all_formats_info() -> dict[str, dict]:
    """获取所有格式的封面嵌入信息（用于 UI 展示）"""
    result = {}
    for ext, info in FORMAT_THUMBNAIL_SUPPORT.items():
        result[ext] = {
            "support": info.support.value,
            "tool": info.tool,
            "note": tr_text(info.note),
        }
    return result
