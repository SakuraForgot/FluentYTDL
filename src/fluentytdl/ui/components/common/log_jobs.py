"""Background logging I/O with Qt-only delivery to the view."""

from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ....utils.log_reader import read_page
from ....utils.log_runtime import record_failure


class LogJobs(QObject):
    history_ready = Signal(int, dict)
    export_ready = Signal(dict)
    export_progress = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def load(self, generation: int, directory: str, **options):
        self._cancel.set()
        self._cancel = threading.Event()
        cancel = self._cancel

        def run():
            try:
                from ....observability.sinks import flush_sinks

                flush_sinks()
                page = read_page(Path(directory), cancelled=cancel.is_set, **options)
                if not cancel.is_set():
                    self.history_ready.emit(generation, page)
            except Exception as exc:
                record_failure("history_job", exc)
                try:
                    self.history_ready.emit(generation, {"records": [], "error": str(exc)})
                except RuntimeError:
                    pass

        threading.Thread(target=run, name="log-history", daemon=False).start()

    def export(self, selection: dict):
        def run():
            from ....observability.bundle import export_bug_bundle

            try:
                path = export_bug_bundle(**selection, progress=self.export_progress.emit)
                result = {"path": str(path) if path else ""}
                if path:
                    with zipfile.ZipFile(path) as archive:
                        result["manifest"] = json.loads(archive.read("manifest.json"))
                self.export_ready.emit(result)
            except Exception as exc:
                record_failure("export_job", exc)
                try:
                    self.export_ready.emit({"path": ""})
                except RuntimeError:
                    pass

        threading.Thread(target=run, name="log-export", daemon=False).start()
