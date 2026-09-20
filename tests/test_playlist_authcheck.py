"""Issue #92: webpage network failures must survive authcheck wrapping."""

import copy
import importlib
import sys
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import diagnose  # noqa: E402
from fluentytdl.models.errors import YtDlpExecutionError  # noqa: E402
from fluentytdl.utils.youtube_request import COOKIE_MODE  # noqa: E402
from fluentytdl.youtube.youtube_service import YoutubeService  # noqa: E402

AUTHCHECK = (
    "ERROR: [youtube:tab] PLi8Q0jV2sWRJPJ9kIDl2hIENahq9BP1sm: "
    "Playlists that require authentication may not extract correctly without a successful "
    "webpage download. If you are not downloading private content, or your cookies are only "
    'for the first account and channel, pass "--extractor-args youtubetab:skip=authcheck" '
    "to skip this check"
)
TIMEOUT = (
    "WARNING: [youtube:tab] PLi8Q0jV2sWRJPJ9kIDl2hIENahq9BP1sm: Unable to download webpage: "
    "Connection to www.youtube.com timed out. (connect timeout=15.0). Giving up after 3 retries"
)
ISSUE92 = f"{TIMEOUT}\n{AUTHCHECK}"
ROUTES = ("extract_info_for_dialog_sync", "extract_playlist_flat", "extract_channel_flat")


@pytest.mark.parametrize(
    ("warning", "code"),
    [
        (TIMEOUT, "connection_timeout"),
        (TIMEOUT.replace("timed out", "getaddrinfo failed"), "dns_resolution_failed"),
    ],
)
def test_authcheck_preserves_failed_webpage_cause(warning, code):
    diag = diagnose(1, f"{warning}\n{AUTHCHECK}", phase="parse")
    assert diag.code == code
    assert diag.category == "network"
    assert diag.phase == "parse"
    assert len(diag.events) == 2
    assert diag.events[-1].level == "error"
    assert AUTHCHECK in diag.raw_tail
    if code == "connection_timeout":
        assert diag.fix_action == "switch_proxy"
        assert "浏览器 VPN" in diag.user_message


@pytest.mark.parametrize(
    "stderr",
    [
        AUTHCHECK,
        f"{AUTHCHECK}\n{TIMEOUT}",  # Not a preceding cause.
        f"{TIMEOUT.replace('[youtube:tab]', '[vimeo]')}\n{AUTHCHECK}",
        f"{TIMEOUT.replace('Unable to download webpage', 'Unable to download subtitles')}\n{AUTHCHECK}",
        f"{TIMEOUT}\nERROR: [youtube:tab] unrelated failure\n{AUTHCHECK}",
    ],
)
def test_authcheck_does_not_invent_network_causes(stderr):
    assert diagnose(1, stderr).code == "unknown"


def test_network_warning_does_not_override_real_auth_failure():
    diag = diagnose(1, f"{TIMEOUT}\n{AUTHCHECK}\nERROR: [youtube] abc: This video is private")
    assert diag.code == "private_video"


@pytest.fixture
def extraction(monkeypatch):
    mod = importlib.import_module("fluentytdl.youtube.youtube_service")
    svc = object.__new__(YoutubeService)
    svc._cookie_generation = 0
    opts = {
        "proxy": "http://127.0.0.1:7890",
        COOKIE_MODE: False,
        "extractor_args": {"youtube": {"player_client": ["default", "mweb"]}},
    }
    monkeypatch.setitem(mod.config_manager.config, "playlist_skip_authcheck", False)
    monkeypatch.setitem(mod.config_manager.config, "youtube_cookies_enabled", False)
    monkeypatch.setattr(mod, "resolve_yt_dlp_exe", lambda: Path("mock-yt-dlp.exe"))
    monkeypatch.setattr(svc, "build_ydl_options", lambda *a, **k: copy.deepcopy(opts))
    monkeypatch.setattr(svc, "_emit_log", Mock())
    monkeypatch.setattr(svc, "_parse_cache_key", lambda *a: "probe")
    monkeypatch.setattr(svc, "_parse_cache_get", lambda *a: None)
    monkeypatch.setattr(svc, "_parse_cache_put", Mock())
    monkeypatch.setattr(svc, "_youtube_sabr_only_flag", lambda: False)
    monkeypatch.setattr(svc, "_handle_channel_tab_fallback", lambda *a: None)
    monkeypatch.setattr(svc, "_extend_playlist_entries_from_youtube_continuations", Mock())
    run = Mock()
    monkeypatch.setattr(mod, "run_dump_single_json", run)
    return svc, run, opts


@pytest.mark.parametrize("route", ROUTES)
def test_authcheck_retries_cli_stderr_once_and_preserves_request(extraction, route):
    svc, run, opts = extraction
    error = YtDlpExecutionError(1, ISSUE92)
    assert "authcheck" not in str(error)
    run.side_effect = [error, {"entries": [{"id": "recovered"}]}]
    cancel = Event()
    result = getattr(svc, route)("https://www.youtube.com/@example", cancel_event=cancel)
    assert result["entries"] == [{"id": "recovered", COOKIE_MODE: False}]
    assert run.call_count == 2
    first, second = (call.args[1] for call in run.call_args_list)
    assert "youtubetab" not in first["extractor_args"]
    assert second["extractor_args"]["youtubetab"]["skip"] == ["authcheck"]
    assert second["extractor_args"]["youtube"] == opts["extractor_args"]["youtube"]
    assert second["proxy"] == opts["proxy"]
    assert second[COOKIE_MODE] is False
    assert all(call.kwargs["cancel_event"] is cancel for call in run.call_args_list)


@pytest.mark.parametrize("route", ROUTES)
def test_failed_authcheck_retry_is_bounded_and_keeps_stderr(extraction, route):
    svc, run, _ = extraction
    error = YtDlpExecutionError(1, ISSUE92)
    run.side_effect = error
    with pytest.raises(YtDlpExecutionError) as caught:
        getattr(svc, route)("https://www.youtube.com/@example")
    assert run.call_count == 2
    assert caught.value.stderr == ISSUE92


@pytest.mark.parametrize("route", ROUTES)
def test_explicit_skip_does_not_retry_again(extraction, monkeypatch, route):
    svc, run, _ = extraction
    mod = importlib.import_module("fluentytdl.youtube.youtube_service")
    monkeypatch.setitem(mod.config_manager.config, "playlist_skip_authcheck", True)
    run.side_effect = YtDlpExecutionError(1, ISSUE92)
    with pytest.raises(YtDlpExecutionError):
        getattr(svc, route)("https://www.youtube.com/@example")
    assert run.call_count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_plain_network_failure_does_not_trigger_authcheck_retry(extraction, route):
    svc, run, _ = extraction
    run.side_effect = YtDlpExecutionError(1, TIMEOUT.replace("WARNING:", "ERROR:"))
    with pytest.raises(YtDlpExecutionError):
        getattr(svc, route)("https://www.youtube.com/@example")
    assert run.call_count == 1
