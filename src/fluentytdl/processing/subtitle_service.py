"""
FluentYTDL 字幕服务层

提供字幕下载和处理的统一服务接口，使用策略模式支持多种字幕下载方案。

**语言解析的观测落点只有一个**：`build_resolution_meta()`。四种 mode
（`exact` / `pattern` / `no_match` / `deferred`）无论走哪条路都要经它构造，所以
`kind=decision subsystem=subtitle` 就在那里 emit。在各个返回点分别写一条的话，
迟早会有第五条路径忘了加 —— 而"字幕怎么没了"恰恰是最需要全覆盖的问题。

唯一的例外是 `NoneStrategy`：它在任何解析发生**之前**就短路了，压根不构造
resolution，所以只能自己记一条 `mode=disabled`。而"字幕功能其实是关的"正是
"字幕怎么没了"的头号答案，缺了这条，日志里就只剩一片什么都没有的沉默。
它不解析任何东西，也就不存在"第五条路径忘了加"的风险。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

from ..core.config_manager import config_manager
from ..models.subtitle_config import (
    SUBTITLE_CONFIG_KEY,
    SUBTITLE_PREFS_KEY,
    SUBTITLE_RESOLUTION_KEY,
    SubtitleConfig,
    SubtitleTypePreference,
)
from ..observability import current_flow, emit_event
from ..utils import bcp47
from .subtitle_manager import (
    SubtitleSourceType,
    SubtitleTrack,
    extract_subtitle_tracks,
    get_subtitle_languages,
)


def filter_tracks_by_type_preference(
    tracks: list[SubtitleTrack],
    preference: SubtitleTypePreference,
) -> list[SubtitleTrack]:
    """根据用户类型偏好过滤字幕轨道"""
    if preference == SubtitleTypePreference.MANUAL_ONLY:
        return [t for t in tracks if t.source_type == SubtitleSourceType.MANUAL]
    elif preference == SubtitleTypePreference.MANUAL_AND_ASR:
        return [t for t in tracks if t.source_type != SubtitleSourceType.AUTO_TRANSLATED]
    else:  # ALL
        return tracks


def deduplicate_by_quality(tracks: list[SubtitleTrack]) -> list[SubtitleTrack]:
    """同一**真实语言代码**只保留质量最高的一条。

    以前这里把 `en-GB` / `en-US` 归并进一个 `en` 桶（`zh` 开头的除外），于是
    `zh-Hans-en-GB` 自成一桶、永远归不到 `zh-Hans` 名下 —— 那个 `startswith("zh")`
    特例正是失效链的一环。

    现在按完整代码分桶：**不同真实代码是不同的字幕，绝不合并**。
    「一个偏好只要一条」的收敛交给 `bcp47.resolve_requested(per_pref_limit=1)` ——
    只有那里才知道用户的偏好是什么。
    """
    best: dict[str, SubtitleTrack] = {}
    for t in tracks:
        code = bcp47.normalize(t.lang_code)
        if code not in best or t.quality_rank < best[code].quality_rank:
            best[code] = t
    return list(best.values())


def resolve_tracks_for_prefs(
    prefs: list[str],
    tracks: list[SubtitleTrack],
    *,
    max_languages: int | None = None,
    trace: Any = None,
) -> tuple[list[SubtitleTrack], list[str]]:
    """把用户偏好解析成视频上真实存在的字幕轨道。

    这是所有策略的公共入口 —— 三个策略以前各写一套精确字符串比较
    (`t.lang_code == self.language`、`lang in available_dict`、硬编码中文阶梯)，
    对真实 YouTube 字幕键**全部失效**。

    Args:
        prefs: 用户偏好，按优先级排列
        tracks: 视频可用轨道（顺序无关，内部按 `quality_rank` 排序）
        max_languages: 语言数上限（`SubtitleConfig.max_languages`），None 表示不限
        trace: 可选的 `FlowTrace` / `TaskTrace`，只用于观测

    Returns:
        `(选中轨道, 一条都没命中的偏好)`
    """
    # 按质量排序后按代码去重：`resolve_requested` 靠调用方给的顺序决胜同紧密度候选，
    # 排在前面的（人工字幕）因此胜过自动生成/自动翻译。sorted 是稳定的。
    ordered = sorted(tracks, key=lambda t: t.quality_rank)
    by_code = {t.lang_code: t for t in deduplicate_by_quality(ordered)}

    codes, missed = bcp47.resolve_requested(prefs, list(by_code))
    selected = [by_code[c] for c in codes]
    if max_languages is not None and max_languages > 0 and len(selected) > max_languages:
        dropped = [t.lang_code for t in selected[max_languages:]]
        # 被上限砍掉的语言**既不在 `matched` 也不在 `missed`** —— 它明明匹配上了，
        # 只是排在第 N+1 位。不单独记一条，它就是彻底的静默丢失。
        emit_event(
            "decision",
            trace=trace if trace is not None else current_flow(),
            stage="select",
            subsystem="subtitle",
            reason="max_languages_exceeded",
            max_languages=max_languages,
            kept=[t.lang_code for t in selected[:max_languages]],
            dropped=dropped,
        )
        selected = selected[:max_languages]
    return selected, missed


#: 一条 `decision` 事件里最多列出多少个可用字幕键。YouTube 的自动翻译能给出 100+ 个
#: 语言，全量落进事件会让单条事件比整段下载日志还长。
_MAX_AVAILABLE_IN_EVENT = 40

#: `strategy` 字段的取值。见 `build_resolution_meta` 的 Args 说明：它答"谁要求的"，
#: 与答"为什么是这个结果"的 `reason` 正交。
_DEFERRED = "deferred_resolution"  # `workers.py` 拿到完整 info 后重定的
_OVERRIDE = "override_languages"  # 用户在选择对话框里当场点的


def _available_fields(prefs: list[str], available: list[str]) -> dict[str, Any]:
    """`available` 的事件表示：完整计数 + 截断清单 +（截断时）与偏好同族的那几个。

    光截断会自己制造误诊 —— 清单砍到 40 条之后，"这视频压根没有 zh" 和
    "`zh-Hans-en-GB` 排在第 78 位" 在日志里长得一模一样。`available_related`
    （主语种子标签与任一偏好相同的键）正是"我要的语言到底存不存在"的答案，
    所以它不受截断影响。
    """
    fields: dict[str, Any] = {"available_n": len(available)}
    if len(available) <= _MAX_AVAILABLE_IN_EVENT:
        fields["available"] = list(available)
        return fields

    fields["available"] = available[:_MAX_AVAILABLE_IN_EVENT]
    fields["available_truncated"] = True
    wanted = {bcp47.normalize(p).split("-")[0] for p in prefs if p}
    related = [code for code in available if bcp47.normalize(code).split("-")[0] in wanted]
    fields["available_related"] = related[:12]
    return fields


def build_resolution_meta(
    mode: str,
    prefs: list[str],
    *,
    matched: list[str] | None = None,
    missed: list[str] | None = None,
    available: list[str] | None = None,
    reason: str = "",
    patterns: list[str] | None = None,
    type_preference: str = "",
    strategy: str = "",
    trace: Any = None,
) -> dict[str, Any]:
    """构造 `SUBTITLE_RESOLUTION_KEY` 的值 —— "字幕去哪了"的唯一说法来源。

    mode 取值：
    - `exact`：偏好解析到了真实字幕键（正常路径）
    - `pattern`：拿不到 info dict，回落成锚定正则（见 `bcp47.to_sub_langs_pattern`）
    - `no_match`：视频确实没有匹配偏好的字幕 —— 任务照样成功，但必须告知用户
    - `deferred`：策略层没有 info dict，交给 `workers.py` 迟解析

    顺带 emit 一条 `kind=decision subsystem=subtitle`：本函数是四种 mode 的公共
    收口点，接在这里就不存在"某条路径忘了记日志"。

    `reason` / `patterns` / `type_preference` / `strategy` / `trace` **只进事件，不进
    返回的 dict** —— 那个 dict 会一路带进 `ydl_opts` 并被 UI
    （`features._explain_missing`）和产物判定（`observability/artifacts`）读取，
    加键就得同步改消费端。

    Args:
        reason: 更细的成因。`no_match` 有两种完全不同的原因 —— "这视频没有匹配的
            语言" 和 "类型偏好把全部轨道过滤光了"，在日志里必须分得开。
        strategy: 谁做的这个决定。`reason` 答"为什么是这个结果"，`strategy` 答
            "谁要求的" —— 两者正交，别挤进一个字段。它区分的是几件从结果反推不出来
            的事：用户在选择对话框里点的语言（`override_languages`）vs 配置里的默认
            多语言（`multi_language`），以及对话框当场定的 vs `workers.py` 拿到完整
            info 后重定的（`deferred_resolution`）。字幕两阶段决策正是最初那个
            "字幕没下到"的成因，日志里必须能分清是哪一阶段的结论。
        trace: 可选的 `FlowTrace` / `TaskTrace`。策略层跑在 GUI 线程上，那里
            `current_flow()` 恒为 None，不显式传就只剩 `flow=-` 的孤儿事件
            （只进文本日志，不进 flow 的 JSONL）。
    """
    meta = {
        "mode": mode,
        "prefs": list(prefs),
        "matched": list(matched or []),
        "missed": list(missed or []),
        "available": list(available or []),
    }
    emit_event(
        "decision",
        trace=trace if trace is not None else current_flow(),
        # 用户要了字幕却一条都没匹配上，是要冒到控制台的；正常命中只进文件与 JSONL
        # （播放列表逐行解析会一行一条，刷控制台没有意义）。
        level="WARNING" if mode in ("no_match", "pattern") else "DEBUG",
        stage="select",
        subsystem="subtitle",
        mode=mode,
        prefs=meta["prefs"],
        matched=meta["matched"],
        missed=meta["missed"],
        reason=reason or None,
        strategy=strategy or None,
        patterns=patterns or None,
        type_preference=type_preference or None,
        **_available_fields(meta["prefs"], meta["available"]),
    )
    return meta


def build_subtitle_opts_from_tracks(
    selected_tracks: list[SubtitleTrack],
    extra_langs: list[str] | None = None,
    *,
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据选中的轨道精确构建 yt-dlp 选项。

    Args:
        selected_tracks: 已解析到的真实轨道
        extra_langs: 追加的 `--sub-langs` 条目（回落正则用），不对应任何具体轨道
        resolution: `build_resolution_meta()` 的产物，附在私有键上带给下游
    """
    has_manual = any(t.source_type == SubtitleSourceType.MANUAL for t in selected_tracks)
    has_auto = any(t.source_type != SubtitleSourceType.MANUAL for t in selected_tracks)

    # 收集语言代码（保持顺序并去重）
    langs = list(dict.fromkeys(t.lang_code for t in selected_tracks))
    if extra_langs:
        # 回落正则不知道命中的是人工还是自动字幕，两边都得开
        has_manual = True
        has_auto = True
        for pattern in extra_langs:
            if pattern not in langs:
                langs.append(pattern)

    opts: dict[str, Any] = {
        "writesubtitles": has_manual,
        "writeautomaticsub": has_auto,
        "subtitleslangs": langs,
    }
    if resolution is not None:
        opts[SUBTITLE_RESOLUTION_KEY] = resolution
    return opts


