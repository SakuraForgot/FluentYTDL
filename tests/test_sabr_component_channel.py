import hashlib
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fluentytdl.core.update_transport import CheckSession, UpdateError
from fluentytdl.utils import ytdlp_runtime

dm = importlib.import_module("fluentytdl.core.dependency_manager")
REPO = ytdlp_runtime.SABR_REPOSITORY


def release(tag="sabr-new", published="2026-09-28T12:00:00Z", **extra):
    return {
        "tag_name": tag,
        "published_at": published,
        "prerelease": True,
        "assets": [{"name": name} for name in ("yt-dlp.exe", "BUILD_INFO.json", "SHA256SUMS.txt")],
        **extra,
    }


def metadata(**extra):
    return {
        "release_repository": REPO,
        "tag": "sabr-new",
        "version": "2026.09.28",
        "architecture": "Windows x64",
        "variant": "sabr-test",
        "source_sha": "a" * 40,
        "exe_sha256": "b" * 64,
        **extra,
    }


def sabr_worker(monkeypatch, transport):
    monkeypatch.setattr(
        dm.config_manager,
        "get",
        lambda key, default=None: "sabr" if key == "ytdlp_channel" else default,
    )
    return dm.UpdateCheckerWorker("yt-dlp", None, CheckSession("cloudflare", transport))


def test_prerelease_latest_uses_published_time_and_ignores_drafts():
    transport = Mock()
    transport.get_json.return_value = [
        release("sabr-old", "2026-09-27T12:00:00Z"),
        release(draft=True, published="2026-10-01T00:00:00Z"),
        release(),
        release("sabr-incomplete", "2026-10-02T00:00:00Z", assets=[]),
    ]
    check = CheckSession("cloudflare", transport)
    result = check.release("yt-dlp", "sabr")
    assert result["tag_name"] == "sabr-new"
    assert (
        result["assets"][0]["browser_download_url"]
        == f"https://github.com/{REPO}/releases/download/sabr-new/yt-dlp.exe"
    )
    assert transport.get_json.call_args.args[0].endswith("/releases?per_page=100")
    assert check.source == "cloudflare" and not check.switched


def test_sabr_release_download_url_and_hash(monkeypatch):
    transport = Mock()
    transport.get_json.side_effect = [[release()], metadata()]
    worker = sabr_worker(monkeypatch, transport)
    monkeypatch.setattr(
        worker,
        "_overlay_manifest",
        lambda *args: pytest.fail("Official manifest must not overlay SABR"),
    )
    result = worker._get_remote_version("yt-dlp")
    assert result == dm.RemoteVersion(
        "2026.09.28",
        "sabr",
        f"https://github.com/{REPO}/releases/download/sabr-new/yt-dlp.exe",
        "b" * 64,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"exe_sha256": ""},
        {"tag": "different"},
        {"release_repository": "yt-dlp/yt-dlp"},
        {"architecture": "Linux"},
    ],
)
def test_invalid_metadata_never_falls_back_to_official(monkeypatch, overrides):
    transport = Mock()
    transport.get_json.side_effect = [[release()], metadata(**overrides)]
    worker = sabr_worker(monkeypatch, transport)
    with pytest.raises(UpdateError):
        worker._get_remote_version("yt-dlp")
    assert transport.get_json.call_count == 2


@pytest.mark.parametrize("changed", [False, True])
def test_same_day_sabr_builds_compare_actual_bytes(monkeypatch, tmp_path, changed):
    binary = tmp_path / "yt-dlp.exe"
    binary.write_bytes(b"installed-build")
    worker = sabr_worker(monkeypatch, Mock())
    worker.manager = SimpleNamespace(
        resolve_exe=lambda _: (binary, "bundled"), get_exe_path=lambda _: binary
    )
    digest = hashlib.sha256(b"next-build" if changed else binary.read_bytes()).hexdigest()
    monkeypatch.setattr(worker, "_get_local_version", lambda *a: ("2026.09.28", "sabr"))
    monkeypatch.setattr(
        worker,
        "_get_remote_version",
        lambda _: dm.RemoteVersion(
            "2026.09.28", "sabr", "https://example.invalid/core.exe", digest
        ),
    )
    results, errors = [], []
    worker.finished_signal.connect(lambda key, result: results.append(result))
    worker.error_signal.connect(lambda *args: errors.append(args))
    worker.run()
    assert not errors
    assert results[0]["update_available"] is changed


