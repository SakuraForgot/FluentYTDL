"""
FluentYTDL 字幕管理模块

提供字幕下载、格式转换、双语合成等功能：
- 多语言字幕选择
- 格式转换 (SRT, ASS, VTT)
- 双语字幕合成
- 字幕嵌入
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 常见字幕语言代码映射
LANGUAGE_NAMES = {
    "zh-Hans": "中文(简体)",
    "zh-Hant": "中文(繁体)",
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "es": "西班牙语",
    "fr": "法语",
    "de": "德语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "ar": "阿拉伯语",
    "hi": "印地语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "auto": "自动生成",
}

# UI显示用的常用语言列表（按使用频率和地区排序）
COMMON_SUBTITLE_LANGUAGES = [
    # 东亚地区（高频）
    ("zh-Hans", "中文(简体)"),
    ("zh-Hant", "中文(繁体)"),
    ("en", "英语"),
    ("ja", "日语"),
    ("ko", "韩语"),
    # 欧洲主要语言
    ("fr", "法语"),
    ("de", "德语"),
    ("es", "西班牙语"),
    ("pt", "葡萄牙语"),
    ("it", "意大利语"),
    ("ru", "俄语"),
    # 其他地区
    ("ar", "阿拉伯语"),
    ("hi", "印地语"),
    ("th", "泰语"),
    ("vi", "越南语"),
    ("id", "印尼语"),
    ("tr", "土耳其语"),
    ("nl", "荷兰语"),
    ("pl", "波兰语"),
    ("sv", "瑞典语"),
    ("no", "挪威语"),
]

# 支持的字幕格式
SUBTITLE_FORMATS = ["srt", "ass", "vtt", "lrc"]


@dataclass
class SubtitleTrack:
    """字幕轨道信息"""

    lang_code: str  # 语言代码 (如 "en", "zh-Hans")
    lang_name: str  # 语言名称 (如 "English", "中文")
    is_auto: bool  # 是否自动生成
    ext: str  # 格式 (srt, vtt, ass)
    url: str | None = None  # 下载 URL
    name: str | None = None  # 显示名称

    @property
    def display_name(self) -> str:
        """获取显示名称"""
        name = LANGUAGE_NAMES.get(self.lang_code, self.lang_name or self.lang_code)
        if self.is_auto:
            name += " [自动]"
        return name


def extract_subtitle_tracks(info: dict[str, Any]) -> list[SubtitleTrack]:
    """
    从视频信息中提取可用字幕轨道

    Args:
        info: yt-dlp 返回的视频信息

    Returns:
        字幕轨道列表
    """
    tracks = []

    # 手动字幕
    subtitles = info.get("subtitles") or {}
    for lang_code, sub_list in subtitles.items():
        if not sub_list:
            continue
        # 取第一个格式
        sub = sub_list[0] if isinstance(sub_list, list) else sub_list
        tracks.append(
            SubtitleTrack(
                lang_code=lang_code,
                lang_name=sub.get("name", ""),
                is_auto=False,
                ext=sub.get("ext", "vtt"),
                url=sub.get("url"),
            )
        )

    # 自动生成字幕
    auto_subs = info.get("automatic_captions") or {}
    for lang_code, sub_list in auto_subs.items():
        if not sub_list:
            continue
        sub = sub_list[0] if isinstance(sub_list, list) else sub_list
        tracks.append(
            SubtitleTrack(
                lang_code=lang_code,
                lang_name=sub.get("name", ""),
                is_auto=True,
                ext=sub.get("ext", "vtt"),
                url=sub.get("url"),
            )
        )

    return tracks


def get_subtitle_languages(info: dict[str, Any]) -> list[dict[str, Any]]:
    """
    获取可用字幕语言列表（用于 UI 显示）

    Args:
        info: 视频信息

    Returns:
        [{"code": "en", "name": "英语", "auto": False}, ...]
    """
    tracks = extract_subtitle_tracks(info)

    # 去重：同一语言优先手动字幕
    seen = {}
    for t in tracks:
        key = t.lang_code
        if key not in seen or (not t.is_auto and seen[key]["auto"]):
            seen[key] = {
                "code": t.lang_code,
                "name": t.display_name,
                "auto": t.is_auto,
                "ext": t.ext,
            }

    # 排序：中文 > 英语 > 日语 > 其他
    priority = ["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]

    def sort_key(item):
        code = item["code"]
        try:
            return (0, priority.index(code))
        except ValueError:
            return (1, code)

    return sorted(seen.values(), key=sort_key)


def convert_subtitle(
    input_path: str | Path,
    output_format: str,
    output_path: str | Path | None = None,
    ffmpeg_path: str | None = None,
) -> Path:
    """
    转换字幕格式

    Args:
        input_path: 输入字幕文件
        output_format: 目标格式 (srt, ass, vtt)
        output_path: 输出路径，None 则自动生成
        ffmpeg_path: ffmpeg 路径

    Returns:
        输出文件路径
    """
    input_path = Path(input_path)

    if output_format not in SUBTITLE_FORMATS:
        raise ValueError(f"不支持的字幕格式: {output_format}")

    if output_path is None:
        output_path = input_path.with_suffix(f".{output_format}")
    else:
        output_path = Path(output_path)

    # 使用 ffmpeg 转换
    ffmpeg = ffmpeg_path or "ffmpeg"
    cmd = [
        ffmpeg,
        "-y",  # 覆盖输出
        "-i",
        str(input_path),
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 转换失败: {result.stderr}")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("字幕转换超时") from e

    return output_path