def build_no_match_opts(
    prefs: list[str],
    tracks: list[SubtitleTrack],
    missed: list[str],
    *,
    reason: str = "",
    type_preference: str = "",
    strategy: str = "",
    trace: Any = None,
) -> dict[str, Any]:
    """偏好一条都没命中时的返回值 —— 关键是**不再** `return {}`。

    `return {}` 让调用方的 `row_opts.update({})` 变成空操作：字幕被无声无息地关掉，
    日志里一行都没有，用户只看到"未找到字幕文件"。

    这里显式关掉字幕（免得 yt-dlp 按自己的默认去下一堆没人要的语言），同时把
    "要过什么 / 有什么 / 差什么"写进私有键，交给 UI 和日志去解释。

    `tracks` 一律传**类型偏好过滤前**的完整清单：给用户看的那句提示要能说出
    "该视频只有 en-GB、zh-Hans-en-GB"，而过滤后的空列表会把"轨道被你的类型偏好
    排除了"说成"这视频没有字幕" —— 两件完全不同的事，`reason` 负责分开它们。
    """
    available = [t.lang_code for t in tracks]
    return {
        "writesubtitles": False,
        "writeautomaticsub": False,
        "embedsubtitles": False,
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "no_match",
            prefs,
            missed=missed,
            available=available,
            reason=reason or ("no_tracks" if not available else "no_language_match"),
            type_preference=type_preference,
            strategy=strategy,
            trace=trace,
        ),
    }


