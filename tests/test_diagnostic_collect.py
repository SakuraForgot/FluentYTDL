"""`diagnostics/collect.py` 的契约测试。

这一层的价值全在"哪些行留、留几份、什么顺序"三件事上，而它是失败诊断的**唯一输入**：
判据一漂，`diagnose()` 就会在错误现场拿到错的素材，而且不会有任何报错提示。所以
docstring 里写的三条契约在这里逐条锁死。
"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import diagnose, parse_events  # noqa: E402
from fluentytdl.diagnostics.collect import (  # noqa: E402
    DiagnosticLineCollector,
    extract_diagnostic_lines,
    is_diagnostic_line,
)
from fluentytdl.download.executor import DownloadExecutor  # noqa: E402
from fluentytdl.models.errors import YtDlpExecutionError  # noqa: E402

# ── 单行判定 ────────────────────────────────────────────────


def test_keeps_level_prefixed_lines():
    assert is_diagnostic_line("ERROR: unable to download video data: HTTP Error 429")
    assert is_diagnostic_line("WARNING: [youtube] nsig extraction failed")
    # 级别前缀的判定是大小写不敏感的（`parse_level` 用 upper()）
    assert is_diagnostic_line("error: something broke")


def test_keeps_info_channel():
    assert is_diagnostic_line("[info] Downloading 1 format(s): 315+251")


def test_keeps_bare_line_rules():
    """无级别前缀但命中 bareLine / 过滤器跳过通道的行。

    这两条是本模块存在的理由：`output_parser` 把它们判成 `status`，下载路径的
    warning/error/info 分支碰不到，于是 `engine._match_skip_line` /
    `_match_bare_line` 在下载路径上从来没拿到过输入。
    """
    assert is_diagnostic_line("[info] There are no subtitles for the requested languages")
    assert is_diagnostic_line("[download] dQw4w9WgXcQ: upload date is not in range, skipping")


def test_drops_progress_and_chatter():
    assert not is_diagnostic_line("")
    assert not is_diagnostic_line("   ")
    assert not is_diagnostic_line("[download] 100% of 12.34MiB in 00:03")
    assert not is_diagnostic_line("[download] Downloading item 3 of 10")
    assert not is_diagnostic_line("[youtube] dQw4w9WgXcQ: Downloading webpage")
    assert not is_diagnostic_line(
        "FLUENTYTDL|download|1048576|10485760|NA|1048576|9|avc1|mp4a|mp4|v.mp4"
    )


# ── 三条契约 ────────────────────────────────────────────────


def test_contract_preserves_order():
    """契约 1：保持原始出现顺序。

    `pick_primary` 的第三级仲裁按 `line_no` 取靠后者，重排会改判主因。
    """
    output = "\n".join(
        [
            "WARNING: first",
            "[download] 50% of 1.00MiB",
            "ERROR: second",
            "[info] third",
        ]
    )
    assert extract_diagnostic_lines(output) == [
        "WARNING: first",
        "ERROR: second",
        "[info] third",
    ]


def test_contract_dedups_identical_lines():
    """契约 2：同一原始行最多出现一次。

    yt-dlp 会对每个格式 / 每种语言各刷一条同样的警告，不去重就会把真正的主因
    挤出这 400 格的窗口；而重复对 `pick_primary`（取 max）毫无价值。
    """
    line = "WARNING: [youtube] nsig extraction failed: Some formats may be missing"
    output = "\n".join([line, "[download] 10% of 1.00MiB", line, line])
    assert extract_diagnostic_lines(output) == [line]


def test_contract_dedup_ignores_ansi_and_trailing_space():
    """去重键做 ANSI 剥离 + 首尾裁剪：同一句话带不带颜色码算同一行。"""
    plain = "WARNING: rate limited"
    colored = "\x1b[33mWARNING: rate limited\x1b[0m   "
    kept = extract_diagnostic_lines("\n".join([plain, colored]))
    assert kept == [plain]


def test_contract_does_not_rewrite_text():
    """契约 3：不改写原文 —— 语义化留给 `parse_events()`。

    尤其不能剥掉 `[info]` 前缀或做小写化：bareLine 规则匹配的是完整原文。
    """
    line = "[info] There are no subtitles for the requested languages"
    assert extract_diagnostic_lines(line) == [line]
    # 存进去的原文必须还能被引擎重新识别（这就是"输入接回来了"的定义）
    events = parse_events("\n".join(extract_diagnostic_lines(line)))
    assert [ev.code for ev in events] == ["subtitles_no_language_match"]


def test_warning_that_also_matches_a_bare_line_rule_appears_once():
    """既是 `WARNING:` 又能命中裸行通道的一行，只能返回一次。

    分轮扫描（先扫级别前缀、再扫关键字）会把这类行存两份 —— 这正是原先三处各写
    一套字符串判断时的现成陷阱。
    """
    line = "WARNING: There are no subtitles for the requested languages"
    assert extract_diagnostic_lines(line) == [line]


# ── 流式收集器 ──────────────────────────────────────────────


def test_collector_is_iterable_and_sized():
    """`_scan_subtitle_warnings(self.executor.diag_lines)` 只靠迭代取值。"""
    c = DiagnosticLineCollector()
    assert c.feed("ERROR: boom") is True
    assert c.feed("[download] 1% of 1.00MiB") is False
    assert len(c) == 1
    assert list(c) == ["ERROR: boom"]
    assert c.as_text() == "ERROR: boom"


def test_collector_clear_resets_dedup():
    """自动重试复用同一个 executor：上一轮的行不能算进这一轮，也不能因去重被吞掉。"""
    c = DiagnosticLineCollector()
    c.feed("ERROR: boom")
    c.clear()
    assert len(c) == 0
    assert c.feed("ERROR: boom") is True
    assert list(c) == ["ERROR: boom"]


def test_collector_respects_maxlen():
    c = DiagnosticLineCollector(maxlen=2)
    for i in range(5):
        c.feed(f"ERROR: e{i}")
    assert list(c) == ["ERROR: e3", "ERROR: e4"]


def test_collector_strips_trailing_whitespace_only():
    c = DiagnosticLineCollector()
    c.feed("  WARNING: indented  ")
    assert list(c) == ["  WARNING: indented"]


# ── 端到端：executor 失败路径真的拿到了这些行 ───────────────


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_executor_failure_feeds_diag_lines_not_tail(mock_popen, _mock_resolve):
    """rc != 0 时抛出的 stderr 必须来自 `diag_lines`，而不是混着进度行的 `tail`。

    构造 200 行进度把 `tail`（maxlen=120）灌满：修好之前那条 `ERROR:` 会被挤出
    窗口，`diagnose()` 只能落到兜底。
    """
    lines = [b"ERROR: unable to download video data: HTTP Error 429: Too Many Requests"]
    lines += [
        b"FLUENTYTDL|download|%d|10485760|NA|1048576|9|avc1|mp4a|mp4|v.mp4" % (i * 1024)
        for i in range(200)
    ]

    process = MagicMock()
    process.stdout = BytesIO(b"\n".join(lines))
    process.wait.return_value = 1
    process.returncode = 1
    mock_popen.return_value = process

    try:
        DownloadExecutor().execute(
            url="https://youtube.com/watch?v=mock_id_456",
            ydl_opts={"format": "best_mp4"},
            on_progress=lambda _e: None,
            on_status=lambda _s: None,
            on_path=lambda _p: None,
            cancel_check=lambda: False,
        )
    except YtDlpExecutionError as exc:
        assert "HTTP Error 429" in exc.stderr
        assert "FLUENTYTDL|" not in exc.stderr
        assert diagnose(exc.exit_code, exc.stderr).code == "rate_limited_429"
    else:
        raise AssertionError("rc=1 且无有效产物时应抛 YtDlpExecutionError")


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_executor_collects_prefixless_skip_line(mock_popen, _mock_resolve):
    """`[download] ... skipping` 被 `output_parser` 判成 `status`，照样要进缓冲区。

    这条通道（`input_filter_skipped`）在下载路径上从来没被喂过输入。
    """
    output = b"\n".join(
        [
            b"[download] dQw4w9WgXcQ: upload date is not in range, skipping",
            b"[info] There are no subtitles for the requested languages",
            b"FLUENTYTDL|download|10485760|10485760|NA|1048576|0|avc1|mp4a|mp4|v.mp4",
        ]
    )

    process = MagicMock()
    process.stdout = BytesIO(output)
    process.wait.return_value = 0
    process.returncode = 0
    mock_popen.return_value = process

    executor = DownloadExecutor()
    executor.execute(
        url="https://youtube.com/watch?v=mock_id_456",
        ydl_opts={"format": "best_mp4"},
        on_progress=lambda _e: None,
        on_status=lambda _s: None,
        on_path=lambda _p: None,
        cancel_check=lambda: False,
    )

    codes = {ev.code for ev in parse_events(executor.diag_lines.as_text())}
    assert "input_filter_skipped" in codes
    assert "subtitles_no_language_match" in codes


@patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe"))
@patch("fluentytdl.download.executor.subprocess.Popen")
def test_executor_diag_lines_reset_between_runs(mock_popen, _mock_resolve):
    """规则驱动的自动重试复用同一个 executor，上一轮的警告不能算进这一轮。"""
    executor = DownloadExecutor()

    def run(payload: bytes) -> None:
        process = MagicMock()
        process.stdout = BytesIO(payload)
        process.wait.return_value = 0
        process.returncode = 0
        mock_popen.return_value = process
        executor.execute(
            url="https://youtube.com/watch?v=mock_id_456",
            ydl_opts={"format": "best_mp4"},
            on_progress=lambda _e: None,
            on_status=lambda _s: None,
            on_path=lambda _p: None,
            cancel_check=lambda: False,
        )

    run(b"WARNING: [youtube] nsig extraction failed\n")
    run(b"[info] Downloading 1 format(s): 315+251\n")

    assert "nsig extraction failed" not in executor.diag_lines.as_text()
