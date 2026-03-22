"""
FluentYTDL 字幕服务层

提供字幕下载和处理的统一服务接口，使用策略模式支持多种字幕下载方案。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.config_manager import config_manager
from ..models.subtitle_config import SubtitleConfig
from .subtitle_manager import (
    extract_subtitle_tracks,
    get_subtitle_languages,
)


def should_embed_subtitles(config: SubtitleConfig) -> bool:
    """
    判断是否应该嵌入字幕到视频容器（软嵌入）

    Args:
        config: 字幕配置对象

    Returns:
        True 表示应该嵌入，False 表示不嵌入
    """
    # 只有软嵌入类型才使用容器嵌入
    if config.embed_type != "soft":
        return False

    # 检查嵌入模式
    return config.embed_mode != "never"


def build_embed_opts(config: SubtitleConfig) -> dict[str, Any]:
    """
    根据 embed_type 构建完整的嵌入相关选项

    这是统一的入口，确保 embedsubtitles、merge_output_format、
    writesubtitles 等选项的一致性。

    Args:
        config: 字幕配置对象

    Returns:
        嵌入相关的 yt-dlp 选项
    """
    from ..utils.logger import logger as _logger

    _logger.info(
        "[SubEmbed] build_embed_opts: embed_type={}, embed_mode={}",
        config.embed_type,
        config.embed_mode,
    )

    opts: dict[str, Any] = {}

    if config.embed_type == "soft":
        # 软嵌入：封装到视频容器中
        if config.embed_mode != "never":
            opts["embedsubtitles"] = True
            # 注意：不在此处设置 merge_output_format
            # MP4 和 MKV 都支持字幕嵌入（FFmpeg 会自动将 SRT 转为 mov_text）
            # 只有 WebM 不支持 SRT/ASS 嵌入
            # 容器格式由格式选择器决定，仅在必要时（WebM/未指定）才覆盖
        else:
            opts["embedsubtitles"] = False
        opts["writesubtitles"] = True  # 需要先下载字幕才能嵌入

    elif config.embed_type == "external":
        # 外置文件：只下载字幕，不嵌入
        opts["embedsubtitles"] = False
        opts["writesubtitles"] = True

    _logger.info("[SubEmbed] build_embed_opts 返回: {}", opts)
    return opts


@dataclass
class SubtitleRequest:
    """
    字幕下载请求

    封装一次字幕下载所需的所有信息。
    """

    video_id: str
    """视频 ID"""

    video_info: dict[str, Any]
    """yt-dlp 返回的视频信息"""

    user_config: SubtitleConfig | None = None
    """用户指定的配置（None 表示使用全局配置）"""

    override_languages: list[str] | None = None
    """临时覆盖语言列表（None 表示使用配置中的语言）"""


class SubtitleStrategy(ABC):
    """
    字幕下载策略接口

    不同的策略实现不同的字幕下载方案（单语、双语、智能选择等）。
    """

    @abstractmethod
    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        """
        应用策略生成 yt-dlp 选项

        Args:
            request: 字幕下载请求

        Returns:
            yt-dlp 选项字典
        """
        pass

    @abstractmethod
    def get_description(self) -> str:
        """获取策略描述（用于日志和 UI 显示）"""
        pass


class NoneStrategy(SubtitleStrategy):
    """不下载字幕策略"""

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        # 显式禁用所有字幕选项，确保覆盖外部 yt-dlp 配置
        return {
            "writesubtitles": False,
            "writeautomaticsub": False,
            "embedsubtitles": False,
        }

    def get_description(self) -> str:
        return "不下载字幕"


class SingleLanguageStrategy(SubtitleStrategy):
    """
    单语言字幕策略

    下载单个语言的字幕（优先手动字幕，回退自动字幕）。
    """

    def __init__(self, language: str, enable_auto: bool = True):
        self.language = language
        self.enable_auto = enable_auto

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)

        # 检查字幕是否可用
        available = [t for t in tracks if t.lang_code == self.language]
        if not available:
            return {}

        config = request.user_config or config_manager.get_subtitle_config()
        embed_opts = build_embed_opts(config)

        opts = {
            "writeautomaticsub": self.enable_auto,
            "subtitleslangs": [self.language],
        }
        opts.update(embed_opts)
        return opts

    def get_description(self) -> str:
        return f"单语言字幕: {self.language}"


class MultiLanguageStrategy(SubtitleStrategy):
    """
    多语言字幕策略

    下载多个语言的字幕，按优先级列表顺序尝试。
    """

    def __init__(self, languages: list[str], max_languages: int = 10):
        self.languages = languages
        self.max_languages = max_languages

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)
        available_codes = {t.lang_code for t in tracks}

        # 按优先级筛选可用语言
        selected = []
        for lang in self.languages:
            if lang in available_codes:
                selected.append(lang)
                if len(selected) >= self.max_languages:
                    break

        if not selected:
            return {}

        config = request.user_config or config_manager.get_subtitle_config()
        embed_opts = build_embed_opts(config)

        opts = {
            "writeautomaticsub": config.enable_auto_captions,
            "subtitleslangs": selected,
        }
        opts.update(embed_opts)
        return opts

    def get_description(self) -> str:
        return (
            f"多语言字幕: {', '.join(self.languages[:3])}{'...' if len(self.languages) > 3 else ''}"
        )


class SmartStrategy(SubtitleStrategy):
    """
    智能字幕策略

    根据视频可用字幕自动选择最佳语言：
    1. 优先中文（简体/繁体/通用）
    2. 回退英语
    3. 回退日语
    4. 如果以上都没有，选择第一个可用字幕
    """

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)
        if not tracks:
            return {}

        available_codes = {t.lang_code for t in tracks}

        # 智能选择逻辑
        selected = []

        # 1. 中文优先
        for zh_variant in ["zh-Hans", "zh-Hant", "zh", "zh-CN", "zh-TW"]:
            if zh_variant in available_codes:
                selected.append(zh_variant)
                break

        # 2. 英语作为第二语言
        if "en" in available_codes and len(selected) < 2:
            selected.append("en")

        # 3. 日语作为第三选择
        if not selected and "ja" in available_codes:
            selected.append("ja")

        # 4. 如果还是空，使用第一个可用字幕
        if not selected and tracks:
            selected.append(tracks[0].lang_code)

        if not selected:
            return {}

        config = request.user_config or config_manager.get_subtitle_config()
        embed_opts = build_embed_opts(config)

        opts = {
            "writeautomaticsub": config.enable_auto_captions,
            "subtitleslangs": selected,
        }
        opts.update(embed_opts)
        return opts

    def get_description(self) -> str:
        return "智能选择字幕（中文→英语→日语）"


class SubtitleService:
    """
    字幕服务单例

    提供字幕下载的统一入口，管理配置和策略选择。
    """

    _instance: SubtitleService | None = None

    def __new__(cls) -> SubtitleService:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_config(self) -> SubtitleConfig:
        """获取当前字幕配置"""
        return config_manager.get_subtitle_config()

    def save_config(self, config: SubtitleConfig) -> None:
        """保存字幕配置"""
        config_manager.set_subtitle_config(config)

    def resolve_strategy(
        self,
        video_info: dict[str, Any],
        config: SubtitleConfig | None = None,
    ) -> SubtitleStrategy:
        """
        根据配置和视频信息解析出合适的策略

        Args:
            video_info: 视频信息
            config: 字幕配置（None 表示使用全局配置）

        Returns:
            字幕下载策略
        """
        if config is None:
            config = self.get_config()

        # 全局禁用
        if not config.enabled:
            return NoneStrategy()

        # 多语言模式
        if len(config.default_languages) > 1:
            return MultiLanguageStrategy(
                config.default_languages,
                config.max_languages,
            )

        # 单语言模式
        if len(config.default_languages) == 1:
            return SingleLanguageStrategy(
                config.default_languages[0],
                config.enable_auto_captions,
            )

        # 无配置语言，使用智能策略
        return SmartStrategy()

    def apply(
        self,
        video_id: str,
        video_info: dict[str, Any],
        user_config: SubtitleConfig | None = None,
        override_languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        应用字幕策略，生成 yt-dlp 选项

        Args:
            video_id: 视频 ID
            video_info: 视频信息
            user_config: 用户临时配置
            override_languages: 临时覆盖语言列表

        Returns:
            yt-dlp 选项字典
        """
        request = SubtitleRequest(
            video_id=video_id,
            video_info=video_info,
            user_config=user_config,
            override_languages=override_languages,
        )

        # 如果有语言覆盖，使用多语言策略
        if override_languages:
            config = user_config or self.get_config()
            strategy = MultiLanguageStrategy(override_languages, config.max_languages)
        else:
            strategy = self.resolve_strategy(video_info, user_config)

        return strategy.apply(request)

    def get_available_languages(self, video_info: dict[str, Any]) -> list[dict[str, Any]]:
        """获取视频可用字幕语言列表（用于 UI 显示）"""
        return get_subtitle_languages(video_info)


# 全局单例
subtitle_service = SubtitleService()
