"""产物级「期望 vs 实际」—— `kind=expect` / `kind=actual` 的唯一词汇表。

## 为什么这一层非得存在

用户最初的痛点是"字幕和音轨出了问题查不出来"。而"字幕没下到"这件事在原来的日志里
**根本没有落点**：yt-dlp 少写一个 `.vtt` 不会让 rc 非零，任务照样 `completed`，
`_scan_subtitle_warnings()` 顶多把一句本地化文案推给 UI，一关窗口就没了。
于是日志只剩"✅ 下载并处理完成"，而用户手上没有中文字幕。

这里给那件事一个落点：`select` 阶段把**用户实际请求的产物集合**记下来，`verify`
阶段把**实际拿到的产物集合**记下来，两者相减就是 `missing`。

## 硬规则 3 的唯一合法被减数

`degraded` 只能是 `expected − actual`，**绝不是"系统支持的 − actual"**。所以
`expected_artifacts()` 只读**最终 ydl_opts**（用户勾完、Feature 链改完之后的那份），
不读任何"我们支持什么"的能力集：用户没勾封面而封面不存在，不是降级。

## 词汇表（封闭，加词请连同 `tests/test_observability_artifacts.py` 一起改）

| token | 含义 | 期望来源 | 实际来源 |
|---|---|---|---|
| `media` | 主媒体文件（视频或音频本体） | `not skip_download` | 主输出路径 / 任一非附属文件 |
| `container:<ext>` | 主文件的容器后缀 | `merge_output_format`（仅在确定不会被改写时） | 主输出路径的后缀 |
| `subtitle:<lang>` | 某个具体语言键的字幕 | 迟解析命中的真实键 + 明确没命中的偏好 | 字幕文件名里的语言段 |
| `subtitle:any` | "至少一条字幕" | 正则回落模式（此时指不出具体语言） | 存在任意字幕文件 |
| `thumbnail` | 封面图被取到 | `writethumbnail` | 任一图片文件 |
| `embedded:subtitle` | 字幕轨真的进了容器 | `embedsubtitles` | `manifest.embed_evidence`（结构化 PP 证据） |
| `embedded:thumbnail` | 封面真的进了容器 | `embedthumbnail` | 同上 |
| `delivered:media` | 主媒体最终提交到了用户目录 | 期望 `media` 时恒有 | journal 里 `published` 的成员 |
| `delivered:subtitle:<lang>` | 外挂字幕最终落到用户目录 | 期望该字幕 **∧** `__fluentytdl_keep_subtitle` | 同上 |
| `delivered:thumbnail` | 独立封面最终落到用户目录 | `writethumbnail` **∧** `__fluentytdl_keep_thumbnail` | 同上 |

## 三层事实，三组 token（`embedded:` / `delivered:` 存在的理由）

下面这两个空洞用现有词汇表**都推不出 `missing`**，补法相同 —— 给封闭词汇表加词。

**空洞一：嵌入失败。** 外挂文件被保留、字幕本身不缺，`expected − actual` 是空集，
可用户要的字幕轨并不在容器里。`embedded:*` 让这件事有落点。

**空洞二：`actual` 是历史事实，所以物理丢失也推不出 `missing`。** 判据是"报告过创建"
（见下面「不做存在性检查」），于是文件写出来又消失时 `actual` 里**照样**有它 ——
这不是 bug，那个契约正是"嵌进去了别误报成没拿到"的唯一防线，不能为此破坏。
所以加第二层期望，把**取得**与**交付**分开：

```
observed    创建事实      ← StagedArtifact.origin / producer   → subtitle:ja
present     当前物理事实  ← StagedArtifact.presence
committed   最终交付事实  ← final_path + journal published     → delivered:subtitle:ja
```

字幕写出来又丢了 ⇒ `actual` 有 `subtitle:ja`（历史不丢）、没有
`delivered:subtitle:ja` ⇒ `missing` 非空 ⇒ **`degraded` 合法产生**。而"嵌入成功 +
`keep_external=False`"根本不期望 `delivered:subtitle:*`，所以不会制造假降级 ——
这正是把交付期望挂在 `__fluentytdl_keep_subtitle` 而不是 `writesubtitles` 上的原因。

**`container:*` 刻意不进 delivery**：容器是主文件的属性，不是一个交付物。
`metadata:*` 也刻意不加：基础词汇表里还没有 `metadata`，加交付层就得先加取得层。

### 生命周期约束（硬约束，不只是实施细节）

**`delivered:*` 只在 `commit()` 之后成立，任何提交前的门都不得对它做减法。**
`StagingArea.verify()` 是事务安全门（"现在提交安全吗"，二值），只问
`expected_artifacts()` 里**有没有** `media` 这一个布尔；`emit_actual()` 才是契约评估
（`expected − actual`，全流程只算一次）。倒过来的话每个正常下载都会在自己的门前
"缺" `delivered:media` —— 那是生命周期倒置，不是断言变严。

**`subtitle:any` 不是偷懒。** 正则回落模式（拿不到 info dict 时把 `en` 编译成
`en(-.+)?` 交给 yt-dlp 自己匹配）下，命中的真实键可能是 `en-GB` —— 若按偏好逐条记
`subtitle:en`，实际拿到 `subtitle:en-GB`，集合相减立刻得到一个**假的** `missing`，
而那正是本项目最常见的正常情形。宁可只断言"至少有一条"，也不要制造假降级。

反过来，`found_artifacts()` 在有任意字幕时**同时**记 `subtitle:any` 和
`subtitle:<lang>`，所以实际集合始终是「找到了什么」的纯函数，不依赖期望集合 ——
匹配逻辑不许渗进 `actual` 那一侧，否则 `kind=actual` 就不再是事实记录。

## 刻意不进词汇表的两个

- **音轨语言**：请求得到（`_fytdl_audio_langs`），但**验不了** ——
  确认合并产物里的音轨语言需要对成品跑一次 ffprobe，那是新增行为而不是加日志。
  它作为 `expect` 事件的 `audio_langs` 字段出现（可搜索），但不产 token：
  没有 `actual` 就凭空判 `missing`，等于把"未验证"记成"缺失"。
- **画质高度**：`expect` 侧同理有值（`QualityIntent.target_height`），`actual` 侧的
  产生者 `QualityGuard.post_verify()` 在本项目里**没有任何调用点**（死代码）。
  接上它是补一个缺失的功能，不是加一条日志。

## 不做存在性检查

判据是 yt-dlp **报告过创建**（`on_file_created` 收到的路径），不是"此刻磁盘上还在"。
嵌入字幕 / 嵌入封面成功之后，那两个独立文件按设计就被删掉了 —— 去 stat 一遍会把
"嵌进去了"误报成"没拿到"。附带好处：`verify` 阶段零 I/O。

"此刻还在不在"这件事并没有因此失去落点，它只是搬到了**交付侧**：`delivered:*` 的
实际来源是 journal 里 `published` 的成员，写出来又丢掉的文件不会出现在那里。所以
`actual` 保持"报告过创建"的纯粹语义，物理丢失照样能推出 `missing` —— 两件事各有
各的 token，谁都不用为对方破例。

代价是"文件写出来了但内容是坏的"这一种在这里判为已拿到。那件事有它自己的通道
（`SubtitleProcessor` 的完整性校验 + `kind=signal`），不该由产物集合兼职。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..models.subtitle_config import SUBTITLE_RESOLUTION_KEY
from ..utils.aux_files import IMAGE_EXTS, SUBTITLE_EXTS
from .events import emit_event

# ── 词汇表 ──────────────────────────────────────────────────

MEDIA = "media"
THUMBNAIL = "thumbnail"
SUBTITLE_PREFIX = "subtitle:"
#: "至少一条字幕" —— 正则回落模式下唯一诚实的期望（见模块 docstring）。
SUBTITLE_ANY = "subtitle:any"
CONTAINER_PREFIX = "container:"

#: 嵌入事实层 —— 实际来源是 `manifest.embed_evidence`（结构化 PP 证据），不看磁盘、
#: 也不看 yt-dlp 的最终 rc。"请求过嵌入"和"嵌入成功了"是两件事。
EMBEDDED_PREFIX = "embedded:"
EMBEDDED_SUBTITLE = "embedded:subtitle"
EMBEDDED_THUMBNAIL = "embedded:thumbnail"

#: 交付事实层 —— 实际来源是 journal 里 `published` 的成员。只在 `commit()` 之后成立，
#: 提交前的门不得对它做减法（见模块 docstring 的生命周期约束）。
DELIVERED_PREFIX = "delivered:"
DELIVERED_MEDIA = "delivered:media"
DELIVERED_THUMBNAIL = "delivered:thumbnail"

#: 既不是主媒体也不是附属产物的伴生文件。落进 `media` 会让"主文件到手了"这个判断
#: 被一个 `.info.json` 满足，而那恰恰是下载失败时最容易留下的东西。
_IGNORED_EXTS: frozenset[str] = frozenset(
    {
        ".part",
        ".ytdl",
        ".temp",
        ".tmp",
        ".json",
        ".txt",
        ".xml",
        ".description",
        ".m3u8",
        ".mpd",
        ".url",
        ".lnk",
    }
)

#: 真实字幕键长这样（`zh-Hans-en-GB` / `en_US` / `pt-BR`）。`--sub-langs` 里还可能是
#: 正则（`en(-.+)?`）、`all`、或 `-live_chat` 这样的排除项 —— 那些指不出具体语言。
_CONCRETE_LANG = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: 这些选项会让最终后缀与 `merge_output_format` 不一致，此时**不**断言容器。
#: 宁可少报一个 `container:` token，也不要让"音频提取成 mp3"被记成容器不符。
_EXT_REWRITING_OPTS: tuple[str, ...] = (
    "extractaudio",
    "audioformat",
    "recodevideo",
    "remuxvideo",
    "final_ext",
)

_EXT_REWRITING_POSTPROCESSORS: frozenset[str] = frozenset(
    {"FFmpegExtractAudio", "FFmpegVideoConvertor", "FFmpegVideoRemuxer"}
)


def subtitle_token(lang: str) -> str:
    """`zh-Hans` → `subtitle:zh-Hans`；指不出具体语言时退回 `subtitle:any`。"""
    text = str(lang or "").strip()
    if not text or not _CONCRETE_LANG.match(text) or text.lower() == "all":
        return SUBTITLE_ANY
    return f"{SUBTITLE_PREFIX}{text}"


def container_token(ext: str) -> str:
    """`.MP4` / `mp4` → `container:mp4`。"""
    return f"{CONTAINER_PREFIX}{str(ext or '').strip().lstrip('.').lower()}"


# ── 期望侧 ──────────────────────────────────────────────────


def _expected_container(opts: Mapping[str, Any]) -> str:
    """只在**确定不会被改写**时才给出容器期望，否则返回空串。

    `merge_output_format` 允许写成 `mp4/mkv` 这样的候选链，那时"期望"本身就不是
    单一值；音频提取 / 转码 / remux 又会整体改写后缀。这两种情况下断言容器只会
    制造假不符，所以一律弃权 —— 这个 token 存在的意义是捕捉**意外**的容器变化。
    """
    for key in _EXT_REWRITING_OPTS:
        if opts.get(key):
            return ""
    postprocessors = opts.get("postprocessors")
    if isinstance(postprocessors, (list, tuple)):
        for pp in postprocessors:
            if (
                isinstance(pp, Mapping)
                and str(pp.get("key") or "") in _EXT_REWRITING_POSTPROCESSORS
            ):
                return ""
    merge_fmt = str(opts.get("merge_output_format") or "").strip().lower()
    if not merge_fmt or "/" in merge_fmt or "," in merge_fmt:
        return ""
    return merge_fmt


def _subtitle_langs_from_opts(opts: Mapping[str, Any]) -> list[str]:
    raw = opts.get("subtitleslangs")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        # `-live_chat` 是排除项，不是请求项
        if text and not text.startswith("-"):
            out.append(text)
    return out


def _expected_subtitles(
    opts: Mapping[str, Any],
    resolution: Mapping[str, Any] | None,
) -> set[str]:
    """字幕期望。**以迟解析结果为准，不是以 `writesubtitles` 布尔为准。**

    `no_match`（视频确实没有匹配偏好的字幕）会把 `writesubtitles` 显式关掉 ——
    按布尔判就等于"用户没要字幕"，而那正是必须报 `degraded` 的头号场景：用户勾了
    中文字幕、一个都没拿到。所以先看 `SUBTITLE_RESOLUTION_KEY` 的 mode。
    """
    if resolution is None:
        candidate = opts.get(SUBTITLE_RESOLUTION_KEY)
        resolution = candidate if isinstance(candidate, Mapping) else None

    if resolution is not None:
        mode = str(resolution.get("mode") or "")
        matched = [str(x) for x in (resolution.get("matched") or [])]
        missed = [str(x) for x in (resolution.get("missed") or [])]
        prefs = [str(x) for x in (resolution.get("prefs") or [])]
        if mode == "exact":
            # 命中的按真实键记；没命中的按偏好记 —— 那几条在 select 阶段就已知拿不到，
            # 让它们出现在 verify 的 `missing` 里正是要的效果（"你要的 ja 这视频没有"）。
            return {subtitle_token(x) for x in (*matched, *missed)}
        if mode == "no_match":
            return {subtitle_token(x) for x in (missed or prefs)}
        if mode in ("pattern", "deferred"):
            # 正则回落 / 还没解析：真实键未知，只能断言"至少一条"。
            return {SUBTITLE_ANY} if (prefs or _subtitle_langs_from_opts(opts)) else set()

    if not opts.get("writesubtitles") and not opts.get("writeautomaticsub"):
        return set()
    langs = _subtitle_langs_from_opts(opts)
    if not langs:
        # 开了字幕却没给语言 = yt-dlp 按自己的默认来，指不出具体语言。
        return {SUBTITLE_ANY}
    return {subtitle_token(x) for x in langs}


def expected_artifacts(
    opts: Mapping[str, Any],
    *,
    subtitle_resolution: Mapping[str, Any] | None = None,
) -> set[str]:
    """用户**实际请求**了哪些产物。硬规则 3 唯一合法的被减数。

    Args:
        opts: **最终** ydl_opts —— 用户勾选 + 迟解析 + Feature 链改写之后的那份。
            传改写前的版本会让系统自己做的容器降级（`ensure_subtitle_compatible_container`）
            被记成用户的期望，于是每个这样的任务都假降级。
        subtitle_resolution: `build_resolution_meta()` 的产物；缺省时从
            `opts[SUBTITLE_RESOLUTION_KEY]` 取。
    """
    expected: set[str] = set()
    if not opts.get("skip_download"):
        expected.add(MEDIA)
        # 主媒体一定是要交到用户手上的；`container:*` 刻意不进 delivery（属性不是交付物）。
        expected.add(DELIVERED_MEDIA)
        container = _expected_container(opts)
        if container:
            expected.add(container_token(container))

    subtitles = _expected_subtitles(opts, subtitle_resolution)
    expected |= subtitles
    # 交付期望挂在 `__fluentytdl_keep_subtitle` 而不是 `writesubtitles` 上：嵌入成功且
    # 用户没要外挂时，字幕本来就不该出现在用户目录里，期望它就是制造假降级。
    # 缺失即保留（硬约束 2），所以这里的缺省是 True。
    if subtitles and opts.get("__fluentytdl_keep_subtitle", True):
        expected |= {f"{DELIVERED_PREFIX}{token}" for token in subtitles}

    if opts.get("writethumbnail"):
        expected.add(THUMBNAIL)
        if opts.get("__fluentytdl_keep_thumbnail", True):
            expected.add(DELIVERED_THUMBNAIL)

    if opts.get("embedsubtitles"):
        expected.add(EMBEDDED_SUBTITLE)
    if opts.get("embedthumbnail"):
        expected.add(EMBEDDED_THUMBNAIL)
    return expected


def audio_langs_from_opts(opts: Mapping[str, Any]) -> list[str]:
    """请求的音轨语言，**不产 token**（验不了）。

    读 `_fytdl_audio_langs` —— 与 `yt_dlp_cli._plan_language_injection()` 的**同一个
    输入**。以前这里从 `format_sort` 抠 `lang:xx`，那些条目在 yt-dlp 的排序器里从来
    无效（§4 规则 4），现在也不再生成；两处对不上就会自相矛盾（这边说"请求了日语"，
    那边说"没什么可注入的"）。

    原音不在这里：它是 `_fytdl_audio_strategy`，是策略而不是一个语言请求。
    """
    raw = opts.get("_fytdl_audio_langs")
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    out: list[str] = []
    for item in items:
        code = str(item or "").strip()
        if code and code.lower() not in {"orig", "original"} and code not in out:
            out.append(code)
    return out


def expect_fields(
    opts: Mapping[str, Any],
    *,
    subtitle_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`kind=expect` 的描述性字段 —— 解释 `artifacts` 那个集合是怎么来的。

    只有集合没有理由的话，`missing=["subtitle:ja"]` 读起来像凭空出现的。
    这里补上"用户原本要什么、这视频有什么、解析走的哪条路"。
    """
    if subtitle_resolution is None:
        candidate = opts.get(SUBTITLE_RESOLUTION_KEY)
        subtitle_resolution = candidate if isinstance(candidate, Mapping) else None

    fields: dict[str, Any] = {
        "artifacts": sorted(expected_artifacts(opts, subtitle_resolution=subtitle_resolution)),
        "skip_download": bool(opts.get("skip_download")),
        "container": _expected_container(opts) or None,
        "sub_langs": _subtitle_langs_from_opts(opts),
        "audio_langs": audio_langs_from_opts(opts),
        "thumbnail": bool(opts.get("writethumbnail")),
        "embed_subtitles": bool(opts.get("embedsubtitles")),
        "embed_thumbnail": bool(opts.get("embedthumbnail")),
    }
    if subtitle_resolution is not None:
        # 「字幕去哪了」的唯一说法来源（`build_resolution_meta`）。展平成前缀字段而不是
        # 塞一个嵌套 dict：JSONL 要能直接 `jq 'select(.sub_mode=="no_match")'`。
        fields["sub_mode"] = str(subtitle_resolution.get("mode") or "") or None
        fields["sub_prefs"] = [str(x) for x in (subtitle_resolution.get("prefs") or [])]
        fields["sub_matched"] = [str(x) for x in (subtitle_resolution.get("matched") or [])]
        fields["sub_missed"] = [str(x) for x in (subtitle_resolution.get("missed") or [])]
        fields["sub_available"] = [str(x) for x in (subtitle_resolution.get("available") or [])]

    intent = opts.get("__fluentytdl_quality_intent")
    if isinstance(intent, Mapping):
        # 画质意图只作为字段出现：`actual` 那一侧的产生者是死代码，没有对手的期望
        # 不该进 `artifacts`（见模块 docstring）。
        fields["target_height"] = intent.get("target_height")
        fields["preset_id"] = intent.get("preset_id") or None
        fields["download_type"] = intent.get("download_type") or None
    return fields


