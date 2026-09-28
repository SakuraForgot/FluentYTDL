"""
装配预览视图模型（Phase 4）—— 「最终会产出什么」的单一只读快照。

## 为什么需要它

改一个开关，用户看不到「最终会下成什么容器 / 几条音轨 / 字幕怎么处理」——因为配置全散在
transient `ydl_opts` 里，没有一个可绑定的「配方」对象。本模块提供那个对象：
`ResolvedDownloadPlan`，一个**按需计算、不持久化**的 view-model（绝不写进 `VideoTask`）。

## 单一权威 + 从结果反推（不二次判决容器）

关键不变量：**预览说的容器/格式，必须与任务真正装配出的完全一致。** 做法沿用
`youtube.py::_emit_format_decision()` 的套路——不在这里重算容器，而是接过
`_compute_selection_result()` 已经算好的 `{format, extra_opts}`，从 `format` 串反推被选中的流，
从 `extra_opts["merge_output_format"]` / `["audio_format"]` 读最终值。容器决策的唯一权威仍是
`utils/container_compat.resolve_output_container`；本模块只**呈现**它的结论（`ContainerResolution`
provenance 原样带过来），绝不复制那条阶梯——否则「预览算一遍、装配又算一遍」必然分叉
（正是计划里的 R1/R3 风险）。

纯数据、无 Qt、无单例：`build_download_plan()` 只吃已算好的输入，方便表驱动单测。i18n 归口在
`summary_lines()`（`tr_text`，纯函数），渲染交给 Phase 4 的预览控件。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.models.subtitle_config import SubtitleConfig
from fluentytdl.utils.container_compat import ContainerResolution
from fluentytdl.utils.format_scorer import audio_track_kind
from fluentytdl.utils.formatters import format_size
from fluentytdl.utils.ui_text import tr_text

# vcodec/acodec 前缀 → 人读短名。只认前缀，`avc1.640028` 与 `avc1` 同归 H.264。
_VCODEC_LABELS = (
    ("avc1", "H.264"),
    ("h264", "H.264"),
    ("vp9", "VP9"),
    ("vp09", "VP9"),
    ("av01", "AV1"),
    ("av1", "AV1"),
    ("hev1", "HEVC"),
    ("hvc1", "HEVC"),
)
_ACODEC_LABELS = (
    ("mp4a", "AAC"),
    ("aac", "AAC"),
    ("opus", "Opus"),
    ("vorbis", "Vorbis"),
    ("mp3", "MP3"),
    ("flac", "FLAC"),
    ("ac-3", "AC-3"),
    ("ec-3", "E-AC-3"),
)

# 徽标配色沿用流表 `youtube.py::_analyze_format_tags` 的马卡龙色系，保证「装配预览」的芯片与
# 「视频流/音频流」表格视觉一致（AV1 蓝 / VP9 绿 / H.264 灰 / Opus 绿 / AAC 灰）。键是
# `_codec_label()` 归一后的短名。认不出回落 gray。
_CODEC_COLORS = {
    "AV1": "blue",
    "VP9": "green",
    "H.264": "gray",
    "HEVC": "purple",
    "Opus": "green",
    "AAC": "gray",
    "Vorbis": "gray",
    "MP3": "gray",
    "FLAC": "blue",
    "AC-3": "gray",
    "E-AC-3": "gray",
}
# 容器/音频格式 → 芯片色（MKV 紫最醒目：它多半是被硬约束升级来的最终容器）。
_CONTAINER_COLORS = {
    "MKV": "purple",
    "MP4": "blue",
    "WEBM": "green",
    "MOV": "blue",
    "MP3": "gray",
    "M4A": "gray",
    "OPUS": "green",
    "FLAC": "blue",
    "WAV": "gray",
}

# audio_track_kind() 的返回值 → 预览短标签；unknown 留空（不占一格噪音）。
# QT_TRANSLATE_NOOP 只做「在定义处标注、在 `tr_text(变量)` 处翻译」的标记：运行时原样返回
# 中文源串（故表驱动断言不变），但让 lupdate/i18n_audit 认出它们归 RuntimeText 上下文。
_AUDIO_KIND_LABELS = {
    "original": QT_TRANSLATE_NOOP("RuntimeText", "原声"),
    "default": QT_TRANSLATE_NOOP("RuntimeText", "默认音轨"),
    "dub": QT_TRANSLATE_NOOP("RuntimeText", "配音"),
    "descriptive": QT_TRANSLATE_NOOP("RuntimeText", "音频描述"),
}
# audio_track_kind() → 徽标色，沿用流表 `_analyze_format_tags`（原声绿 / 默认蓝 / 描述金）。
_AUDIO_KIND_COLORS = {
    "original": "green",
    "default": "blue",
    "dub": "gray",
    "descriptive": "gold",
}


def _codec_label(codec: str | None, table: tuple[tuple[str, str], ...]) -> str:
    """把 `avc1.640028` / `mp4a.40.2` 归一成 `H.264` / `AAC`；认不出就回原值上半段。"""
    c = str(codec or "").strip().lower()
    if not c or c == "none":
        return ""
    for prefix, label in table:
        if c.startswith(prefix):
            return label
    return c.split(".")[0].upper()


# 分辨率清晰度分档 → 徽标色（4K 金 / 2K 紫 / 1080 蓝 / 720 绿 / 更低 灰），呼应流表马卡龙色系，
# 让「分辨率即画质」在预览里一眼可读。height<=0（未知）→ None，交回 detail() 明文兜底。
def _resolution_badge(height: int) -> tuple[str, str] | None:
    if height <= 0:
        return None
    if height >= 2160:
        color = "gold"
    elif height >= 1440:
        color = "purple"
    elif height >= 1080:
        color = "blue"
    elif height >= 720:
        color = "green"
    else:
        color = "gray"
    return f"{height}p", color


# 音频码率质量分档 → 徽标色（≥256k 金 / ≥128k 蓝 / 更低 灰）。0/缺失 → None，不出徽标。
def _bitrate_badge(abr: int) -> tuple[str, str] | None:
    if abr <= 0:
        return None
    if abr >= 256:
        color = "gold"
    elif abr >= 128:
        color = "blue"
    else:
        color = "gray"
    return f"{abr}k", color


# ── 流摘要 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class StreamSummary:
    """被选中的视频流（或整合流）的展示摘要。"""

    kind: str  # "video" | "muxed"
    height: int
    ext: str
    vcodec: str
    filesize: int | None = None

    def describe(self) -> str:
        parts = [f"{self.height}p" if self.height else tr_text("未知分辨率")]
        label = _codec_label(self.vcodec, _VCODEC_LABELS)
        if label:
            parts.append(label)
        if self.ext:
            parts.append(self.ext.upper())
        line = " · ".join(parts)
        size = format_size(self.filesize, zero="")
        return f"{line} · {size}" if size else line

    def badges(self) -> tuple[tuple[str, str], ...]:
        """分辨率（按清晰度分档配色）+ 编码短名，各一枚彩色徽标；与流表同色系。"""
        out: list[tuple[str, str]] = []
        res = _resolution_badge(self.height)
        if res:
            out.append(res)
        label = _codec_label(self.vcodec, _VCODEC_LABELS)
        if label:
            out.append((label, _CODEC_COLORS.get(label, "gray")))
        return tuple(out)

    def detail(self) -> str:
        """徽标之外的明文：容器 · 大小（分辨率/编码已归徽标，不重复）；分辨率未知才兜底明文。"""
        parts: list[str] = []
        if not self.height:
            parts.append(tr_text("未知分辨率"))
        if self.ext:
            parts.append(self.ext.upper())
        size = format_size(self.filesize, zero="")
        if size:
            parts.append(size)
        return " · ".join(parts)


@dataclass(frozen=True)
class AudioSummary:
    """被选中的单条音频流的展示摘要。"""

    language: str | None
    kind: str  # audio_track_kind() 的返回值
    abr: int
    ext: str
    acodec: str

    def describe(self) -> str:
        parts: list[str] = []
        if self.language:
            parts.append(self.language)
        kind_label = _AUDIO_KIND_LABELS.get(self.kind)
        if kind_label:
            parts.append(tr_text(kind_label))
        codec = _codec_label(self.acodec, _ACODEC_LABELS) or self.ext.upper()
        if self.abr:
            parts.append(f"{self.abr}k")
        if codec:
            parts.append(codec)
        return " · ".join(parts) if parts else tr_text("音频")

    def badges(self) -> tuple[tuple[str, str], ...]:
        """语言 · 音轨类型（原声/配音…）· 码率（按质量分档）· 编码，逐项一枚彩色徽标；与流表同
        色系。语言/码率此前是明文，现在也升为徽标（用户「音频语言、质量都要徽章优雅展示」）。"""
        out: list[tuple[str, str]] = []
        if self.language:
            # 语言短码用中性灰：它是身份标识而非质量信号（原声/配音的类型另有徽标）。
            out.append((self.language.upper(), "gray"))
        kind_label = _AUDIO_KIND_LABELS.get(self.kind)
        if kind_label:
            out.append((tr_text(kind_label), _AUDIO_KIND_COLORS.get(self.kind, "gray")))
        bitrate = _bitrate_badge(self.abr)
        if bitrate:
            out.append(bitrate)
        codec = _codec_label(self.acodec, _ACODEC_LABELS)
        if codec:
            out.append((codec, _CODEC_COLORS.get(codec, "gray")))
        return tuple(out)

    def detail(self) -> str:
        """语言/码率/类型/编码已全部归徽标，明文无剩余内容。"""
        return ""


# ── 字幕计划 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SubtitlePlan:
    """字幕最终会被怎么处理：嵌入 / 外挂 / 都不要，及语言与目标格式。

    `embed` 与 `keep_external` **正交**（见 `SubtitleConfig` 那张四态表）：可以都开
    （既嵌入又另存），也可以都关（这次不要字幕）。预览要如实反映这四态。
    """

    enabled: bool
    embed: bool
    keep_external: bool
    languages: tuple[str, ...]
    output_format: str

    @classmethod
    def from_config(cls, cfg: SubtitleConfig) -> SubtitlePlan:
        """从全局 `SubtitleConfig` 折出计划。语言按 `max_languages` 截断（预览也是这个上限）。"""
        langs = tuple(cfg.default_languages[: max(cfg.max_languages, 0)]) if cfg.enabled else ()
        return cls(
            enabled=cfg.enabled,
            embed=cfg.enabled and cfg.embed,
            keep_external=cfg.enabled and cfg.keep_external,
            languages=langs,
            output_format=cfg.output_format,
        )

    @property
    def active(self) -> bool:
        """真的会产出字幕吗（嵌入或外挂至少一个）。"""
        return self.enabled and (self.embed or self.keep_external)

    def describe(self) -> str:
        if not self.active:
            return tr_text("不下载")
        modes: list[str] = []
        if self.embed:
            modes.append(tr_text("内嵌"))
        if self.keep_external:
            modes.append(tr_text("外挂文件"))
        parts = [" + ".join(modes)]
        if self.languages:
            parts.append("/".join(self.languages))
        parts.append(self.output_format.upper())
        return " · ".join(parts)

    def badges(self) -> tuple[tuple[str, str], ...]:
        """处理方式 → 徽标：内嵌蓝、外挂灰；不下载则无徽标（明文兜底）。"""
        if not self.active:
            return ()
        out: list[tuple[str, str]] = []
        if self.embed:
            out.append((tr_text("内嵌"), "blue"))
        if self.keep_external:
            out.append((tr_text("外挂文件"), "gray"))
        return tuple(out)

    def detail(self) -> str:
        """徽标之外的明文：语言 · 目标格式；不下载时直接给「不下载」。"""
        if not self.active:
            return tr_text("不下载")
        parts: list[str] = []
        if self.languages:
            parts.append("/".join(self.languages))
        parts.append(self.output_format.upper())
        return " · ".join(parts)


# ── 装配预览：顶层 view-model ─────────────────────────────────

# 从 `_compute_selection_result()` 的 `{format, extra_opts}` 反推出的下载类型。
# 与 `_emit_format_decision()` 的 `route` 同义，供预览分流文案。
KIND_VIDEO_AUDIO = "video_audio"
KIND_AUDIO_ONLY = "audio_only"
KIND_VIDEO_ONLY = "video_only"
KIND_MUXED = "muxed"
KIND_FALLBACK = "fallback"

_KIND_LABELS = {
    KIND_VIDEO_AUDIO: QT_TRANSLATE_NOOP("RuntimeText", "视频 + 音频"),
    KIND_AUDIO_ONLY: QT_TRANSLATE_NOOP("RuntimeText", "仅音频"),
    KIND_VIDEO_ONLY: QT_TRANSLATE_NOOP("RuntimeText", "仅视频"),
    KIND_MUXED: QT_TRANSLATE_NOOP("RuntimeText", "整合流"),
    KIND_FALLBACK: QT_TRANSLATE_NOOP("RuntimeText", "自动（yt-dlp 兜底）"),
}

# ContainerResolution.reason（锁定词表）→ 预览里「为什么改了容器」的人读理由。
_REASON_LABELS = {
    "audio_multistream": QT_TRANSLATE_NOOP("RuntimeText", "保留多条音轨"),
    "subtitle_multi_lang": QT_TRANSLATE_NOOP("RuntimeText", "保留多语言内嵌字幕"),
    "subtitle_webm_incompatible": QT_TRANSLATE_NOOP("RuntimeText", "WebM 不支持内嵌字幕"),
    "subtitle_container_unset": QT_TRANSLATE_NOOP("RuntimeText", "字幕嵌入需要明确容器"),
}

# 下载类型 → 徽标色（与流表色系一致，纯装饰）。
_KIND_COLORS = {
    KIND_VIDEO_AUDIO: "blue",
    KIND_AUDIO_ONLY: "green",
    KIND_VIDEO_ONLY: "gray",
    KIND_MUXED: "purple",
    KIND_FALLBACK: "gray",
}

# 封面（缩略图）最终处理方式。与「独立封面/嵌入视频」两个互斥开关一一对应：
# embed→内嵌进容器，external→另存图片文件，none→这次不要封面。标签复用字幕那套
# 内嵌/外挂文件/不下载（RuntimeText 已收录，无新增翻译负担），保证卡内措辞一致。
COVER_NONE = "none"
COVER_EMBED = "embed"
COVER_EXTERNAL = "external"

_COVER_LABELS = {
    COVER_EMBED: QT_TRANSLATE_NOOP("RuntimeText", "内嵌"),
    COVER_EXTERNAL: QT_TRANSLATE_NOOP("RuntimeText", "外挂文件"),
    COVER_NONE: QT_TRANSLATE_NOOP("RuntimeText", "不下载"),
}
_COVER_COLORS = {COVER_EMBED: "blue", COVER_EXTERNAL: "gray", COVER_NONE: "gray"}


@dataclass(frozen=True)
class PreviewItem:
    """预览卡里的一行「配方」条目：类别标签 + 若干彩色徽标 + 徽标后的补充明文。

    渲染交给 Phase 4 的芯片控件（`assembly_preview._PreviewChip`）：`label` 走暗色小字，
    `badges` 逐枚出 `QualityBadge`（`color` 是 badge 的 `color_style` 键），`text` 走正文小字。
    `note=True` 是容器被硬约束改写时的 `↳` 说明行——无类别标签、整行暗色。
    """

    label: str
    badges: tuple[tuple[str, str], ...] = ()
    text: str = ""
    note: bool = False


@dataclass(frozen=True)
class ResolvedDownloadPlan:
    """一次下载「最终会产出什么」的完整只读快照。按需构造，绝不持久化。"""

    download_kind: str
    format_expr: str
    container: str | None
    audio_format: str | None
    resolution: ContainerResolution | None
    video: StreamSummary | None
    audio_tracks: tuple[AudioSummary, ...]
    subtitle: SubtitlePlan
    output_dir: str
    cover: str = COVER_NONE

    @property
    def output_label(self) -> str:
        """最终产物的容器/格式短标签（仅音频取音频格式，其余取容器）。"""
        value = self.audio_format if self.download_kind == KIND_AUDIO_ONLY else self.container
        return (value or "").upper() or tr_text("自动")

    @property
    def override_note(self) -> str | None:
        """若用户显式选的容器被硬约束改掉了，返回一句「为什么」；否则 None。"""
        res = self.resolution
        if not res or not res.overridden:
            return None
        why = _REASON_LABELS.get(res.reason or "")
        before = (res.user_requested or "").upper()
        after = (res.resolved or "").upper()
        head = tr_text("已从 {0} 升级为 {1}", before, after)
        return f"{head}：{tr_text(why)}" if why else head

    def summary_lines(self) -> list[str]:
        """预览控件逐行渲染用。每行 `标签：值`，纯 `tr_text`，无 Qt。"""
        lines = [
            f"{tr_text('类型')}：{tr_text(_KIND_LABELS.get(self.download_kind, self.download_kind))}",
        ]
        if self.download_kind == KIND_AUDIO_ONLY:
            lines.append(f"{tr_text('输出格式')}：{self.output_label}")
        else:
            lines.append(f"{tr_text('输出容器')}：{self.output_label}")
        note = self.override_note
        if note:
            lines.append(f"  ↳ {note}")
        if self.video:
            lines.append(f"{tr_text('视频')}：{self.video.describe()}")
        for track in self.audio_tracks:
            lines.append(f"{tr_text('音频')}：{track.describe()}")
        lines.append(f"{tr_text('字幕')}：{self.subtitle.describe()}")
        lines.append(f"{tr_text('封面')}：{tr_text(_COVER_LABELS.get(self.cover, self.cover))}")
        return lines

    def preview_items(self) -> list[PreviewItem]:
        """结构化配方条目（徽标 + 明文），供芯片控件渲染。逐项与 `summary_lines()` 同源，
        只是把编码/类型/容器等拆成彩色徽标，其余留明文——两者绝不各算一遍。

        刻意不含「保存到」：下载位置已固定在窗口页脚栏（见计划 Phase 4），预览只讲
        「最终产出什么」。封面（内嵌/外挂/不下载）取代它，回答用户的「图片去哪了」。
        """
        kind_label = tr_text(_KIND_LABELS.get(self.download_kind, self.download_kind))
        items = [
            PreviewItem(
                tr_text("类型"),
                ((kind_label, _KIND_COLORS.get(self.download_kind, "gray")),),
            )
        ]
        out_label = self.output_label
        out_color = _CONTAINER_COLORS.get(out_label, "gray")
        items.append(
            PreviewItem(
                tr_text("输出格式")
                if self.download_kind == KIND_AUDIO_ONLY
                else tr_text("输出容器"),
                ((out_label, out_color),),
            )
        )
        note = self.override_note
        if note:
            items.append(PreviewItem("", text=note, note=True))
        if self.video:
            items.append(PreviewItem(tr_text("视频"), self.video.badges(), self.video.detail()))
        for track in self.audio_tracks:
            items.append(PreviewItem(tr_text("音频"), track.badges(), track.detail()))
        sub_badges = self.subtitle.badges()
        if sub_badges:
            items.append(PreviewItem(tr_text("字幕"), sub_badges, self.subtitle.detail()))
        else:
            items.append(PreviewItem(tr_text("字幕"), ((tr_text("不下载"), "gray"),)))
        cover_label = tr_text(_COVER_LABELS.get(self.cover, self.cover))
        items.append(
            PreviewItem(tr_text("封面"), ((cover_label, _COVER_COLORS.get(self.cover, "gray")),))
        )
        return items


# ── 从选择结果装配预览 ────────────────────────────────────────


def _row_filesize(row: dict) -> int | None:
    """行里的字节数（精确优先，回落估算）；0/缺失 → None，交给 `format_size` 显示空。"""
    val = row.get("filesize") or row.get("filesize_approx")
    return int(val) if val else None


def _stream_summary(vid_row: dict | None, mux_row: dict | None) -> StreamSummary | None:
    """从被选中的视频行（或降级整合流行）折出展示摘要；两者都无 → None。"""
    row = vid_row or mux_row
    if not row:
        return None
    return StreamSummary(
        kind="video" if vid_row else "muxed",
        height=int(row.get("height") or 0),
        ext=str(row.get("ext") or ""),
        vcodec=str(row.get("vcodec") or ""),
        filesize=_row_filesize(row),
    )


def _audio_summary(row: dict) -> AudioSummary:
    """把一条被选中的音频行折成展示摘要。类型判定复用 `audio_track_kind()`（唯一权威）。"""
    lang = str(row.get("language") or "").strip()
    return AudioSummary(
        language=lang or None,
        kind=audio_track_kind(row),
        abr=int(row.get("abr") or row.get("tbr") or 0),
        ext=str(row.get("ext") or ""),
        acodec=str(row.get("acodec") or ""),
    )


def _classify_kind(
    result: dict, vid_row: dict | None, aud_rows: list[dict], mux_row: dict | None
) -> str:
    """与 `youtube.py::_decision_route()` 同源的路线判定，收敛到预览的五个 KIND。

    顺序必须与那边一致：`extract_audio` 先判（简易「仅音频」路径的 format 是条音频流，回查会
    命中 `aud_rows`，若不先拦就会被误判成 fallback），再判兜底串，最后按反推出的流归类。
    末尾补一条「只有音频流、无 extract_audio」→ KIND_AUDIO_ONLY：专业模式手选音频流不转码
    （不设 extract_audio），此前会一路落到 fallback；它其实就是仅音频，据此归类。
    """
    if not result:
        return KIND_FALLBACK
    if (result.get("extra_opts") or {}).get("extract_audio"):
        return KIND_AUDIO_ONLY
    if str(result.get("format") or "") in ("best", ""):
        return KIND_FALLBACK
    if vid_row and aud_rows:
        return KIND_VIDEO_AUDIO
    if mux_row:
        return KIND_MUXED
    if vid_row:
        return KIND_VIDEO_ONLY
    if aud_rows:
        return KIND_AUDIO_ONLY
    return KIND_FALLBACK


def build_download_plan(
    *,
    result: dict,
    rows: list[dict],
    resolution: ContainerResolution | None,
    subtitle: SubtitlePlan,
    output_dir: str = "",
    cover: str = COVER_NONE,
) -> ResolvedDownloadPlan:
    """把已算好的 `{format, extra_opts}` + provenance 折成一份只读装配预览。

    **绝不重算容器**：`container` 直接读 `extra_opts["merge_output_format"]`（youtube.py 里
    `resolve_output_container` 已写好的终值），`resolution` 是那次决策的原样 provenance。被选中
    的流从 `format` 串反推 —— 与 `_emit_format_decision()` 完全同一套 `by_id` 回查（`/` 归一成
    `+` 再拆 id），保证「预览 == 实际装配」，不复制容器阶梯（否则必与装配分叉，即计划 R1/R3）。

    纯函数：只吃传入的 `result`/`rows`/`resolution`/`subtitle`/`output_dir`，不碰单例、不碰 Qt，
    方便表驱动单测。
    """
    extra = result.get("extra_opts") or {}
    fmt = str(result.get("format") or "")

    picked = [p for p in fmt.replace("/", "+").split("+") if p]
    by_id = {str(r.get("format_id")): r for r in rows}
    vid_row = next((by_id[p] for p in picked if by_id.get(p, {}).get("kind") == "video"), None)
    aud_rows = [by_id[p] for p in picked if by_id.get(p, {}).get("kind") == "audio"]
    mux_row = next((by_id[p] for p in picked if by_id.get(p, {}).get("kind") == "muxed"), None)

    download_kind = _classify_kind(result, vid_row, aud_rows, mux_row)
    extract_audio = bool(extra.get("extract_audio"))

    # 容器/音频格式取值：合并路径（视频+音频）读 resolver 写回的 `merge_output_format`，
    # 提取音频路径读 `audio_format`。单流路径（专业仅视频 / 仅音频不转码）两者都没有，容器
    # 就是所选流自己的原生 ext——据此回落，避免预览「输出容器/格式」空着显示「自动」。
    # 这只影响预览展示，不改传给 yt-dlp 的 result（守「预览==实际装配」：单流本就不合并，
    # 落地文件的扩展名正是该流的 ext）。
    container = extra.get("merge_output_format")
    audio_format = extra.get("audio_format") if extract_audio else None
    if download_kind == KIND_VIDEO_ONLY and not container and vid_row:
        container = str(vid_row.get("ext") or "") or None
    if download_kind == KIND_AUDIO_ONLY and not extract_audio and not audio_format and aud_rows:
        audio_format = str(aud_rows[0].get("ext") or "") or None

    return ResolvedDownloadPlan(
        download_kind=download_kind,
        format_expr=fmt,
        container=container,
        audio_format=audio_format,
        resolution=resolution,
        video=_stream_summary(vid_row, mux_row),
        audio_tracks=tuple(_audio_summary(r) for r in aud_rows),
        subtitle=subtitle,
        output_dir=output_dir,
        cover=cover,
    )
