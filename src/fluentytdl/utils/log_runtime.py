"""Logging identity, paths, health and managed file lifecycle (no Qt dependency)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from .log_privacy import redact_text, redact_value
from .paths import user_data_dir

SESSION_ID = uuid.uuid4().hex
ORIGIN = os.environ.get("FLUENTYTDL_LOG_ORIGIN", "app")
_root: Path | None = None
_lock = threading.RLock()
_health: Counter = Counter()
_last_error = ""
_active: set[Path] = set()
MAX_TOTAL_BYTES = 200 * 1024 * 1024
_maintenance_started = False


def record_failure(channel: str, error: object = "") -> None:
    global _last_error
    with _lock:
        _health[channel] += 1
        _last_error = redact_text(error, 500)


def health_snapshot() -> dict:
    with _lock:
        return {
            "failures": dict(_health),
            "total": sum(_health.values()),
            "last_error": _last_error,
        }


def get_log_root() -> Path:
    global _root
    with _lock:
        if _root is None:
            candidates = [user_data_dir() / "logs", Path(tempfile.gettempdir()) / "FluentYTDL_logs"]
            for candidate in candidates:
                try:
                    candidate.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryFile(dir=candidate):
                        pass
                    _root = candidate.resolve()
                    break
                except OSError as exc:
                    record_failure("directory", exc)
            if _root is None:
                _root = candidates[-1].absolute()
        return _root


def patch_record(record: dict) -> None:
    """Assign identity once, before a record fans out into sinks."""
    try:
        extra = record["extra"]
        extra.setdefault("record_id", uuid.uuid4().hex)
        extra.setdefault("session", SESSION_ID)
        extra.setdefault("origin", ORIGIN)
        extra.setdefault("process", os.getpid())
        record["message"] = redact_text(record["message"], 64000)
        for key in ("localized", "fytdl"):
            if isinstance(extra.get(key), dict):
                extra[key] = redact_value(extra[key])
    except Exception as exc:
        record_failure("redaction", exc)
        record["message"] = "<log redaction failed>"
        record["extra"].pop("localized", None)
        record["extra"].pop("fytdl", None)


def mark_active(path: Path, active: bool) -> None:
    with _lock:
        if active:
            _active.add(path.resolve())
        else:
            _active.discard(path.resolve())


class SafeFileSink:
    """Sanitize the final formatted traceback as well as the message itself."""

    def __init__(
        self,
        directory: Path,
        prefix: str,
        *,
        daily: bool = True,
        max_bytes: int = 20 * 1024 * 1024,
        jsonl: bool = False,
    ):
        self.directory, self.prefix = directory, prefix
        self.jsonl = jsonl
        self.suffix = ".jsonl" if jsonl else ".log"
        self.daily, self.max_bytes = daily, max_bytes
        self.path = None
        self.file = None
        self.day = None
        self.lock = threading.RLock()

    def _close(self):
        if self.file is not None:
            handle, self.file = self.file, None
            try:
                handle.close()
            except OSError as exc:
                record_failure("file_close", exc)
            finally:
                mark_active(self.path, False)

    def _open(self, day):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (
            f"{self.prefix}_{day}{self.suffix}" if self.daily else f"{self.prefix}{self.suffix}"
        )
        self.file = self.path.open("ab")
        self.day = day
        mark_active(self.path, True)

    def write(self, message):
        try:
            with self.lock:
                day = message.record["time"].strftime("%Y-%m-%d")
                data = (
                    (json.dumps(redact_value(json.loads(str(message))), ensure_ascii=False) + "\n")
                    if self.jsonl
                    else redact_text(str(message))
                ).encode("utf-8", "replace")
                if len(data) > self.max_bytes:
                    if self.jsonl:
                        record_failure("oversize_metadata")
                        return
                    data = (
                        data[: self.max_bytes - 64].decode("utf-8", "ignore").encode("utf-8")
                        + b"\n<record truncated>\n"
                    )
                    record_failure("oversize_record")
                if self.file is None:
                    self._open(day)
                rollover = self.file and (
                    (self.daily and self.day != day)
                    or self.file.tell() + len(data) > self.max_bytes
                )
                if rollover:
                    old = self.path
                    self._close()
                    rotated = old.with_name(
                        f"{old.stem}.{time.time_ns()}.{uuid.uuid4().hex[:8]}{self.suffix}"
                    )
                    old.rename(rotated)
                    try:
                        with zipfile.ZipFile(
                            str(rotated) + ".zip", "w", zipfile.ZIP_DEFLATED
                        ) as zf:
                            zf.write(rotated, rotated.name)
                        rotated.unlink()
                    except OSError as exc:
                        record_failure("compression", exc)
                if self.file is None:
                    self._open(day)
                self.file.write(data)
                self.file.flush()
        except Exception as exc:
            record_failure("file_write", exc)
            with self.lock:
                self._close()

    def stop(self):
        with self.lock:
            self._close()


def sweep_logs(root: Path | None = None) -> None:
    """Only manage known log files below the resolved root, never traverse links."""
    try:
        root = (root or get_log_root()).resolve()
        files = []
        now = time.time()
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if not (Path(directory) / d).is_symlink()
                and not (Path(directory) / d).is_junction()
                and d != "bundles"
            ]
            for name in names:
                p = Path(directory) / name
                if p.is_symlink() or not p.resolve().is_relative_to(root):
                    continue
                rel = p.relative_to(root)
                trace = rel.parts[0] == "traces"
                known = (
                    trace
                    and name.startswith(("flow-", "task-", "session-"))
                    and (name.endswith(".jsonl") or name.endswith(".ytdlp.log"))
                ) or (
                    len(rel.parts) == 1
                    and name.startswith(("app_", "display_", "errors_sync"))
                    and name.endswith((".log", ".jsonl", ".zip"))
                )
                if known:
                    st = p.stat()
                    files.append((st.st_mtime, p, st.st_size, 14 if trace else 7))
        total = sum(row[2] for row in files)
        for modified, p, size, days in sorted(files):
            with _lock:
                if p.resolve() in _active:
                    continue
                if now - modified > days * 86400 or total > MAX_TOTAL_BYTES:
                    try:
                        p.unlink()
                        total -= size
                    except OSError as exc:
                        record_failure("retention", exc)
    except Exception as exc:
        record_failure("retention", exc)


def start_maintenance() -> None:
    global _maintenance_started
    with _lock:
        if _maintenance_started:
            return
        _maintenance_started = True

    def run():
        while True:
            sweep_logs()
            time.sleep(1800)

    threading.Thread(target=run, name="log-retention", daemon=True).start()
