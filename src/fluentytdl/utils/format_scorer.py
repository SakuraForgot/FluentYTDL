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

    audio_strategy: str = "original_first"
    """音轨策略：`original_first` / `language_first` / `original_only`（见 `score_audio_format()`）"""

    allow_descriptive: bool = False
    """是否允许自动选中音频描述轨（False 时给地板分而**不**硬过滤，见 `score_audio_format()`）"""


# ── 音轨类型判定 ──────────────────────────────────────────────

# yt-dlp YouTube extractor 算好的 `language_preference` 取值，
# 见 `extractor/youtube/_video.py::get_language_code_and_preference()`。
# **这是判定音轨类型的唯一权威**：项目原先读的 `audio_track_type` 在 yt-dlp 里
# 根本不存在（`_format_fields` 白名单里没有，extractor 也从不写），恒为 None。
AUDIO_ORIGINAL = 10  # displayName 含 "original"
AUDIO_DEFAULT = 5  # audioIsDefault：账号/地区默认音轨，**不是**原音
AUDIO_DUB = -1  # 普通配音
AUDIO_DESCRIPTIVE = -10  # displayName 含 "descriptive"，语言码带 `-desc` 后缀

KIND_ORIGINAL = "original"
KIND_DEFAULT = "default"
KIND_DUB = "dub"
KIND_DESCRIPTIVE = "descriptive"
KIND_UNKNOWN = "unknown"

# 类型档位加权：同一语言偏好等级内决定谁赢。
# 量级刻意小于 `_AUDIO_PREF_STEP`，`original_first` 靠调换主键顺序而不是靠压过语言分。
_KIND_TIER: dict[str, int] = {
    KIND_ORIGINAL: 3,
    KIND_DEFAULT: 2,
    KIND_DUB: 1,
    KIND_UNKNOWN: 1,
    KIND_DESCRIPTIVE: 0,
}


def audio_track_kind(f: dict[str, Any]) -> str:
    """判定音轨类型，返回 `original` / `default` / `dub` / `descriptive` / `unknown`。

    `language_preference` 有值就以它为准 —— 这是 yt-dlp 已经算好、已经在 `-J` 输出里的
    字段，比任何字符串猜测都准。缺失时（Twitter 等非 YouTube extractor 不写这个字段）
    回落到 `format_note` 子串与 `-desc` 语言码后缀。

    > ⚠️ **不要**把 `(default)` 当原音：它是 `audioIsDefault`（账号/地区默认），上游明确
    > 把这两者分开（`ORIGINAL_LANG_VALUE = 10` vs `DEFAULT_LANG_VALUE = 5`）。旧实现里
    > `"default" in format_note` 那条判定会在一个原音是日语、账号默认是英语配音的视频上
    > 把英语配音报成原音。

    > ⚠️ 回落路径**不认** `"auto" in format_note`：YouTube 的 `format_note` 里唯一含
    > "auto" 的 token 是 `AI-upscaled`，那是**视频**超分标记，不是 AI 配音。也不认
    > `"dubbed"` —— YouTube 从不产出这个词。
    """
    pref = f.get("language_preference")
    if isinstance(pref, bool):
        pref = None  # bool 是 int 的子类，挡掉它免得 True 被当成 1
    if isinstance(pref, int):
        if pref >= AUDIO_ORIGINAL:
            return KIND_ORIGINAL
        if pref >= AUDIO_DEFAULT:
            return KIND_DEFAULT
        if pref <= AUDIO_DESCRIPTIVE:
            return KIND_DESCRIPTIVE
        if pref < 0:
            return KIND_DUB
        return KIND_UNKNOWN

    # 非 YouTube extractor 的回落路径
    lang = str(f.get("language") or "").strip().lower()
    note = str(f.get("format_note") or "").strip().lower()
    if lang.endswith("-desc") or "descriptive" in note:
        return KIND_DESCRIPTIVE
    if "original" in note or lang in {"orig", "original"}:
        return KIND_ORIGINAL
    if "default" in note:
        return KIND_DEFAULT
    return KIND_UNKNOWN


# ── 音频打分 ──────────────────────────────────────────────────

