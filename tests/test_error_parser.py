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