def build_deferred_opts(
    prefs: list[str],
    config: SubtitleConfig,
    *,
    strategy: str = "",
    trace: Any = None,
) -> dict[str, Any]:
    """info dict 里没有任何字幕信息时，把解析推迟到 `workers.py`。

    播放列表未逐行解析时 info dict 是 flat 的，压根没有 `subtitles` /
    `automatic_captions` 字段 —— 这跟"视频确实没有这些语言"是两件事，不能当
    `no_match` 处理。此时只声明意图，`workers.py` 拿到完整 info 之后再解析。
    """
    opts = build_embed_opts(config, trace=trace)
    # `apply_subtitle_delivery` 只管人工字幕（`writesubtitles`），自动字幕得单独开 ——
    # 少了这一行，推迟解析的行永远拿不到自动生成字幕，而 YouTube 上
    # 6 Minute English 这类视频的中文字幕**只有**自动翻译版本。
    #
    # 但**第四态（不嵌入也不另存）不能被这一行推翻**：那时交付层已经显式把两个 write
    # 旗标关掉了，这里再无条件写回去就等于「用户说不要字幕，我们照样下自动字幕」。
    if opts.get("writesubtitles"):
        opts["writeautomaticsub"] = config.enable_auto_captions
        opts.update(declare_subtitle_intent(prefs, config, strategy=strategy, trace=trace))
    return opts


