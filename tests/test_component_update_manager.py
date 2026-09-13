"""Tests for ComponentUpdateManager — version parsing, channels, and logic."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── Pure function tests (no Qt required) ──────────────────────────────────


class TestParseVersion:
    """Test _parse_version version comparison logic."""

    @staticmethod
    def _parse(ver: str) -> tuple[int, ...]:
        from fluentytdl.core.component_update_manager import _parse_version

        return _parse_version(ver)

    def test_newer_is_greater(self):
        assert self._parse("3.0.18") > self._parse("3.0.16")

    def test_same_is_not_greater(self):
        assert not (self._parse("3.0.16") > self._parse("3.0.16"))

    def test_major_version_matters(self):
        assert self._parse("4.0.0") > self._parse("3.9.9")

    def test_strips_v_prefix(self):
        assert self._parse("v3.0.18") == (3, 0, 18)

    def test_strips_prerelease_suffix(self):
        assert self._parse("3.5.6-rc.1") == (3, 5, 6)
        assert self._parse("3.6.0-beta.2") == (3, 6, 0)

    def test_prerelease_compares_equal_to_its_release(self):
        # 数值相同 → 不会把 rc 误判为比正式版新
        assert self._parse("3.5.6-rc.1") == self._parse("3.5.6")

    # ── 3.5.5 之前的旧前缀格式，升级路径上仍会遇到 ──
    def test_strips_legacy_v_dash_prefix(self):
        assert self._parse("v-3.0.19") == (3, 0, 19)

    def test_legacy_v_dash_ordering(self):
        assert self._parse("v-3.0.19") > self._parse("v-3.0.18")

    def test_strips_legacy_pre_prefix(self):
        assert self._parse("pre-3.0.18") == (3, 0, 18)

    def test_strips_legacy_beta_prefix(self):
        assert self._parse("beta-0.0.5") == (0, 0, 5)

    def test_legacy_and_new_format_compare(self):
        """老客户端的 v-3.5.5 与新清单的 3.5.6 必须能正确比较。"""
        assert self._parse("3.5.6") > self._parse("v-3.5.5")


class TestGetUpdateChannel:
    """Installed version never opts users into prereleases."""

    @staticmethod
    def _channel(version: str) -> str:
        from fluentytdl.core.component_update_manager import _get_update_channel

        with (
            patch("fluentytdl.__version__", version),
            patch(
                "fluentytdl.core.component_update_manager.config_manager.get", return_value="stable"
            ),
        ):
            return _get_update_channel()

    @pytest.mark.parametrize("version", ["3.5.5", "3.0.16", "4.0.0"])
    def test_bare_version_is_stable(self, version):
        assert self._channel(version) == "stable"

    @pytest.mark.parametrize("version", ["3.5.6-rc.1", "3.6.0-beta.1"])
    def test_prerelease_suffix_defaults_stable(self, version):
        assert self._channel(version) == "stable"

    def test_v_tag_form_is_stable(self):
        """容忍误写成 tag 形式的版本号，不应把用户判成 locked。"""
        assert self._channel("v3.5.5") == "stable"

    # ── 3.5.5 之前的旧前缀格式：原地升级时 VERSION 文件可能残留 ──
    def test_legacy_v_dash_is_stable(self):
        assert self._channel("v-3.0.16") == "stable"

    @pytest.mark.parametrize("version", ["pre-3.0.18", "beta-0.0.5"])
    def test_legacy_prerelease_defaults_stable(self, version):
        assert self._channel(version) == "stable"

    def test_dev_build_defaults_stable(self):
        """通道默认值不取决于当前开发版本。"""
        assert self._channel("0.0.0-dev") == "stable"


# ── PySide6 signal tests (require QApplication) ──────────────────────────

HAS_PYSIDE6 = True
try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    HAS_PYSIDE6 = False

requires_windows_qt = pytest.mark.skipif(
    not HAS_PYSIDE6 or not sys.platform == "win32",
    reason="PySide6 and Windows required for signal tests",
)


@pytest.fixture(scope="module")
def qapp():
    """Create a QApplication for signal tests."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def manager(qapp):
    """Create a fresh ComponentUpdateManager for each test."""
    from fluentytdl.core.component_update_manager import ComponentUpdateManager

    return ComponentUpdateManager()


@requires_windows_qt
class TestUpdateAccess:
    """Installed prereleases retain access to updates."""

    def test_prerelease_is_not_locked(self, manager):
        with patch(
            "fluentytdl.core.component_update_manager._get_update_channel",
            return_value="pre",
        ):
            assert manager.is_locked() is False
            assert manager.is_beta() is False

    def test_is_locked_false_for_stable(self, manager):
        with patch(
            "fluentytdl.core.component_update_manager._get_update_channel",
            return_value="stable",
        ):
            assert manager.is_locked() is False
            assert manager.is_beta() is False


@requires_windows_qt
class TestCompareAppVersion:
    """Test _compare_app_version with mocked manifest."""

    @pytest.mark.parametrize(
        ("version", "flag"),
        [("3.0.18", True), ("3.0.18-rc.1", False), ("3.0.18-beta.1", False)],
    )
    def test_stable_filters_prerelease(self, manager, qapp, version, flag):
        """Stable channel should ignore prerelease manifests."""
        manager._manifest = {
            "app_version": version,
            "_is_prerelease": flag,
            "_release_body": "",
            "components": {"app-core": {"url": "", "sha256": ""}},
        }

        signals_received = []
        manager.app_no_update.connect(lambda: signals_received.append(True))

        with (
            patch(
                "fluentytdl.core.component_update_manager._get_update_channel",
                return_value="stable",
            ),
            patch("fluentytdl.__version__", "3.0.17"),
        ):
            manager._compare_app_version()

        assert len(signals_received) == 1

    def test_skipped_version_suppresses(self, manager, qapp):
        """Skipped version should emit app_no_update."""
        manager._app_check_silent = True
        manager._manifest = {
            "app_version": "3.0.18",
            "_is_prerelease": False,
            "_release_body": "",
            "components": {"app-core": {"url": "", "sha256": ""}},
        }

        signals_received = []
        manager.app_no_update.connect(lambda: signals_received.append(True))

        with (
            patch(
                "fluentytdl.core.component_update_manager._get_update_channel",
                return_value="stable",
            ),
            patch("fluentytdl.__version__", "3.0.17"),
            patch(
                "fluentytdl.core.component_update_manager.config_manager",
                get=lambda k, d=None: "3.0.18" if k == "skipped_stable_version" else d,
            ),
        ):
            manager._compare_app_version()

        assert len(signals_received) == 1
