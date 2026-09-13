"""Isolated, reproducible offscreen history/live-load probe; no application data touched."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="fytdl-log-probe-")
os.environ["FLUENTYTDL_LOG_ORIGIN"] = "probe"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.ui.components.dialogs.log_viewer_window import LogViewerWindow  # noqa: E402
from fluentytdl.utils.log_runtime import SESSION_ID, get_log_root  # noqa: E402


def record(number):
    return {
        "id": str(number),
        "_ts": 1000 + number,
        "time": "2026-09-12T16:00:00",
        "session": SESSION_ID,
        "level": "INFO",
        "module": "download",
        "raw": "Download processing: video",
        "event": {
            "kind": "stage",
            "stage": "download",
            "session": SESSION_ID,
            "flow": "flow" + str(number // 100),
            "task": str(number // 100),
            "run": "run" + str(number // 100),
            "attempt": 0,
            "phase": "video",
        },
    }


def main():
    root = get_log_root()
    with (root / "display_2026-09-12.jsonl").open("w", encoding="utf-8") as stream:
        for number in range(10000):
            stream.write(json.dumps(record(number)) + "\n")
    app = QApplication([])
    # The offscreen Windows plugin has no automatic system font discovery.
    for filename in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc", "consola.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Microsoft YaHei", 10))
    view = LogViewerWindow()
    view.resize(1200, 760)
    view.show()
    gaps = []
    previous = time.perf_counter()
    counter = 10000

    def tick():
        nonlocal previous, counter
        now = time.perf_counter()
        gaps.append((now - previous) * 1000)
        previous = now
        if counter < 13000 and not view._history_loading:
            view._on_record_batch([record(i) for i in range(counter, counter + 10)])
            counter += 10

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    timer.start()
    start = time.perf_counter()
    while time.perf_counter() - start < 7:
        app.processEvents()
        time.sleep(0.002)
    timer.stop()
    view.viewSwitcher.setCurrentItem("timeline")
    view._on_view_changed("timeline")
    view.timelineView.scrollToTop()
    until = time.perf_counter() + 0.3
    while time.perf_counter() < until:
        app.processEvents()
        time.sleep(0.005)
    view.repaint()
    output = ROOT / "artifacts/logging-review"
    output.mkdir(parents=True, exist_ok=True)
    view.grab().save(str(output / "viewer.png"))
    result = {
        "history_source_records": 10000,
        "live_records": counter - 10000,
        "displayed_records": len(view._log_buffer),
        "displayed_events": view.timelineView.event_count(),
        "timer_p95_ms": round(sorted(gaps)[int(len(gaps) * 0.95)], 2),
        "timer_max_ms": round(max(gaps), 2),
        "samples": len(gaps),
        "platform": "Qt offscreen; constructor time excluded",
    }
    (output / "performance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    view.close()
    app.processEvents()


if __name__ == "__main__":
    main()
