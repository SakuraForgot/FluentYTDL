"""Frozen smoke check with no account/service initialization."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def run_selftest(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = str(output / "data")
    report = {"frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    try:
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
