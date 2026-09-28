"""Source UI geometry probe, isolated/offscreen; no extraction or downloads."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
(ROOT / "build/issue-105").mkdir(parents=True, exist_ok=True)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="fytdl-105-")
os.environ["FLUENTYTDL_LOG_ORIGIN"] = "test"
sys.path.insert(0, str(ROOT / "src"))
from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.ui.components.dialogs.download_config_window import (  # noqa: E402
    DownloadConfigWindow,
)

app = QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)
QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
font = QFont("Microsoft YaHei UI", 9)
font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
app.setFont(font)


class Screen:
    def __init__(self, rect):
        self.rect = rect

    def availableGeometry(self):
        return self.rect


info = {
    "id": "probe105",
    "title": "布局研究 / Layout probe",
    "uploader": "UI probe",
    "duration": 600,
    "webpage_url": "https://www.youtube.com/watch?v=probe105",
    "extractor_key": "Youtube",
    "formats": [
        {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "none",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "url": "https://example.invalid/video",
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a",
            "abr": 128,
            "url": "https://example.invalid/audio",
        },
    ],
}
results = []
with (
    patch.object(DownloadConfigWindow, "start_extraction", lambda self: None),
    patch.object(DownloadConfigWindow, "_run_cookie_precheck", lambda self: None),
    patch.object(DownloadConfigWindow, "_reload_webview2_account_combo", lambda self: None),
):
    for name, rect in [
        ("short", QRect(0, 0, 1280, 680)),
        ("portrait", QRect(0, 0, 720, 1240)),
        ("large", QRect(0, 0, 1920, 1040)),
    ]:
        w = DownloadConfigWindow(info["webpage_url"])
        w.screen = lambda rect=rect: Screen(rect)
        w.image_loader.load = lambda *args, **kwargs: None
        w.show()
        w.on_parse_success(dict(info))
        QTest.qWait(400)
        for expanded in (False, True):
            if expanded:
                w._section_selector.enable_switch.setChecked(True)
                QTest.qWait(400)
            hits = []
            for x in [20, 50, 99, 101, 200, 400]:
                hit = w.childAt(QPoint(x, 16))
                hits.append(
                    {
                        "x": x,
                        "type": type(hit).__name__,
                        "is_title": hit is w.titleBar,
                        "is_main_widget": hit is w.main_widget,
                    }
                )
            button = QRect(w.yesButton.mapToGlobal(QPoint()), w.yesButton.size())
            results.append(
                {
                    "case": name,
                    "expanded": expanded,
                    "available": rect.getRect(),
                    "geometry": w.geometry().getRect(),
                    "minimum": w.minimumSize().toTuple(),
                    "minimum_hint": w.minimumSizeHint().toTuple(),
                    "download_button": button.getRect(),
                    "download_inside_available": rect.contains(button),
                    "main_widget": w.main_widget.geometry().getRect(),
                    "title_hits": hits,
                    "selector_minimum": w.selector_widget.minimumSizeHint().toTuple(),
                    "stack_minimum": w.selector_widget.stack.minimumSizeHint().toTuple(),
                    "simple_minimum": w.selector_widget.simple_widget.minimumSizeHint().toTuple(),
                    "advanced_minimum": w.selector_widget.advanced_widget.minimumSizeHint().toTuple(),
                    "options_minimum": w.options_container.minimumSizeHint().toTuple(),
                }
            )
            if name == "short" and not expanded:
                w.grab().save(str(ROOT / "build/issue-105/short-source.png"))
        w.titleBar.raise_()
        results[-1]["after_raise_left_hit_is_title"] = w.childAt(QPoint(20, 16)) is w.titleBar
        w.close()
        app.processEvents()
(ROOT / "build/issue-105/result.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(results, ensure_ascii=False, indent=2))
