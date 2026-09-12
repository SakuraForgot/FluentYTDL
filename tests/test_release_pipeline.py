"""Behavioral contracts for latest components, source isolation and release gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build  # noqa: E402
import component_snapshot as snapshots  # noqa: E402
import release_pipeline as release  # noqa: E402


def test_latest_asset_is_resolved_once_and_digest_verified(monkeypatch, tmp_path):
    import hashlib

    content = b"new upstream version"
    calls = []
    asset = {
        "name": "tool.exe",
        "id": 100,
        "size": len(content),
        "browser_download_url": "https://github.com/example/tool/releases/download/v2/tool.exe",
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }

    def api(endpoint):
        calls.append(endpoint)
        return {"id": 2, "tag_name": "v2", "assets": [asset]}

    monkeypatch.setattr(snapshots.fetch_tools, "github_api", api)
    downloads = []

    def download(url, dest, *args, **kwargs):
        downloads.append(url)
        dest.write_bytes(content)

    monkeypatch.setattr(snapshots.fetch_tools, "download_file", download)
    source = snapshots.ReleaseSource("example/tool")
    source.download(
        "https://github.com/example/tool/releases/latest/download/tool.exe", tmp_path / "tool.exe"
    )
    assert calls == ["/repos/example/tool/releases/latest"]
    assert downloads == [asset["browser_download_url"]]
    assert source.metadata()["assets"]["tool.exe"]["verification"] == "github-sha256"
    asset["digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="digest mismatch"):
        source.download("https://github.com/tool.exe", tmp_path / "tool.exe")


def test_missing_latest_asset_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(
        snapshots.fetch_tools,
        "github_api",
        lambda endpoint: {"id": 2, "tag_name": "v2", "assets": [{"name": "other.zip"}]},
    )
    source = snapshots.ReleaseSource("example/tool")
    with pytest.raises(RuntimeError, match="missing"):
        source.download("https://github.com/tool.exe", tmp_path / "tool.exe")


def test_snapshot_rejects_incomplete_component_set(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schema_version": 1, "tools": {"yt-dlp": {}}}))
    with pytest.raises(RuntimeError, match="six"):
        snapshots.verify(path)


def test_snapshot_rejects_escape_before_hashing(tmp_path):
    tools = {
        name: {"files": {"../outside": {"sha256": "", "size": 1}}}
        for name in snapshots.REPOSITORIES
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schema_version": 1, "tools": tools}))
    with pytest.raises(RuntimeError, match="Invalid snapshot"):
        snapshots.verify(path)


def test_version_override_only_writes_staging(tmp_path, monkeypatch):
    for name, content in {"VERSION": "3.7.1", "pyproject.toml": "untouched"}.items():
        (tmp_path / name).write_text(content)
    builder = build.Builder(override_version="3.7.2-rc.1")
    builder.work_dir = tmp_path / "staging"
    builder.work_dir.mkdir()
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setenv("FLUENTYTDL_VERSION_SOURCE", "previous")
    builder._sync_version_to_all()
    assert (tmp_path / "VERSION").read_text() == "3.7.1"
    assert (tmp_path / "pyproject.toml").read_text() == "untouched"
    assert (builder.work_dir / "VERSION").read_text().strip() == "3.7.2-rc.1"


def test_release_rejects_stale_artifact_bytes(tmp_path):
    archive = tmp_path / "FluentYTDL-3.7.1-win64-full.7z"
    archive.write_bytes(b"original")
    result = {
        "target": "7z",
        "version": "3.7.1",
        "artifacts": [
            {
                "name": archive.name,
                "path": str(archive),
                "size": 8,
                "sha256": build.sha256_file(archive),
            }
        ],
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result))
    release.validate_result(path)
    archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="mismatch"):
        release.validate_result(path)


def test_publish_rejects_replayed_snapshot_before_network(monkeypatch):
    monkeypatch.setattr(
        release, "validate_result", lambda *args: {"dirty": False, "component_policy": "replay"}
    )
    with pytest.raises(ValueError, match="latest snapshot"):
        release.publish()


def test_toolchain_python_file_matches_contract():
    from build_environment import versions

    assert (ROOT / ".python-version").read_text().strip() == versions()["python"]


def test_existing_public_release_cannot_be_overwritten(monkeypatch):
    result = {
        "dirty": False,
        "component_policy": "latest",
        "git_commit": "a" * 40,
        "version": "3.7.1",
    }
    monkeypatch.setattr(release, "validate_result", lambda *args: result)
    monkeypatch.setattr(
        release, "resolve", lambda: {"publish": "true", "version": "3.7.1", "tag": "v3.7.1"}
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/test")
    calls = []

    def command(*args):
        calls.append(args)
        if args[0] == "git":
            return "a" * 40
        if args[:2] == ("gh", "api"):
            return json.dumps([[{"tag_name": "v3.7.1", "draft": False}]])
        raise AssertionError("Release mutation should never be reached")

    monkeypatch.setattr(release, "command", command)
    with pytest.raises(ValueError, match="public release already exists"):
        release.publish()
    assert len(calls) == 2


@pytest.mark.parametrize("scope", ["user", "machine"])
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell filesystem semantics")
def test_complete_uninstall_cleanup_uses_original_profiles(tmp_path, scope):
    """Run the real Clean action; mocked profile discovery restricts every root to tmp_path."""
    import subprocess

    app = tmp_path / "app"
    app.mkdir()
    profiles = [tmp_path / "alice", tmp_path / "bob"]
    expected = [app]
    for profile in profiles:
        for relative in ("AppData/Local/FluentYTDL", "Documents/FluentYTDL"):
            root = profile / relative
            root.mkdir(parents=True)
            expected.append(root)
        auth = profile / "AppData/Local/Temp/fluentytdl_auth"
        auth.mkdir(parents=True)
        (auth / "cached.txt").write_text("credential")
    for root in expected:
        for name in (
            "config.json",
            "tasks.db",
            "state/tasks/tasks.db",
            "bin/dle_user/accounts.json",
            "bin/cookies.txt",
            "bin/cookies_youtube.txt.tmp",
            "legacy_conflict_install/state/tasks/tasks.db",
        ):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(
                json.dumps({"download_dir": str(app)}) if name == "config.json" else "runtime"
            )
    (app / "movie.mp4").write_bytes(b"downloaded")
    (app / "notes.txt").write_text("user content")
    (app / ".install-owner.json").write_text(json.dumps({"sid": "S-1-5-21-111111-1001"}))
    script = tmp_path / "clean.ps1"
    helper = str(ROOT / "installer/maintenance.ps1").replace("'", "''")
    script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
function Get-CimInstance {{
    param([string]$ClassName)
    if ($ClassName -ne 'Win32_UserProfile') {{ throw 'Unexpected system discovery in test' }}
    [PSCustomObject]@{{SID='S-1-5-21-111111-1001'; LocalPath=(Join-Path $PSScriptRoot 'alice'); Special=$false}}
    [PSCustomObject]@{{SID='S-1-5-21-111111-1002'; LocalPath=(Join-Path $PSScriptRoot 'bob'); Special=$false}}
}}
& '{helper}' -Action Clean -AppDirectory (Join-Path $PSScriptRoot 'app') -Scope {scope}
if ($LASTEXITCODE -ne 0) {{ throw 'Cleanup failed' }}
""",
        encoding="utf-8-sig",
    )
    process = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(script)],
        timeout=30,
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert process.returncode == 0, process.stdout + process.stderr
    for root in expected:
        should_clean = scope == "machine" or not root.is_relative_to(profiles[1])
        assert (root / "state/tasks/tasks.db").exists() is not should_clean
        assert (root / "bin/dle_user/accounts.json").exists() is not should_clean
        assert (root / "bin/cookies.txt").exists() is not should_clean
    assert (app / "movie.mp4").read_bytes() == b"downloaded"
    assert (app / "notes.txt").read_text() == "user content"


