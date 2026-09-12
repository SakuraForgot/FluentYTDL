"""
FluentYTDL 智能片段模块

提供 SponsorBlock 集成和章节嵌入功能：
- SponsorBlock 广告跳过
- 章节信息嵌入
- 智能片段处理
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.utils.ui_text import tr_text

# SponsorBlock 片段类型
SPONSOR_CATEGORIES = {
    "sponsor": (
        QT_TRANSLATE_NOOP("RuntimeText", "赞助广告"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过赞助商内容"),
    ),
    "selfpromo": (
        QT_TRANSLATE_NOOP("RuntimeText", "自我推广"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过频道推广"),
    ),
    "interaction": (
        QT_TRANSLATE_NOOP("RuntimeText", "互动提醒"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过订阅/点赞提醒"),
    ),
    "intro": (
        QT_TRANSLATE_NOOP("RuntimeText", "片头"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过视频片头"),
    ),
    "outro": (
        QT_TRANSLATE_NOOP("RuntimeText", "片尾"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过视频片尾"),
    ),
    "preview": (
        QT_TRANSLATE_NOOP("RuntimeText", "预告"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过预告片段"),
    ),
    "music_offtopic": (
        QT_TRANSLATE_NOOP("RuntimeText", "非音乐"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过非音乐部分"),
    ),
    "poi_highlight": (
        QT_TRANSLATE_NOOP("RuntimeText", "高光"),
        QT_TRANSLATE_NOOP("RuntimeText", "视频精华时刻"),
    ),
    "filler": (
        QT_TRANSLATE_NOOP("RuntimeText", "填充"),
        QT_TRANSLATE_NOOP("RuntimeText", "跳过无关内容"),
    ),
}

# 默认启用的类别
DEFAULT_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro"]


@dataclass
class SponsorSegment:
    """SponsorBlock 片段"""

    category: str  # 类别
    start: float  # 开始时间（秒）
    end: float  # 结束时间（秒）
    action: str = "skip"  # 动作: skip, mute, poi

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def category_name(self) -> str:
        return tr_text(SPONSOR_CATEGORIES.get(self.category, (self.category, ""))[0])

    def __str__(self) -> str:
        return f"{self.category_name} ({self.start:.1f}s - {self.end:.1f}s)"


@dataclass
class Chapter:
    """视频章节"""

    title: str
    start: float  # 开始时间（秒）
    end: float  # 结束时间（秒）

    @property
    def duration(self) -> float:
        return self.end - self.start

    def __str__(self) -> str:
        m = int(self.start // 60)
        s = int(self.start % 60)
        return f"{m}:{s:02d} - {self.title}"


def build_sponsorblock_opts(
    categories: list[str] | None = None,
    remove: bool = True,
    mark: bool = False,
) -> dict[str, Any]:
    """
    构建 SponsorBlock 相关的 yt-dlp 选项

    Args:
        categories: 要处理的类别列表，None 使用默认
        remove: 是否移除片段
        mark: 是否标记章节

    Returns:
        yt-dlp 选项字典
    """
    opts: dict[str, Any] = {}

    cats = categories or DEFAULT_CATEGORIES

    if remove:
        # --sponsorblock-remove CATEGORY
        opts["sponsorblock_remove"] = cats

    if mark:
        # --sponsorblock-mark CATEGORY
        opts["sponsorblock_mark"] = cats

    return opts


def build_sponsorblock_cli_args(
    categories: list[str] | None = None,
    remove: bool = True,
    mark: bool = False,
) -> list[str]:
    """
    构建 SponsorBlock CLI 参数

    Args:
        categories: 类别列表
        remove: 是否移除
        mark: 是否标记

    Returns:
        CLI 参数列表
    """
    args = []
    cats = categories or DEFAULT_CATEGORIES

    if remove:
        for cat in cats:
            args.extend(["--sponsorblock-remove", cat])

    if mark:
        for cat in cats:
            args.extend(["--sponsorblock-mark", cat])

    return args


def extract_chapters(info: dict[str, Any]) -> list[Chapter]:
    """
    从视频信息中提取章节

    Args:
        info: yt-dlp 返回的视频信息

    Returns:
        章节列表
    """
    chapters = []
    raw_chapters = info.get("chapters") or []

    for ch in raw_chapters:
        try:
            title = str(ch.get("title", "")).strip()
            start = float(ch.get("start_time", 0))
            end = float(ch.get("end_time", 0))

            if title and end > start:
                chapters.append(Chapter(title=title, start=start, end=end))
        except (ValueError, TypeError):
            continue

    return chapters


def build_chapter_embed_opts() -> dict[str, Any]:
    """
    构建章节嵌入选项

    Returns:
        yt-dlp 选项字典
    """
    return {
        "embed_chapters": True,
        "postprocessors": [{"key": "FFmpegMetadata"}],
    }


def build_chapter_cli_args() -> list[str]:
    """
    构建章节嵌入 CLI 参数

    Returns:
        CLI 参数列表
    """
    return ["--embed-chapters"]


def get_available_categories() -> list[dict[str, str]]:
    """
    获取可用的 SponsorBlock 类别（用于 UI）

    Returns:
        [{"id": "sponsor", "name": "赞助广告", "desc": "跳过赞助商内容"}, ...]
    """
    return [
        {"id": cat_id, "name": tr_text(name), "desc": tr_text(desc)}
        for cat_id, (name, desc) in SPONSOR_CATEGORIES.items()
    ]


def get_default_categories() -> list[str]:
    """获取默认启用的类别"""
    return list(DEFAULT_CATEGORIES)


class SponsorBlockConfig:
    """
    SponsorBlock 配置管理

    管理用户的类别选择和偏好设置。
    """

    def __init__(self):
        self._enabled = True
        self._remove_categories = list(DEFAULT_CATEGORIES)
        self._mark_categories: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)

    @property
    def remove_categories(self) -> list[str]:
        return list(self._remove_categories)

    @remove_categories.setter
    def remove_categories(self, value: list[str]):
        valid = [c for c in value if c in SPONSOR_CATEGORIES]
        self._remove_categories = valid

    @property
    def mark_categories(self) -> list[str]:
        return list(self._mark_categories)

    @mark_categories.setter
    def mark_categories(self, value: list[str]):
        valid = [c for c in value if c in SPONSOR_CATEGORIES]
        self._mark_categories = valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "remove_categories": self._remove_categories,
            "mark_categories": self._mark_categories,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SponsorBlockConfig:
        config = cls()
        config._enabled = data.get("enabled", True)
        config._remove_categories = data.get("remove_categories", list(DEFAULT_CATEGORIES))
        config._mark_categories = data.get("mark_categories", [])
        return config

    def get_cli_args(self) -> list[str]:
        """获取 CLI 参数"""
        if not self._enabled:
            return []

        return build_sponsorblock_cli_args(
            categories=self._remove_categories,
            remove=bool(self._remove_categories),
            mark=bool(self._mark_categories),
        )

    def get_opts(self) -> dict[str, Any]:
        """获取 yt-dlp 选项"""
        if not self._enabled:
            return {}

        return build_sponsorblock_opts(
            categories=self._remove_categories,
            remove=bool(self._remove_categories),
            mark=bool(self._mark_categories),
        )


# 全局配置实例
sponsorblock_config = SponsorBlockConfig()
