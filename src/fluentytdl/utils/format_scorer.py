"""
打分引擎模块

提供简易模式格式选择所需的统一打分逻辑，涵盖：
- 音轨语言偏好评分（等差间距 + BCP-47 别名匹配）
- 视频流打分（分辨率 + 编解码器兼容性）
- 容器格式决策（感知字幕嵌入需求）
- BCP-47 语言工具函数（薄委托到 `utils/bcp47.py`，供 youtube_service.py format_sort 复用）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bcp47 import expand_for_sort as bcp47_expand_for_sort  # noqa: F401  (re-export)
from .bcp47 import matches as _bcp47_match
from .container_compat import choose_lossless_merge_container

# BCP-47 匹配与别名表已迁到 utils/bcp47.py（字幕路径也要用同一套语义）。
# 这里保留两个旧名字作为薄委托：`_bcp47_match` 供本模块的音轨打分使用，
# `bcp47_expand_for_sort` 供 youtube_service.py 的 format_sort 拼装使用。


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

    audio_track_count: int = 1
    """音轨数量（> 1 时因 mp4 对多音轨支持不佳，强制或建议升级为 mkv）"""


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
    format_note = str(f.get("format_note") or "").strip().lower()
    abr = int(f.get("abr") or f.get("tbr") or 0)
    ext = str(f.get("ext") or "").strip().lower()
    acodec = str(f.get("acodec") or "").strip().lower()

    # 容器亲和性补偿：如果目标是 MP4，重赏原生支持的音频流，
    # 足以抵消 WebM/Opus (如 160kbps) 对比 M4A/AAC (如 128kbps) 的微弱码率优势，而不影响宏观的语言偏好顺序
    affinity_bonus = 0
    if ctx.prefer_ext == "mp4":
        if ext in {"m4a", "aac"} or "mp4a" in acodec or "aac" in acodec:
            affinity_bonus = 2000

    # 综合判断是否为原音 (yt-dlp 经常将 original 放在 format_note 中)
    is_orig = (
        ttype == "original"
        or "original" in format_note
        or "default" in format_note
        or lang in {"orig", "original"}
    )

    # 配音类型加权：确保在同一语言偏好等级下，原音 > 人工配音 > AI配音
    dub_bonus = 0
    if is_orig:
        dub_bonus = 50000
    elif "auto" in format_note or "translated" in format_note or "ai " in format_note:
        dub_bonus = -50000
    elif "dubbed" in format_note or ttype == "dubbed":
        dub_bonus = 10000

    for i, pref in enumerate(ctx.preferred_audio_langs):
        score = _AUDIO_PREF_BASE - i * _AUDIO_PREF_STEP + affinity_bonus + dub_bonus
        p = pref.strip().lower()
        if p == "orig" and is_orig:
            return score + abr
        if _bcp47_match(p, lang):
            return score + abr

    # 无偏好命中
    if is_orig:
        return _AUDIO_ORIG_BONUS + affinity_bonus + dub_bonus + abr
    return affinity_bonus + dub_bonus + abr


def rank_audio_formats(
    rows: list[dict[str, Any]], ctx: ScoringContext
) -> list[tuple[dict[str, Any], int]]:
    """按得分降序排列候选音轨，附带各自得分。**与 `max(rows, key=score)` 完全等价。**

    `sorted()` 是稳定排序，并列时仍然取原始顺序里的第一条 —— 和 `max()` 的语义一样，
    所以拿 `rank_audio_formats(...)[0]` 换掉 `max(...)` 不改变任何选择结果。

    存在的理由是**可观测**：`max()` 只吐出赢家，而"为什么是它赢"必须看到亚军和分差。
    同语言的两条流谁赢由配音加权（原音 +50000 / 人工配音 +10000 / AI 配音 −50000）、
    mp4 亲和加分和码率决定，这些在命令行和界面上一律看不见 —— 用户能看到的只有
    "怎么给我下了个 AI 配音"。让打分函数自己 emit 是不行的：它在候选集上逐条跑，
    一个多语言视频有十几条音轨，那会是十几行噪音。**排名交给调用点一次记完。**
    """
    return sorted(((r, score_audio_format(r, ctx)) for r in rows), key=lambda pair: -pair[1])


def format_ranking(ranked: list[tuple[dict[str, Any], int]], limit: int = 4) -> list[str]:
    """把排名压成 `format_id:lang:score` 的紧凑串，供 `kind=decision` 事件携带。

    只留前 `limit` 名：赢家和它的直接对手才有解释价值，第八名不重要。
    """
    out = []
    for row, score in ranked[:limit]:
        lang = str(row.get("language") or "-").lower()
        out.append(f"{row.get('format_id')}:{lang}:{score}")
    return out


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
    """对单条视频流评分。**当前全项目没有调用点。**

    原 docstring 写的是"旧接口，内部仍使用"，那句话已经不成立了：真正在挑视频流的
    `_pick_best_video()` / `resolve_global_format()` 都是按 `(height, vbr)` 取 max，
    压根不过这里。所以下面这套简易模式偏好 —— 大幅惩罚 VP9/AV1（避免触发 FFmpeg
    转封装假死）、奖励 H.264 + mp4 —— **实际从未生效过**：简易模式下选到 VP9 再转一遍
    的情况仍会发生（另见 `_emit_format_decision()` 里关于 `prefer_ext` 的那段）。

    保留不删，是因为它记录的是一个明确的意图，接上去是个待定的行为变更（会改变
    既有用户的画质选择结果），不该在纯观测的改动里顺手做掉。
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
    # 多音轨嵌入 → 强制 mkv
    if getattr(ctx, "audio_track_count", 1) > 1:
        return "mkv"

    # 多字幕嵌入 → 强制 mkv
    if ctx.embed_subtitles and ctx.subtitle_lang_count > 1:
        return "mkv"

    naive = choose_lossless_merge_container(vid_ext, aud_ext)

    # 单字幕 + WebM → mkv
    if ctx.embed_subtitles and naive == "webm":
        return "mkv"

    return naive or "mkv"
