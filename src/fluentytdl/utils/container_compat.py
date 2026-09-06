"""
容器兼容性引擎 — 全局唯一权威

所有容器格式决策和字幕兼容性修正必须通过本模块执行。
禁止在其他文件中复制此逻辑。

## 观测

两个 `ensure_*` 函数是**原地改写**：它们会把用户/打分引擎定好的容器悄悄换掉，而换完
之后从命令行上再也看不出原来是什么。"我明明选了 MP4，出来的却是 MKV" 的答案只在这里，
所以它们各收一个可选 `trace`，**只在真的改了值时**落一条 `kind=decision subsystem=container`。

`trace` 传的是对象而不是 `import observability`：`utils/` 是 Foundation 层，反向 import
上层会成环；`trace` 自带 `emit()` 出口，方向天然是对的（同 `utils/clean_logger.py`）。
不传 trace 就完全不记 —— 观测永远是 best-effort，不许影响改写本身（硬规则 5）。
"""

from __future__ import annotations

from typing import Any

# MP4 和 MKV 都支持字幕嵌入，只有 WebM 不支持 SRT/ASS
_SUBTITLE_COMPATIBLE_CONTAINERS = {"mp4", "mkv", "mov", "m4v"}


def _report_rewrite(trace: Any, *, before: str, after: str, reason: str, **fields: Any) -> None:
    """把一次容器改写落成 `kind=decision subsystem=container`。

    **只在值真的变了时候调**（调用方负责）—— "检查过、保持原样"不是决策，记下来只是噪音，
    而且会让 `count(kind=decision subsystem=container)` 不再等于"容器被系统改过几次"。

    `level="DEBUG"`：进文件与 JSONL，不刷控制台。改写本身是**正确行为**（不改会产出
    播不了的文件），不是警告；它只是不可见，而不可见才是要修的那个毛病。

    `_depth=4` 数的是 `emit_event ← trace.emit ← 本函数 ← ensure_* ← 调用点`：日志里的
    `{name}:{function}:{line}` 要指向拼 opts 的那个调用点（四处之一），而不是本模块 ——
    "哪个 ensure_ 改的" 已经由 `reason` 说清了，"哪条装配路径" 才是查不出来的那半。
    """
    if trace is None:
        return
    trace.emit(
        "decision",
        level="DEBUG",
        stage="select",
        _depth=4,
        subsystem="container",
        reason=reason,
        container_before=before or None,
        container=after,
        **fields,
    )


def choose_lossless_merge_container(video_ext: str | None, audio_ext: str | None) -> str | None:
    """无损合并容器推断。

    - mp4+m4a → mp4
    - webm+webm → webm
    - 其他组合 → mkv
    - 信息不足 → None
    """
    v = str(video_ext or "").strip().lower()
    a = str(audio_ext or "").strip().lower()
    if not v or not a:
        return None
    if v == "webm" and a == "webm":
        return "webm"
    if v in {"mp4", "m4v"} and a in {"m4a", "aac", "mp4"}:
        return "mp4"
    return "mkv"


def ensure_subtitle_compatible_container(opts: dict[str, Any], *, trace: Any = None) -> None:
    """确保容器格式兼容字幕嵌入（原地修改 opts）。

    仅当 embedsubtitles=True 时生效：
    - 多语言字幕 + mp4/未指定 → 升级 mkv
    - WebM → mkv
    - 未指定容器 → mkv
    - mp4 单字幕 → 保持（mov_text 单轨可用）
    - mkv/mov → 保持

    Args:
        trace: 可选的 `FlowTrace` / `TaskTrace`，只用于观测（见模块 docstring）。
    """
    if not opts.get("embedsubtitles"):
        return

    fmt = (opts.get("merge_output_format") or "").lower()

    sub_langs = opts.get("subtitleslangs") or []
    if isinstance(sub_langs, list) and len(sub_langs) > 1 and (fmt == "mp4" or not fmt):
        opts["merge_output_format"] = "mkv"
        # 语言条数是这条分支的全部理由，必须一起记：`bcp47.resolve_requested(per_pref_limit=1)`
        # 的默认值就是为了不在这里凭空触发升级，哪天它松了，这个字段是唯一的现场证据。
        _report_rewrite(
            trace,
            before=fmt,
            after="mkv",
            reason="subtitle_multi_lang",
            sub_langs_n=len(sub_langs),
        )
        return

    if fmt in _SUBTITLE_COMPATIBLE_CONTAINERS:
        return
    elif fmt == "webm":
        opts["merge_output_format"] = "mkv"
        _report_rewrite(trace, before=fmt, after="mkv", reason="subtitle_webm_incompatible")
    elif not fmt:
        opts["merge_output_format"] = "mkv"
        _report_rewrite(trace, before=fmt, after="mkv", reason="subtitle_container_unset")


