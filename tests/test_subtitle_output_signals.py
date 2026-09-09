"""字幕失败必须**冒到用户面前**，且清理不能越界。

两条独立的回归：

1. 报告 `FluentYTDL-字幕下载问题排查报告.md` 里用户唯一能看到的线索是一句
   "未找到字幕文件"，而且只在日志里。`SubtitleFeature.on_post_process` 拿到
   `success=False` 时只写了一行 `logger.warning`，UI 侧一个字都没有；至于原因
   （语言没匹配上 / 被限速 / 需要 PO Token）压根没人组织。
   —— 字幕是 best-effort，任务照样成功，但**必须说清为什么没有**。

2. 内嵌模式下 `on_post_process` 会 `os.remove` 掉每一个交到它手上的字幕文件，所以
   "哪些文件是本任务的字幕"这个问题答错就是真丢数据。以前的答案是四级定位，最后一级
   会把**整个目录**的字幕交出来 —— 在共享下载目录里那就是删掉别人视频的字幕。

   《下载产物事务层》之后这个问题只有一个答案：`manifest.kept("subtitle")` ——
   报告者声明过的那些 artifact。而**删除本身也不再是删除**：`_dispose_external_subtitles`
   能做的最狠的事是 `manifest.drop(id=...)`（标成不交付），物理删除只发生在沙盒
   `rmtree`。所以本文件第 2 组用例守的是三件事：

   - 没被声明过的文件**根本不进清单**，无论名字多像、离得多近；
   - 声明过且嵌入成功的字幕被 `drop()` —— 掉出 `kept()`、留在 `observed()`、
     **文件还在盘上**（历史事实不丢，交付集合干净）；
   - 嵌入没成功 / 校验没过的字幕**仍在 `kept()`**，只是 `degraded=True`。
     旧代码在这两种情况下照样 `os.remove`，那是"少删误删"里的误删。

清单是真的：每个用例都建一个真实 `StagingArea`，产物落在它的 `payload/` 里 ——
`add_reported()` 会对 payload 外的路径硬失败（`StagingEscape`），所以拿 `tmp_path`
根目录当产物目录的旧写法本身就已经违反事务层的包含性不变量了。
"""

import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subsig-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.download.features import DownloadContext, SubtitleFeature  # noqa: E402
from fluentytdl.download.staging import (  # noqa: E402
    Kind,
    Manifest,
    StagedArtifact,
    StagingArea,
)
from fluentytdl.models.subtitle_config import SUBTITLE_RESOLUTION_KEY  # noqa: E402
from fluentytdl.processing.subtitle_service import build_resolution_meta  # noqa: E402
from fluentytdl.youtube.yt_dlp_cli import ydl_opts_to_cli_args  # noqa: E402

VALID_SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello\n"


class _Signal:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def emit(self, msg: str) -> None:
        self.messages.append(msg)


class _FakeWorker:
    """`DownloadContext` 只碰 worker 的这几个属性，不需要真的起 Qt。

    刻意不带 `_clean_logger`：`emit_status` 因此走 `status_msg.emit` 分支。

    以前这里还有一个 `dest_paths` —— executor 记下的**混装**集合（视频、分片、字幕、
    封面全在里面）。Feature 链从此只认 `staging.manifest`，那个集合在这里已经没有
    读者了，所以连带删掉：留着它会让人以为它还是一个输入。
    """

    def __init__(self, output_path: str, staging: StagingArea) -> None:
        self.url = "https://www.youtube.com/watch?v=MSJMJxd1udk"
        self.output_path = output_path
        self.staging = staging
        self.status_msg = _Signal()


