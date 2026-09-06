"""诊断引擎端到端测试：真实 yt-dlp stderr 样本 → code / category / retry。

覆盖三件事：
1. 每类错误的识别（每条断言都用真实出现过的 stderr 片段）
2. 主因仲裁（多事件共存时选谁、伴随信号如何改写 fix_action）
3. 兜底（规则全不命中时仍要给出有信息量的结果）
"""

import sys
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import (  # noqa: E402
    FALLBACK_CODE,
    diagnose,
    parse_events,
)

# (stderr 样本, 期望 code) —— 样本均取自真实 yt-dlp 输出
SAMPLES: list[tuple[str, str]] = [
    (
        "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication.",
        "bot_check_sign_in",
    ),
    (
        "ERROR: [youtube] abc123: Join this channel to get access to members-only content",
        "members_only",
    ),
    (
        "ERROR: [youtube] abc123: Private video. Sign in if you've been granted access "
        "to this video",
        "private_video",
    ),
    ("ERROR: [youtube] abc123: Video unavailable", "video_unavailable"),
    (
        "ERROR: [youtube] abc123: Sign in to confirm your age. "
        "This video may be inappropriate for some users.",
        "age_restricted",
    ),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate_limited_429"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "http_403_forbidden"),
    ("ERROR: unable to download webpage: HTTP Error 404: Not Found", "http_404_not_found"),
    ("ERROR: ffprobe/ffmpeg not found. Please install or provide the path", "ffmpeg_not_found"),
    (
        "ERROR: unable to download video data: <urlopen error [Errno 11001] getaddrinfo failed>",
        "dns_resolution_failed",
    ),
    ("ERROR: [Errno 28] No space left on device", "disk_full"),
    ("ERROR: unable to open for writing: [Errno 13] Permission denied", "permission_denied"),
    (
        "ERROR: [youtube] abc123: Requested format is not available. "
        "Use --list-formats for a list of available formats",
        "format_unavailable",
    ),
]


@pytest.mark.parametrize(("stderr", "expected_code"), SAMPLES, ids=[c for _, c in SAMPLES])
def test_sample_maps_to_expected_code(stderr: str, expected_code: str) -> None:
    diag = diagnose(1, stderr)
    assert diag.code == expected_code


@pytest.mark.parametrize(("stderr", "expected_code"), SAMPLES, ids=[c for _, c in SAMPLES])
def test_sample_has_user_facing_text(stderr: str, expected_code: str) -> None:
    """每条命中的规则都必须有非空文案，否则 UI 会弹出空对话框。"""
    diag = diagnose(1, stderr)
    assert diag.user_title.strip()
    assert diag.user_message.strip()


def test_never_retry_for_terminal_errors() -> None:
    """会员专属这类错误必须是 never —— 挂起等用户点击只会卡死批量队列。"""
    diag = diagnose(1, SAMPLES[1][0])
    assert diag.retry.policy == "never"
    assert not diag.retry.is_automatic


def test_rate_limit_is_automatic_backoff() -> None:
    diag = diagnose(1, "ERROR: HTTP Error 429: Too Many Requests")
    assert diag.retry.policy == "backoff"
    assert diag.retry.is_automatic
    assert diag.retry.max_attempts > 0
    # 指数退避：第 0 次等 base，第 1 次翻倍
    assert diag.retry.delay_for(1) == diag.retry.delay_for(0) * 2


def test_cookie_error_is_after_fix() -> None:
    diag = diagnose(1, SAMPLES[0][0])
    assert diag.retry.policy == "after_fix"
    assert diag.category == "auth"
    assert diag.fix_action


# ── 主因仲裁 ────────────────────────────────────────────────────────

NSIG_PLUS_403 = """\
WARNING: [youtube] abc123: nsig extraction failed: Some formats may be missing
WARNING: [youtube] abc123: Signature extraction failed: Some formats may be missing
ERROR: unable to download video data: HTTP Error 403: Forbidden
"""


def test_error_beats_warning() -> None:
    """ERROR 行永远压过 WARNING 行，哪怕 WARNING 的 priority 更高。"""
    diag = diagnose(1, NSIG_PLUS_403)
    assert diag.code == "http_403_forbidden"


def test_companion_signal_redirects_fix_action() -> None:
    """nsig 失败伴随 403 时，正确处置是更新 yt-dlp，而不是引导用户换代理节点。"""
    diag = diagnose(1, NSIG_PLUS_403)
    assert diag.fix_action == "update_component"
    assert diag.has_event("nsig_extraction_failed")
    assert any("yt-dlp" in note for note in diag.extra_notes)


def test_all_events_are_collected() -> None:
    events = parse_events(NSIG_PLUS_403)
    codes = {ev.code for ev in events}
    assert "nsig_extraction_failed" in codes
    assert "http_403_forbidden" in codes


