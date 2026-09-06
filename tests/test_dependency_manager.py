"""组件更新链路的两条核心判定：制品来源，以及 yt-dlp 的频道判定。

这两块以前各有一个用户可见的 bug：

1. **制品来源反了。** `_get_remote_version()` 只要在清单里找到条目就直接 return，
   连 `url` 是空串都照样 return，而 `scripts/generate_manifest.py` 给每个 `bin/*`
   写死 `"url": ""` —— 下面那段能用的 GitHub API 分支被完全遮住，`download_url`
   恒为空，deno 点「立即更新」只会得到"没能解析出下载地址"。
   现在的模型是**上游 API 定版本，清单只在版本精确相等时补制品元数据**。

2. **频道被塞进版本字符串。** 比较靠 `lstrip("vn")` + 补零元组，
   nightly→stable 这种版本号变小的切换根本判不出来；而 `channel_switched`
   那条分支是死代码（`_get_local_version()` 从不返回这个值）。
   现在 channel 是独立字段，判据是「本地频道 vs 配置频道」。

`_compare()` / `_overlay_manifest()` 都不碰 Qt 事件循环，直接在裸实例上调 ——
`QThread.__init__` 不需要 QApplication，但导入链会落地配置文件，所以先改数据目录。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-depmgr-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.core.dependency_manager import (  # noqa: E402
    RemoteVersion,
    UpdateCheckerWorker,
)

API = RemoteVersion(
    version="2.9.5",
    url="https://api.example.com/deno-2.9.5.zip",
    sha256="a" * 64,
)


@pytest.fixture
def worker():
    """裸 worker：`_overlay_manifest` / `_compare` 都不用 manager 和事件循环。"""
    return UpdateCheckerWorker("deno", None)  # type: ignore[arg-type]


def _patch_manifest(monkeypatch, entry: dict | None) -> None:
    """替掉 `component_update_manager.get_manifest_component` 的返回值。"""
    from fluentytdl.core import component_update_manager as cum

    monkeypatch.setattr(
        cum.component_update_manager,
        "get_manifest_component",
        lambda _key: entry,
        raising=True,
    )


# ── 制品来源：API 定版本，清单只补下载元数据 ──────────────────────────


class TestArtifactSource:
    def test_empty_manifest_url_falls_back_to_api(self, worker, monkeypatch):
        """钉住 deno 那个 bug：清单 version 命中但 url 是空串，必须用 API 的地址。"""
        _patch_manifest(monkeypatch, {"version": "2.9.5", "url": "", "sha256": ""})

        got = worker._overlay_manifest("deno", API)

        assert got.url == API.url
        assert got.sha256 == API.sha256

    def test_manifest_version_differs_uses_api(self, worker, monkeypatch):
        """清单是构建那一刻的快照，版本对不上时它的哈希属于别的版本，不能用。"""
        _patch_manifest(
            monkeypatch,
            {
                "version": "2.9.4",
                "url": "https://manifest.example.com/deno-2.9.4.zip",
                "sha256": "b" * 64,
            },
        )

        got = worker._overlay_manifest("deno", API)

        assert got.url == API.url
        assert got.sha256 == API.sha256

    def test_manifest_hit_overrides_url_and_sha256(self, worker, monkeypatch):
        """版本精确相等且带 url —— 用清单的，省一次 GitHub API。"""
        m_url = "https://manifest.example.com/deno-2.9.5.zip"
        _patch_manifest(monkeypatch, {"version": "2.9.5", "url": m_url, "sha256": "c" * 64})

        got = worker._overlay_manifest("deno", API)

        assert got.url == m_url
        assert got.sha256 == "c" * 64
        assert got.version == "2.9.5"  # 版本判定权始终在 API

    def test_manifest_without_sha256_keeps_api_hash(self, worker, monkeypatch):
        """清单只给了地址没给哈希时，别把 API 拿到的哈希丢掉 —— 否则下载不校验。"""
        m_url = "https://manifest.example.com/deno-2.9.5.zip"
        _patch_manifest(monkeypatch, {"version": "2.9.5", "url": m_url, "sha256": ""})

        got = worker._overlay_manifest("deno", API)

        assert got.url == m_url
        assert got.sha256 == API.sha256

    def test_no_manifest_entry_uses_api(self, worker, monkeypatch):
        _patch_manifest(monkeypatch, None)

        assert worker._overlay_manifest("deno", API).url == API.url

    def test_unknown_remote_is_untouched(self, worker, monkeypatch):
        """API 都没查到最新版本时，清单不该趁机把自己的版本顶上来。"""
        _patch_manifest(
            monkeypatch,
            {"version": "2.9.5", "url": "https://manifest.example.com/x.zip", "sha256": "d" * 64},
        )

        got = worker._overlay_manifest("deno", RemoteVersion())

        assert got.version == "unknown"
        assert got.url == ""


# ── yt-dlp 频道判定 ──────────────────────────────────────────────────


class TestYtDlpChannel:
    @staticmethod
    def _worker(monkeypatch, wanted_channel: str) -> UpdateCheckerWorker:
        # `from fluentytdl.core import dependency_manager` 拿到的是 core/__init__.py
        # 里 re-export 的**单例**，不是模块 —— 只能从 sys.modules 取模块本身。
        dm = sys.modules["fluentytdl.core.dependency_manager"]

        monkeypatch.setattr(
            dm.config_manager,
            "get",
            lambda key, default=None: wanted_channel if key == "ytdlp_channel" else default,
            raising=True,
        )
        return UpdateCheckerWorker("yt-dlp", None)  # type: ignore[arg-type]

    def test_channel_mismatch_same_version(self, monkeypatch):
        """用户当前的真实状态：sidecar 记 nightly，配置要 master，版本号相同。

        以前这里判不出更新 —— 频道藏在字符串里，元组比较看不见它。
        """
        w = self._worker(monkeypatch, "master")
        remote = RemoteVersion(version="2026.08.20.234504", channel="master")

        available, reason = w._compare("2026.08.20.234504", "nightly", remote)

        assert available is True
        assert reason == "channel_mismatch"

    def test_channel_switch_to_lower_version(self, monkeypatch):
        """nightly → stable 版本号是往下走的，按大小比永远判不出来。"""
        w = self._worker(monkeypatch, "stable")
        remote = RemoteVersion(version="2026.07.04", channel="stable")

        available, reason = w._compare("2026.08.20.234504", "nightly", remote)

        assert available is True
        assert reason == "channel_mismatch"

    def test_same_channel_same_version_is_up_to_date(self, monkeypatch):
        """频道判定不能让同版本永久显示"有更新"。"""
        w = self._worker(monkeypatch, "stable")
        remote = RemoteVersion(version="2026.07.04", channel="stable")

        available, reason = w._compare("2026.07.04", "stable", remote)

        assert available is False
        assert reason == "up_to_date"

    def test_same_channel_newer_version(self, monkeypatch):
        w = self._worker(monkeypatch, "stable")
        remote = RemoteVersion(version="2026.09.01", channel="stable")

        available, reason = w._compare("2026.07.04", "stable", remote)

        assert available is True
        assert reason == "remote_newer"

    def test_not_installed_is_not_a_channel_mismatch(self, monkeypatch):
        """没装的时候没有"本地频道"可比，别把它报成频道切换。"""
        w = self._worker(monkeypatch, "master")

        available, reason = w._compare("unknown", "", RemoteVersion(version="2026.09.01"))

        assert reason != "channel_mismatch"


# ── 非 yt-dlp 组件不受频道逻辑影响 ────────────────────────────────────


class TestOtherComponents:
    def test_deno_ignores_channel(self, worker):
        available, reason = worker._compare("2.9.5", "", RemoteVersion(version="2.9.5"))
        assert available is False
        assert reason == "up_to_date"

    def test_remote_unknown_never_reports_update(self, worker):
        available, reason = worker._compare("2.9.5", "", RemoteVersion())
        assert available is False
        assert reason == "remote_unknown"

    def test_ffmpeg_date_comparison(self):
        w = UpdateCheckerWorker("ffmpeg", None)  # type: ignore[arg-type]

        newer, _ = w._compare(
            "N-125100-g10e9f273ee-20260618", "", RemoteVersion(version="latest (20260701)")
        )
        same, _ = w._compare(
            "N-125100-g10e9f273ee-20260618", "", RemoteVersion(version="latest (20260618)")
        )

        assert newer is True
        assert same is False