class _Bench:
    """一次下载事务的测试台。

    产物必须落在 `payload/` 里 —— `StagingArea.add_reported()` 走
    `landing_path()`，payload / `.parts` 之外的路径是 `StagingEscape` 硬失败。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.staging = StagingArea.create(str(tmp_path / "downloads"), "42")
        self.declared: dict[Path, StagedArtifact] = {}

    @property
    def manifest(self) -> Manifest:
        return self.staging.manifest

    @property
    def payload(self) -> Path:
        return Path(self.staging.payload_dir)

    def write(self, name: str, text: str = "x") -> Path:
        path = self.payload / name
        path.write_text(text, encoding="utf-8")
        return path

    def video(self, stem: str = "Plain Title") -> Path:
        return self.write(f"{stem}.mkv", "fake video")

    def declare(
        self,
        path: Path,
        kind: Kind,
        *,
        primary: bool = False,
        opts: dict | None = None,
    ) -> StagedArtifact:
        """「报告者声明过本任务产出了这个文件，它是这个角色」。

        **不声明就不存在** —— 这是字幕清理的全部输入来源，也是本文件第 2 组用例的
        核心：盘上有个同名字幕文件，不等于它是本任务的产物。角色也只在这里判定一次，
        下游不再推断。
        """
        art = self.staging.add_reported(str(path), kind, primary=primary, opts=opts)
        self.declared[path] = art
        return art

    def artifact(self, path: Path) -> StagedArtifact:
        return self.declared[path]

    def context(
        self,
        video: Path,
        opts: dict,
        *,
        declared: Sequence[Path] = (),
        embedded: bool = False,
    ) -> DownloadContext:
        """`declared` 里的路径按字幕登记；`embedded=True` 表示嵌入**确实成功了**。

        嵌入证据是结构化的（`FFmpegEmbedSubtitle` 报了 `finished`），不是"我们请求过
        嵌入所以它应该成了"。少了这一步，`drop()` 就没有合法理由。
        """
        if video.exists():
            self.declare(video, "media", primary=True)
        for path in declared:
            self.declare(path, "subtitle", opts=opts)
        if embedded:
            self.manifest.embed_evidence.add("subtitle")
        return DownloadContext(_FakeWorker(str(video), self.staging), opts)


def _warnings(context: DownloadContext) -> list[str]:
    return [m for m in context.worker.status_msg.messages if "⚠️" in m]


def _kept_subtitle_ids(bench: _Bench) -> set[str]:
    return {art.id for art in bench.manifest.kept("subtitle")}


def _observed_ids(bench: _Bench) -> set[str]:
    return {art.id for art in bench.manifest.observed()}


# ── --keep-subs：让后处理有东西可校验 ─────────────────────────


def test_embedding_requests_keep_subs(tmp_path: Path) -> None:
    """`--embed-subs` 嵌入完就删外置字幕，后处理因此永远校验不到东西。

    这也让 `on_post_process` 里那段清理从此才是活代码。
    """
    bench = _Bench(tmp_path)
    opts = {"embedsubtitles": True, "writesubtitles": True}
    SubtitleFeature().on_download_start(bench.context(bench.video(), opts))

    assert opts["keepsubtitles"] is True
    assert opts["merge_output_format"] == "mkv"  # 原有行为不变


def test_keep_subs_reaches_the_command_line() -> None:
    """置了位还得真的传给 yt-dlp —— 这个 emitter 以前整个不存在。"""
    args = ydl_opts_to_cli_args({"writesubtitles": True, "embedsubtitles": True})
    assert "--embed-subs" in args
    assert "--keep-subs" not in args

    args = ydl_opts_to_cli_args(
        {"writesubtitles": True, "embedsubtitles": True, "keepsubtitles": True}
    )
    assert "--keep-subs" in args


def test_no_keep_subs_when_not_embedding(tmp_path: Path) -> None:
    """外置模式下 yt-dlp 本来就会留文件，不必多传一个开关。"""
    bench = _Bench(tmp_path)
    opts = {"embedsubtitles": False, "writesubtitles": True}
    SubtitleFeature().on_download_start(bench.context(bench.video(), opts))

    assert "keepsubtitles" not in opts


# ── 失败必须说清原因 ──────────────────────────────────────────


def test_no_match_explains_what_the_video_actually_has(tmp_path: Path) -> None:
    """`no_match` 是"视频确实没有这些语言" —— 得把有什么一起告诉用户。"""
    bench = _Bench(tmp_path)
    opts = {
        "writesubtitles": True,
        "subtitleslangs": [],
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "no_match",
            ["ja", "ko"],
            missed=["ja", "ko"],
            available=["en-GB", "en-en-GB", "zh-Hans-en-GB"],
        ),
    }
    context = bench.context(bench.video(), opts)

    SubtitleFeature().on_post_process(context)

    warned = _warnings(context)
    assert len(warned) == 1
    assert "未命中" in warned[0]
    assert "ja" in warned[0] and "en-GB" in warned[0]


def test_exact_request_with_no_files_hints_at_throttling(tmp_path: Path) -> None:
    """语言明明解析对了却一个文件都没有 —— 那是限速或 PO Token，不是没匹配上。"""
    bench = _Bench(tmp_path)
    opts = {
        "writesubtitles": True,
        "subtitleslangs": ["en-GB"],
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "exact", ["en"], matched=["en-GB"], available=["en-GB"]
        ),
    }
    context = bench.context(bench.video(), opts)

    SubtitleFeature().on_post_process(context)

    warned = _warnings(context)
    assert len(warned) == 1
    assert "en-GB" in warned[0]
    assert "PO Token" in warned[0]


def test_warning_even_without_resolution_metadata(tmp_path: Path) -> None:
    """没有解析元数据（迟解析路径、旧任务）也不能沉默。"""
    bench = _Bench(tmp_path)
    context = bench.context(
        bench.video(), {"writesubtitles": True, "subtitleslangs": ["en-GB"]}
    )

    SubtitleFeature().on_post_process(context)

    assert len(_warnings(context)) == 1


def test_missing_video_does_not_add_a_subtitle_warning(tmp_path: Path) -> None:
    """视频本身没落盘时早有各自的失败提示，再冒字幕警告只会盖住真正的原因。"""
    bench = _Bench(tmp_path)
    context = bench.context(bench.payload / "never-written.mkv", {"writesubtitles": True})

    SubtitleFeature().on_post_process(context)

    assert _warnings(context) == []


def test_success_is_not_warned_about(tmp_path: Path) -> None:
    bench = _Bench(tmp_path)
    video = bench.video()
    sub = bench.write("Plain Title.en-GB.srt", VALID_SRT)
    context = bench.context(
        video, {"writesubtitles": True, "subtitleslangs": ["en-GB"]}, declared=[sub]
    )

    SubtitleFeature().on_post_process(context)

    assert _warnings(context) == []
    assert any("字幕" in m for m in context.worker.status_msg.messages)


def test_subtitles_disabled_short_circuits(tmp_path: Path) -> None:
    bench = _Bench(tmp_path)
    context = bench.context(bench.video(), {"embedthumbnail": True})

    SubtitleFeature().on_post_process(context)

    assert context.worker.status_msg.messages == []


# ── 清理边界：内嵌成功后不再交付，但绝不越界、绝不物理删除 ────


def test_embedded_subtitle_leaves_kept_but_stays_on_disk(tmp_path: Path) -> None:
    """嵌入成功 + 用户没要外挂 ⇒ `drop()`。

    三件事同时成立，缺一件就是旧行为：

    - 掉出 `kept()`：commit 不会把它搬到用户目录，容器里已经有了；
    - 留在 `observed()`：`emit_actual` 的判据是"报告过创建"，删了这条记录会把
      "嵌进去了"误报成"没拿到"，进而伪造一个 degraded；
    - **文件还在盘上**：物理删除只发生在沙盒 `rmtree`。Feature 手上没有 `os.remove`。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    good = bench.write("Plain Title.en-GB.srt", VALID_SRT)

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "keepsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
            "subtitleslangs": ["en-GB"],
        },
        declared=[good],
        embedded=True,
    )
    SubtitleFeature().on_post_process(context)

    art = bench.artifact(good)
    assert art.id not in _kept_subtitle_ids(bench)
    assert art.id in _observed_ids(bench)
    assert art.keep is False
    assert art.reason == "embedded_successfully"
    assert good.exists(), "物理删除只应发生在沙盒 rmtree"
    assert bench.artifact(video).id in {a.id for a in bench.manifest.kept("media")}


