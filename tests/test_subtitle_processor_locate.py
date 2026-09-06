"""字幕后处理必须**真的找到**磁盘上的字幕文件。

报告 `FluentYTDL-字幕下载问题排查报告.md` 里那句"未找到字幕文件"有多个来源，
定位逻辑本身就是其中一个：`subtitle_processor._find_subtitle_files()` 只有一招
`parent_dir.glob(f"{stem}.*")`，而 `stem` 直接来自用户数据（视频标题）且**未转义**。

实测（本目录 tmp 复现）：标题 `Children in war zones [4K] Six Minute English` 下，
`glob(stem + ".*")` 命中 **0** 个文件 —— `[4K]` 被当成字符集，连视频自己都匹配不到。
`[4K]` / `[Official Video]` 这类方括号在 YouTube 标题里遍地都是。

这里钉住四级定位各自的命中，外加两条安全边界：
- 第 4 级"整目录扫描"必须能关掉（调用方在内嵌模式下会删掉返回的文件）
- `subtitleslangs` 里的正则/路径片段不能把候选路径带出目标目录
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subloc-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.processing.subtitle_processor import (  # noqa: E402
    LOCATE_DEST_PATHS,
    LOCATE_DIR_SCAN,
    LOCATE_LANG_PATTERN,
    LOCATE_PREFIX_SCAN,
    _is_fragment_tag,
    _is_safe_lang_segment,
    subtitle_processor,
)

# 报告里的真实标题（带方括号与全角字符）—— `?` 不在其中是刻意的：
# yt-dlp 在 Windows 上会把它净化成全角 `？`，而全角问号不是 glob 元字符。
NASTY_STEM = "Children in war zones [4K] ⏲️ 6 Minute English"

VALID_VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello\n"
VALID_SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello\n"

# 有字幕就得走完整流程，两个开关任意一个开着即可
SUB_ENABLED = {"writesubtitles": True}


def _video(tmp_path: Path, stem: str = NASTY_STEM) -> Path:
    path = tmp_path / f"{stem}.mkv"
    path.write_text("fake video", encoding="utf-8")
    return path


def _sub(tmp_path: Path, name: str, content: str = VALID_VTT) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _process(video: Path, **kwargs):
    opts = dict(SUB_ENABLED)
    opts["subtitleslangs"] = kwargs.pop("sub_langs", [])
    return subtitle_processor.process(str(video), opts, **kwargs)


# ── 四级定位 ─────────────────────────────────────────────────


def test_tier1_dest_paths_wins_even_when_names_mismatch(tmp_path: Path) -> None:
    """executor 记下的精确路径最可信 —— 这一级以前完全没被查过。

    故意让字幕名与视频名毫无共同前缀（模拟 yt-dlp stdout 丢失特殊 Unicode 字符
    的情形），只有 `dest_paths` 能救。
    """
    video = _video(tmp_path)
    sub = _sub(tmp_path, "completely-different-name.en-GB.vtt")

    result = _process(video, dest_paths=[str(sub), str(video)])

    assert result.success
    assert result.located_by == LOCATE_DEST_PATHS
    assert result.processed_files == [str(sub)]  # 视频自己不该被当成字幕


def test_tier2_uses_real_lang_code_from_sub_langs(tmp_path: Path) -> None:
    """`<stem>.<lang>.<ext>`：lang 取 `opts["subtitleslangs"]`（阶段 2 之后是真实键）。"""
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")

    result = _process(video, sub_langs=["en-GB", "zh-Hans-en-GB"])

    assert result.located_by == LOCATE_LANG_PATTERN
    assert result.processed_files == [str(sub)]


def test_tier3_prefix_scan_survives_glob_metacharacters(tmp_path: Path) -> None:
    """这条就是旧实现的死因。

    `sub_langs` 给的是回落正则，拼不出真实文件名（第 2 级必然扑空）；而标题里的
    `[4K]` 让旧的 `glob(stem + ".*")` 一个文件都匹配不到。前缀扫描不解释元字符，
    所以照样命中。
    """
    video = _video(tmp_path)
    sub = _sub(tmp_path, f"{NASTY_STEM}.en-GB.vtt")

    result = _process(video, sub_langs=["en(-.+)?", "zh-Hans(-.+)?"])

    assert result.located_by == LOCATE_PREFIX_SCAN
    assert result.processed_files == [str(sub)]
    # 反证：旧实现在同一个目录上确实是 0 命中
    assert list(tmp_path.glob(f"{NASTY_STEM}.*")) == []


def test_tier3_matches_fragment_stem_video(tmp_path: Path) -> None:
    """output_path 仍指向分片文件（`.f136.mkv`）时，字幕名不带分片标记。"""
    video = tmp_path / "Plain Title.f136.mkv"
    video.write_text("fake fragment", encoding="utf-8")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")

    result = _process(video)

    assert result.located_by == LOCATE_PREFIX_SCAN
    assert result.processed_files == [str(sub)]


def test_tier4_dir_scan_is_gated(tmp_path: Path) -> None:
    """整目录扫描默认关闭 —— 调用方在内嵌模式下会删掉这里返回的文件。

    在共享的下载目录里全扫，等于把别人的字幕文件交给删除逻辑，那是真丢数据。
    只有任务沙盒（每任务独享）才允许开。
    """
    video = _video(tmp_path)
    sub = _sub(tmp_path, "unrelated-video.en-GB.vtt")

    blocked = _process(video)
    assert blocked.success is False
    assert blocked.reason == "not_found"

    allowed = _process(video, allow_dir_scan=True)
    assert allowed.located_by == LOCATE_DIR_SCAN
    assert allowed.processed_files == [str(sub)]


def test_lrc_suffix_is_recognized(tmp_path: Path) -> None:
    """后缀白名单以前是另一份字面量 `[".srt", ".ass", ".vtt"]`，`.lrc` 就是那么丢的。"""
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.lrc", "[00:01.00]Hello\n")

    result = _process(video, sub_langs=["en-GB"])

    assert result.processed_files == [str(sub)]


# ── 空结果与坏文件 ────────────────────────────────────────────


def test_missing_subtitles_is_a_failure_with_reason(tmp_path: Path) -> None:
    """一个都没找到时**必须** `success=False`。

    旧实现返回 `success=True, message="未找到字幕文件"`，调用方的 `if result.success`
    直接跳过所有提示，用户只看到"字幕开了但没有字幕"。
    """
    video = _video(tmp_path)

    result = _process(video, sub_langs=["en-GB"])

    assert result.success is False
    assert result.reason == "not_found"
    assert result.processed_files == []


def test_invalid_files_are_reported_separately(tmp_path: Path) -> None:
    """坏文件不算成功产出，但要带着原因回去 —— 内嵌模式下这些残骸也得清掉。"""
    video = _video(tmp_path, "Plain Title")
    good = _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)
    empty = _sub(tmp_path, "Plain Title.ja.vtt", "")
    no_timecode = _sub(tmp_path, "Plain Title.fr.srt", "just some text\n")

    result = _process(video, sub_langs=["en-GB", "ja", "fr"])

    assert result.success
    assert result.processed_files == [str(good)]
    assert {Path(p).name for p, _ in result.invalid_files} == {empty.name, no_timecode.name}


def test_all_invalid_is_a_failure(tmp_path: Path) -> None:
    video = _video(tmp_path, "Plain Title")
    _sub(tmp_path, "Plain Title.en-GB.vtt", "")

    result = _process(video, sub_langs=["en-GB"])

    assert result.success is False
    assert result.reason == "all_invalid"
    assert len(result.invalid_files) == 1


def test_disabled_and_missing_video_are_distinguishable(tmp_path: Path) -> None:
    """两种"没处理"必须能被上层区分：一个是用户没开字幕，一个是视频压根没落盘。"""
    disabled = subtitle_processor.process(str(_video(tmp_path)), {})
    assert disabled.success is True
    assert disabled.reason == "disabled"

    missing = subtitle_processor.process(str(tmp_path / "nope.mkv"), dict(SUB_ENABLED))
    assert missing.success is False
    assert missing.reason == "video_missing"


# ── status_callback ─────────────────────────────────────────


def test_status_callback_reports_success_and_bad_files(tmp_path: Path) -> None:
    """`status_callback` 参数存在已久，但从来没被调用过。"""
    video = _video(tmp_path, "Plain Title")
    _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)
    _sub(tmp_path, "Plain Title.ja.vtt", "")
    seen: list[str] = []

    result = _process(video, sub_langs=["en-GB", "ja"], status_callback=seen.append)

    assert result.success
    assert any("1 个字幕文件" in m for m in seen)
    assert any("Plain Title.ja.vtt" in m for m in seen)


def test_status_callback_exception_does_not_abort_processing(tmp_path: Path) -> None:
    """UI 回调抛异常不该把字幕后处理带走 —— 校验结果照样要返回。"""
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)

    def boom(_msg: str) -> None:
        raise RuntimeError("UI exploded")

    result = _process(video, sub_langs=["en-GB"], status_callback=boom)

    assert result.processed_files == [str(sub)]


# ── 辅助判定 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "safe"),
    [
        ("en-GB", True),
        ("zh-Hans-en-GB", True),
        ("en(-.+)?", True),  # 回落正则：拼出的路径不存在，无害
        ("../../etc/passwd", False),
        ("..\\secrets", False),
        ("sub/dir", False),
        ("..", False),
        ("", False),
    ],
)
def test_lang_segment_safety(lang: str, safe: bool) -> None:
    """拦的是路径分隔符和 `..` —— 它们能把候选路径带出目标目录。"""
    assert _is_safe_lang_segment(lang) is safe


@pytest.mark.parametrize(
    ("tag", "is_fragment"),
    [("f136", True), ("F251", True), ("01", False), ("1080p", False), ("", False)],
)
def test_fragment_tag_detection(tag: str, is_fragment: bool) -> None:
    """只有真的分片标记才允许从 stem 上剥掉。

    无条件剥最后一段会把 `Ep.01` 剥成 `Ep`、`S01E02.1080p` 剥成 `S01E02`，
    拼出来的字幕名反而对不上。
    """
    assert _is_fragment_tag(tag) is is_fragment
