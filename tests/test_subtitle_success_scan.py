"""任务**成功**之后的字幕警告扫描（阶段 6 的用户可见出口）。

`diagnose()` 只在 `rc != 0` 时被调用，而字幕下不到从来不会让 yt-dlp 返回非零 ——
于是"视频好了、字幕全空"这件事在诊断层根本没有入口。报告里那四条任务就是这么
只留下一句"未找到字幕文件"的。`_scan_subtitle_warnings` 补上那个入口。

这里钉住四件事：

1. 三种字幕失败都能从成功任务的输出里被捞出来，并且互相可区分；
2. **只捞字幕三码** —— 成功下载里 `nsig extraction failed` 极其常见，一并冒出来
   只会训练用户忽略提示；
3. 同一个码只报一次（字幕 429 是每个请求语言各来一条）；
4. 送到 UI 的载荷里**不含**未命中的那些行 —— `raw_tail` 会带本地路径。

不碰网络、不起进程：只调方法，用 Qt 信号接载荷。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-scantest-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.diagnostics import SUBTITLE_WARNING_CODES  # noqa: E402
from fluentytdl.download.workers import DownloadWorker  # noqa: E402

URL = "https://www.youtube.com/watch?v=MSJMJxd1udk"

# 报告 `FluentYTDL-字幕下载问题排查报告.md:174-182` 里被 `--no-warnings` 吃掉的那几行
SUB_RATE_LIMITED = (
    "WARNING: Unable to download video subtitles for 'zh-Hans-en-GB': "
    "HTTP Error 429: Too Many Requests"
)
SUB_POT_REQUIRED = "WARNING: [youtube] MSJMJxd1udk: Some automatic captions require a PO Token"
SUB_NO_MATCH = "[info] There are no subtitles for the requested languages"

# 成功的下载里这两行是常客 —— priority 还比字幕三码高
NSIG = "WARNING: [youtube] MSJMJxd1udk: nsig extraction failed: Some formats may be missing"
# 真实日志里这行紧跟在合并之后，带完整本地路径
MERGE_LINE = r"[Merger] Merging formats into D:\Users\Sakura\Videos\6 Minute English.mkv"


class _Capture:
    """接 `task_warning` 的载荷。信号在同线程是直连，调完方法就已经到手了。"""

    def __init__(self, worker: DownloadWorker) -> None:
        self.payloads: list[dict] = []
        worker.task_warning.connect(self.payloads.append)


def _scan(*lines: str) -> tuple[str | None, list[dict]]:
    """跑一次扫描，返回 (给状态栏的短标题, emit 出来的载荷)。"""
    worker = DownloadWorker(URL, {"format": "bv*+ba/b"})
    cap = _Capture(worker)
    return worker._scan_subtitle_warnings(lines), cap.payloads


# ── 三码都能被捞出来 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "expected_code"),
    [
        (SUB_RATE_LIMITED, "subtitle_download_rate_limited"),
        (SUB_POT_REQUIRED, "subtitle_pot_required"),
        (SUB_NO_MATCH, "subtitles_no_language_match"),
    ],
    ids=["rate_limited", "pot_required", "no_language_match"],
)
def test_each_subtitle_code_surfaces_on_success(line: str, expected_code: str) -> None:
    """rc 是 0（任务成功），警告仍必须冒出来 —— 这就是这个方法存在的全部理由。"""
    title, payloads = _scan(line)

    assert len(payloads) == 1
    assert payloads[0]["code"] == expected_code
    # 任务已经成功了，把它标成 fatal 是彻头彻尾的误报
    assert payloads[0]["severity"] == "warning"
    assert title == payloads[0]["user_title"]
    assert title


def test_pot_warning_carries_an_actionable_fix() -> None:
    """决策 3「只加提示，不改默认」：文案要能点，但绝不代替用户改开关。"""
    _, payloads = _scan(SUB_POT_REQUIRED)

    assert payloads[0]["fix_action"] == "enable_pot_provider"
    assert payloads[0]["recovery_hint"].strip()


# ── 只捞字幕三码 ─────────────────────────────────────────────


def test_common_warnings_do_not_trigger_a_popup() -> None:
    """`nsig extraction failed` 在成功的下载里太常见了。

    每次成功都弹一次提示，用户三天之后就会对这个 InfoBar 完全免疫 —— 那时真正的
    字幕警告也一起被无视了。所以这里刻意不做通用扫描。
    """
    title, payloads = _scan(NSIG, MERGE_LINE)

    assert payloads == []
    assert title is None


def test_subtitle_code_wins_even_when_nsig_is_present() -> None:
    """`nsig_extraction_failed` 的 priority 比字幕三码高。

    所以整段输出**不能**直接喂给 `diagnose()` —— 那样主因会被 nsig 抢走，用户拿到
    的提示变成"去更新组件"，而真正的原因是字幕语言没匹配上。
    """
    _, payloads = _scan(NSIG, SUB_NO_MATCH, MERGE_LINE)

    assert len(payloads) == 1
    assert payloads[0]["code"] == "subtitles_no_language_match"


def test_unmatched_lines_stay_out_of_the_payload() -> None:
    """`technical_detail` 里含 `raw_tail`，而 `raw_tail` 里会有本地路径。

    报告的安全说明明确要求日志里的本地路径不要外流；这个载荷是要渲染到 UI 上、
    并且用户很可能截图发到 Issue 里的。喂进去多少行，就只可能漏出这几行。
    """
    _, payloads = _scan(NSIG, MERGE_LINE, SUB_RATE_LIMITED)

    detail = payloads[0]["technical_detail"]
    assert "Sakura" not in detail
    assert "nsig" not in detail
    assert "429" in detail


# ── 去重与边界 ───────────────────────────────────────────────


def test_same_code_reports_once() -> None:
    """字幕 429 是每个请求语言各来一条，逐条弹就是三连击。"""
    _, payloads = _scan(
        SUB_RATE_LIMITED,
        SUB_RATE_LIMITED.replace("zh-Hans-en-GB", "en-GB"),
        SUB_RATE_LIMITED.replace("zh-Hans-en-GB", "en-en-GB"),
    )

    assert len(payloads) == 1


def test_two_distinct_codes_pick_one_primary() -> None:
    """两种字幕问题同时出现时只报一次：优先级更高的那个（429 p98 > 无匹配 p28）。

    弹两个 InfoBar 会互相盖住，而且"被限流"和"没匹配上"给的处置方式不同 ——
    让规则表的 priority 拿主意，别把选择题抛给用户。
    """
    _, payloads = _scan(SUB_NO_MATCH, SUB_RATE_LIMITED)

    assert len(payloads) == 1
    assert payloads[0]["code"] == "subtitle_download_rate_limited"


@pytest.mark.parametrize("lines", [(), ("",), ("", ""), (MERGE_LINE,)], ids=str)
def test_nothing_to_say_stays_silent(lines: tuple[str, ...]) -> None:
    """没收到日志行（executor 没跑、纯封面任务）时必须安静收场，不能炸。"""
    title, payloads = _scan(*lines)

    assert title is None
    assert payloads == []


def test_scanned_codes_all_exist_in_the_rule_set() -> None:
    """`SUBTITLE_WARNING_CODES` 是硬编码常量，规则表改名它不会自动跟着改。

    对不上时的后果是静默的：扫描永远命中不了，那三条规则等于白写。
    """
    from fluentytdl.diagnostics import get_rule_set

    codes = {r.code for r in get_rule_set().rules}
    missing = SUBTITLE_WARNING_CODES - codes
    assert not missing, f"扫描清单里的码在规则表里不存在: {sorted(missing)}"
