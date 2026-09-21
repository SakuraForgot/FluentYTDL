"""One active inspection plus the latest pending selection; UI-thread arbitration."""

from __future__ import annotations

import threading
from copy import deepcopy

from PySide6.QtCore import QObject, QThread, Signal

from ..processing.media_inspection_process import InspectionCancelled, InspectionError
from ..processing.media_inspector import export_report, inspect_media


class ReportWorker(QThread):
    result = Signal(str)

    def __init__(self, result, destination, options, parent=None):
        super().__init__(parent)
        self.snapshot = deepcopy(result)
        self.destination = destination
        self.options = options

    def run(self):
        try:
            export_report(self.snapshot, self.destination, **self.options)
            self.result.emit("")
        except InspectionError as exc:
            self.result.emit(str(exc))
        except Exception:
            self.result.emit("export_failed")


class InspectionWorker(QThread):
    progress = Signal(int, str)
    result = Signal(int, object, bool)
    failed = Signal(int, str)

    def __init__(self, request_id, path, configured, parent=None):
        super().__init__(parent)
        self.request_id = request_id
        self.path = path
        self.configured = configured
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = inspect_media(
                self.path,
                self.cancel_event.is_set,
                configured=self.configured,
                progress=lambda stage: self.progress.emit(self.request_id, stage),
                partial=lambda result: self.result.emit(self.request_id, deepcopy(result), False),
            )
            self.result.emit(self.request_id, result, True)
        except InspectionCancelled:
            self.failed.emit(self.request_id, "cancelled")
        except InspectionError as exc:
            self.failed.emit(self.request_id, str(exc))
        except Exception:
            self.failed.emit(self.request_id, "reader_failed")


class MediaInspectionService(QObject):
    progress = Signal(str)
    result = Signal(object, bool)
    failed = Signal(str)
    stopped = Signal()
    export_finished = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.generation = 0
        self.worker = None
        self.pending = None
        self.closing = False
        self.export_worker = None
        self.export_error = ""

    @property
    def busy(self):
        return self.worker is not None or self.export_worker is not None

    def export(self, result, destination, options):
        if self.closing or self.export_worker:
            return
        worker = ReportWorker(result, destination, options, self)
        self.export_worker = worker
        worker.result.connect(self._export_result)
        worker.finished.connect(self._export_done)
        worker.start()

    def _export_result(self, error):
        self.export_error = error

    def _export_done(self):
        self.export_worker.deleteLater()
        self.export_worker = None
        self.export_finished.emit(self.export_error)
        if self.closing and not self.busy:
            self.stopped.emit()

    def inspect(self, path: str):
        if self.closing:
            return
        from .config_manager import config_manager

        self.generation += 1
        self.pending = (self.generation, path, str(config_manager.get("ffmpeg_path") or ""))
        if self.worker:
            self.worker.cancel_event.set()
            self.progress.emit("switching")
        else:
            self._start_pending()

    def _start_pending(self):
        if not self.pending or self.closing:
            return
        worker = InspectionWorker(*self.pending, parent=self)
        self.pending = None
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.result.connect(self._result)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        self.progress.emit("validating")
        worker.start()

    def _progress(self, generation, stage):
        if generation == self.generation and not self.closing:
            self.progress.emit(stage)

    def _result(self, generation, result, final):
        if generation == self.generation and not self.closing:
            self.result.emit(result, final)

    def _failed(self, generation, code):
        if generation == self.generation and not self.closing:
            self.failed.emit(code)

    def _finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        if self.closing and not self.busy:
            self.stopped.emit()
        else:
            self._start_pending()

    def cancel(self):
        self.generation += 1
        self.pending = None
        if self.worker:
            self.worker.cancel_event.set()
        self.failed.emit("cancelled")

    def shutdown(self):
        self.closing = True
        self.generation += 1
        self.pending = None
        if self.worker:
            self.worker.cancel_event.set()
        if not self.busy:
            self.stopped.emit()

    def wait_for_shutdown(self):
        # aboutToQuit fallback; normal window exits use asynchronous shutdown.
        self.shutdown()
        if self.worker:
            self.worker.wait()
        if self.export_worker:
            self.export_worker.wait()
