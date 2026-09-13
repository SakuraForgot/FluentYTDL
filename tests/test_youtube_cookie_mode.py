"""Cookie opt-out is a request/task contract, not deletion of saved credentials."""

import importlib

import pytest

from fluentytdl.auth.auth_service import WebView2Account, auth_service
from fluentytdl.auth.cookie_sentinel import cookie_sentinel
from fluentytdl.core.config_manager import config_manager
from fluentytdl.download.workers import DownloadWorker, InfoExtractWorker, VRInfoExtractWorker
from fluentytdl.utils.youtube_request import COOKIE_MODE, SABR_SCOPE, request_scope
from fluentytdl.youtube.youtube_service import (
    YoutubeServiceOptions,
    YtDlpAuthOptions,
    freeze_youtube_options,
    youtube_service,
)
from fluentytdl.youtube.yt_dlp_cli import _maybe_mark_sabr_only, ydl_opts_to_cli_args

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for key, value in {
        "youtube_cookies_enabled": True,
        "pot_provider_enabled": False,
        "youtube_po_token": "",
        "download_dir": str(tmp_path),
        "parse_cache_ttl_seconds": 1800,
    }.items():
        monkeypatch.setitem(config_manager.config, key, value)
    monkeypatch.setattr(
        type(youtube_service), "_maybe_configure_youtube_js_runtime", lambda self, opts: None
    )
    monkeypatch.setattr(auth_service, "_webview2_accounts", {})
    monkeypatch.setattr(auth_service, "_current_webview2_account_ids", {})
    monkeypatch.setattr(auth_service, "_session_sabr_only", False)
    monkeypatch.setattr(auth_service, "_load_webview2_accounts", lambda: None)
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\ttest\n")
    monkeypatch.setattr(cookie_sentinel, "get_cookie_path_for_platform", lambda platform: cookie)
    youtube_service.invalidate_parse_cache()
    yield cookie
    youtube_service.invalidate_parse_cache()


@pytest.mark.parametrize("direct", [False, True])
def test_off_never_reads_or_injects_cookie(isolated, monkeypatch, direct):
    before = isolated.read_bytes()
    monkeypatch.setattr(
        type(youtube_service),
        "_count_youtube_related_cookies",
        lambda self, path: pytest.fail("anonymous request inspected cookies"),
    )
    opts = YoutubeServiceOptions(
        auth=YtDlpAuthOptions(
            cookies_file=str(isolated) if direct else None, use_youtube_cookies=False
        )
    )
    result = youtube_service.build_ydl_options(opts, url=URL)
    assert result[COOKIE_MODE] is False
    assert "cookiefile" not in result
    assert isolated.read_bytes() == before
    # Task merges cannot reintroduce a stale explicit cookie file.
    result["cookiefile"] = str(isolated)
    assert "--cookies" not in ydl_opts_to_cli_args(result)
    assert not any("__fluentytdl" in x for x in ydl_opts_to_cli_args(result))


def test_off_does_not_change_other_platform(isolated, monkeypatch):
    monkeypatch.setitem(config_manager.config, "youtube_cookies_enabled", False)
    monkeypatch.setattr(type(youtube_service), "_count_platform_cookies", lambda *a: 1)
    result = youtube_service.build_ydl_options(url="https://x.com/test/status/123")
    assert result["cookiefile"] == str(isolated)
    assert COOKIE_MODE not in result


def test_existing_workers_keep_mode_after_toggle(isolated, monkeypatch):
    options = freeze_youtube_options()
    worker = InfoExtractWorker(URL, options)
    vr = VRInfoExtractWorker(URL, options)
    task = DownloadWorker(URL, {COOKIE_MODE: True})
    monkeypatch.setitem(config_manager.config, "youtube_cookies_enabled", False)
    assert worker.options.auth.use_youtube_cookies is True
    assert vr.options.auth.use_youtube_cookies is True
    assert task.opts[COOKIE_MODE] is True
    assert freeze_youtube_options().auth.use_youtube_cookies is False


def test_anonymous_sabr_does_not_mark_selected_account(isolated, monkeypatch):
    account = WebView2Account(account_id="selected", display_name="Selected")
    auth_service._webview2_accounts["selected"] = account
    auth_service._current_webview2_account_ids["youtube"] = "selected"
    monkeypatch.setattr(
        auth_service, "_save_webview2_accounts", lambda: pytest.fail("account written")
    )
    _maybe_mark_sabr_only("YouTube is forcing SABR streaming", {COOKIE_MODE: False, SABR_SCOPE: ""})
    assert auth_service.get_youtube_sabr_only("")
    assert not account.sabr_only


def test_toggle_during_parse_rejects_old_cache_write(isolated, monkeypatch):
    module = importlib.import_module("fluentytdl.youtube.youtube_service")
    monkeypatch.setattr(module, "resolve_yt_dlp_exe", lambda *a: isolated)
    monkeypatch.setattr(type(youtube_service), "build_ydl_options", lambda *a, **k: {})
    seen = []

    def dump(*a, **k):
        seen.append(request_scope.get()[COOKIE_MODE])
        if len(seen) == 1:
            monkeypatch.setitem(config_manager.config, "youtube_cookies_enabled", False)
            config_manager.configChanged.emit("youtube_cookies_enabled", False)
        return {"id": "x", "entries": [{"id": "child"}], "_type": "playlist"}

    monkeypatch.setattr(module, "run_dump_single_json", dump)
    first = youtube_service.extract_info_for_dialog_sync(URL)
    assert first[COOKIE_MODE] is True
    assert first["entries"][0][COOKIE_MODE] is True
    assert not youtube_service._parse_cache
    second = youtube_service.extract_info_for_dialog_sync(URL)
    assert seen == [True, False]
    assert second[COOKIE_MODE] is False


