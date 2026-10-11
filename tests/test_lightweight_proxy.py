"""Subtitle/cover subprocesses must use the same proxy choice as video downloads."""

import importlib

import pytest
from PySide6.QtWidgets import QApplication

from fluentytdl.download import workers
from fluentytdl.utils.youtube_request import COOKIE_MODE

cli = importlib.import_module("fluentytdl.youtube.yt_dlp_cli")
_UNSET = object()


@pytest.mark.parametrize("output", ["writesubtitles", "writethumbnail"])
@pytest.mark.parametrize(
    "base_proxy,task_proxy,expected",
    [
        ("http://127.0.0.1:19091", _UNSET, "http://127.0.0.1:19091"),
        ("socks5://127.0.0.1:19091", _UNSET, "socks5://127.0.0.1:19091"),
        ("", _UNSET, ""),
        (_UNSET, _UNSET, None),
        ("http://127.0.0.1:19091", "", ""),
        ("", "socks5://127.0.0.1:19092", "socks5://127.0.0.1:19092"),
    ],
    ids=["http", "socks5", "off", "system", "task-off", "task-proxy"],
)
def test_lightweight_proxy_reaches_subprocess(
    tmp_path, monkeypatch, output, base_proxy, task_proxy, expected
):
    app = QApplication.instance() or QApplication([])
    base = {"extractor_args": {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}}}
    if base_proxy is not _UNSET:
        base["proxy"] = base_proxy
    opts = {"skip_download": True, output: True, COOKIE_MODE: False}
    if task_proxy is not _UNSET:
        opts["proxy"] = task_proxy
    monkeypatch.setattr(type(workers.youtube_service), "build_ydl_options", lambda *a, **kw: base)
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: tmp_path / "yt-dlp.exe")
    monkeypatch.setattr(cli, "prepare_yt_dlp_env", lambda **kw: {})
    worker = workers.DownloadWorker("https://www.youtube.com/watch?v=jNQXAC9IVRw", opts)
    monkeypatch.setattr(worker, "_resolve_subtitle_prefs", lambda opts: None)
    monkeypatch.setattr(worker, "_fastpath_open_staging", lambda opts: None)
    monkeypatch.setattr(worker, "_fastpath_paths_args", lambda opts: [])
    commands = []

    class Captured(BaseException):
        pass

    def capture(command, **kwargs):
        commands.append(command)
        raise Captured

    monkeypatch.setattr(workers, "ytdlp_popen", capture)
    try:
        with pytest.raises(Captured):
            worker._run_lightweight_extract()
        command = commands[0]
        if expected is None:
            assert "--proxy" not in command
        else:
            assert command.count("--proxy") == 1
            assert command[command.index("--proxy") + 1] == expected
        assert "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416" in command
    finally:
        worker.deleteLater()
        app.processEvents()