def declare_subtitle_intent(
    prefs: list[str],
    config: SubtitleConfig,
    *,
    strategy: str = "",
    trace: Any = None,
) -> dict[str, Any]:
    """只声明"用户想要哪些语言"，**不碰任何 write / auto / embed 旗标**。

    生产端（配置窗口、快速下载）以前把偏好原样写进 `subtitleslangs`，而
    `--sub-langs` 的每一项被 yt-dlp 当成**锚定正则** —— 裸 `en` 匹配不到真实键
    `en-GB`，一个 `.vtt` 都不会写出来。改为声明意图后，由 `workers.py` 在拿到完整
    info dict 之后调用 `resolve_deferred_langs()` 换成真实字幕键。

    只返回三个私有键是刻意的：每个调用点都有自己的嵌入决策（纯字幕模式就明确要
    `embedsubtitles=False` + `skip_download=True`），这里多设一个旗标就会把它踩掉。

    `SUBTITLE_CONFIG_KEY` 必须一起带上：`writeautomaticsub` 是个布尔，表达不了
    `type_preference` —— 而默认的 `MANUAL_AND_ASR` 排除自动翻译，正是本次失效的
    直接原因。少了它，未解析的行会下到用户设置明确排除的轨道。
    """
    return {
        SUBTITLE_PREFS_KEY: list(prefs),
        SUBTITLE_CONFIG_KEY: config.to_dict(),
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "deferred",
            prefs,
            type_preference=config.type_preference.value,
            strategy=strategy,
            trace=trace,
        ),
    }


def build_pattern_opts(
    prefs: list[str],
    config: SubtitleConfig,
    *,
    strategy: str = "",
    trace: Any = None,
) -> dict[str, Any]:
    """拿不到 info dict 时的回落：把偏好编译成锚定正则交给 yt-dlp 自己匹配。

    这是"绝不静默关掉字幕"的兜底 —— `en(-.+)?` 能命中 `en-GB` / `en-en-GB`，
    而不像 `en.*` 那样连 `eng` / `enm` 一起收（见 `bcp47.to_sub_langs_pattern`）。

    代价是正则模式分不清人工 / 自动生成 / 自动翻译，`MANUAL_AND_ASR` 对自动翻译的
    排除在这条路上无法执行 —— 宁可多下一条也不要一条都没有。`MANUAL_ONLY` 例外：
    那是明确的"只要人工字幕"，用 `--write-auto-subs` 的有无就能表达。
    """
    patterns = [bcp47.to_sub_langs_pattern(p) for p in prefs]
    opts: dict[str, Any] = {
        "writesubtitles": True,
        "subtitleslangs": patterns,
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "pattern",
            prefs,
            missed=list(prefs),
            reason="no_info_dict",
            patterns=patterns,
            type_preference=config.type_preference.value,
            strategy=strategy,
            trace=trace,
        ),
    }
    if config.type_preference == SubtitleTypePreference.MANUAL_ONLY:
        opts["writeautomaticsub"] = False
    return opts