def check_container_codec_compat(
    container: str, vcodec: str | None, acodec: str | None
) -> list[str]:
    """
    检查容器与编解码器的兼容性，返回警告消息列表。
    """
    container = container.lower()
    vcodec = (vcodec or "").lower()
    acodec = (acodec or "").lower()

    warnings = []

    # 纯音频情况
    if vcodec == "none" and acodec != "none":
        return warnings

    if container == "mp4":
        if vcodec.startswith("vp") or vcodec.startswith("av01"):
            warnings.append(
                f"⚠️ {vcodec.upper()} 视频流封装为 MP4 可能需要转码或支持不佳，可能耗时较长。"
            )
        if acodec == "opus":
            warnings.append("⚠️ Opus 音频流封装为 MP4 通常需要重做编码，将触发 FFmpeg 慢速转码。")

    elif container == "webm":
        if vcodec.startswith("avc") or vcodec.startswith("hevc") or vcodec.startswith("h26"):
            warnings.append(f"❌ {vcodec.upper()} 与 WebM 不兼容，强烈建议使用 MP4 或 MKV。")
        if acodec in ("m4a", "aac"):
            warnings.append(f"❌ {acodec.upper()} 与 WebM 不兼容。")

    return warnings


def check_subtitle_container_compat(
    container: str, embed_subtitles: bool, subtitle_lang_count: int
) -> str | None:
    """
    检查显式指定的容器与字幕嵌入的兼容性。
    返回冲突描述字符串，或 None 表示无冲突。
    """
    if not embed_subtitles or subtitle_lang_count == 0:
        return None

    container = container.lower()

    if container == "webm":
        return "WebM 容器不支持嵌入 SRT/ASS 等常用字幕。建议切换至 MKV 或 MP4。"

    if container == "mp4" and subtitle_lang_count > 1:
        return f"MP4 对多语言软字幕（您已选择 {subtitle_lang_count} 种）支持有限。建议切换至 MKV。"

    return None


def ensure_audio_multistream_compatible_container(
    opts: dict[str, Any], audio_track_count: int, *, trace: Any = None
) -> None:
    """确保容器格式支持多音轨（原地修改 opts）。

    Args:
        trace: 可选的 `FlowTrace` / `TaskTrace`，只用于观测（见模块 docstring）。
    """
    if audio_track_count <= 1 and not opts.get("audio_multistreams"):
        return

    fmt = (opts.get("merge_output_format") or "").lower()

    # MKV 始终是最佳选择。
    # 为了保护用户体验，如果 UI 没带特别强烈的指令而发现冲突，或者没指定格式，默认升 MKV
    if fmt == "mp4" or fmt == "webm" or not fmt:
        opts["merge_output_format"] = "mkv"
        _report_rewrite(
            trace,
            before=fmt,
            after="mkv",
            reason="audio_multistream",
            audio_tracks=audio_track_count,
        )


def check_audio_multistream_container_compat(container: str, track_count: int) -> str | None:
    """检查多音轨选项与目标容器的兼容性"""
    if track_count <= 1:
        return None

    container = container.lower()

    if container == "webm":
        return "⚠ WebM 容器对多音轨支持有限，合并过程可能报错。强烈建议切换至 MKV。"
    if container == "mp4":
        return f"⚠ MP4 容器包含多条音轨（已选 {track_count} 种）时，在部分自带播放器中可能无法切换音频或出现异常。\n建议使用 MKV 容器或专业播放器（如 VLC/PotPlayer）。"

    return None
