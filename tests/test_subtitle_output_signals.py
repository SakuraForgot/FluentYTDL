"""字幕失败必须**冒到用户面前**，且清理不能越界。

两条独立的回归：

1. 报告 `FluentYTDL-字幕下载问题排查报告.md` 里用户唯一能看到的线索是一句
   "未找到字幕文件"，而且只在日志里。`SubtitleFeature.on_post_process` 拿到
   `success=False` 时只写了一行 `logger.warning`，UI 侧一个字都没有；至于原因
   （语言没匹配上 / 被限速 / 需要 PO Token）压根没人组织。
   —— 字幕是 best-effort，任务照样成功，但**必须说清为什么没有**。

2. 内嵌模式下 `on_post_process` 会 `os.remove` 掉定位到的每个字幕文件。定位新增的
   "整目录兜底扫描"因此必须限定在任务沙盒内，否则在共享下载目录里会删掉别的视频的
   字幕文件 —— 那是真丢数据，不是提示语问题。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subsig-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.download.features import DownloadContext, SubtitleFeature  # noqa: E402
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
    """

    def __init__(self, output_path: str, *, sandbox_dir: str | None = None) -> None:
        self.url = "https://www.youtube.com/watch?v=MSJMJxd1udk"
        self.output_path = output_path
        self.dest_paths: set[str] = set()
        self.sandbox_dir = sandbox_dir
        self.status_msg = _Signal()


def _context(video: Path, opts: dict, *, sandbox: Path | None = None) -> DownloadContext:
    worker = _FakeWorker(str(video), sandbox_dir=str(sandbox) if sandbox else None)
    return DownloadContext(worker, opts)


def _video(tmp_path: Path, stem: str = "Plain Title") -> Path:
    path = tmp_path / f"{stem}.mkv"
    path.write_text("fake video", encoding="utf-8")
    return path


def _warnings(context: DownloadContext) -> list[str]:
    return [m for m in context.worker.status_msg.messages if "⚠️" in m]


# ── --keep-subs：让后处理有东西可校验 ─────────────────────────


def test_embedding_requests_keep_subs(tmp_path: Path) -> None:
    """`--embed-subs` 嵌入完就删外置字幕，后处理因此永远校验不到东西。

    这也让 `on_post_process` 里那段 `os.remove` 清理从此才是活代码。
    """
    opts = {"embedsubtitles": True, "writesubtitles": True}
    SubtitleFeature().on_download_start(_context(_video(tmp_path), opts))

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
    opts = {"embedsubtitles": False, "writesubtitles": True}
    SubtitleFeature().on_download_start(_context(_video(tmp_path), opts))

    assert "keepsubtitles" not in opts


# ── 失败必须说清原因 ──────────────────────────────────────────


def test_no_match_explains_what_the_video_actually_has(tmp_path: Path) -> None:
    """`no_match` 是"视频确实没有这些语言" —— 得把有什么一起告诉用户。"""
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
    context = _context(_video(tmp_path), opts)

    SubtitleFeature().on_post_process(context)

    warned = _warnings(context)
    assert len(warned) == 1
    assert "未命中" in warned[0]
    assert "ja" in warned[0] and "en-GB" in warned[0]


def test_exact_request_with_no_files_hints_at_throttling(tmp_path: Path) -> None:
    """语言明明解析对了却一个文件都没有 —— 那是限速或 PO Token，不是没匹配上。"""
    opts = {
        "writesubtitles": True,
        "subtitleslangs": ["en-GB"],
        SUBTITLE_RESOLUTION_KEY: build_resolution_meta(
            "exact", ["en"], matched=["en-GB"], available=["en-GB"]
        ),
    }
    context = _context(_video(tmp_path), opts)

    SubtitleFeature().on_post_process(context)

    warned = _warnings(context)
    assert len(warned) == 1
    assert "en-GB" in warned[0]
    assert "PO Token" in warned[0]


def test_warning_even_without_resolution_metadata(tmp_path: Path) -> None:
    """没有解析元数据（迟解析路径、旧任务）也不能沉默。"""
    context = _context(_video(tmp_path), {"writesubtitles": True, "subtitleslangs": ["en-GB"]})

    SubtitleFeature().on_post_process(context)

    assert len(_warnings(context)) == 1