def test_broken_subtitle_is_degraded_not_destroyed(tmp_path: Path) -> None:
    """校验不过的字幕**保留** + `integrity_failed`，不是当残骸清掉。

    旧代码把它和校验通过的一起 `os.remove` —— 理由是"坏文件同样是残骸"。可"坏"是
    我们自己的判断（`.srt` 里没有 `-->`、读出来是空的），判错就是把用户唯一的那份
    字幕销毁掉，而且销毁的正是排查这次失败所需要的证据。所以现在它照常交付，只是
    带上 `degraded` 标记。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    good = bench.write("Plain Title.en-GB.srt", VALID_SRT)
    broken = bench.write("Plain Title.ja.vtt", "")

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "keepsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
            "subtitleslangs": ["en-GB", "ja"],
        },
        declared=[good, broken],
        embedded=True,
    )
    SubtitleFeature().on_post_process(context)

    bad = bench.artifact(broken)
    assert bad.degraded is True
    assert bad.reason == "integrity_failed"
    assert bad.id in _kept_subtitle_ids(bench), "校验不过不是不交付的理由"
    assert broken.exists()

    # 校验通过的那条照常 drop，坏文件不影响它
    assert bench.artifact(good).id not in _kept_subtitle_ids(bench)

    # 用户得知道有个文件不对劲，但不该冒出"嵌入未完成"——嵌入是成功的
    warned = _warnings(context)
    assert len(warned) == 1
    assert "Plain Title.ja.vtt" in warned[0]


def test_embed_without_evidence_keeps_the_files(tmp_path: Path) -> None:
    """**请求过嵌入 ≠ 嵌入成功了。**

    判据是结构化的后处理证据（`manifest.embed_evidence`），不是 opts 里那个开关、
    也不是 yt-dlp 的最终退出码。ffmpeg 那一步失败时容器里没有字幕轨，此时删掉外置
    文件 = 用户彻底拿不到字幕。所以保留 + `embed_failed_fallback` + 明确告知。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    sub = bench.write("Plain Title.en-GB.srt", VALID_SRT)

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "keepsubtitles": True,
            "subtitleslangs": ["en-GB"],
        },
        declared=[sub],
    )
    SubtitleFeature().on_post_process(context)

    art = bench.artifact(sub)
    assert art.id in _kept_subtitle_ids(bench)
    assert art.keep is True
    assert art.degraded is True
    assert art.reason == "embed_failed_fallback"
    assert sub.exists()

    warned = _warnings(context)
    assert len(warned) == 1
    assert "嵌入未完成" in warned[0]


