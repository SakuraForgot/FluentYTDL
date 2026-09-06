"""Tests for the build-time manifest generator."""

import hashlib
import sys
from pathlib import Path

import pytest

# Resolve scripts/ for direct execution
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import generate_manifest as gm  # noqa: E402
from generate_manifest import generate_manifest, sha256_file  # noqa: E402
from version_manager import (  # noqa: E402
    is_valid_version,
    parse_version,
    strip_v_prefix,
    tag_for,
)


class TestParseVersion:
    def test_stable(self):
        assert parse_version("3.5.5") == ("3.5.5", "stable")

    def test_rc(self):
        assert parse_version("3.5.6-rc.1") == ("3.5.6", "rc")

    def test_beta(self):
        assert parse_version("3.6.0-beta.2") == ("3.6.0", "beta")

    def test_tolerates_v_prefix(self):
        """tag 名可以直接传进来。"""
        assert parse_version("v3.5.5") == ("3.5.5", "stable")
        assert parse_version("v3.5.6-rc.1") == ("3.5.6", "rc")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "3.5",
            "3.5.5.1",
            "v-3.5.5",  # 旧前缀格式不再受支持
            "pre-3.5.5",
            "beta-0.0.5",
            "3.5.5-alpha.1",  # 只认 rc / beta
            "3.5.5-rc",  # 必须带序号
            "3.5.5rc1",  # 必须用 -rc.N 写法
        ],
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_version(bad)

    def test_is_valid_version_rejects_v_prefix(self):
        """is_valid_version 用于校验 VERSION 文件内容，不该放行 tag 形式。"""
        assert is_valid_version("3.5.5")
        assert is_valid_version("3.5.6-rc.1")
        assert not is_valid_version("v3.5.5")


class TestTagFor:
    def test_stable(self):
        assert tag_for("3.5.5") == "v3.5.5"

    def test_prerelease(self):
        assert tag_for("3.5.6-rc.1") == "v3.5.6-rc.1"

    def test_idempotent(self):
        """已经带 v 的不会变成 vv。"""
        assert tag_for("v3.5.5") == "v3.5.5"

    def test_strip_v_prefix(self):
        assert strip_v_prefix("v3.5.5") == "3.5.5"
        assert strip_v_prefix("3.5.5") == "3.5.5"


class TestSha256File:
    def test_known_content(self, tmp_path):
        """SHA256 of known content should match expected hash."""
        test_file = tmp_path / "test.bin"
        content = b"Hello, FluentYTDL!"
        test_file.write_bytes(content)

        expected = hashlib.sha256(content).hexdigest()
        assert sha256_file(test_file) == expected

    def test_empty_file(self, tmp_path):
        test_file = tmp_path / "empty.bin"
        test_file.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert sha256_file(test_file) == expected


class TestGenerateManifest:
    def test_structure_has_required_fields(self, tmp_path):
        """Manifest should have manifest_version, app_version, components."""
        manifest = generate_manifest("3.0.18", tmp_path, "https://example.com/releases")
        assert "manifest_version" in manifest
        assert "app_version" in manifest
        assert "release_tag" in manifest
        assert "components" in manifest

    def test_manifest_version_is_1(self, tmp_path):
        manifest = generate_manifest("3.0.18", tmp_path, "https://example.com")
        assert manifest["manifest_version"] == 1

    def test_app_version_is_bare(self, tmp_path):
        """app_version 不带 v 前缀，release_tag 带。"""
        manifest = generate_manifest("3.5.6-rc.1", tmp_path, "https://example.com")
        assert manifest["app_version"] == "3.5.6-rc.1"
        assert manifest["release_tag"] == "v3.5.6-rc.1"

    def test_v_prefixed_input_is_normalized(self, tmp_path):
        """传入 tag 形式也不会污染 app_version。"""
        manifest = generate_manifest("v3.5.5", tmp_path, "https://example.com")
        assert manifest["app_version"] == "3.5.5"
        assert manifest["release_tag"] == "v3.5.5"

    def test_explicit_tag_wins(self, tmp_path):
        manifest = generate_manifest(
            "3.5.5", tmp_path, "https://example.com", release_tag="v3.5.5-hotfix"
        )
        assert manifest["release_tag"] == "v3.5.5-hotfix"

    def test_app_core_component_with_archive(self, tmp_path):
        """When app-core.7z exists, manifest should include it with SHA256."""
        # Create a fake app-core archive
        archive = tmp_path / "FluentYTDL-3.0.18-win64-app-core.7z"
        content = b"fake archive content"
        archive.write_bytes(content)

        manifest = generate_manifest("3.0.18", tmp_path, "https://example.com")
        app_core = manifest["components"].get("app-core")

        assert app_core is not None
        assert app_core["version"] == "3.0.18"
        assert app_core["sha256"] == hashlib.sha256(content).hexdigest()
        assert app_core["size"] == len(content)
        assert "https://example.com" in app_core["url"]

    def test_app_core_url_uses_tag_not_version(self, tmp_path):
        """下载 URL 必须落在 tag 目录下，否则 in-app 更新 404。"""
        archive = tmp_path / "FluentYTDL-3.5.5-win64-app-core.7z"
        archive.write_bytes(b"x")

        base = "https://github.com/SakuraForgot/FluentYTDL/releases/download/v3.5.5"
        manifest = generate_manifest("3.5.5", tmp_path, base)

        url = manifest["components"]["app-core"]["url"]
        assert url == f"{base}/FluentYTDL-3.5.5-win64-app-core.7z"
        assert "/download/v3.5.5/" in url

    def test_app_core_missing_archive(self, tmp_path):
        """When app-core.7z doesn't exist, app-core component should be absent."""
        manifest = generate_manifest("3.0.18", tmp_path, "https://example.com")
        assert "app-core" not in manifest["components"]

    def test_empty_release_dir(self, tmp_path):
        """Empty release dir should produce a manifest with no app-core."""
        manifest = generate_manifest("3.0.18", tmp_path, "https://example.com")
        assert manifest["app_version"] == "3.0.18"
        assert isinstance(manifest["components"], dict)


