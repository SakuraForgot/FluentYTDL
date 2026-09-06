"""
FluentYTDL 字幕配置数据模型

定义字幕下载和处理的配置选项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# ── ydl_opts 里的私有键 ────────────────────────────────────────
# 都以 `__fluentytdl_` 开头，`utils/quick_opts.py` 的透传逻辑按前缀整体放行。

SUBTITLE_PREFS_KEY = "__fluentytdl_subtitle_prefs"
"""**意图**：用户想要哪些语言（`["zh-Hans", "en"]` 这样的偏好，不是真实字幕键）。

生产端（配置窗口 / 快速下载）只声明意图，由 `workers.py` 在拿到 info dict 之后
解析成真实字幕键写进 `subtitleslangs`。

直接把偏好写进 `subtitleslangs` 是本次修复的病根：`--sub-langs` 的每一项被
yt-dlp 当成**锚定正则**，裸 `en` 匹配不到真实键 `en-GB`，一个 `.vtt` 都不会写出来。
"""

SUBTITLE_RESOLUTION_KEY = "__fluentytdl_subtitle_resolution"
"""**结果**：偏好解析成了什么。用于日志、UI 提示和"为什么没有字幕"的追责。

结构见 `processing/subtitle_service.build_resolution_meta()`。
"""

SUBTITLE_CONFIG_KEY = "__fluentytdl_subtitle_config"
"""**上下文**：解析偏好时要遵守哪些设置（`SubtitleConfig.to_dict()` 的产物）。

只有 `SUBTITLE_PREFS_KEY` 时无法迟解析出与"已解析行"一致的结果：`writeautomaticsub`
是个布尔，表达不了 `type_preference` —— 而默认的 `MANUAL_AND_ASR` **排除自动翻译**，
正是 `zh-Hans` 匹配不上 `zh-Hans-en-GB` 的原因。缺了它，未解析的行会下载到用户设置
明确排除的轨道，与解析过的行行为不一致。

`max_languages` 同理（封顶数量）。两者都由 `workers.py` 侧的迟解析读取。
"""


class SubtitleTypePreference(str, Enum):
    MANUAL_ONLY = "manual_only"
    MANUAL_AND_ASR = "manual_and_asr"
    ALL = "all"


@dataclass
class SubtitleConfig:
    """
    字幕配置

    控制字幕下载、嵌入、格式转换等行为。
    """

    # ========== 基础配置 ==========

    enabled: bool = False
    """是否启用字幕下载（全局开关）"""

    type_preference: SubtitleTypePreference = SubtitleTypePreference.MANUAL_AND_ASR
    """字幕类型偏好：控制自动选择时的字幕类型范围"""

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

    output_format: Literal["srt", "ass", "vtt", "lrc"] = "srt"
    """
    字幕输出格式（全局默认）：
    - srt: SubRip（兼容性最佳，推荐）
    - ass: Advanced SubStation Alpha（支持样式）
    - vtt: WebVTT（Web原生格式）
    - lrc: 歌词格式（仅适用于音乐类内容）

    此字段作为所有路径的默认格式权威：
    - 软嵌入时：yt-dlp 获取到字幕后按此格式转换再嵌入容器
    - 外置文件时：下载的字幕文件按此格式转换后保存
    - 纯字幕下载时：作为默认格式（可被 SubtitleSelectorWidget 覆盖）
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
            "type_preference": self.type_preference.value,
            "default_languages": self.default_languages,
            "enable_auto_captions": self.enable_auto_captions,
            "embed_type": self.embed_type,
            "embed_mode": self.embed_mode,
            "output_format": self.output_format,
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
            type_preference=SubtitleTypePreference(data.get("type_preference", "manual_and_asr")),
            default_languages=data.get("default_languages", ["zh-Hans", "en"]),
            enable_auto_captions=data.get("enable_auto_captions", True),
            embed_type=data.get("embed_type", "soft"),
            embed_mode=data.get("embed_mode", "always"),
            output_format=data.get("output_format", "srt"),
            quality_check=data.get("quality_check", True),
            remove_ads=data.get("remove_ads", False),
            fallback_to_english=data.get("fallback_to_english", True),
            max_languages=data.get("max_languages", 10),
        )


@dataclass
class PlaylistSubtitleOverride:
    """播放列表级字幕覆盖配置"""

    target_languages: list[str]
    """用户选择的语言列表"""

    enable_auto_captions: bool
    """是否启用自动字幕"""

    embed_subtitles: bool
    """是否嵌入到视频"""

    output_format: str
    """字幕格式"""
