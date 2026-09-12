"""Regression coverage for release codecs and frozen updater extraction."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import py7zr
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from archive_compat import CLI_OPTIONS, python_filters, verify_archive  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "updater_compat", ROOT / "src/fluentytdl/core/updater.py"
)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def test_python_protocol_and_corruption(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.dll").write_bytes(b"MZ" * 2048)
    archive = tmp_path / "test.7z"
    with py7zr.SevenZipFile(archive, "w", filters=python_filters()) as writer:
        writer.set_encoded_header_mode(False)
        writer.write(source / "a.dll", arcname="a.dll")
    verify_archive(archive, source)
    (source / "a.dll").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="content/hash mismatch"):
        verify_archive(archive, source)


def test_bcj_is_rejected_even_if_py7zr_can_decode(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.exe").write_bytes(b"MZ" * 1024)
    archive = tmp_path / "bcj.7z"
    with py7zr.SevenZipFile(
        archive, "w", filters=[{"id": py7zr.FILTER_X86}, *python_filters()]
    ) as writer:
        writer.writeall(source, arcname=".")
    with pytest.raises(RuntimeError, match="Incompatible release codecs"):
        verify_archive(archive, source)


def test_bundled_tool_uses_resource_root_and_argument_list(tmp_path, monkeypatch):
    resource = tmp_path / "resources"
    tool = resource / "tools/7zip/7za.exe"
    tool.parent.mkdir(parents=True)
    tool.touch()
    monkeypatch.setattr(updater.sys, "_MEIPASS", str(resource), raising=False)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(updater.subprocess, "run", run)
    dest = tmp_path / "中文 path (1)/_update_tmp"
    updater._extract_7z(tmp_path / "update.7z", dest)
    assert calls[0][0][0] == str(tool)
    assert f"-o{dest.resolve()}" in calls[0][0]
    assert calls[0][1]["shell"] is False


def test_decoder_error_does_not_silently_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_bundled_7za", lambda: tmp_path / "7za.exe")
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 2, b"", b"CRC Failed"),
    )
    with pytest.raises(RuntimeError, match="CRC Failed"):
        updater._extract_7z(tmp_path / "broken.7z", tmp_path / "out")


def test_launch_failure_falls_back(tmp_path, monkeypatch):
    archive = tmp_path / "test.7z"
    with py7zr.SevenZipFile(archive, "w", filters=python_filters()) as writer:
        writer.writestr(b"payload", "hello.txt")
    monkeypatch.setattr(updater, "_bundled_7za", lambda: tmp_path / "7za.exe")

    def fail(*args, **kwargs):
        raise OSError("cannot launch")

    monkeypatch.setattr(updater.subprocess, "run", fail)
    updater._extract_7z(archive, tmp_path / "out")
    assert (tmp_path / "out/hello.txt").read_bytes() == b"payload"
    with pytest.raises(RuntimeError, match="cannot launch"):
        updater._extract_7z(archive, tmp_path / "native-only", require_native=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console integration")
def test_cli_real_dlls_and_marker(tmp_path):
    tool = Path(os.environ.get("FLUENTYTDL_7ZIP_DIR", ROOT / "build/tools/7zip")) / "7za.exe"
    if not tool.is_file():
        if os.environ.get("FLUENTYTDL_REQUIRE_BUILD_TESTS"):
            pytest.fail("Required build decoder was not prepared")
        pytest.skip("Run scripts/prepare_7zip.py for console integration")
    import webview

    source = tmp_path / "source"
    source.mkdir()
    runtimes = Path(webview.__file__).parent / "lib/runtimes"
    for arch in ("win-x64", "win-arm64"):
        dll = runtimes / arch / "native/WebView2Loader.dll"
        assert dll.is_file()
        shutil.copy2(dll, source / f"{arch}.dll")
    archive = tmp_path / "full.7z"
    subprocess.run(
        [str(tool), "a", *CLI_OPTIONS, str(archive), "."],
        cwd=source,
        check=True,
        capture_output=True,
    )
    marker = tmp_path / "portable.txt"
    marker.write_text("portable")
    subprocess.run(
        [str(tool), "a", *CLI_OPTIONS, str(archive), str(marker)], check=True, capture_output=True
    )
    verify_archive(archive, source, {"portable.txt": marker})
