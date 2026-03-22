"""
打分引擎模块

提供简易模式格式选择所需的统一打分逻辑，涵盖：
- 音轨语言偏好评分（等差间距 + BCP-47 别名匹配）
- 视频流打分（分辨率 + 编解码器兼容性）
- 容器格式决策（感知字幕嵌入需求）
- BCP-47 语言工具函数（供 youtube_service.py format_sort 复用）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── BCP-47 别名映射 ───────────────────────────────────────────
# key: 用户偏好写法（小写）  value: YouTube/yt-dlp 实际可能使用的等价 tag 集合
_BCP47_ALIASES: dict[str, set[str]] = {
    "zh-hans": {"zh-cn", "zh-sg", "zh-simplified", "zh"},
    "zh-hant": {"zh-tw", "zh-hk", "zh-mo", "zh-traditional"},
    "zh": {"zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-sg", "zh-hk"},
    "en": {"en-us", "en-gb", "en-au", "en-ca"},
}


def _bcp47_match(pref: str, lang: str) -> bool:
    """
    判断音轨语言标注 lang 是否符合用户偏好 pref。

    匹配规则（优先级从高到低）：
    1. 完全相等（大小写不敏感）
    2. lang 以 pref+"-" 开头（前缀匹配：zh 命中 zh-Hans）
    3. 查别名表（zh-hans 命中 zh-cn、zh-sg 等）
    """
    if not pref or not lang:
        return False
    pref_lower = pref.strip().lower()
    lang_lower = lang.strip().lower()
    if pref_lower == lang_lower:
        return True
    if lang_lower.startswith(pref_lower + "-"):
        return True
    return lang_lower in _BCP47_ALIASES.get(pref_lower, set())


def bcp47_expand_for_sort(lang: str) -> list[str]:
    """
    将单个语言偏好展开为 yt-dlp format_sort lang: 条目列表（含别名）。

    用于 youtube_service.py 旧路径的 format_sort 拼装，替代原来分散的
    手工别名追加逻辑，确保与 _bcp47_match 使用同一数据源。

    示例：
      bcp47_expand_for_sort("zh-Hans") → ["lang:zh-hans","lang:zh-cn","lang:zh-sg",...]
      bcp47_expand_for_sort("orig")    → ["lang:orig"]
    """
    norm = lang.strip().lower()
    result = [f"lang:{norm}"]
    for alias in _BCP47_ALIASES.get(norm, set()):
        result.append(f"lang:{alias}")
    return result


# ── 打分上下文 ────────────────────────────────────────────────


@dataclass
class ScoringContext:
    """
    打分上下文：封装影响格式选择的所有外部因素。

    由 format_selector.py get_selection_result() 在进行打分前构建，
    将用户设置、预设意图和字幕配置统一传递给各打分函数。
    """

    is_simple_mode: bool = True
    """是否处于简易模式（影响容器兼容性惩罚）"""

    max_height: int | None = None
    """分辨率上限（None = 不限制）"""

    prefer_ext: str | None = "mp4"
    """偏好容器格式，如 'mp4'（None = 不限制）"""

    preferred_audio_langs: list[str] = field(default_factory=lambda: ["orig", "zh-Hans", "en"])
    """音轨语言偏好序列（从 config_manager preferred_audio_languages 读取）"""

    embed_subtitles: bool = False
    """是否嵌入字幕（影响容器决策预判，但最终权威是 _ensure_subtitle_compatible_container）"""

    subtitle_lang_count: int = 0
    """嵌入字幕语言数（> 1 时 mp4 mov_text 多轨支持差，建议升级为 mkv）"""


# ── 音频打分 ──────────────────────────────────────────────────

# 偏好权重常数（等差间距，第 10 个偏好仍有效）
_AUDIO_PREF_BASE = 100_000_000  # 偏好基准，远超 abr 数值范围 (0–500 kbps)
_AUDIO_PREF_STEP = 10_000_000  # 每一偏好位降低（等差）
_AUDIO_ORIG_BONUS = 1_000_000  # orig 无偏好命中时的兜底加分


def score_audio_format(f: dict[str, Any], ctx: ScoringContext) -> int:
    """
    对单条音频流评分，数值越大越优先。

    评分逻辑：
    - 命中用户偏好列表第 i 项 → BASE - i*STEP + abr
    - 无命中但为原音轨      → ORIG_BONUS + abr
    - 完全无匹配            → abr（码率兜底，避免返回 0）
    """
    lang = str(f.get("language") or "").strip().lower()
    ttype = str(f.get("audio_track_type") or "").strip().lower()
    abr = int(f.get("abr") or f.get("tbr") or 0)
    ext = str(f.get("ext") or "").strip().lower()
    acodec = str(f.get("acodec") or "").strip().lower()

    # 容器亲和性补偿：如果目标是 MP4，重赏原生支持的音频流，
    # 足以抵消 WebM/Opus (如 160kbps) 对比 M4A/AAC (如 128kbps) 的微弱码率优势，而不影响宏观的语言偏好顺序
    affinity_bonus = 0
    if ctx.prefer_ext == "mp4":
        if ext in {"m4a", "aac"} or "mp4a" in acodec or "aac" in acodec:
            affinity_bonus = 2000

    is_orig = ttype == "original" or lang in {"orig", "original"}

    for i, pref in enumerate(ctx.preferred_audio_langs):
        score = _AUDIO_PREF_BASE - i * _AUDIO_PREF_STEP + affinity_bonus
        p = pref.strip().lower()
        if p == "orig" and is_orig:
            return score + abr
        if _bcp47_match(p, lang):
            return score + abr

    # 无偏好命中
    if is_orig:
        return _AUDIO_ORIG_BONUS + affinity_bonus + abr
    return affinity_bonus + abr


# ── 视频打分（保留旧函数签名供其他模块按需调用）────────────────


def is_mkv_heavy_stream(f: dict[str, Any]) -> bool:
    """粗略判断视频流是否为强迫转码封装（VP9 / AV1 等难以无损汇入 MP4 的格式）"""
    vcodec = (f.get("vcodec") or "").lower()
    if "avc" in vcodec or "h264" in vcodec:
        return False
    if "av01" in vcodec or "vp9" in vcodec:
        return True
    return False


def score_video_format(f: dict[str, Any], is_simple_mode: bool = True) -> int:
    """
    对单条视频流评分（旧接口，内部仍使用）。

    简易模式下大幅惩罚 VP9/AV1 流（避免触发 FFmpeg 转封装假死），
    并奖励 H.264 + mp4 组合以保证最大播放兼容性。
    """
    score = 0
    h = int(f.get("height") or 0)
    score += h * 10

    fps = f.get("fps")
    if fps and float(fps) > 30:
        score += 500

    ext = (f.get("ext") or "").lower()
    vcodec = (f.get("vcodec") or "").lower()

    if is_simple_mode:
        if ext == "mp4":
            score += 2000
        if "avc" in vcodec or "h264" in vcodec:
            score += 1000
        if is_mkv_heavy_stream(f):
            score -= 5000

    return score


# ── 容器决策 ──────────────────────────────────────────────────


def _choose_lossless_merge_container(video_ext: str | None, audio_ext: str | None) -> str | None:
    """无损合并容器推断（与 format_selector.py 中同名函数保持一致）"""
    v = str(video_ext or "").strip().lower()
    a = str(audio_ext or "").strip().lower()
    if not v or not a:
        return None
    if v == "webm" and a == "webm":
        return "webm"
    if v in {"mp4", "m4v"} and a in {"m4a", "aac", "mp4"}:
        return "mp4"
    return "mkv"


def decide_merge_container(
    vid_ext: str | None,
    aud_ext: str | None,
    ctx: ScoringContext,
) -> str:
    """
    统一容器决策函数，感知字幕嵌入需求。

    优先级：
    1. 多语言字幕嵌入（> 1 语言）→ 强制 mkv（mp4 mov_text 多轨播放支持差）
    2. 单字幕 + WebM → mkv
    3. 无字幕/单字幕：按 vid_ext + aud_ext 无损推断
    4. 兜底：mkv

    注意：此函数在 subtitle_service.apply() **之前**执行，
    subtitle_lang_count 来自 ScoringContext 预填充。
    _ensure_subtitle_compatible_container 作为后置修正兜底，
    可以再次覆盖本函数的决策。
    """
    # 多字幕嵌入 → 强制 mkv
    if ctx.embed_subtitles and ctx.subtitle_lang_count > 1:
        return "mkv"

    naive = _choose_lossless_merge_container(vid_ext, aud_ext)

    # 单字幕 + WebM → mkv
    if ctx.embed_subtitles and naive == "webm":
        return "mkv"

    return naive or "mkv"
