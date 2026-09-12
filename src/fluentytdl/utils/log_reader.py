"""Background history queries, with stable identity and bounded result sets."""

from __future__ import annotations

import hashlib
import heapq
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .log_runtime import record_failure

_LINE = re.compile(r"^(\d{4}-\d\d-\d\d [\d:.]+) \| (\w+)\s*\| (\S+) - (.*)$")


def _bounded_lines(stream):
    while line := stream.readline(1024 * 1024 + 1):
        if len(line) > 1024 * 1024:
            while line and not line.endswith("\n"):
                line = stream.readline(1024 * 1024)
            record_failure("history_oversize_line")
            continue
        yield line


def lines(path: Path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    with archive.open(info) as raw:
                        yield from _bounded_lines(
                            io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                        )
    else:
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield from _bounded_lines(stream)


def read_page(
    directory: Path,
    *,
    before=None,
    limit=500,
    session="",
    query="",
    since=None,
    until=None,
    cancelled=None,
) -> dict:
    """Use a temporary disk index to match legacy text without unbounded memory."""
    with tempfile.TemporaryDirectory(prefix="fytdl-log-index-") as tmp:
        with closing(sqlite3.connect(str(Path(tmp) / "matches.db"))) as index:
            index.execute("CREATE TABLE matches (key TEXT PRIMARY KEY, count INTEGER)")
            index.execute("CREATE TABLE ids (id TEXT PRIMARY KEY)")
            result = _read_page(
                directory,
                index,
                before=before,
                limit=limit,
                session=session,
                query=query,
                since=since,
                until=until,
                cancelled=cancelled,
            )
    return result


def _fingerprint(record):
    stamp = int(float(record.get("_ts") or 0) * 1000)
    module = str(record.get("module") or "").split(":", 1)[0]
    raw = str(record.get("raw") or "").splitlines()
    return hashlib.sha256(f"{stamp}:{module}:{raw[0] if raw else ''}".encode()).hexdigest()


def _read_page(directory, index, *, before, limit, session, query, since, until, cancelled):
    heap = []
    seen = set()
    candidates = 0

    def keep(record):
        nonlocal candidates
        identity = record.get("id")
        if not identity or identity in seen:
            return
        ts = float(record.get("_ts") or 0)
        key = (ts, identity)
        if before and key >= tuple(before):
            return
        if session and record.get("session") != session:
            return
        if since is not None and ts < since or until is not None and ts > until:
            return
        if query and query.lower() not in json.dumps(record, ensure_ascii=False).lower():
            return
        candidates += 1
        if len(heap) < limit:
            heapq.heappush(heap, (key, record))
            seen.add(identity)
        elif key > heap[0][0]:
            evicted = heapq.heapreplace(heap, (key, record))
            seen.discard(evicted[0][1])
            seen.add(identity)

    for path in sorted(directory.glob("display_*.jsonl*"), reverse=True):
        if path.suffix not in (".jsonl", ".zip"):
            continue
        try:
            for line in lines(path):
                if cancelled and cancelled():
                    return {"records": []}
                try:
                    record = json.loads(line)
                    if (
                        not isinstance(record, dict)
                        or not isinstance(record.get("id"), str)
                        or not isinstance(record.get("raw"), str)
                    ):
                        continue
                    if not record.get("_ts") and record.get("time"):
                        record["_ts"] = datetime.fromisoformat(record["time"]).timestamp()
                    identity = record.get("id")
                    if (
                        identity
                        and index.execute(
                            "INSERT OR IGNORE INTO ids VALUES (?)", (identity,)
                        ).rowcount
                    ):
                        index.execute(
                            "INSERT INTO matches VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET count=count+1",
                            (_fingerprint(record),),
                        )
                    keep(record)
                except (ValueError, TypeError, OverflowError):
                    record_failure("history_corrupt_record")
        except (OSError, zipfile.BadZipFile) as exc:
            record_failure("history_read", exc)
    for path in (directory / "traces").glob("*.jsonl"):
        try:
            for number, line in enumerate(lines(path)):
                if cancelled and cancelled():
                    return {"records": []}
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    identity = (
                        event.get("_event_id")
                        or hashlib.sha256(f"{path.name}:{number}".encode()).hexdigest()
                    )
                    stamp = float(event.get("_ts") or 0)
                    keep(
                        {
                            "id": identity,
                            "_ts": stamp,
                            "time": datetime.fromtimestamp(stamp).isoformat(),
                            "session": event.get("session"),
                            "level": event.get("_level", "INFO"),
                            "raw": json.dumps(event, ensure_ascii=False),
                            "module": "observability",
                            "event": event,
                        }
                    )
                except (ValueError, TypeError, OverflowError):
                    record_failure("history_corrupt_record")
        except OSError as exc:
            record_failure("history_read", exc)

    def keep_legacy(record):
        fingerprint = _fingerprint(record)
        found = index.execute("SELECT count FROM matches WHERE key=?", (fingerprint,)).fetchone()
        if found and found[0] > 0:
            index.execute("UPDATE matches SET count=count-1 WHERE key=?", (fingerprint,))
            return
        keep(record)

    # Match individual records, including dates where metadata began partway through.

    for path in directory.glob("app_*.log*"):
        try:
            current = None
            for number, line in enumerate(lines(path)):
                if cancelled and cancelled():
                    return {"records": []}
                match = _LINE.match(line.rstrip())
                if match:
                    if current:
                        keep_legacy(current)
                    stamp, level, module, raw = match.groups()
                    current = {
                        "id": hashlib.sha256(f"{path.name}:{number}".encode()).hexdigest(),
                        "time": stamp,
                        "_ts": datetime.fromisoformat(stamp).timestamp(),
                        "level": level,
                        "module": module,
                        "raw": raw,
                        "legacy": True,
                    }
                elif current and len(current["raw"]) < 64000:
                    current["raw"] += "\n" + line.rstrip()
            if current:
                keep_legacy(current)
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            record_failure("history_read", exc)
    records = [row[1] for row in sorted(heap, key=lambda row: row[0])]
    return {
        "records": records,
        "before": heap[0][0] if heap else None,
        "has_more": candidates > len(heap),
        "count": len(heap),
    }