def test_pot_opt_out_is_independent(isolated, monkeypatch):
    from fluentytdl.youtube.pot_manager import pot_manager

    monkeypatch.setitem(config_manager.config, "pot_provider_enabled", True)
    monkeypatch.setattr(pot_manager, "wait_until_ready", lambda: False)
    with pytest.raises(RuntimeError):
        youtube_service.build_ydl_options(freeze_youtube_options(enabled=False), url=URL)


def test_subtitle_late_parse_uses_task_mode(isolated, monkeypatch):
    from types import SimpleNamespace

    from fluentytdl.download import workers

    task = DownloadWorker(URL, {COOKIE_MODE: False})
    captured = []
    monkeypatch.setattr(
        workers,
        "youtube_service",
        SimpleNamespace(
            extract_info_sync=lambda url, options, **kw: (
                captured.append(options.auth.use_youtube_cookies) or {}
            ),
        ),
    )
    task._subtitle_info_for_resolution()
    assert captured == [False]


def test_settings_switch_signals_preserve_cookie_and_source(isolated, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    from qfluentwidgets import Theme, setTheme

    from fluentytdl.ui.settings_page import InfoBar, SettingsPage

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(InfoBar, "info", lambda *a, **k: None)
    import os
    from pathlib import Path

    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei", 10))
    page = SettingsPage.__new__(SettingsPage)
    QWidget.__init__(page)
    layout = QVBoxLayout(page)
    page._init_account_group(page, layout)
    page.youtubeCookiesCard.switchButton.setChecked(True)
    page.resize(1050, 850)
    page.show()
    app.processEvents()

    before = isolated.read_bytes()
    source = auth_service.current_source
    switch = page.youtubeCookiesCard.switchButton
    for expected in (False, True):
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert config_manager.get("youtube_cookies_enabled") is expected
        assert isolated.read_bytes() == before
        assert auth_service.current_source == source
        assert page.youtubeCookiesCard.y() < page.cookieModeCard.y()
        assert page.cookieModeCard.isEnabled()
    for theme in (Theme.LIGHT, Theme.DARK):
        setTheme(theme)
        page.setStyleSheet(
            "background: #202020;" if theme == Theme.DARK else "background: #f5f5f5;"
        )
        app.processEvents()
        assert page.grab().save(str(tmp_path / f"cookie-settings-{theme.value}.png"))
    setTheme(Theme.LIGHT)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_task_mode_roundtrip_and_legacy_restore(isolated, monkeypatch, tmp_path):
    import json

    from fluentytdl.storage.task_db import TaskDB

    module = importlib.import_module("fluentytdl.download.download_manager")
    database = TaskDB(tmp_path / "tasks.db")
    monkeypatch.setattr(module, "task_db", database)
    monkeypatch.setattr(module.db_writer, "enqueue_metadata", lambda *a: None)
    monkeypatch.setattr(module.download_manager, "active_workers", [])
    task = module.download_manager.create_worker(URL, {}, cached_info={COOKIE_MODE: False})
    saved = json.loads(database.get_task(task.db_id)["ydl_opts_json"])
    assert saved[COOKIE_MODE] is False
    restored = module.download_manager.create_worker(URL, saved, restore_db_id=task.db_id)
    assert restored.opts[COOKIE_MODE] is False  # global setting is still enabled
    legacy_id = database.insert_task(URL, {})
    monkeypatch.setitem(config_manager.config, "youtube_cookies_enabled", False)
    module.download_manager.create_worker(URL, {}, restore_db_id=legacy_id)
    assert json.loads(database.get_task(legacy_id)["ydl_opts_json"])[COOKIE_MODE] is False


def test_anonymous_reload_error_does_not_refresh_cookies(isolated, monkeypatch):
    module = importlib.import_module("fluentytdl.youtube.youtube_service")
    monkeypatch.setattr(module, "resolve_yt_dlp_exe", lambda *a: isolated)
    monkeypatch.setattr(
        type(youtube_service),
        "_try_refresh_cookie_for_reload_error",
        lambda *a: pytest.fail("anonymous recovery refreshed cookies"),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("The page needs to be reloaded")

    monkeypatch.setattr(module, "run_dump_single_json", fail)
    with pytest.raises(RuntimeError, match="reload"):
        youtube_service.extract_info_for_dialog_sync(URL, freeze_youtube_options(enabled=False))


def test_dialog_retry_keeps_original_mode(isolated, monkeypatch):
    from types import SimpleNamespace

    module = importlib.import_module("fluentytdl.ui.components.dialogs.download_config_window")
    captured = []
    signal = SimpleNamespace(connect=lambda *a: None)

    def worker(url, options, **kwargs):
        captured.append(options.auth.use_youtube_cookies)
        return SimpleNamespace(finished=signal, error=signal, start=lambda: None)

    monkeypatch.setattr(module, "InfoExtractWorker", worker)
    window = SimpleNamespace(
        worker=None,
        _error_label=None,
        retryWidget=SimpleNamespace(hide=lambda: None),
        _current_options=freeze_youtube_options(enabled=False),
        _vr_mode=False,
        url=URL,
        trace=None,
        _switch_to_state=lambda *a, **k: None,
        tr=lambda text: text,
        on_parse_success=lambda *a: None,
        on_parse_error=lambda *a: None,
    )
    module.DownloadConfigWindow._retry_parse_with_auth(window)
    assert captured == [False]  # global setting is enabled
