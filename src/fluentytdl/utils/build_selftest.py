"""Frozen smoke check with no account/service initialization."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def check_pot_sources() -> None:
    """Fail the frozen build when external plugin data is missing or invalid."""
    source = (
        Path(__file__).resolve().parents[1] / "yt_dlp_plugins_ext" / "yt_dlp_plugins" / "extractor"
    )
    for name in ("getpot_bgutil.py", "getpot_bgutil_http.py", "getpot_bgutil_cli.py"):
        content = (source / name).read_bytes()
        if not content.strip():
            raise RuntimeError(f"Empty bundled POT plugin: {name}")
        compile(content, str(source / name), "exec")


def run_selftest(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = str(output / "data")
    report = {"frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    try:
        check_pot_sources()
        report["checks"].append("Bundled POT plugin sources and syntax")
        from ..processing.metadata_normalizer import normalize_metadata
        from ..processing.metadata_writers import read_id3, write_id3

        tag_file = output / "metadata-selftest.id3"
        tag_file.write_bytes(b"")
        values = normalize_metadata(
            {"title": "Metadata roundtrip", "upload_date": "20260920"}
        ).values
        write_id3(str(tag_file), values)
        if read_id3(str(tag_file), values) != values:
            raise RuntimeError("Metadata tag roundtrip failed")
        report["checks"].append("Metadata normalization and bundled Mutagen ID3 roundtrip")
        from ..processing.media_inspection_process import run_reader
        from ..processing.media_inspector import native_command

        native = json.loads(
            run_reader(
                native_command(),
                lambda: False,
                request=json.dumps({"path": str(tag_file.resolve()), "kind": "mp3"}).encode(
                    "utf-8"
                ),
                timeout=15,
            )
        )
        if native.get("status") != "ready" or not any(
            row["key"] == "TIT2" and row["values"] == ["Metadata roundtrip"]
            for row in native.get("tags", [])
        ):
            raise RuntimeError("Native tag helper pipe roundtrip failed")
        report["checks"].append("Native tag helper process and inherited pipes")
        from PySide6.QtCore import QTranslator
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from qfluentwidgets import FluentWindow

        from .paths import resource_path

        app = QApplication.instance() or QApplication([])
        window = FluentWindow()
        icon = QIcon(str(resource_path("assets", "FluentYTDL_v2.ico")))
        if icon.isNull():
            raise RuntimeError("Application icon missing")
        translator = QTranslator()
        if not translator.load(str(resource_path("assets", "locales", "fluentytdl_en_US.qm"))):
            raise RuntimeError("English translation missing")
        json.loads(resource_path("assets", "error_rules.json").read_text(encoding="utf-8"))
        report["checks"].append("Qt window, icon, translation, diagnostic resources")
        import clr  # noqa: F401
        import webview  # noqa: F401

        report["checks"].append("Python.NET and pywebview import")
        window.show()
        app.processEvents()
        window.close()
        report["status"] = "passed"
        result = 0
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        result = 1
    (output / "self-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return result
