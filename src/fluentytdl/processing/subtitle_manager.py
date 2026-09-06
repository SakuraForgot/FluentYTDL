"""
FluentYTDL 字幕轨道解析

从 yt-dlp 的 info 字典里读出可用字幕轨道，并给出可读的名字：
- 区分人工 / 自动生成(ASR) / 自动翻译三种来源
- 语言代码 → 本地化显示名（含来源语种）
- 供 UI 选择器与设置页共用的常用语言表

真实的下载、格式转换、嵌入都由 yt-dlp CLI 自己完成（`--write-subs`
/ `--convert-subs` / `--embed-subs`），本模块不碰进程也不碰文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

# 常见字幕语言代码映射
LANGUAGE_NAMES = {
    "zh-Hans": QT_TRANSLATE_NOOP("SubtitleManager", "中文(简体)"),
    "zh-Hant": QT_TRANSLATE_NOOP("SubtitleManager", "中文(繁体)"),
    "zh": QT_TRANSLATE_NOOP("SubtitleManager", "中文"),
    "en": QT_TRANSLATE_NOOP("SubtitleManager", "英语"),
    "ja": QT_TRANSLATE_NOOP("SubtitleManager", "日语"),
    "ko": QT_TRANSLATE_NOOP("SubtitleManager", "韩语"),
    "es": QT_TRANSLATE_NOOP("SubtitleManager", "西班牙语"),
    "fr": QT_TRANSLATE_NOOP("SubtitleManager", "法语"),
    "de": QT_TRANSLATE_NOOP("SubtitleManager", "德语"),
    "ru": QT_TRANSLATE_NOOP("SubtitleManager", "俄语"),
    "pt": QT_TRANSLATE_NOOP("SubtitleManager", "葡萄牙语"),
    "it": QT_TRANSLATE_NOOP("SubtitleManager", "意大利语"),
    "ar": QT_TRANSLATE_NOOP("SubtitleManager", "阿拉伯语"),
    "hi": QT_TRANSLATE_NOOP("SubtitleManager", "印地语"),
    "th": QT_TRANSLATE_NOOP("SubtitleManager", "泰语"),
    "vi": QT_TRANSLATE_NOOP("SubtitleManager", "越南语"),
    "id": QT_TRANSLATE_NOOP("SubtitleManager", "印尼语"),
    "auto": QT_TRANSLATE_NOOP("SubtitleManager", "自动生成"),
}

# UI显示用的常用语言列表（按使用频率和地区排序）
COMMON_SUBTITLE_LANGUAGES = [
    # 东亚地区（高频）
    ("zh-Hans", QT_TRANSLATE_NOOP("SubtitleManager", "中文(简体)")),
    ("zh-Hant", QT_TRANSLATE_NOOP("SubtitleManager", "中文(繁体)")),
    ("en", QT_TRANSLATE_NOOP("SubtitleManager", "英语")),
    ("ja", QT_TRANSLATE_NOOP("SubtitleManager", "日语")),
    ("ko", QT_TRANSLATE_NOOP("SubtitleManager", "韩语")),
    # 欧洲主要语言
    ("fr", QT_TRANSLATE_NOOP("SubtitleManager", "法语")),
    ("de", QT_TRANSLATE_NOOP("SubtitleManager", "德语")),
    ("es", QT_TRANSLATE_NOOP("SubtitleManager", "西班牙语")),
    ("pt", QT_TRANSLATE_NOOP("SubtitleManager", "葡萄牙语")),
    ("it", QT_TRANSLATE_NOOP("SubtitleManager", "意大利语")),
    ("ru", QT_TRANSLATE_NOOP("SubtitleManager", "俄语")),
    # 其他地区
    ("ar", QT_TRANSLATE_NOOP("SubtitleManager", "阿拉伯语")),
    ("hi", QT_TRANSLATE_NOOP("SubtitleManager", "印地语")),
    ("th", QT_TRANSLATE_NOOP("SubtitleManager", "泰语")),
    ("vi", QT_TRANSLATE_NOOP("SubtitleManager", "越南语")),
    ("id", QT_TRANSLATE_NOOP("SubtitleManager", "印尼语")),
    ("tr", QT_TRANSLATE_NOOP("SubtitleManager", "土耳其语")),
    ("nl", QT_TRANSLATE_NOOP("SubtitleManager", "荷兰语")),
    ("pl", QT_TRANSLATE_NOOP("SubtitleManager", "波兰语")),
    ("sv", QT_TRANSLATE_NOOP("SubtitleManager", "瑞典语")),
    ("no", QT_TRANSLATE_NOOP("SubtitleManager", "挪威语")),
]

# 支持的字幕格式
SUBTITLE_FORMATS = ["srt", "ass", "vtt", "lrc"]

# 归一化索引：`LANGUAGE_NAMES` 的键是规范大小写（`zh-Hans`），yt-dlp 回来的键不保证
_LANGUAGE_NAMES_LC = {code.lower(): name for code, name in LANGUAGE_NAMES.items()}


def _lookup_language_name(code: str) -> str | None:
    """逐级截断查 `LANGUAGE_NAMES`：`en-GB` 先查全串，miss 再查 `en`。

    表里只有裸语种码，而真实字幕键几乎总带地区（`en-GB`）或文字（`zh-Hans`），
    所以直接 `.get()` 是必然 miss —— 那正是选择器里中英文混排的成因。
    """
    parts = code.split("-")
    while parts:
        hit = _LANGUAGE_NAMES_LC.get("-".join(parts).lower())
        if hit:
            return hit
        parts.pop()
    return None


class SubtitleSourceType(str, Enum):
    MANUAL = "manual"
    AUTO_GENERATED = "auto_generated"
    AUTO_TRANSLATED = "auto_translated"


@dataclass
class SubtitleTrack:
    """字幕轨道信息"""

    lang_code: str  # 语言代码 (如 "en", "zh-Hans")
    lang_name: str  # 语言名称 (如 "English", "中文")
    source_type: SubtitleSourceType  # 字幕来源类型
    ext: str  # 格式 (srt, vtt, ass)
    url: str | None = None  # 下载 URL
    name: str | None = None  # 显示名称
    is_original_lang: bool = False  # 是否是原语种

    @property
    def is_auto(self) -> bool:
        """向后兼容属性"""
        return self.source_type != SubtitleSourceType.MANUAL

    @property
    def quality_rank(self) -> int:
        """质量排序权重（越低越好）"""
        return {
            SubtitleSourceType.MANUAL: 0,
            SubtitleSourceType.AUTO_GENERATED: 1,
            SubtitleSourceType.AUTO_TRANSLATED: 2,
        }[self.source_type]

    @property
    def display_name(self) -> str:
        """人类可读的轨道名：`英语`、`中文(简体)（由 en-GB 自动翻译）`。

        以前这里是 `LANGUAGE_NAMES.get(self.lang_code, …)`，对**真实 YouTube 字幕键
        必然 miss**（表里是裸语种码，键是 `en-GB` / `zh-Hans-en-GB`），miss 之后回落
        到 yt-dlp 给的英文 `name` 或裸代码，同一张选择器表里中英文混排。现在先用
        `bcp47.split_translated_key()` 拆出目标语种，再逐级截断查表。

        来源语种直接写进名字：自动翻译的质量完全取决于源轨道，
        `中文(简体)（由 en-GB 自动翻译）` 比笼统的 `[自动翻译]` 有用得多 ——
        用户能据此判断"要不要干脆直接下英文人工字幕"。
        """
        from PySide6.QtCore import QCoreApplication

        from ..utils import bcp47

        target, source = bcp47.split_translated_key(self.lang_code)
        table_name = _lookup_language_name(target)
        if table_name:
            translated_name = QCoreApplication.translate("SubtitleManager", table_name)
        else:
            # 表外语种：yt-dlp 的 name 通常是英文全名（`Welsh`），仍比裸代码好读。
            # 复合键的 name 是 YouTube 拼的 `Welsh from English (United Kingdom)`，
            # 来源已经在里面了 —— 不切掉就会和下面的"（由 … 自动翻译）"重复一遍。
            fallback = self.lang_name or self.lang_code
            translated_name = fallback.split(" from ")[0] if source else fallback

        if self.source_type == SubtitleSourceType.MANUAL:
            return translated_name

        if source:
            if self.source_type == SubtitleSourceType.AUTO_GENERATED:
                suffix = QCoreApplication.translate("SubtitleManager", "（由 {0} 自动生成）")
            else:
                suffix = QCoreApplication.translate("SubtitleManager", "（由 {0} 自动翻译）")
            return translated_name + suffix.format(source)

        # 拆不出来源（非复合键的自动字幕）时保持原有措辞
        if self.source_type == SubtitleSourceType.AUTO_GENERATED:
            translated_name += QCoreApplication.translate("SubtitleManager", " [自动生成]")
        elif self.source_type == SubtitleSourceType.AUTO_TRANSLATED:
            translated_name += QCoreApplication.translate("SubtitleManager", " [自动翻译]")
        return translated_name


def _same_language(a: str, b: str) -> bool:
    """两个 tag 是不是同一语种（忽略地区/文字差异）。

    **双向**是刻意的，且与 `bcp47.matches()` 的单向语义不冲突：那里在回答"用户点名
    要 X，这条 Y 算不算"（`en-GB` 不该被一条笼统的 `en` 顶替），这里在回答"这两个
    标注指的是同一种语言吗"，没有谁点名谁。

    比它取代的 `_lang_matches()`（按 `-` 切开比首段）**更严**：那个把 `zh-Hans` 和
    `zh-Hant` 判成同一种语言，于是"简中原声 → 繁中翻译"的轨道会被误标成自动生成。
    """
    from ..utils import bcp47

    return bcp47.matches(a, b) or bcp47.matches(b, a)


def _detect_original_language(info: dict[str, Any]) -> str | None:
    """检测视频原始语种"""
    auto_caps = info.get("automatic_captions") or {}
    for lang_code in auto_caps:
        if lang_code.endswith("-orig"):
            return lang_code.replace("-orig", "")

    for lang_code, sub_list in auto_caps.items():
        if isinstance(sub_list, list) and sub_list:
            first = sub_list[0] if isinstance(sub_list[0], dict) else {}
            name = str(first.get("name", "")).lower()
            if "asr" in name or "auto-generated" in name:
                return lang_code

    lang = info.get("language")
    if isinstance(lang, str) and lang:
        return lang
    return None


def _is_asr_track(lang_code: str, sub_list: Any, original_lang: str | None) -> bool:
    """判断这条自动字幕是 ASR 原声转写，还是从别的语言机翻过来的。

    yt-dlp 把两者混在同一个 `automatic_captions` 里，只能靠键的形状分辨。复合键
    （`en-en-GB` / `zh-Hans-en-GB`）自己就带着答案：来源语种写在后半段，目标 == 来源
    即原声转写。这条判定比拿 `original_lang` 猜可靠得多 —— 顺带修掉原声是中文时、
    每条 `zh-*-<来源>` 机翻轨都被误标成"自动生成"的老毛病（旧的 `_lang_matches()`
    只比首段，`zh-Hans` 和 `zh-Hant` 在它眼里是同一种语言）。
    """
    from ..utils import bcp47

    if lang_code.endswith("-orig"):
        return True

    target, source = bcp47.split_translated_key(lang_code)
    if source and _same_language(target, source):
        return True

    # 非复合键（裸 `en`、`zh-Hans`）才需要拿视频原声语种来判。这里传整个 lang_code：
    # 复合键匹配不上任何简单 tag，于是自然落空，不会把机翻轨误判回原声。
    if original_lang and _same_language(lang_code, original_lang):
        return True

    if isinstance(sub_list, list) and sub_list:
        first = sub_list[0] if isinstance(sub_list[0], dict) else {}
        name = str(first.get("name", "")).lower()
        if "asr" in name or "auto-generated" in name:
            return True

    return False


def extract_subtitle_tracks(info: dict[str, Any]) -> list[SubtitleTrack]:
    """
    从视频信息中提取可用字幕轨道

    Args:
        info: yt-dlp 返回的视频信息

    Returns:
        字幕轨道列表
    """
    tracks = []

    # 检测原始语种
    original_lang = _detect_original_language(info)

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
                source_type=SubtitleSourceType.MANUAL,
                ext=sub.get("ext", "vtt"),
                url=sub.get("url"),
                name=sub.get("name"),
            )
        )

    # 自动生成/翻译字幕
    auto_subs = info.get("automatic_captions") or {}
    for lang_code, sub_list in auto_subs.items():
        if not sub_list:
            continue
        sub = sub_list[0] if isinstance(sub_list, list) else sub_list
        is_asr = _is_asr_track(lang_code, sub_list, original_lang)
        source_type = (
            SubtitleSourceType.AUTO_GENERATED if is_asr else SubtitleSourceType.AUTO_TRANSLATED
        )
        tracks.append(
            SubtitleTrack(
                lang_code=lang_code,
                lang_name=sub.get("name", ""),
                source_type=source_type,
                ext=sub.get("ext", "vtt"),
                url=sub.get("url"),
                name=sub.get("name"),
                is_original_lang=(lang_code == original_lang),
            )
        )

    return tracks


# `get_subtitle_languages()` 在拿不到用户偏好时的排序回落
_DEFAULT_SORT_PREFS = ["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]


def get_subtitle_languages(
    info: dict[str, Any],
    prefs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """获取可用字幕语言列表（用于 UI 显示）。

    Args:
        info: 视频信息
        prefs: 用户的字幕语言偏好，按优先级排列；None 时回落到 `_DEFAULT_SORT_PREFS`

    Returns:
        `[{"code": "en-GB", "name": "英语", "auto": False, "ext": "vtt"}, ...]`
        —— 命中偏好的语言在前，其余按代码字典序。
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

    # 排序按偏好命中度，而**不是**精确代码比对：`priority.index("en-GB")` 抛
    # ValueError，真实 YouTube 键会整批落进兜底档，排出来的顺序和偏好毫无关系。
    from ..utils import bcp47

    order = prefs or _DEFAULT_SORT_PREFS

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        idx, tier = bcp47.preference_rank(order, item["code"])
        return idx, tier, item["code"]

    return sorted(seen.values(), key=sort_key)