# ── 实际侧 ──────────────────────────────────────────────────


def _subtitle_lang_from_name(basename: str) -> str:
    """`Title.zh-Hans-en-GB.vtt` → `zh-Hans-en-GB`；认不出就空串。

    取的是**倒数第二个**点分段：yt-dlp 写字幕时把语言键插在主名和后缀之间。
    主名里本来就有点（`S01.E02.mkv` 的字幕是 `S01.E02.en.vtt`）不影响 ——
    要的始终是紧贴后缀那一段。
    """
    stem = os.path.splitext(basename)[0]
    if "." not in stem:
        return ""
    return stem.rsplit(".", 1)[1]


def found_artifacts(
    paths: Iterable[str],
    *,
    output_path: str = "",
) -> set[str]:
    """把观察到的路径折算成产物集合。**纯函数，不看磁盘、不看期望集合。**

    Args:
        paths: yt-dlp 报告过创建的路径（`on_file_created` 收集的那些）。沙盒路径也行，
            这里只读 basename。
        output_path: 主输出路径。`container:` **只从它推**，不从 `paths` 推 ——
            DASH 的中间流（`Title.f399.mp4` + `Title.f251.webm`）也在 `paths` 里，
            让它们参与容器判定的话，"要 mp4 却合成了 mkv"会被中间流的 `.mp4`
            掩盖成匹配。
    """
    found: set[str] = set()
    for raw in paths:
        name = os.path.basename(str(raw or "").strip())
        if not name:
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in _IGNORED_EXTS:
            continue
        if ext in SUBTITLE_EXTS:
            found.add(SUBTITLE_ANY)
            lang = _subtitle_lang_from_name(name)
            if lang:
                found.add(subtitle_token(lang))
        elif ext in IMAGE_EXTS:
            found.add(THUMBNAIL)
        elif ext:
            found.add(MEDIA)

    main = os.path.basename(str(output_path or "").strip())
    if main:
        ext = os.path.splitext(main)[1].lower()
        if ext and ext not in _IGNORED_EXTS and ext not in SUBTITLE_EXTS and ext not in IMAGE_EXTS:
            found.add(MEDIA)
            found.add(container_token(ext))
    return found


