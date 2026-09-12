"""Shared, read-only build environment contract for CLI, GUI and CI."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions() -> dict[str, str]:
    return json.loads((ROOT / "build-environment.json").read_text(encoding="utf-8"))


def find_iscc() -> Path | None:
    candidates = [
        os.environ.get("FLUENTYTDL_ISCC"),
        str(ROOT / "build/tools/inno/ISCC.exe"),
        str(ROOT / "build/tools/inno-extracted/app/ISCC.exe"),
        shutil.which("ISCC"),
        "C:/Program Files (x86)/Inno Setup 6/ISCC.exe",
        "C:/Program Files/Inno Setup 6/ISCC.exe",
    ]
    return next((Path(p) for p in candidates if p and Path(p).is_file()), None)


def preflight(target: str) -> list[tuple[bool, str]]:
    import tomllib

    expected = versions()
    checks = [
        (
            platform.system() != "Windows" or struct.calcsize("P") != 8,
            "Build platform must be Windows x64",
        ),
        (
            platform.python_version() != expected["python"],
            f"Python {expected['python']} required; found {platform.python_version()}",
        ),
    ]
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    for package in ("pyinstaller", "PySide6", "py7zr", "pythonnet", "clr-loader", "pywebview"):
        try:
            actual = importlib.metadata.version(package)
            accepted = {
                item["version"] for item in locked if item["name"].lower() == package.lower()
            }
            checks.append(
                (
                    actual not in accepted or (package == "py7zr" and actual != expected["py7zr"]),
                    f"{package}: {actual}; locked: {', '.join(sorted(accepted))}",
                )
            )
        except importlib.metadata.PackageNotFoundError:
            checks.append(
                (True, f"Missing {package}; run uv sync --locked --extra dev --extra build")
            )
    if target in ("all", "setup"):
        compiler = find_iscc()
        checks.append((compiler is None, f"Inno Setup {expected['inno']} required"))
        if compiler:
            with tempfile.TemporaryDirectory(prefix="inno_probe_") as temporary:
                probe = subprocess.run(
                    [str(compiler), "/O-", "-"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    input="[Setup]\nAppName=probe\nAppVersion=1\nDefaultDirName={tmp}\\probe\nOutput=no\n",
                    cwd=temporary,
                    timeout=30,
                )
            checks.append(
                (
                    expected["inno"] not in probe.stdout + probe.stderr,
                    f"Inno compiler must be {expected['inno']}",
                )
            )
    checks.append(
        (
            shutil.disk_usage(ROOT).free < 8 * 1024**3,
            "At least 8 GiB free workspace disk space required",
        )
    )
    return checks


if __name__ == "__main__":
    import os
    import sys

    for key, value in versions().items():
        line = f"{key}={value}"
        print(line)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
    if "--check" in sys.argv:
        failures = [message for failed, message in preflight("spec") if failed]
        if failures:
            raise SystemExit("\n".join(failures))
