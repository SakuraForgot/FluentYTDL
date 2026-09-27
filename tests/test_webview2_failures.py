"""Login failures must retain the actual failing layer."""

import builtins
import queue
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fluentytdl.auth.providers import webview2_provider as module  # noqa: E402


# pythonnet(clr) 先于 pywebview 被探测，所以 CLR 缺失时先冒出 pythonnet_load_failed。
# Linux 上没有 pythonnet 这条链，import 走到 pywebview 才失败 → pywebview_load_failed，
# 断言的错误码顺序只在 Windows 运行时成立。
@pytest.mark.windows_only
def test_bridge_failure_returned_before_window_creation(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "clr":
            raise RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize")
        if name == "webview":
            raise AssertionError("must not initialize browser after CLR failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    channel = Mock()
    module._webview_subprocess(channel, "https://www.youtube.com/", str(tmp_path), 1, False, 1)
    result = channel.put.call_args.args[0]
    assert result["code"] == "pythonnet_load_failed"
    assert "Loader.Initialize" in result["error"]
    assert "缺少 Microsoft Edge" not in result["error"]


def test_provider_preserves_error_and_resets_between_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "is_webview2_runtime_available", lambda: (True, "152"))
    process = Mock()
    process.is_alive.return_value = False
    monkeypatch.setattr(module.multiprocessing, "Process", Mock(return_value=process))
    monkeypatch.setattr(module.multiprocessing, "Queue", Mock())
    provider = module.WebView2CookieProvider()
    error = {"error": "DLL load failed", "code": "pythonnet_load_failed", "stage": "pythonnet"}
    monkeypatch.setattr(provider, "_wait_for_result", lambda *args: error)
    assert provider.extract_cookies(storage_path=str(tmp_path)) is None
    assert provider.get_last_error() == error
    copy = provider.get_last_error()
    copy.clear()
    assert provider.get_last_error() == error
    monkeypatch.setattr(provider, "_wait_for_result", lambda *args: {"cookies": [{"name": "test"}]})
    assert provider.extract_cookies(storage_path=str(tmp_path))
    assert provider.get_last_error() == {}


def test_unexpected_exit_is_not_runtime_missing():
    channel = Mock()
    channel.get.side_effect = queue.Empty
    process = Mock()
    process.is_alive.return_value = False
    result = module.WebView2CookieProvider._wait_for_result(channel, process, 2)
    assert result["code"] == "subprocess_exited"


def test_dead_process_with_queued_result_is_success():
    channel = Mock()
    expected = {"cookies": [{"name": "test"}]}
    channel.get.side_effect = [queue.Empty, expected]
    process = Mock()
    process.is_alive.return_value = False
    assert module.WebView2CookieProvider._wait_for_result(channel, process, 2) == expected
