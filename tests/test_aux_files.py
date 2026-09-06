"""「彻底删除」必须连字幕一起删掉。

`core/controller.py::_aux_files()` 原来是 `base_name + ext` 精确拼接，也就是只找
`Title.vtt`。而 yt-dlp 写出来的永远带语言代码 —— `Title.en-GB.vtt` ——
所以字幕从来没被删掉过：用户删完一个任务，目录里还躺着一堆 `.vtt`。
见 `FluentYTDL-字幕下载问题排查报告.md`。

同时钉住两条边界：视频本体不能被当成附属产物删掉，同目录里别的任务的文件也不行。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-aux-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.processing.subtitle_manager import SUBTITLE_FORMATS  # noqa: E402
from fluentytdl.utils.aux_files import AUX_EXTS, aux_files, is_aux_name  # noqa: E402


def _touch(path: Path) -> Path:
    path.write_text("x", encoding="utf-8")
    return path


def test_subtitle_formats_are_all_deletable() -> None:
    """`AUX_EXTS` 与 `SUBTITLE_FORMATS` 的覆盖关系没法靠 import 保证（utils 是底层，
    subtitle_manager 依赖 PySide6），所以在这里机械地钉住。

    加一种字幕格式而忘了加进 `AUX_EXTS`，表现就是"那种格式删不掉"。
    """
    missing = [f".{ext}" for ext in SUBTITLE_FORMATS if f".{ext}" not in AUX_EXTS]
    assert missing == [], f"这些字幕后缀不会被删除逻辑清理: {missing}"


def test_language_tagged_subtitles_are_found(tmp_path: Path) -> None:
    """这就是旧实现的死因：语言代码夹在主名和后缀之间。"""
    video = _touch(tmp_path / "Children in war zones [4K].mkv")
    subs = [
        _touch(tmp_path / "Children in war zones [4K].en-GB.vtt"),
        _touch(tmp_path / "Children in war zones [4K].zh-Hans-en-GB.srt"),
        _touch(tmp_path / "Children in war zones [4K].webp"),
    ]

    found = aux_files(str(video))

    assert sorted(found) == sorted(str(p) for p in subs)


def test_the_video_itself_is_never_returned(tmp_path: Path) -> None:
    """返回值直接进删除队列 —— 把主文件混进去就是删用户的视频。"""
    video = _touch(tmp_path / "Title.mkv")
    _touch(tmp_path / "Title.en.srt")

    found = aux_files(str(video))

    assert str(video) not in found
    assert len(found) == 1


def test_other_tasks_files_are_left_alone(tmp_path: Path) -> None:
    video = _touch(tmp_path / "Title.mkv")
    _touch(tmp_path / "Another Video.en-GB.vtt")
    _touch(tmp_path / "Another Video.webp")
    _touch(tmp_path / "Title.en-GB.vtt")

    found = aux_files(str(video))

    assert found == [str(tmp_path / "Title.en-GB.vtt")]


def test_similar_stem_is_not_a_prefix_match(tmp_path: Path) -> None:
    """`Title 2` 不该被 `Title` 的删除带走 —— 前缀里的那个点是关键。"""
    video = _touch(tmp_path / "Title.mkv")
    _touch(tmp_path / "Title 2.en.srt")
    _touch(tmp_path / "Titles.en.srt")

    assert aux_files(str(video)) == []


def test_case_insensitive_suffix(tmp_path: Path) -> None:
    video = _touch(tmp_path / "Title.mkv")
    upper = _touch(tmp_path / "Title.EN.SRT")

    assert aux_files(str(video)) == [str(upper)]


def test_missing_directory_and_empty_input(tmp_path: Path) -> None:
    """删除路径上任何异常都不该抛 —— 调用方在批量删除循环里。"""
    assert aux_files("") == []
    assert aux_files(str(tmp_path / "gone" / "Title.mkv")) == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Title.en-GB.vtt", True),
        ("Title.LRC", True),
        ("cover.jpeg", True),
        ("Title.mkv", False),
        ("Title.mp4", False),
        ("Title.vtt.part", False),
    ],
)
def test_is_aux_name(name: str, expected: bool) -> None:
    assert is_aux_name(name) is expected