def test_installer_messages_have_both_languages():
    text = (ROOT / "installer/FluentYTDL.iss").read_text(encoding="utf-8")
    custom = text.split("[CustomMessages]", 1)[1].split("[Messages]", 1)[0]
    keys = {
        language: {
            line.split("=", 1)[0].split(".", 1)[1]
            for line in custom.splitlines()
            if line.startswith(language + ".")
        }
        for language in ("english", "chinesesimp")
    }
    assert keys["english"] and keys["english"] == keys["chinesesimp"]
    assert "taskkill" not in text
    assert "[UninstallDelete]" not in text
    assert "UninstallLogMode=overwrite" in text


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell filesystem semantics")
def test_uninstall_tree_keeps_downloads_and_refuses_junctions(tmp_path):
    import subprocess

    root = tmp_path / "runtime"
    root.mkdir()
    (root / "tasks.db").write_bytes(b"history")
    (root / "video.mp4").write_bytes(b"media")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_bytes(b"keep")
    script = tmp_path / "verify.ps1"
    maintenance = str(ROOT / "installer/maintenance.ps1").replace("'", "''")
    script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{maintenance}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw $errors[0] }}
foreach ($function in $ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}}, $true)) {{
    Invoke-Expression $function.Extent.Text
}}
$protected = @()
$media = @('.mp4')
$failures = [Collections.Generic.List[string]]::new()
$root = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Junction -Path (Join-Path $root 'linked') -Target (Join-Path $PSScriptRoot 'outside') | Out-Null
Remove-OwnedTree $root
if (Test-Path -LiteralPath (Join-Path $root 'tasks.db')) {{ throw 'History retained' }}
if (-not (Test-Path -LiteralPath (Join-Path $root 'video.mp4'))) {{ throw 'Media deleted' }}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'outside/secret.json'))) {{ throw 'Escaped root' }}
if ($failures.Count -eq 0) {{ throw 'Reparse point not reported' }}
""",
        encoding="utf-8-sig",
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(script)],
        check=True,
        timeout=30,
        capture_output=True,
    )