def delivery_tokens(final_paths: Iterable[str]) -> set[str]:
    """已交付的最终路径 → `delivered:*`。喂 `journal` 里 `published` 成员的 `dst`。

    内部**复用 `found_artifacts()`** 再加前缀，而不是另写一遍分支 —— 这样语言键的
    粒度规则（认不出语言时只记 `subtitle:any`）两侧自动一致，不会各自漂移。

    `container:*` 被丢掉：容器是主文件的属性，不是一个交付物，断言"交付了 mp4 容器"
    没有意义（`container:` 该不该匹配已经由取得侧回答过了）。
    """
    out: set[str] = set()
    for token in found_artifacts(final_paths):
        if token.startswith(CONTAINER_PREFIX):
            continue
        out.add(f"{DELIVERED_PREFIX}{token}")
    return out


def embed_tokens(evidence: Iterable[str]) -> set[str]:
    """`manifest.embed_evidence`（`{"subtitle", "thumbnail"}`）→ `embedded:*`。

    判据是结构化的后处理证据，不是 opts 里那个开关、也不是 yt-dlp 的最终退出码 ——
    ffmpeg 那一步失败时容器里没有字幕轨，而请求过嵌入的期望仍然在，于是
    `embedded:subtitle` 进 `missing`，这正是空洞一要的落点。
    """
    out: set[str] = set()
    for item in evidence:
        name = str(item or "").strip()
        if name:
            out.add(f"{EMBEDDED_PREFIX}{name}")
    return out


