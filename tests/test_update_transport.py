import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from datetime import UTC

from fluentytdl.core.update_transport import (
    CheckSession,
    Transport,
    UpdateError,
    check_response,
    classify,
    make_session,
)


def release():
    return {"tag_name": "v1", "assets": [{"name": "a b.exe"}]}


def test_github_limit_switches_once_and_never_changes_preference():
    from datetime import datetime

    transport = Mock()
    transport.get_json.side_effect = [
        UpdateError("update_rate_limited"),
        {
            **release(),
            "repository": "yt-dlp/yt-dlp",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    ]
    check = CheckSession("github", transport)
    assert "%20" in check.release("yt-dlp")["assets"][0]["browser_download_url"]
    assert check.source == "cloudflare" and check.preferred == "github"
    transport.get_json.side_effect = UpdateError("update_service")
    with pytest.raises(UpdateError):
        check.release("yt-dlp")
    assert transport.get_json.call_count == 3
    with pytest.raises(UpdateError):
        check.release("yt-dlp")
    assert transport.get_json.call_count == 3


def test_cf_falls_back_to_github_but_github_network_errors_do_not_switch():
    transport = Mock()
    transport.get_json.side_effect = [UpdateError("update_timeout"), release()]
    check = CheckSession("cloudflare", transport)
    assert check.release("yt-dlp")["tag_name"] == "v1"
    assert check.source == "github"
    transport = Mock()
    transport.get_json.side_effect = UpdateError("update_timeout")
    with pytest.raises(UpdateError):
        CheckSession("github", transport).release("yt-dlp")
    assert transport.get_json.call_count == 1


@pytest.mark.parametrize(
    "status,headers,body,limited",
    [
        (403, {}, "forbidden", False),
        (403, {"x-ratelimit-remaining": "0"}, "", True),
        (403, {}, "secondary rate limit", True),
        (429, {}, "", True),
        (404, {}, "", False),
    ],
)
def test_rate_limit_evidence(status, headers, body, limited):
    response = Mock(ok=False, status_code=status, headers=headers, text=body)
    with pytest.raises(UpdateError) as e:
        check_response(response)
    assert (e.value.code == "update_rate_limited") == limited


def test_bounded_retry_and_certificate_classification():
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = False
    transport = Transport(session=Mock(), cancel=event)
    operation = Mock(side_effect=requests.Timeout())
    with pytest.raises(UpdateError):
        transport.retry(operation)
    assert operation.call_count == 3 and event.wait.call_count == 2
    assert not classify(requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")).retryable
    assert classify(requests.exceptions.SSLError("unexpected EOF")).retryable


def test_proxy_off_ignores_environment_and_socks_uses_remote_dns():
    off = make_session("off")
    assert not off.trust_env and not off.proxies
    socks = make_session("socks5", "socks5://127.0.0.1:1080")
    assert socks.proxies["https"] == "socks5h://127.0.0.1:1080"
    assert socks.verify is True


def test_cancel_prevents_network_request():
    event = Mock()
    event.is_set.return_value = True
    operation = Mock()
    with pytest.raises(UpdateError):
        Transport(session=Mock(), cancel=event).retry(operation)
    operation.assert_not_called()


def test_incomplete_download_retries_then_removes_partial_file(tmp_path):
    response = Mock(ok=True, headers={"Content-Length": "8"})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.side_effect = lambda *_: iter([b"short"])
    session = Mock()
    session.get.return_value = response
    event = Mock()
    event.is_set.return_value = event.wait.return_value = False
    destination = tmp_path / "update.exe"
    with pytest.raises(UpdateError) as exc:
        Transport(session=session, cancel=event).download("https://github.com/file", destination)
    assert exc.value.code == "update_truncated"
    assert session.get.call_count == 3
    assert not destination.exists()


def test_hash_mismatch_stops_before_install_without_retry(tmp_path):
    response = Mock(ok=True, headers={"Content-Length": "3"})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b"abc"]
    session = Mock()
    session.get.return_value = response
    destination = tmp_path / "update.exe"
    with pytest.raises(UpdateError) as exc:
        Transport(session=session).download("https://github.com/file", destination, "0" * 64)
    assert exc.value.code == "update_hash"
    assert session.get.call_count == 1 and not destination.exists()