def resolve_deferred_langs(
    prefs: list[str],
    info: dict[str, Any] | None,
    config: SubtitleConfig,
    *,
    trace: Any = None,
) -> dict[str, Any]:
    """迟解析：把 `declare_subtitle_intent()` 声明的偏好换成真实字幕键。

    **只返回语言相关的键。** 绝不返回 `embedsubtitles` / `convertsubtitles` ——
    那些由生产端决定，而这里已经是下载路径上的最后一站，覆盖回去就会把纯字幕模式
    刻意设的 `embedsubtitles=False` 翻回 True。唯一的例外是 `no_match`：那时确实
    没有字幕可嵌，关掉既正确又与逐行解析过的任务完全一致。

    三种结果：

    - `exact` —— 拿到 info 且命中。走的是策略层同样的两个函数
      (`filter_tracks_by_type_preference` + `resolve_tracks_for_prefs`)，所以
      未解析的行和解析过的行结果一致。
    - `no_match` —— 拿到 info，但视频确实没有能匹配的字幕。
    - `pattern` —— 拿不到 info（或取 info 失败）。回落正则，绝不静默关掉字幕。

    调用方可以依赖这个不变式：**要么 `subtitleslangs` 有值，要么字幕被显式关掉。**
    """
    tracks = extract_subtitle_tracks(info) if info else []
    if not tracks:
        if info:
            # 拿到了 info 却一条轨道都没有 —— 这个视频真的没有字幕
            return build_no_match_opts(
                prefs, [], list(prefs), reason="no_tracks", strategy=_DEFERRED, trace=trace
            )
        return build_pattern_opts(prefs, config, strategy=_DEFERRED, trace=trace)

    # `available` 刻意用**过滤前**的完整清单（策略层用的是过滤后的）：
    # 用户能看到的那句提示要说出"该视频只有 en-GB、zh-Hans-en-GB"，
    # 而报告作者当初正是自己跑 `--list-subs` 才拿到这份清单的。
    all_tracks = tracks
    tracks = filter_tracks_by_type_preference(tracks, config.type_preference)
    if not tracks:
        return build_no_match_opts(
            prefs,
            all_tracks,
            list(prefs),
            reason="type_preference_excluded_all",
            type_preference=config.type_preference.value,
            strategy=_DEFERRED,
            trace=trace,
        )

    selected, missed = resolve_tracks_for_prefs(
        prefs, tracks, max_languages=config.max_languages, trace=trace
    )
    if not selected:
        return build_no_match_opts(
            prefs,
            all_tracks,
            missed,
            reason="no_language_match",
            type_preference=config.type_preference.value,
            strategy=_DEFERRED,
            trace=trace,
        )

    return build_subtitle_opts_from_tracks(
        selected,
        resolution=build_resolution_meta(
            "exact",
            prefs,
            matched=[t.lang_code for t in selected],
            missed=missed,
            available=[t.lang_code for t in all_tracks],
            reason="deferred_resolved",
            type_preference=config.type_preference.value,
            strategy=_DEFERRED,
            trace=trace,
        ),
    )


