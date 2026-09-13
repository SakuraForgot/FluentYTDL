"""首次请求必须等待 POT，失败不能返回无 POT 的选项。"""

import importlib
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.core.config_manager import config_manager  # noqa: E402
from fluentytdl.youtube.youtube_service import YoutubeService  # noqa: E402

pot_mod = importlib.import_module("fluentytdl.youtube.pot_manager")


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(YoutubeService(), "_pot_plugin_checked", False, raising=False)
    provider = Mock()
    provider.wait_until_ready.return_value = True
    provider.get_extractor_args.return_value = (
        "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4417"
    )
    provider.verify_plugin_loadable.return_value = (True, "ok")
    monkeypatch.setattr(pot_mod, "pot_manager", provider)
    monkeypatch.setitem(config_manager.config, "pot_provider_enabled", True)
    return provider


def test_first_request_waits_and_injects(provider):
    opts = YoutubeService().build_ydl_options(url="https://www.youtube.com/watch?v=test")
    provider.wait_until_ready.assert_called_once()
    assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == ["http://127.0.0.1:4417"]
    assert opts["extractor_args"].get("youtube", {}).get("fetch_pot") != ["never"]


@pytest.mark.parametrize("failure", ["not_ready", "plugin", "missing_args"])
def test_failure_never_falls_back(provider, failure):
    if failure == "not_ready":
        provider.wait_until_ready.return_value = False
    elif failure == "plugin":
        provider.verify_plugin_loadable.return_value = (False, "plugin missing")
    else:
        provider.get_extractor_args.return_value = None
    with pytest.raises(RuntimeError):
        YoutubeService().build_ydl_options(url="https://www.youtube.com/watch?v=test")


def test_twitter_does_not_wait(provider):
    YoutubeService().build_ydl_options(url="https://x.com/example/status/123")
    provider.wait_until_ready.assert_not_called()


def test_explicitly_disabled_does_not_wait(provider, monkeypatch):
    monkeypatch.setitem(config_manager.config, "pot_provider_enabled", False)
    YoutubeService().build_ydl_options(url="https://www.youtube.com/watch?v=test")
    provider.wait_until_ready.assert_not_called()


@pytest.mark.parametrize("ready", [True, False])
def test_wait_reuses_worker_and_reports_result(monkeypatch, ready):
    mgr = pot_mod.POTManager.__new__(pot_mod.POTManager)
    mgr._warm_event = threading.Event()
    mgr._lock = threading.Lock()
    mgr._warm_thread = Mock()
    mgr._warm_thread.join.side_effect = lambda **kw: mgr._warm_event.set() if ready else None
    monkeypatch.setattr(mgr, "is_running", lambda: True)
    warm = Mock()
    monkeypatch.setattr(mgr, "ensure_warm_async", warm)
    assert mgr.wait_until_ready(timeout=0.25) is ready
    warm.assert_called_once()
    mgr._warm_thread.join.assert_called_once_with(timeout=0.25)