def test_warning_only_output_stays_warning() -> None:
    diag = diagnose(0, "WARNING: [youtube] abc123: nsig extraction failed: Some formats missing")
    assert diag.severity == "warning"


def test_filter_skip_is_not_an_error() -> None:
    """`--match-filter` 跳过不是错误，不能弹错误框。"""
    diag = diagnose(0, "[download] abc123: skipping .. does not pass filter (duration < 60)")
    assert diag.code == "input_filter_skipped"
    assert diag.severity == "warning"


# ── 字幕三码（去掉 `--no-warnings` 之后才拿得到这些行）────────────────

# 报告 `FluentYTDL-字幕下载问题排查报告.md:174-182` 列出的三条被抑制的行
SUB_RATE_LIMITED = (
    "WARNING: Unable to download video subtitles for 'zh-Hans-en-GB': "
    "HTTP Error 429: Too Many Requests"
)
SUB_POT_REQUIRED = "WARNING: [youtube] MSJMJxd1udk: Some automatic captions require a PO Token"
SUB_NO_MATCH = "[info] There are no subtitles for the requested languages"

# 真机复现拿到的**原文**：报告转述时写成了 `WARNING:`，yt-dlp 实际打的是 `ERROR:` ——
# 它描述的是单条字幕轨那一次子请求失败，任务本身照旧继续、照旧收尾。
# 两种前缀都必须归到同一个码上，否则 `_scan_subtitle_warnings` 在真机上永远扫不到东西。
SUB_RATE_LIMITED_REAL = (
    "ERROR: Unable to download video subtitles for 'zh-Hans-en-GB': "
    "HTTP Error 429: Too Many Requests"
)
SUB_POT_REQUIRED_REAL = (
    "ERROR: [youtube] MSJMJxd1udk: Unable to download automatic captions: PO Token is required"
)


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [
        (SUB_RATE_LIMITED, "subtitle_download_rate_limited"),
        (SUB_POT_REQUIRED, "subtitle_pot_required"),
        (SUB_NO_MATCH, "subtitles_no_language_match"),
        (SUB_RATE_LIMITED_REAL, "subtitle_download_rate_limited"),
        (SUB_POT_REQUIRED_REAL, "subtitle_pot_required"),
    ],
    ids=[
        "rate_limited",
        "pot_required",
        "no_language_match",
        "rate_limited_real_error_prefix",
        "pot_required_real_error_prefix",
    ],
)
def test_subtitle_warning_is_identified_and_stays_a_warning(
    stderr: str, expected_code: str
) -> None:
    """三种字幕失败必须互相可区分，且一律只是警告。

    报告 P2 的原话是"对 429、PO Token 缺失、语言无匹配三种情况给出可区分的提示"。
    `severity` 必须是 warning：视频已经下载完成了，把任务标红是彻头彻尾的误报。
    """
    diag = diagnose(0, stderr)
    assert diag.code == expected_code
    assert diag.severity == "warning"
    assert diag.user_title.strip()
    assert diag.user_message.strip()
    # 字幕层面的失败绝不能触发整个任务的自动重试
    assert diag.retry.policy == "never"


def test_bare_info_line_reaches_the_engine() -> None:
    """`[info] There are no subtitles…` 既没有 `ERROR:` 也没有 `WARNING:` 前缀。

    这是 `bareLine` 通道存在的唯一理由 —— 没有它，这行在 `parse_events` 里连事件
    都产生不了，用户只剩一句"未找到字幕文件"。
    """
    events = parse_events(SUB_NO_MATCH)
    assert [ev.code for ev in events] == ["subtitles_no_language_match"]
    assert events[0].level == "warning"


def test_subtitle_rate_limit_beats_generic_429() -> None:
    """p98 必须压过 `rate_limited_429`(p73)，否则字幕单独限流会被报成整任务网络故障。"""
    diag = diagnose(0, SUB_RATE_LIMITED)
    assert diag.code != "rate_limited_429"


@pytest.mark.parametrize(
    "stderr",
    [SUB_RATE_LIMITED_REAL, SUB_POT_REQUIRED_REAL],
    ids=["rate_limited", "pot_required"],
)
def test_subtitle_error_line_is_demoted_to_a_warning_event(stderr: str) -> None:
    """`demoteToWarning`：字幕子请求的 `ERROR:` 行只产出 warning 级事件。

    降级是免费换来两条护栏：任务真失败时 `pick_primary` 的"error 压 warning"让它永远
    当不上主因（见下面两个 `..._still_wins_...`），而 priority 98/97 保持不变，仍然能在
    **同一行**上压过更泛化的 `rate_limited_429` / `pot_token_required`。
    """
    events = parse_events(stderr)
    assert len(events) == 1
    assert events[0].level == "warning", "字幕子请求失败混进 error 层就会挤掉真正的失败原因"
    assert events[0].priority >= 97, "降级只改级别，不该动 priority"


