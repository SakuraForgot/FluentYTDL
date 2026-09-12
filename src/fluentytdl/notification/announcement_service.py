"""Remote announcements with independent durable acknowledgement records."""

from __future__ import annotations

import json
import locale
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..core.update_transport import CF_BASE, UpdateError, _fresh, configured_transport, record
from ..utils.language import normalize_language


def version_key(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(beta|rc)\.(\d+))?", value)
    if not match:
        raise ValueError("Invalid version")
    return (
        *map(int, match.group(1, 2, 3)),
        {"beta": 0, "rc": 1, None: 2}[match[4]],
        int(match[5] or 0),
    )


def eligible(item: dict, version: str, language: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if item.get("language", "all") not in {"all", language}:
            return False
        if (
            item.get("expires_at")
            and datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00")).timestamp() <= now
        ):
            return False
        current = version_key(version)
        return not (
            item.get("min_version")
            and current < version_key(item["min_version"])
            or item.get("max_version")
            and current > version_key(item["max_version"])
        )
    except (ValueError, TypeError):
        return False


def validate(snapshot: dict) -> dict:
    _fresh(snapshot)
    items = snapshot.get("announcements")
    if not isinstance(items, list) or len(items) > 1000:
        raise UpdateError("update_invalid_response")
    identities = set()
    for item in items:
        if not isinstance(item, dict):
            raise UpdateError("update_invalid_response")
        identity = (item.get("id"), item.get("revision"))
        if (
            not isinstance(identity[0], str)
            or not 0 < len(identity[0]) <= 80
            or type(identity[1]) is not int
            or identity[1] < 1
            or identity in identities
            or any(
                not isinstance(item.get(k, ""), str) or len(item.get(k, "")) > n
                for k, n in (("title", 120), ("summary", 400), ("body", 60000))
            )
        ):
            raise UpdateError("update_invalid_response")
        identities.add(identity)
    return snapshot


class AnnouncementStore:
    """Small separate DB; clearing bell messages cannot clear acknowledgements."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS revisions (id TEXT, revision INTEGER, "
                "notified INTEGER DEFAULT 0, acknowledged INTEGER DEFAULT 0, "
                "PRIMARY KEY(id,revision)); "
                "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT);"
            )

    def connect(self):
        return sqlite3.connect(self.path)

    def state(self, item):
        with self.connect() as db:
            return db.execute(
                "SELECT notified,acknowledged FROM revisions WHERE id=? AND revision=?",
                (item["id"], item["revision"]),
            ).fetchone() or (0, 0)

    def mark(self, item, column):
        if column not in {"notified", "acknowledged"}:
            raise ValueError(column)
        with self.connect() as db:
            db.execute(
                f"INSERT INTO revisions(id,revision,{column}) VALUES(?,?,1) "
                f"ON CONFLICT(id,revision) DO UPDATE SET {column}=1",
                (item["id"], item["revision"]),
            )

    def save(self, snapshot):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO snapshot VALUES(1,?)", (json.dumps(snapshot),))

    def cached(self):
        with self.connect() as db:
            row = db.execute("SELECT body FROM snapshot WHERE id=1").fetchone()
        return validate(json.loads(row[0])) if row else None


class AnnouncementFetch(QThread):
    received = Signal(dict, bool)
    failed = Signal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.transport = configured_transport()

    def run(self):
        try:
            snapshot = validate(self.transport.get_json(f"{CF_BASE}/announcements"))
            self.store.save(snapshot)
            self.received.emit(snapshot, False)
        except Exception as error:
            self.failed.emit(str(error))
            try:
                snapshot = self.store.cached()
                if snapshot:
                    self.received.emit(snapshot, True)
            except Exception:
                pass
        finally:
            self.transport.session.close()


class AnnouncementService(QObject):
    """Window-owned service. UI communicates through request/result signals."""

    refresh_requested = Signal()
    acknowledge_requested = Signal(dict)
    available = Signal(list, bool)
    failed = Signal(str)

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        from ..utils.paths import user_data_dir

        self.store = store or AnnouncementStore(user_data_dir() / "announcements.sqlite3")
        self.worker = None
        self.active = set()
        self.refresh_requested.connect(self.refresh)
        self.acknowledge_requested.connect(self.acknowledge)
        self.timer = QTimer(self)
        self.timer.setInterval(15 * 60 * 1000)
        self.timer.timeout.connect(self.refresh_requested)
        self.timer.start()
        QTimer.singleShot(5000, self.refresh_requested.emit)

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        if self.worker:
            self.worker.deleteLater()
        self.worker = AnnouncementFetch(self.store, self)
        self.worker.received.connect(self.receive)
        self.worker.failed.connect(self.failed)
        self.worker.start()

    def receive(self, snapshot, offline):
        from fluentytdl import __version__

        from ..core.config_manager import config_manager
        from .notification_center import notification_center
        from .notification_model import Notification

        language = normalize_language(
            str(config_manager.get("app_language") or "auto"), locale.getlocale()[0] or "en_US"
        )
        items = [
            item for item in snapshot["announcements"] if eligible(item, __version__, language)
        ]
        self.active = {(item["id"], item["revision"]) for item in items}
        for item in items:
            notified, _ = self.store.state(item)
            if not notified:
                if notification_center.push(
                    Notification(
                        type="announcement",
                        title=item["title"],
                        message=item.get("summary", ""),
                        severity=item.get("severity", "info"),
                        metadata={"announcement": item},
                    )
                ):
                    self.store.mark(item, "notified")
        pending = [
            item for item in items if item.get("require_ack") and not self.store.state(item)[1]
        ]
        record("announcements_received", count=len(items), offline=offline, pending=len(pending))
        self.available.emit(pending, offline)

    def acknowledge(self, item):
        self.store.mark(item, "acknowledged")

    def stop(self):
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.transport.cancel.set()
            self.worker.wait(30000)
