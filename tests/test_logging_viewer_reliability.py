import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fluentytdl.ui.components.dialogs import log_viewer_window as module
from fluentytdl.utils.log_runtime import SESSION_ID
from fluentytdl.utils.log_signal_handler import log_signal_handler


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "LOG_DIR", str(tmp_path))
    view = module.LogViewerWindow()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if view._generation and not view._history_loading:
            break
        time.sleep(0.005)
    yield view
    view.close()
    app.processEvents()


def record(identity, **event):
    return {
        "id": identity,
        "session": SESSION_ID,
        "raw": "message",
        "level": "INFO",
        "time": "2026-09-12T12:00:00",
        "_ts": 100,
        "event": {
            "session": SESSION_ID,
            "flow": "f",
            "task": "1",
            "run": "r",
            "kind": "stage",
            "stage": "download",
            **event,
        },
    }


def test_snapshot_live_overlap_is_deduplicated(window):
    window._history_loading = True
    same = record("same")
    window._on_record_batch([same, record("after")])
    window._on_history_ready(window._generation, {"records": [same], "count": 1})
    assert list(window._record_ids) == ["same", "after"]
    assert window.timelineView.event_count() == 2


def test_stale_query_cannot_replace_newer_result(window):
    window._on_record_batch([record("new")])
    window._on_history_ready(window._generation - 1, {"records": [record("old")]})
    assert "new" in window._record_ids and "old" not in window._record_ids


def test_same_flow_from_two_sessions_has_separate_groups(window):
    view = window.timelineView
    view.add_event(record("a")["event"])
    view.add_event(record("b", session="other")["event"])
    assert view.topLevelItemCount() == 2


def test_warning_grouping_does_not_merge_attempts(window):
    view = window.timelineView
    first = record("a", kind="signal", code="cookie_expired", severity_hint="warning", attempt=0)[
        "event"
    ]
    view.add_event(first)
    view.add_event(dict(first, _time="12:00:01"))
    view.add_event(dict(first, attempt=1))
    assert view.event_count() == 2


def test_display_queue_is_bounded_and_reports_drops(window):
    from datetime import datetime
    from types import SimpleNamespace

    from fluentytdl.utils.log_runtime import health_snapshot

    with log_signal_handler._pending_lock:
        log_signal_handler._pending.clear()
    before = health_snapshot()["failures"].get("display_dropped", 0)
    for i in range(4100):
        log_signal_handler._emit_log(
            SimpleNamespace(
                record={
                    "extra": {"record_id": str(i)},
                    "time": datetime.now(),
                    "level": SimpleNamespace(name="DEBUG"),
                    "message": "test",
                }
            )
        )
    assert len(log_signal_handler._pending) == 4000
    assert health_snapshot()["failures"]["display_dropped"] - before == 100
    with log_signal_handler._pending_lock:
        log_signal_handler._pending.clear()