def test_subtitle_pot_does_not_escalate_to_fatal() -> None:
    """p97 必须压过 `pot_token_required`(p90, severity=fatal)。

    去掉 `--no-warnings` 之后，字幕级 PO Token 警告开始进入诊断层；被 p90 接住就会
    从"字幕少一个文件"升级成"认证致命错误"，比原来的静默失败更糟。
    """
    diag = diagnose(0, SUB_POT_REQUIRED)
    assert diag.severity != "fatal"
    assert diag.fix_action == "enable_pot_provider"


def test_non_subtitle_pot_warning_still_fatal() -> None:
    """反向护栏：字幕专属规则不能把普通 PO Token 问题一起降级。"""
    diag = diagnose(1, "WARNING: [youtube] abc123: Some web client formats require a PO Token")
    assert diag.code == "pot_token_required"


def test_failed_task_is_not_blamed_on_a_subtitle_warning() -> None:
    """rc != 0 且全场只有警告级线索时，不得拿它当主因。

    `executor` 的两级体积校验会在**没有 ERROR: 行**的情况下抛
    `YtDlpExecutionError`（Windows 上 `.part-Frag` 删不掉就是这条路）。那一刻顶上来
    的很可能只是一条字幕限流警告 —— 报成任务的失败主因等于指错方向。
    事件本身要留在 `events` 里，详情列表还要展示。
    """
    diag = diagnose(1, SUB_RATE_LIMITED)
    assert diag.code == FALLBACK_CODE
    assert diag.has_event("subtitle_download_rate_limited")


def test_real_error_still_wins_over_subtitle_warning() -> None:
    """护栏只在"只有警告"时生效，不能让真正的 ERROR 行也落到兜底。"""
    diag = diagnose(1, SUB_RATE_LIMITED + "\nERROR: [youtube] abc123: Video unavailable")
    assert diag.code == "video_unavailable"


def test_real_error_still_wins_over_subtitle_error_line() -> None:
    """同上，但字幕那行也是 `ERROR:` —— 这是真机上真正会出现的组合。

    字幕规则的 p98 高于绝大多数故障规则，靠 `demoteToWarning` 把它压回 warning 层才
    不至于把致命原因挤下去。少了降级，这里会报"字幕下载被限流"，而用户实际撞上的是
    人机验证。
    """
    diag = diagnose(
        1,
        SUB_RATE_LIMITED_REAL + "\nERROR: [youtube] abc123: Sign in to confirm you're not a bot",
    )
    assert diag.code == "bot_check_sign_in"
    assert diag.has_event("subtitle_download_rate_limited")


def test_whole_task_429_wins_over_subtitle_429() -> None:
    """视频数据本身也被限流时，主因是整任务限流，不是"少了个字幕文件"。

    两者都是 429、都是 `ERROR:` 行，区别只在 `rate_limited_429` 带 backoff 重试预算。
    把字幕那条报成主因会连重试一起丢掉。
    """
    diag = diagnose(
        1,
        SUB_RATE_LIMITED_REAL
        + "\nERROR: unable to download video data: HTTP Error 429: Too Many Requests",
    )
    assert diag.code == "rate_limited_429"
    assert diag.retry.policy == "backoff"


# ── 兜底 ────────────────────────────────────────────────────────────


def test_unmatched_error_falls_back_but_keeps_http_status() -> None:
    """规则没命中时兜底仍要抽出 HTTP 状态码，比一句“未知错误”有用得多。

    400 故意选的：规则表只覆盖 403/404/410/429/5xx，这条必然走兜底。
    """
    diag = diagnose(1, "ERROR: something nobody has ever seen: HTTP Error 400: Bad Request")
    assert diag.code == FALLBACK_CODE
    assert "400" in diag.user_title
    assert diag.category == "network"


def test_completely_unknown_error_still_diagnoses() -> None:
    diag = diagnose(1, "ERROR: 天外飞仙式的未知故障")
    assert diag.code == FALLBACK_CODE
    assert diag.user_title.strip()
    assert diag.retry.policy in ("never", "after_fix")


def test_empty_stderr_does_not_crash() -> None:
    diag = diagnose(0, "")
    assert diag.code == FALLBACK_CODE
    assert diag.events == []


def test_to_dict_carries_everything_ui_needs() -> None:
    """UI 侧只拿 dict，不接触 catalog —— 所有展示字段必须已经展开。"""
    payload = diagnose(1, SAMPLES[0][0]).to_dict()
    for key in (
        "code",
        "category",
        "severity",
        "retry",
        "fix_action",
        "user_title",
        "user_message",
        "recovery_hint",
        "technical_detail",
    ):
        assert key in payload, f"to_dict() 缺少 UI 依赖的字段: {key}"