def test_missing_video_does_not_add_a_subtitle_warning(tmp_path: Path) -> None:
    """视频本身没落盘时早有各自的失败提示，再冒字幕警告只会盖住真正的原因。"""
    context = _context(tmp_path / "never-written.mkv", {"writesubtitles": True})

    SubtitleFeature().on_post_process(context)

    assert _warnings(context) == []


def test_success_is_not_warned_about(tmp_path: Path) -> None:
    video = _video(tmp_path)
    (tmp_path / "Plain Title.en-GB.srt").write_text(VALID_SRT, encoding="utf-8")
    context = _context(video, {"writesubtitles": True, "subtitleslangs": ["en-GB"]})

    SubtitleFeature().on_post_process(context)

    assert _warnings(context) == []
    assert any("字幕" in m for m in context.worker.status_msg.messages)


def test_subtitles_disabled_short_circuits(tmp_path: Path) -> None:
    context = _context(_video(tmp_path), {"embedthumbnail": True})

    SubtitleFeature().on_post_process(context)

    assert context.worker.status_msg.messages == []


# ── 清理边界：内嵌后删外置，但绝不越界 ────────────────────────


def test_embed_mode_cleans_up_validated_and_broken_files(tmp_path: Path) -> None:
    """`--keep-subs` 留下的文件校验完就该清掉，坏文件同样是残骸。"""
    video = _video(tmp_path)
    good = tmp_path / "Plain Title.en-GB.srt"
    good.write_text(VALID_SRT, encoding="utf-8")
    broken = tmp_path / "Plain Title.ja.vtt"
    broken.write_text("", encoding="utf-8")

    context = _context(
        video,
        {
            "writesubtitles": True,
            "embedsubtitles": True,
            "keepsubtitles": True,
            "subtitleslangs": ["en-GB", "ja"],
        },
    )
    SubtitleFeature().on_post_process(context)

    assert not good.exists()
    assert not broken.exists()
    assert video.exists()


def test_external_mode_keeps_the_files(tmp_path: Path) -> None:
    video = _video(tmp_path)
    sub = tmp_path / "Plain Title.en-GB.srt"
    sub.write_text(VALID_SRT, encoding="utf-8")

    context = _context(video, {"writesubtitles": True, "subtitleslangs": ["en-GB"]})
    SubtitleFeature().on_post_process(context)

    assert sub.exists()


def test_dir_scan_never_deletes_unrelated_files_outside_sandbox(tmp_path: Path) -> None:
    """**数据安全护栏**：非沙盒目录里不许整目录扫描。

    `on_post_process` 在内嵌模式下会删掉定位到的每个字幕文件。若兜底扫描在共享的
    下载目录里生效，同目录另一个视频的字幕就会被当成本任务的残骸删掉。
    """
    video = _video(tmp_path)
    stranger = tmp_path / "Someone Elses Video.en-GB.srt"
    stranger.write_text(VALID_SRT, encoding="utf-8")

    context = _context(
        video, {"writesubtitles": True, "embedsubtitles": True, "subtitleslangs": ["en-GB"]}
    )
    assert context.is_in_sandbox(str(video)) is False

    SubtitleFeature().on_post_process(context)

    assert stranger.exists(), "非沙盒目录里的他人字幕被误删"
    assert len(_warnings(context)) == 1  # 本任务确实没有字幕，照样得提示


def test_dir_scan_applies_inside_the_task_sandbox(tmp_path: Path) -> None:
    """沙盒每任务独享，所以里面的字幕文件必然属于本任务 —— 兜底扫描在这里安全。"""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    video = _video(sandbox)
    # 名字与视频毫无共同前缀：只有第 4 级能找到（模拟 stdout 丢字符）
    mismatched = sandbox / "name-lost-in-stdout.en-GB.srt"
    mismatched.write_text(VALID_SRT, encoding="utf-8")

    context = _context(
        video,
        {"writesubtitles": True, "embedsubtitles": True, "subtitleslangs": ["en-GB"]},
        sandbox=sandbox,
    )
    assert context.is_in_sandbox(str(video)) is True

    SubtitleFeature().on_post_process(context)

    assert not mismatched.exists()
    assert _warnings(context) == []


@pytest.mark.parametrize("path", ["", None])
def test_is_in_sandbox_handles_empty_input(tmp_path: Path, path) -> None:
    context = _context(_video(tmp_path), {}, sandbox=tmp_path)
    assert context.is_in_sandbox(path) is False