# ── 出口 ────────────────────────────────────────────────────


def emit_expect(
    opts: Mapping[str, Any],
    *,
    trace: Any = None,
    stage: str = "select",
    subtitle_resolution: Mapping[str, Any] | None = None,
    **common: Any,
) -> set[str]:
    """落一条 `kind=expect` 并把产物集合喂给 trace，返回该集合。

    喂 trace 是关键的一半：`TaskTrace.missing` / `degraded` 是只读派生量，
    终态边界的 `finish()` 直接读它们（硬规则 3、4）。只 emit 不喂，日志里有期望
    而 `outcome` 永远 `degraded=false`。
    """
    try:
        expected = expected_artifacts(opts, subtitle_resolution=subtitle_resolution)
        fields = expect_fields(opts, subtitle_resolution=subtitle_resolution)
    except Exception:
        return set()  # 硬规则 5：观测失败绝不回传业务层

    fn = getattr(trace, "expect_artifacts", None)
    if fn is not None:
        try:
            fn(expected)
        except Exception:
            pass
    fields.update(common)
    emit_event("expect", trace=trace, stage=stage, fields=fields, _depth=2)
    return expected


def emit_actual(
    paths: Iterable[str],
    *,
    trace: Any = None,
    output_path: str = "",
    stage: str = "verify",
    extra_actual: Iterable[str] | None = None,
    **common: Any,
) -> set[str]:
    """落一条 `kind=actual` 并把产物集合喂给 trace，返回该集合。

    不一致用 `matched=false expected=... actual=...` 表达 —— 刻意**不设** `mismatch`
    kind：多一个 kind 就多一处"该发哪个"的判断，而这条事件本来就要把两个集合都带上。

    `missing` 与 `unexpected` 都记，但只有前者进 `degraded`：多拿到东西不是降级
    （硬规则 3 的方向性）。`unexpected` 的用处是反向的 —— "封面明明没勾却下了一张"
    这类问题在这里现形。

    Args:
        paths: 取得侧的事实 —— 喂 `manifest.observed()` 的 **payload 内原名**
            （语言段完整、未经整组改名）。
        extra_actual: 路径推不出来的那两层事实，由调用方组装：
            `delivery_tokens(staging.published_paths())`（交付）
            `| embed_tokens(manifest.embed_evidence)`（嵌入）。
            走参数而不是让 `found_artifacts()` 多接两个入口，是为了保住它的纯路径函数
            契约 —— 交付与嵌入都不是"从文件名看得出来"的事。
    """
    try:
        found = found_artifacts(paths, output_path=output_path)
        if extra_actual:
            found |= {str(t) for t in extra_actual if t}
    except Exception:
        return set()  # 硬规则 5

    fn = getattr(trace, "record_artifacts", None)
    if fn is not None:
        try:
            fn(found)
        except Exception:
            pass

    expected = set(getattr(trace, "expected_artifacts", None) or ())
    missing = sorted(expected - found)
    fields: dict[str, Any] = {
        # 字段形状恒定，`jq 'select(.kind=="actual") | .missing'` 不该时有时无。
        "matched": not missing,
        "expected": sorted(expected),
        "actual": sorted(found),
        "missing": missing,
        "unexpected": sorted(found - expected),
    }
    fields.update(common)
    emit_event(
        "actual",
        trace=trace,
        # 缺产物是"任务成功了但结果不对"，这正是整轮重构要抓的那一类 ——
        # 落 INFO 会被文件 sink 之外的所有地方吞掉。
        level="WARNING" if missing else "INFO",
        stage=stage,
        fields=fields,
        _depth=2,
    )
    return found