def test_keep_external_survives_a_successful_embed(tmp_path: Path) -> None:
    """"嵌入"和"另存"是两个独立开关 —— 都要就都给。

    旧的 `embed_type` 是 `soft` XOR `external`，这一格根本表达不出来。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    sub = bench.write("Plain Title.en-GB.srt", VALID_SRT)

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "keepsubtitles": True,
            "__fluentytdl_keep_subtitle": True,
            "subtitleslangs": ["en-GB"],
        },
        declared=[sub],
        embedded=True,
    )
    SubtitleFeature().on_post_process(context)

    art = bench.artifact(sub)
    assert art.id in _kept_subtitle_ids(bench)
    assert art.keep is True
    assert art.degraded is False
    assert _warnings(context) == []


def test_external_mode_keeps_the_files(tmp_path: Path) -> None:
    bench = _Bench(tmp_path)
    video = bench.video()
    sub = bench.write("Plain Title.en-GB.srt", VALID_SRT)

    context = bench.context(
        video, {"writesubtitles": True, "subtitleslangs": ["en-GB"]}, declared=[sub]
    )
    SubtitleFeature().on_post_process(context)

    art = bench.artifact(sub)
    assert art.id in _kept_subtitle_ids(bench)
    assert art.degraded is False
    assert sub.exists()


def test_undeclared_neighbour_never_enters_the_manifest(tmp_path: Path) -> None:
    """**数据安全护栏**：没被声明过的字幕文件不进删除范围。

    四级定位年代第 3/4 级会把整个目录的字幕交出来 —— 共享下载目录里同名视频的字幕
    长得跟本任务的产物一模一样（`<stem>.<lang>.srt`），那就是删掉别人的字幕。

    现在唯一的输入是 `manifest.kept("subtitle")`，所以这条用例守的是：**盘上存在 ≠
    本任务产出**。这里刻意让 `stranger` 与视频同前缀（`Plain Title.` 开头），旧的前缀
    扫描会命中它。护栏比以前厚了一层：它不但不会被删，连清单都进不去 —— 而清单是
    "本任务产出了什么"这个问题的唯一出口。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    stranger = bench.write("Plain Title.zh-Hans.srt", VALID_SRT)

    context = bench.context(
        video, {"writesubtitles": True, "embedsubtitles": True, "subtitleslangs": ["en-GB"]}
    )
    SubtitleFeature().on_post_process(context)

    assert stranger.exists(), "没被声明过的字幕文件被误删"
    assert bench.manifest.kept("subtitle") == [], "没被声明过的文件不该出现在清单里"
    assert len(_warnings(context)) == 1  # 本任务确实没有字幕，照样得提示