# 分数是两个有序键 + 两个小额修正的位置记数拼装：
#
#     score = primary * _AUDIO_PRIMARY + secondary * _AUDIO_SECONDARY + affinity + abr
#
# 哪个键当 primary 由策略决定（见 `score_audio_format()`）。这样"等差"是精确的：
# 语言偏好每退一位，分数就少一个整的 `_AUDIO_PRIMARY`（或 `_AUDIO_SECONDARY`），
# 第 21 个偏好和第 1 个一样有效，abr 和容器亲和加分永远越不过键的边界。
_AUDIO_PRIMARY = 100_000_000  # 主键单位，远超 abr 数值范围 (0–500 kbps) 与亲和加分
_AUDIO_SECONDARY = 1_000_000  # 次键单位
_AUDIO_ORIGINAL_ONLY_WIN = 10**12  # `original_only` 命中原音时的压倒性加分
_AUDIO_DESC_FLOOR = -(10**12)  # 描述性音轨在 allow_descriptive=False 时的地板分

STRATEGY_ORIGINAL_FIRST = "original_first"
STRATEGY_LANGUAGE_FIRST = "language_first"
STRATEGY_ORIGINAL_ONLY = "original_only"
AUDIO_STRATEGIES = (STRATEGY_ORIGINAL_FIRST, STRATEGY_LANGUAGE_FIRST, STRATEGY_ORIGINAL_ONLY)


def _lang_pref_rank(lang: str, kind: str, prefs: list[str]) -> int:
    """把语言偏好命中转成"越大越好"的名次：命中第 0 项 → `len(prefs)`，未命中 → 0。

    历史遗留：偏好列表里可能还留着 `orig` 这一项（老配置迁移前、或用户手改过
    config.json）。这里把它当"命中原音轨"处理，免得迁移没跑到的场合整份偏好错位。
    新配置里 `orig` 已经拆成独立策略，不再出现在列表中。
    """
    for i, pref in enumerate(prefs):
        p = pref.strip().lower()
        if p in {"orig", "original"}:
            if kind == KIND_ORIGINAL:
                return len(prefs) - i
            continue
        if _bcp47_match(p, lang):
            return len(prefs) - i
    return 0


def score_audio_format(f: dict[str, Any], ctx: ScoringContext) -> int:
    """对单条音频流评分，数值越大越优先。

    排序主键由 `ctx.audio_strategy` 决定：

    | 策略 | 主键 → 次键 |
    | --- | --- |
    | `original_first` | 类型档（原音 > 默认 > 配音）→ 语言偏好 → abr |
    | `language_first` | 语言偏好 → 类型档 → abr |
    | `original_only` | 原音命中即赢；无原音时退化为 `language_first`（"无则回退最佳"） |

    `descriptive` 在 `ctx.allow_descriptive=False` 时拿地板分而**不被硬过滤** ——
    过滤会让"只有描述性音轨"的视频拿不到任何音频。
    """
    lang = str(f.get("language") or "").strip().lower()
    abr = int(f.get("abr") or f.get("tbr") or 0)
    ext = str(f.get("ext") or "").strip().lower()
    acodec = str(f.get("acodec") or "").strip().lower()
    kind = audio_track_kind(f)

    # 容器亲和性补偿：如果目标是 MP4，重赏原生支持的音频流，
    # 足以抵消 WebM/Opus (如 160kbps) 对比 M4A/AAC (如 128kbps) 的微弱码率优势，而不影响宏观的语言偏好顺序
    affinity_bonus = 0
    if ctx.prefer_ext == "mp4":
        if ext in {"m4a", "aac"} or "mp4a" in acodec or "aac" in acodec:
            affinity_bonus = 2000

    if kind == KIND_DESCRIPTIVE and not ctx.allow_descriptive:
        # 地板分：低于任何其它候选，但仍是有限值 —— "只有 desc 轨"时它照样能被选中
        return _AUDIO_DESC_FLOOR + abr

    tier = _KIND_TIER.get(kind, 1)
    lang_rank = _lang_pref_rank(lang, kind, ctx.preferred_audio_langs)
    strategy = (ctx.audio_strategy or STRATEGY_ORIGINAL_FIRST).strip().lower()

    if strategy == STRATEGY_ORIGINAL_FIRST:
        primary, secondary = tier, lang_rank
    else:
        # language_first，以及 original_only（原音靠下面的压倒性加分取胜，
        # 无原音时这里就是它的退化路径）
        primary, secondary = lang_rank, tier

    score = primary * _AUDIO_PRIMARY + secondary * _AUDIO_SECONDARY + affinity_bonus + abr

    if strategy == STRATEGY_ORIGINAL_ONLY and kind == KIND_ORIGINAL:
        score += _AUDIO_ORIGINAL_ONLY_WIN

    return score


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