def test_stale_official_check_is_discarded(monkeypatch):
    monkeypatch.setattr(dm.config_manager, "get", lambda *args: "sabr")
    manager = dm.DependencyManager()
    checks = Mock()
    monkeypatch.setattr(manager, "check_update", checks)
    manager._on_check_finished("yt-dlp", {"requested_channel": "stable", "url": "official"})
    assert manager.components["yt-dlp"].download_url is None
    checks.assert_called_once_with("yt-dlp")


def test_install_rechecks_cached_link_after_channel_switch(monkeypatch):
    monkeypatch.setattr(dm.config_manager, "get", lambda *args: "sabr")
    manager = dm.DependencyManager()
    manager.components["yt-dlp"].download_url = "https://example.invalid/official.exe"
    manager.components["yt-dlp"].latest_channel = "stable"
    checks = Mock()
    monkeypatch.setattr(manager, "check_update", checks)
    manager.install_component("yt-dlp")
    assert manager.components["yt-dlp"].download_url is None
    checks.assert_called_once_with("yt-dlp")


def test_runtime_recognizes_sabr_repository_without_sidecar(monkeypatch, tmp_path):
    binary = tmp_path / "yt-dlp.exe"
    binary.touch()
    monkeypatch.setattr(
        ytdlp_runtime.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout="", stderr=f"[debug] yt-dlp version {REPO}@2026.09.28 [abcdef] (sabr-test*)"
        ),
    )
    assert ytdlp_runtime.probe_version(binary) == ytdlp_runtime.RuntimeVersion(
        "2026.09.28", "sabr", "ok"
    )


def test_parallel_sabr_progress_preserves_audio_video():
    from fluentytdl.download.output_parser import YtDlpOutputParser

    parser = YtDlpOutputParser()
    audio = parser.parse_line("1: FLUENTYTDL|download|100|200|NA|1|1|none|opus|mkv|C:\\a.mkv")
    video = parser.parse_line("2: FLUENTYTDL|download|300|400|NA|1|1|av01|none|mkv|C:\\v.mkv")
    assert audio.type == video.type == "progress"
    assert audio.progress.downloaded_bytes == 100
    assert video.progress.downloaded_bytes == 300
    assert audio.progress.info_dict["acodec"] == "opus"
    assert video.progress.info_dict["vcodec"] == "av01"


@pytest.mark.windows_gui
@pytest.mark.parametrize("accepted", [False, True])
def test_real_warning_dialog_cancel_and_accept(monkeypatch, accepted):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from qfluentwidgets import ComboBox, MessageBox

    from fluentytdl.ui import settings_page as ui

    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.resize(900, 600)
    combo = ComboBox(window)
    combo.addItems(list(ytdlp_runtime.YTDLP_CHANNELS))
    combo.setCurrentIndex(3)
    values = {"ytdlp_channel": "stable"}
    monkeypatch.setattr(
        ui.config_manager, "get", lambda key, default=None: values.get(key, default)
    )
    monkeypatch.setattr(ui.config_manager, "set", lambda key, value: values.update({key: value}))
    monkeypatch.setattr(ui.dependency_manager, "is_installing", lambda _: False)
    checks = Mock()
    monkeypatch.setattr(ui.dependency_manager, "check_update", checks)
    monkeypatch.setattr(ui.InfoBar, "info", Mock())
    dialogs = []

    def dialog(title, content, parent):
        # VR 通道已在 SABR 核心下打通：文案不再无条件说 VR 受限，只留直播受限。
        assert "VR 现已支持" in content
        assert "仅直播解析仍暂时受限" in content
        box = MessageBox(title, content, parent)
        dialogs.append(box)
        QTimer.singleShot(0, box.yesButton.click if accepted else box.cancelButton.click)
        return box

    monkeypatch.setattr(ui, "MessageBox", dialog)
    page = SimpleNamespace(
        tr=lambda text: text,
        window=lambda: window,
        ytDlpChannelCard=SimpleNamespace(comboBox=combo),
    )
    ui.SettingsPage._on_ytdlp_channel_changed(page, 3)
    assert len(dialogs) == 1
    assert values["ytdlp_channel"] == ("sabr" if accepted else "stable")
    assert combo.currentIndex() == (3 if accepted else 0)
    assert checks.call_count == int(accepted)
    window.close()
    app.processEvents()