def apply_subtitle_delivery(
    opts: dict[str, Any],
    config: SubtitleConfig,
    *,
    embed_override: bool | None = None,
    trace: Any = None,
) -> None:
    """把「嵌入 / 另存」两个开关翻译成 ydl_opts —— **全项目唯一一处**。

    旧模型是 `embed_type: soft | external` 这个 XOR，于是每个调用点都得自己写一遍
    `if embed_type == "soft": ... elif == "external": ...`，全项目约 8 处，谁漏一个
    分支谁就静默改变交付行为。收敛到一处之后，四种组合的真值表只有这一份：

    | `embed` | `keep_external` | `writesubtitles` | `embedsubtitles` | `__fluentytdl_keep_subtitle` |
    |---|---|---|---|---|
    | True | False | True | True | False |
    | True | True | True | True | True |
    | False | True | True | False | True |
    | False | False | **False** | False | —（不请求字幕，无从谈保留）|

    第四态是旧模型表达不出来的「这次不要字幕」，所以它必须**显式**关掉两个 write
    旗标：不写就等于沿用 yt-dlp 的默认，而上游可能已经设过 `writeautomaticsub=True`。

    `keepsubtitles` 恒为 True 是刻意的 —— 先让所有字幕都留在 payload 里，「留哪些」
    这个决策交给 Manifest 在后处理之后做（`SubtitleFeature._dispose_external_subtitles`
    读的就是 `__fluentytdl_keep_subtitle`）。yt-dlp 自己在嵌入后删外置文件的话，
    完整性校验和「嵌入其实失败了」的兜底都没有东西可看。

    **`writeautomaticsub` 只在第四态被碰。** 交付路径上它归调用方 —— 自动字幕要不要
    下是轨道解析的结论（`type_preference` / `enable_auto_captions`），不是交付开关的
    结论；这里多设一个就会踩掉纯字幕模式等调用点自己的判断。

    Args:
        opts: 就地修改的 ydl_opts
        config: 字幕配置
        embed_override: 单次覆盖「嵌入」开关（UI 的每任务选择）。**不覆盖另存开关** ——
            那是全局偏好，没有任何 UI 提供逐任务的另存选择。
        trace: 可选的 `FlowTrace` / `TaskTrace`，只用于观测
    """
    embed = config.embed if embed_override is None else embed_override
    keep = config.keep_external

    if not embed and not keep:
        opts["writesubtitles"] = False
        opts["writeautomaticsub"] = False
        opts["embedsubtitles"] = False
        opts.pop("convertsubtitles", None)
    else:
        opts["writesubtitles"] = True  # 具体语言仍由迟解析 / 轨道选择决定
        opts["embedsubtitles"] = embed
        # 注意：不在此处设置 merge_output_format —— MP4 和 MKV 都支持字幕嵌入
        # （FFmpeg 会把 SRT 转成 mov_text），只有 WebM 不支持。容器由格式选择器决定，
        # 仅在必要时（WebM/未指定）由 `SubtitleFeature` 覆盖。
        opts["keepsubtitles"] = True
        opts["__fluentytdl_keep_subtitle"] = keep
        if config.output_format:
            opts["convertsubtitles"] = config.output_format

    # 一条事件顶掉原先进出各一行的 `[SubEmbed]` INFO：两行都是 INFO，播放列表逐行
    # 走一遍就把控制台刷满，而"嵌入判成了什么"这个结论一行就说完了。
    emit_event(
        "decision",
        trace=trace if trace is not None else current_flow(),
        level="DEBUG",
        stage="select",
        subsystem="subtitle_embed",
        embed=embed,
        keep_external=keep,
        overridden=embed_override is not None,
        write=bool(opts.get("writesubtitles")),
        convert_to=opts.get("convertsubtitles") or None,
    )


def apply_subtitle_embed_choice(
    opts: dict[str, Any],
    config: SubtitleConfig,
    *,
    embed: bool,
    trace: Any = None,
) -> None:
    """把弹窗里那个 **XOR** ComboBox（「软嵌入到视频」/「外置字幕文件」）翻成两个开关。

    `SubtitlePickerDialog` / `PlaylistSubtitleConfigDialog` / 播放列表逐行覆盖都只有
    一个二选一控件（`SubtitlePickerResult.embed_subtitles`、
    `PlaylistSubtitleOverride.embed_subtitles`），表达不出四态。直接把它当成
    `apply_subtitle_delivery(embed_override=False)` 会踩一个坑：全局
    `keep_external=False` 时那正好落进第四态「都不要」，于是用户在弹窗里**明确勾了**
    的字幕会被整个关掉。所以「外置」这一侧必须连带把另存打开 ——
    `keep_external = keep_external or not embed`，两个新态只能由设置页选出来。

    判据仍然只有 `apply_subtitle_delivery()` 一份，这里只做 XOR → 两开关的翻译，
    好让这条知识不必在 3 个 UI 文件里各写一遍。

    调用方仍要自己判断「本任务到底要不要字幕」（`opts.get("writesubtitles")`）——
    `NoneStrategy` 关掉字幕后再进来一次会把它重新打开。
    """
    apply_subtitle_delivery(
        opts,
        replace(config, keep_external=config.keep_external or not embed),
        embed_override=embed,
        trace=trace,
    )


def build_embed_opts(config: SubtitleConfig, *, trace: Any = None) -> dict[str, Any]:
    """`apply_subtitle_delivery()` 的返回值形态包装，供 `opts.update(...)` 的调用点用。

    判据一份都不在这里 —— 见 `apply_subtitle_delivery()`。
    """
    opts: dict[str, Any] = {}
    apply_subtitle_delivery(opts, config, trace=trace)
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

    trace: Any = None
    """可选的 `FlowTrace` / `TaskTrace`，只用于观测。

    策略层跑在 GUI 线程上（配置窗口 / 选择对话框），那里 `current_flow()` 恒为
    None —— 不显式传就只剩 `flow=-` 的孤儿事件，进不了这条 flow 的 JSONL。
    两个对话框都已经持有 `self.trace`，传下来只是一个参数的事。
    """


