"""Channel selection, release ordering, source isolation and UI consent."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="channel_tests_"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from PySide6.QtWidgets import QApplication

from fluentytdl.core.component_update_manager import ComponentUpdateManager, _get_update_channel
from fluentytdl.core.update_transport import CheckSession, UpdateError
from fluentytdl.utils.app_version import version_key


@pytest.fixture
def manager():
    app = QApplication.instance() or QApplication([])
    manager = ComponentUpdateManager()
    yield manager
    assert app


@pytest.mark.parametrize(
    "current,latest,channel,available",
    [
        ("3.6.0-rc.1", "3.6.0-rc.2", "pre", True),
        ("3.6.0-rc.2", "3.6.0-rc.10", "pre", True),
        ("3.6.0-rc.10", "3.6.0", "stable", True),
        ("3.6.0", "3.7.0-rc.1", "stable", False),
        ("3.7.0-rc.1", "3.6.0", "stable", False),
        ("3.6.0", "3.6.0-rc.10", "pre", False),
        ("3.6.0-rc.2", "3.6.0-rc.2", "pre", False),
        ("pre-3.6.0", "3.6.0-rc.1", "pre", True),
        ("3.6.0-beta.1", "3.6.0", "stable", True),
    ],
)
def test_upgrade_matrix(manager, current, latest, channel, available):
    manager._manifest = {"app_version": latest, "components": {}}
    found, empty = Mock(), Mock()
    manager.app_update_available.connect(found)
    manager.app_no_update.connect(empty)
    with (
        patch("fluentytdl.__version__", current),
        patch(
            "fluentytdl.core.component_update_manager.config_manager.get",
            side_effect=lambda k, d=None: channel if k == "app_update_channel" else d,
        ),
    ):
        manager._compare_app_version()
    assert found.called == available
    assert empty.called != available
    if available:
        assert found.call_args.args[0]["is_prerelease"] == ("-rc." in latest)


def test_invalid_channel_defaults_stable():
    with patch(
        "fluentytdl.core.component_update_manager.config_manager.get", return_value="unexpected"
    ):
        assert _get_update_channel() == "stable"
    assert version_key("3.6.0-beta.9") < version_key("3.6.0-rc.1") < version_key("3.6.0")


def test_pre_source_selects_highest_eligible_release():
    transport = Mock()
    versions = ["3.6.0-rc.2", "3.6.0-rc.10", "3.7.0-beta.1", "3.5.9"]
    releases = [
        {"tag_name": f"v{v}", "assets": [{"name": "update-manifest.json"}]} for v in versions
    ]
    releases.append(
        {"tag_name": "v9.0.0", "draft": True, "assets": [{"name": "update-manifest.json"}]}
    )
    transport.get_json.side_effect = [
        releases,
        {"app_version": "3.6.0-rc.10", "release_tag": "v3.6.0-rc.10", "components": {}},
    ]
    assert CheckSession(transport=transport).manifest("pre")["app_version"] == "3.6.0-rc.10"
    assert "/v3.6.0-rc.10/" in transport.get_json.call_args.args[0]


def test_pre_rejects_old_cf_server_stable_response_and_falls_back():
    from datetime import UTC, datetime

    transport = Mock()
    transport.get_json.side_effect = [
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "manifest": {"app_version": "1.0.0", "components": {}},
        },
        [],
    ]
    check = CheckSession("cloudflare", transport)
    with pytest.raises(UpdateError):
        check.manifest("pre")
    assert check.source == "github" and check.switched


def test_channel_switch_clears_cached_manifest_and_rechecks(manager):
    manager._manifest = {"app_version": "1.0.0"}
    with (
        patch("fluentytdl.core.component_update_manager.config_manager") as config,
        patch.object(manager, "check_app_update") as check,
    ):
        config.get.return_value = "stable"
        manager.set_update_channel("pre")
        config.set.assert_called_once_with("app_update_channel", "pre")
        assert manager.manifest is None
        check.assert_called_once_with(silent=False)


def test_ui_requires_consent_and_reverts_cancel(manager):
    from fluentytdl.ui.components.settings import app_update_card as ui

    with (
        patch.object(ui, "component_update_manager", manager),
        patch.object(manager, "get_update_channel", return_value="stable"),
        patch.object(ui, "MessageBox") as box,
    ):
        card = ui.AppUpdateSettingCard()
        request = Mock()
        # Observe without launching an actual check.
        manager.channel_change_requested.disconnect()
        manager.channel_change_requested.connect(request)
        box.return_value.exec.return_value = False
        card.channelCombo.setCurrentIndex(1)
        assert card.channelCombo.currentIndex() == 0
        request.assert_not_called()
        box.return_value.exec.return_value = True
        card.channelCombo.setCurrentIndex(1)
        request.assert_called_once_with("pre")
        card.deleteLater()


@pytest.mark.parametrize("busy", ["_manifest_worker", "_download_worker"])
def test_busy_channel_change_does_not_modify_config(manager, busy):
    setattr(manager, busy, Mock())
    with patch("fluentytdl.core.component_update_manager.config_manager") as config:
        config.get.return_value = "stable"
        manager.set_update_channel("pre")
        config.set.assert_not_called()


@pytest.mark.parametrize(
    "silent,skip_channel,expected",
    [(True, "pre", False), (False, "pre", True), (True, "stable", True)],
)
def test_skipped_rc_is_scoped_and_manual_check_can_retry(manager, silent, skip_channel, expected):
    manager._manifest = {"app_version": "3.6.0-rc.2", "components": {}}
    manager._app_check_silent = silent
    found = Mock()
    manager.app_update_available.connect(found)
    with (
        patch("fluentytdl.__version__", "3.6.0-rc.1"),
        patch(
            "fluentytdl.core.component_update_manager.config_manager.get",
            side_effect=lambda k, d=None: {
                "app_update_channel": "pre",
                f"skipped_{skip_channel}_version": "3.6.0-rc.2",
            }.get(k, d),
        ),
    ):
        manager._compare_app_version()
    assert found.called == expected


def test_manifest_worker_captures_selected_channel():
    from fluentytdl.core.component_update_manager import _ManifestWorker

    session = Mock()
    session.manifest.return_value = {"app_version": "3.6.0-rc.2"}
    with patch("fluentytdl.core.component_update_manager.config_manager.get", return_value="pre"):
        worker = _ManifestWorker(check_session=session)
    worker.run()
    session.manifest.assert_called_once_with("pre")


def test_new_channel_labels_have_english_translations():
    from PySide6.QtCore import QTranslator

    translator = QTranslator()
    assert translator.load(str(Path(__file__).parents[1] / "assets/locales/fluentytdl_en.qm"))
    assert translator.translate("AppUpdateSettingCard", "稳定版 (stable)") == "Stable (stable)"
    assert translator.translate("AppUpdateSettingCard", "切换到 pre") == "Switch to pre"