class TestBinComponentArtifacts:
    """bin/* 的 url/sha256 必须来自锁文件，且只在版本对得上时才写。

    以前这两个字段无条件写空串，运行时 `_get_remote_version()` 又优先采信清单条目，
    于是 `download_url` 恒为空 —— deno 点「立即更新」直接报"没能解析出下载地址"。
    """

    ASSET = {
        "version": "2.9.5",
        "url": "https://example.com/deno-x86_64-pc-windows-msvc.zip",
        "sha256": "a" * 64,
        "size": 12345,
    }

    def _patch(self, monkeypatch, detected: dict, assets: dict) -> None:
        monkeypatch.setattr(gm, "detect_component_versions", lambda _dir: detected)
        monkeypatch.setattr(gm, "load_tool_assets", lambda: assets)

    def test_uses_lock_asset_when_version_matches(self, tmp_path, monkeypatch):
        self._patch(
            monkeypatch,
            {"deno": {"version": "2.9.5", "repo": "denoland/deno"}},
            {"deno": self.ASSET},
        )
        entry = generate_manifest("3.6.10", tmp_path, "https://example.com")["components"][
            "bin/deno"
        ]

        assert entry["url"] == self.ASSET["url"]
        assert entry["sha256"] == self.ASSET["sha256"]
        assert entry["size"] == self.ASSET["size"]
        assert entry["version"] == "2.9.5"

    def test_version_mismatch_leaves_url_empty(self, tmp_path, monkeypatch):
        """锁里的哈希属于另一个版本 —— 写进清单会让运行时校验必然失败。"""
        self._patch(
            monkeypatch,
            {"deno": {"version": "2.9.6", "repo": "denoland/deno"}},
            {"deno": self.ASSET},
        )
        entry = generate_manifest("3.6.10", tmp_path, "https://example.com")["components"][
            "bin/deno"
        ]

        assert entry["url"] == ""
        assert entry["sha256"] == ""
        assert "size" not in entry
        assert entry["version"] == "2.9.6"

    def test_missing_lock_asset_leaves_url_empty(self, tmp_path, monkeypatch):
        """ffmpeg 走滚动 tag，锁里没有资产元数据；空串由运行时退回 API 兜住。"""
        self._patch(
            monkeypatch,
            {"ffmpeg": {"version": "8.0", "repo": "yt-dlp/FFmpeg-Builds"}},
            {},
        )
        entry = generate_manifest("3.6.10", tmp_path, "https://example.com")["components"][
            "bin/ffmpeg"
        ]

        assert entry["url"] == ""
        assert entry["sha256"] == ""

    def test_repo_matches_runtime_source(self):
        """清单声明的仓库必须与 dependency_manager 实际查询的一致。"""
        defs = gm.detect_component_versions.__doc__  # 仅确认函数存在
        assert defs is not None
        src = (ROOT / "scripts" / "generate_manifest.py").read_text(encoding="utf-8")
        assert "yt-dlp/FFmpeg-Builds" in src
        assert "BtbN/FFmpeg-Builds" not in src
