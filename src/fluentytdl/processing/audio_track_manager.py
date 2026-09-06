from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..utils.format_scorer import ScoringContext, audio_track_kind, score_audio_format


@dataclass
class AudioTrack:
    format_id: str
    language: str | None
    display_name: str | None
    kind: str  # "original" | "default" | "dub" | "descriptive" | "unknown"，见 audio_track_kind()
    acodec: str | None
    abr: float | None
    ext: str | None
    filesize: int | None
    score: int = 0


def has_multi_language_audio(info: dict[str, Any]) -> bool:
    """快速判断视频是否有多语言音轨 (至少有 2 条不同语言的音轨)"""
    langs = set()
    formats = info.get("formats") or []
    for fmt in formats:
        # 只看纯音频流
        vcodec = fmt.get("vcodec")
        if vcodec != "none" and vcodec is not None:
            continue

        acodec = fmt.get("acodec")
        if acodec == "none" or not acodec:
            continue

        lang = fmt.get("language")
        if lang:
            langs.add(str(lang).lower())

    return len(langs) > 1


def extract_audio_tracks(
    info: dict[str, Any], context: ScoringContext | None = None
) -> list[AudioTrack]:
    """提取所有可用音轨，按 `(类型, 语言, 编码)` 去重（同组保留最好的一条）。

    如有 context 则计算 score 并降序排列。
    """
    formats = info.get("formats") or []
    audio_formats = []

    for fmt in formats:
        vcodec = fmt.get("vcodec")
        if vcodec != "none" and vcodec is not None:
            continue
        acodec = fmt.get("acodec")
        if acodec == "none" or not acodec:
            continue
        audio_formats.append(fmt)

    if not audio_formats:
        return []

    if context is None:
        context = ScoringContext()

    # 去重键含 kind：同语言的原音轨与配音轨必须都留下，否则弹窗里少一条、
    # 而少掉的那条恰恰是用户要手动挑的（YouTube 会给同一语言同时提供原音和配音）。
    tracks_by_kind_lang_codec: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fmt in audio_formats:
        lang = str(fmt.get("language") or "orig")
        codec = str(fmt.get("acodec") or "unknown").split(".")[0].lower()
        kind = audio_track_kind(fmt)
        key = (kind, lang, codec)

        score = score_audio_format(fmt, context)
        fmt["_score"] = score

        # 去重：如果已有同语言同编码，比较 score
        if key not in tracks_by_kind_lang_codec:
            tracks_by_kind_lang_codec[key] = fmt
        else:
            if score > tracks_by_kind_lang_codec[key]["_score"]:
                tracks_by_kind_lang_codec[key] = fmt
            elif score == tracks_by_kind_lang_codec[key]["_score"]:
                # 如果 score 相同，看 abr 谁大
                br1 = fmt.get("abr") or 0
                br2 = tracks_by_kind_lang_codec[key].get("abr") or 0
                if br1 > br2:
                    tracks_by_kind_lang_codec[key] = fmt

    results = []
    for _, fmt in tracks_by_kind_lang_codec.items():
        name = fmt.get("format_note") or fmt.get("format")
        results.append(
            AudioTrack(
                format_id=str(fmt.get("format_id")),
                language=fmt.get("language"),
                display_name=name,
                kind=audio_track_kind(fmt),
                acodec=fmt.get("acodec"),
                abr=fmt.get("abr"),
                ext=fmt.get("ext"),
                filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                score=fmt.get("_score", 0),
            )
        )

    # 按 score 降序排序
    results.sort(key=lambda t: t.score, reverse=True)
    return results


def select_best_n_tracks(
    info: dict[str, Any], n: int, context: ScoringContext | None = None
) -> list[str]:
    """选出最好的前 N 个音轨的 format_id"""
    tracks = extract_audio_tracks(info, context)
    if not tracks:
        return []

    # n <= 0 或 None 表示全部
    if n <= 0 or n >= len(tracks):
        return [t.format_id for t in tracks]

    return [t.format_id for t in tracks[:n]]