class SubtitleStrategy(ABC):
    """
    字幕下载策略接口

    不同的策略实现不同的字幕下载方案（单语、多语、智能选择等）。
    """

    strategy_name: str = ""
    """本策略在 `decision` 事件里的 `strategy` 值。

    没有它，四个策略产出的 `mode=exact` 在日志里长得一模一样 —— 而"我在对话框里选了
    三种语言，怎么只下到一种"的答案，往往就是配置里的单语策略压根没被覆盖掉。
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

    strategy_name = "disabled"

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        # 这条不经 `build_resolution_meta`（这里压根没有解析可言），但必须记：
        # "用户报字幕没下到，其实字幕开关是关的"要能一眼看出来，否则日志里
        # 只剩 `subsystem=subtitle` 一条都没有的沉默，和"解析崩了"无从区分。
        emit_event(
            "decision",
            trace=request.trace if request.trace is not None else current_flow(),
            level="DEBUG",
            stage="select",
            subsystem="subtitle",
            mode="disabled",
            reason="subtitles_disabled",
            strategy=self.strategy_name,
        )
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

    strategy_name = "single_language"

    def __init__(self, language: str, enable_auto: bool = True):
        self.language = language
        self.enable_auto = enable_auto

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)
        config = request.user_config or config_manager.get_subtitle_config()
        prefs = [self.language]

        if not tracks:
            return build_deferred_opts(
                prefs, config, strategy=self.strategy_name, trace=request.trace
            )

        # `available` 一律报**过滤前**的完整清单（同 `resolve_deferred_langs`）：
        # 过滤后的空列表会把"你的类型偏好排除了全部轨道"说成"这视频没有字幕"。
        all_tracks = tracks
        tracks = filter_tracks_by_type_preference(tracks, config.type_preference)
        if not tracks:
            return build_no_match_opts(
                prefs,
                all_tracks,
                prefs,
                reason="type_preference_excluded_all",
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            )

        # BCP-47 匹配：偏好 `en` 要能命中真实键 `en-GB`
        selected, missed = resolve_tracks_for_prefs(
            prefs, tracks, max_languages=1, trace=request.trace
        )
        if not selected:
            return build_no_match_opts(
                prefs,
                all_tracks,
                missed,
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            )

        embed_opts = build_embed_opts(config, trace=request.trace)

        opts = build_subtitle_opts_from_tracks(
            selected,
            resolution=build_resolution_meta(
                "exact",
                prefs,
                matched=[t.lang_code for t in selected],
                available=[t.lang_code for t in all_tracks],
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            ),
        )
        # 如果策略强制禁用自动，则覆盖
        if not self.enable_auto:
            opts["writeautomaticsub"] = False

        opts.update(embed_opts)
        return opts

    def get_description(self) -> str:
        return f"单语言字幕: {self.language}"


class MultiLanguageStrategy(SubtitleStrategy):
    """
    多语言字幕策略

    下载多个语言的字幕，按优先级列表顺序尝试。
    """

    strategy_name = "multi_language"

    def __init__(
        self,
        languages: list[str],
        max_languages: int = 10,
        *,
        strategy_name: str | None = None,
    ):
        self.languages = languages
        self.max_languages = max_languages
        # `SubtitleService.apply()` 处理 `override_languages` 时也复用本策略 ——
        # 那是用户在选择对话框里当场点的，和配置里的默认多语言完全是两件事，
        # 日志里必须分得开（"我选了三种语言"到底有没有生效）。
        if strategy_name is not None:
            self.strategy_name = strategy_name

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)
        config = request.user_config or config_manager.get_subtitle_config()

        if not tracks:
            return build_deferred_opts(
                self.languages, config, strategy=self.strategy_name, trace=request.trace
            )

        all_tracks = tracks
        tracks = filter_tracks_by_type_preference(tracks, config.type_preference)
        if not tracks:
            return build_no_match_opts(
                self.languages,
                all_tracks,
                self.languages,
                reason="type_preference_excluded_all",
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            )

        # 按偏好顺序解析真实字幕键（每个偏好只取一条，避免误触发 mp4→mkv 升级）
        selected, missed = resolve_tracks_for_prefs(
            self.languages, tracks, max_languages=self.max_languages, trace=request.trace
        )
        if not selected:
            return build_no_match_opts(
                self.languages,
                all_tracks,
                missed,
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            )

        embed_opts = build_embed_opts(config, trace=request.trace)

        opts = build_subtitle_opts_from_tracks(
            selected,
            resolution=build_resolution_meta(
                "exact",
                self.languages,
                matched=[t.lang_code for t in selected],
                missed=missed,
                available=[t.lang_code for t in all_tracks],
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            ),
        )
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

    strategy_name = "smart"

    def apply(self, request: SubtitleRequest) -> dict[str, Any]:
        tracks = extract_subtitle_tracks(request.video_info)
        config = request.user_config or config_manager.get_subtitle_config()
        prefs = ["zh-Hans", "en", "ja"]

        if not tracks:
            return build_deferred_opts(
                prefs, config, strategy=self.strategy_name, trace=request.trace
            )

        all_tracks = tracks
        tracks = filter_tracks_by_type_preference(tracks, config.type_preference)
        if not tracks:
            # 这里的空是"按类型偏好过滤之后没剩下"，不是"没拿到 info"
            return build_no_match_opts(
                prefs,
                all_tracks,
                prefs,
                reason="type_preference_excluded_all",
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            )

        selected: list[SubtitleTrack] = []

        # 阶梯探测（最多 4 次 `resolve_tracks_for_prefs`）刻意**不传 trace**：
        # `max_languages=1` + 单个偏好意味着它永远触发不了上限截断事件，而中间那几次
        # 空手而归是这个策略的正常工作方式，不是"字幕丢了"。最终决定由下面那条
        # `exact` 事件一次说完。
        # 1. 中文优先（`zh-CN` / `zh-TW` 由 bcp47 别名表覆盖，不必再列）
        for zh_pref in ("zh-Hans", "zh-Hant", "zh"):
            hit, _ = resolve_tracks_for_prefs([zh_pref], tracks, max_languages=1)
            if hit:
                selected.extend(hit)
                break

        # 2. 英语作为第二语言
        if len(selected) < 2:
            hit, _ = resolve_tracks_for_prefs(["en"], tracks, max_languages=1)
            selected.extend(hit)

        # 3. 日语：只有前两步都空手时才用
        if not selected:
            hit, _ = resolve_tracks_for_prefs(["ja"], tracks, max_languages=1)
            selected.extend(hit)

        # 4. 兜底：质量最高的那条可用字幕（此时 tracks 必非空）
        fallback = False
        if not selected:
            selected.append(sorted(tracks, key=lambda t: t.quality_rank)[0])
            fallback = True

        embed_opts = build_embed_opts(config, trace=request.trace)

        opts = build_subtitle_opts_from_tracks(
            selected,
            resolution=build_resolution_meta(
                "exact",
                prefs,
                matched=[t.lang_code for t in selected],
                # `missed` 刻意留空：这三个偏好是**系统默认阶梯**，不是用户点过的
                # 要求。填进去会让"这视频没有日语字幕"被 `expected − actual`
                # 判成降级（硬规则 3：被减数只能是用户实际请求的东西）。
                available=[t.lang_code for t in all_tracks],
                reason="smart_fallback_first_available" if fallback else "smart_ladder",
                type_preference=config.type_preference.value,
                strategy=self.strategy_name,
                trace=request.trace,
            ),
        )
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
        *,
        trace: Any = None,
    ) -> dict[str, Any]:
        """
        应用字幕策略，生成 yt-dlp 选项

        Args:
            video_id: 视频 ID
            video_info: 视频信息
            user_config: 用户临时配置
            override_languages: 临时覆盖语言列表
            trace: 可选的 `FlowTrace` / `TaskTrace`，只用于观测（见 `SubtitleRequest.trace`）

        Returns:
            yt-dlp 选项字典
        """
        request = SubtitleRequest(
            video_id=video_id,
            video_info=video_info,
            user_config=user_config,
            override_languages=override_languages,
            trace=trace,
        )

        # 如果有语言覆盖，使用多语言策略
        if override_languages:
            config = user_config or self.get_config()
            strategy = MultiLanguageStrategy(
                override_languages, config.max_languages, strategy_name=_OVERRIDE
            )
        else:
            strategy = self.resolve_strategy(video_info, user_config)

        return strategy.apply(request)

    def get_available_languages(self, video_info: dict[str, Any]) -> list[dict[str, Any]]:
        """获取视频可用字幕语言列表（用于 UI 显示）。

        把用户偏好一起传下去：排序按偏好命中度，用户常用的语言才会浮到表头。
        """
        return get_subtitle_languages(video_info, self.get_config().default_languages)


# 全局单例
subtitle_service = SubtitleService()