def test_declaration_not_name_matching_decides_delivery(tmp_path: Path) -> None:
    """名字对不上也照样按声明处理 —— 判据是「报告者声明过」，不是「名字像」。

    这是上一条的对照面，也是四级定位存在的**唯一**正当理由（stdout 在 Windows 代码页
    下丢字符，落盘名与视频 stem 不同前缀）。现在这个场景由声明直接覆盖：executor 从
    `Writing video subtitles to:` 拿到的就是真实落盘路径，不需要靠名字反推。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    mismatched = bench.write("name-lost-in-stdout.en-GB.srt", VALID_SRT)

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
            "subtitleslangs": ["en-GB"],
        },
        declared=[mismatched],
        embedded=True,
    )
    SubtitleFeature().on_post_process(context)

    art = bench.artifact(mismatched)
    assert art.id not in _kept_subtitle_ids(bench)
    assert art.reason == "embedded_successfully"
    assert _warnings(context) == []


def test_non_subtitle_artifacts_are_never_touched(tmp_path: Path) -> None:
    """封面/主媒体不能被当字幕处理。

    以前这道防线是 `_pick_subtitles()` 的后缀白名单 —— 因为调用方交来的是 `dest_paths`
    那个混装集合，每个消费点都得自己再猜一遍角色。现在角色在进入清单时就判定完了，
    `kept("subtitle")` 里结构上不可能出现封面；后缀白名单退居第二道防线。
    """
    bench = _Bench(tmp_path)
    video = bench.video()
    thumb = bench.write("Plain Title.webp", "fake webp")
    sub = bench.write("Plain Title.en-GB.srt", VALID_SRT)
    bench.declare(thumb, "thumbnail")

    context = bench.context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
            "subtitleslangs": ["en-GB"],
        },
        declared=[sub],
        embedded=True,
    )
    assert context.subtitle_candidates() == [bench.artifact(sub).path]

    SubtitleFeature().on_post_process(context)

    kept_ids = {art.id for art in bench.manifest.kept()}
    assert bench.artifact(thumb).id in kept_ids, "封面被当成字幕处理了"
    assert bench.artifact(video).id in kept_ids, "主媒体被当成字幕处理了"
    assert bench.artifact(sub).id not in kept_ids
    assert thumb.exists() and video.exists()
