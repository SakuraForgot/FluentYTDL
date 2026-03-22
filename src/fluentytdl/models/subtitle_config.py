"""
FluentYTDL 字幕配置数据模型

定义字幕下载和处理的配置选项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class SubtitleConfig:
    """
    字幕配置

    控制字幕下载、嵌入、格式转换等行为。
    """

    # ========== 基础配置 ==========

    enabled: bool = False
    """是否启用字幕下载（全局开关）"""

    default_languages: list[str] = field(default_factory=lambda: ["zh-Hans", "en"])
    """默认字幕语言优先级列表（按优先级排序）"""

    enable_auto_captions: bool = True
    """是否启用自动生成字幕（当手动字幕不可用时）"""

    # ========== 嵌入配置 ==========

    embed_type: Literal["soft", "external"] = "soft"
    """
    字幕嵌入类型：
    - soft: 软嵌入到视频容器（可开关，支持多轨，推荐）
    - external: 外置独立文件（.srt/.ass，兼容性最佳）
    """

    embed_mode: Literal["always", "never", "ask"] = "always"
    """
    字幕嵌入模式（仅 embed_type="soft" 时有效）：
    - always: 总是嵌入到视频文件
    - never: 总是保存为单独文件
    - ask: 每次下载时询问
    """

    # ========== 质量与后处理 ==========

    quality_check: bool = True
    """是否启用字幕质量检查（检测空文件、损坏文件）"""

    remove_ads: bool = False
    """是否自动移除字幕中的广告内容（实验性功能）"""

    # ========== 高级选项 ==========

    fallback_to_english: bool = True
    """当首选语言不可用时，是否自动回退到英语"""

    max_languages: int = 10
    """最多下载字幕语言数量（防止过多字幕文件）"""

    def to_dict(self) -> dict:
        """转换为字典格式（用于保存到 JSON）"""
        return {
            "enabled": self.enabled,
            "default_languages": self.default_languages,
            "enable_auto_captions": self.enable_auto_captions,
            "embed_type": self.embed_type,
            "embed_mode": self.embed_mode,
            "quality_check": self.quality_check,
            "remove_ads": self.remove_ads,
            "fallback_to_english": self.fallback_to_english,
            "max_languages": self.max_languages,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SubtitleConfig:
        """从字典创建配置对象"""
        return cls(
            enabled=data.get("enabled", False),
            default_languages=data.get("default_languages", ["zh-Hans", "en"]),
            enable_auto_captions=data.get("enable_auto_captions", True),
            embed_type=data.get("embed_type", "soft"),
            embed_mode=data.get("embed_mode", "always"),
            quality_check=data.get("quality_check", True),
            remove_ads=data.get("remove_ads", False),
            fallback_to_english=data.get("fallback_to_english", True),
            max_languages=data.get("max_languages", 10),
        )
