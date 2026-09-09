"""字幕后处理只校验**别人交给它的路径** —— 它不再自己去找文件。

《下载产物事务层》Step 3 让 `SubtitleProcessor` 的四级定位整体退休：
`_find_subtitle_files()`、`LOCATE_*` 四个常量、`allow_dir_scan`、`located_by`、
`_is_fragment_tag`、`_is_safe_lang_segment` 全部消失，签名变成
`process(output_path, subtitle_paths, opts, status_callback=None)`，
`subtitle_paths` 由调用方给（Step 4/5 起是 `manifest.kept("subtitle")`）。

**为什么定位逻辑该死**：四级里唯一真正需要的那一级是「executor 记下的精确路径」，
其余三级都是在**用文件名反推所有权**，而那是「少删误删」的根因 —— 内嵌模式下调用方
会删掉这里返回的每一个文件，第 4 级却会把整个目录的字幕交出来。历史死因也在下面
`test_name_composition_could_never_have_worked` 里原样保留着：标题里的 `[4K]` 让
`glob(stem + ".*")` 命中 **0** 个文件，连视频自己都匹配不到。

**这个模块真正的价值全部留下**：完整性校验、`invalid_files`、四个 `reason` 码
（`disabled` / `video_missing` / `not_found` / `all_invalid`）、`status_callback`。
所以本文件钉的是「给定声明路径之后的校验与结果结构」，外加 `_pick_subtitles()`
这个纯函数的三条边界：后缀白名单、按真实路径去重、不存在的项直接落选。

删除范围那一侧的边界（**声明过 ≠ 名字像**）由
`tests/test_subtitle_output_signals.py` 第 2 组用例守。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-subval-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.processing.subtitle_processor import subtitle_processor  # noqa: E402

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


def _process(video: Path, *declared: Path, status_callback=None):
    """`declared` 是清单交出来的那批路径 —— 这是"哪些文件是本任务的字幕"的唯一来源。

    刻意**不再**传 `subtitleslangs`：语言键曾是第 2 级定位拼文件名的输入，现在
    `process()` 只看后缀白名单与磁盘现实，语言由清单侧的 `qualifier` 承载。
    """
    return subtitle_processor.process(
        str(video),
        [str(p) for p in declared],
        dict(SUB_ENABLED),
        status_callback=status_callback,
    )


# ── 声明即输入 ───────────────────────────────────────────────


def test_declared_paths_are_validated_and_returned(tmp_path: Path) -> None:
    video = _video(tmp_path, "Plain Title")
    en = _sub(tmp_path, "Plain Title.en-GB.vtt")
    ja = _sub(tmp_path, "Plain Title.ja.srt", VALID_SRT)

    result = _process(video, en, ja)

    assert result.success
    assert result.reason is None
    assert result.processed_files == [str(en), str(ja)]  # 按文件名排序
    assert result.invalid_files == []


def test_name_composition_could_never_have_worked(tmp_path: Path) -> None:
    """旧实现的死因，连同反证一起留着。

    标题里的 `[4K]` 被 glob 当成字符集，`glob(stem + ".*")` 在同一个目录上命中 **0**
    个文件 —— 这就是「用名字反推所有权」这条路走不通的实证。声明路径不解释任何元
    字符，所以照样命中。
    """
    video = _video(tmp_path)
    sub = _sub(tmp_path, f"{NASTY_STEM}.en-GB.vtt")

    result = _process(video, sub)

    assert result.processed_files == [str(sub)]
    # 反证：旧实现在同一个目录上确实是 0 命中
    assert list(tmp_path.glob(f"{NASTY_STEM}.*")) == []


def test_fragment_output_path_no_longer_matters(tmp_path: Path) -> None:
    """`output_path` 还指向分片文件（`.f136.mkv`）时也不影响结果。

    以前得靠 `_is_fragment_tag()` 从 stem 上剥掉分片标记才能拼出字幕名 —— 无条件剥
    最后一段会把 `Ep.01` 剥成 `Ep`。现在 `output_path` 只用来判「视频落盘了没有」。
    """
    video = tmp_path / "Plain Title.f136.mkv"
    video.write_text("fake fragment", encoding="utf-8")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")

    result = _process(video, sub)

    assert result.processed_files == [str(sub)]


def test_process_accepts_a_generator(tmp_path: Path) -> None:
    """`subtitle_paths` 是 `Iterable`，调用方完全可能传生成器。

    实现里必须先物化成 list —— 「挑文件」和「日志里报候选个数」是两次遍历，直接
    遍历生成器的话第二次会拿到空的，`not_found` 的日志就永远写 `candidates=0`。
    """
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")

    result = subtitle_processor.process(
        str(video), (str(p) for p in (sub,)), dict(SUB_ENABLED)
    )

    assert result.success
    assert result.processed_files == [str(sub)]


# ── `_pick_subtitles()` 的三条边界 ───────────────────────────


def test_non_subtitle_declarations_are_filtered_out(tmp_path: Path) -> None:
    """混装清单里的主媒体与封面不能被当成字幕交出去。

    调用方可能整批把 `kept()` 传进来，而内嵌模式下返回的每个文件都会被删 —— 后缀
    白名单是这里唯一的守卫。
    """
    video = _video(tmp_path, "Plain Title")
    thumb = tmp_path / "Plain Title.webp"
    thumb.write_bytes(b"fake webp")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")

    result = _process(video, video, thumb, sub)

    assert result.processed_files == [str(sub)]


def test_lrc_suffix_is_recognized(tmp_path: Path) -> None:
    """后缀白名单以前是另一份字面量 `[".srt", ".ass", ".vtt"]`，`.lrc` 就是那么丢的。

    现在它从 `SUBTITLE_FORMATS` 派生（`SUBTITLE_SUFFIXES`），所以两处不会再漂移。
    """
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.lrc", "[00:01.00]Hello\n")

    result = _process(video, sub)

    assert result.processed_files == [str(sub)]


def test_same_file_declared_twice_is_processed_once(tmp_path: Path) -> None:
    """身份是**解析后的真实路径**，不是字符串。

    同一个文件可以从两条渠道进清单（executor 报告 + `reconcile()` 补录），拼法未必
    一致；按字符串去重会让它被校验两遍、在内嵌模式下被删两次（第二次是
    `FileNotFoundError`）。
    """
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.vtt")
    (tmp_path / "detour").mkdir()
    same_via_detour = tmp_path / "detour" / ".." / sub.name

    result = _process(video, sub, sub, same_via_detour)

    assert result.processed_files == [str(sub)]  # 先进的那个拼法胜出


def test_declared_but_gone_is_not_reported_invalid(tmp_path: Path) -> None:
    """声明过却已经不在盘上 ≠ 文件坏了。

    前者是「清单与磁盘对不上」（事务层用 `presence` 表达），后者是「拿到了但内容
    不可用」。混成一条会让 UI 把两种完全不同的故障说成同一句话。
    """
    video = _video(tmp_path, "Plain Title")
    ghost = tmp_path / "Plain Title.en-GB.vtt"  # 只声明，不创建

    result = _process(video, ghost)

    assert result.success is False
    assert result.reason == "not_found"
    assert result.invalid_files == []


# ── 空结果与坏文件 ────────────────────────────────────────────


def test_missing_subtitles_is_a_failure_with_reason(tmp_path: Path) -> None:
    """一个都没有时**必须** `success=False`。

    旧实现返回 `success=True, message="未找到字幕文件"`，调用方的 `if result.success`
    直接跳过所有提示，用户只看到"字幕开了但没有字幕"。`None` 与空清单等价。
    """
    video = _video(tmp_path)

    result = subtitle_processor.process(str(video), None, dict(SUB_ENABLED))

    assert result.success is False
    assert result.reason == "not_found"
    assert result.processed_files == []


def test_invalid_files_are_reported_separately(tmp_path: Path) -> None:
    """坏文件不算成功产出，但要带着原因回去 —— 内嵌模式下这些残骸也得清掉。"""
    video = _video(tmp_path, "Plain Title")
    good = _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)
    empty = _sub(tmp_path, "Plain Title.ja.vtt", "")
    no_timecode = _sub(tmp_path, "Plain Title.fr.srt", "just some text\n")

    result = _process(video, good, empty, no_timecode)

    assert result.success
    assert result.processed_files == [str(good)]
    assert {Path(p).name for p, _ in result.invalid_files} == {empty.name, no_timecode.name}


def test_all_invalid_is_a_failure(tmp_path: Path) -> None:
    video = _video(tmp_path, "Plain Title")
    broken = _sub(tmp_path, "Plain Title.en-GB.vtt", "")

    result = _process(video, broken)

    assert result.success is False
    assert result.reason == "all_invalid"
    assert len(result.invalid_files) == 1


def test_disabled_and_missing_video_are_distinguishable(tmp_path: Path) -> None:
    """两种"没处理"必须能被上层区分：一个是用户没开字幕，一个是视频压根没落盘。

    `SubtitleFeature` 对 `video_missing` 刻意不冒警告（视频自己的失败提示更准），
    对 `disabled` 则整条短路 —— 两者混成一个 `reason` 就会互相盖掉。
    """
    disabled = subtitle_processor.process(str(_video(tmp_path)), [], {})
    assert disabled.success is True
    assert disabled.reason == "disabled"

    missing = subtitle_processor.process(str(tmp_path / "nope.mkv"), [], dict(SUB_ENABLED))
    assert missing.success is False
    assert missing.reason == "video_missing"


# ── status_callback ─────────────────────────────────────────


def test_status_callback_reports_success_and_bad_files(tmp_path: Path) -> None:
    """`status_callback` 参数存在已久，但从来没被调用过。"""
    video = _video(tmp_path, "Plain Title")
    good = _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)
    broken = _sub(tmp_path, "Plain Title.ja.vtt", "")
    seen: list[str] = []

    result = _process(video, good, broken, status_callback=seen.append)

    assert result.success
    assert any("1 个字幕文件" in m for m in seen)
    assert any("Plain Title.ja.vtt" in m for m in seen)


def test_status_callback_exception_does_not_abort_processing(tmp_path: Path) -> None:
    """UI 回调抛异常不该把字幕后处理带走 —— 校验结果照样要返回。"""
    video = _video(tmp_path, "Plain Title")
    sub = _sub(tmp_path, "Plain Title.en-GB.srt", VALID_SRT)

    def boom(_msg: str) -> None:
        raise RuntimeError("UI exploded")

    result = _process(video, sub, status_callback=boom)

    assert result.processed_files == [str(sub)]
