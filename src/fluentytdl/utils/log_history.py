"""Bounded, corruption-tolerant readers for display log metadata."""

from __future__ import annotations

import io
import json
import zipfile
from collections import defaultdict, deque
from pathlib import Path


def read_display_records(directory: Path, limit: int = 1000) -> list[dict]:
    records = deque(maxlen=limit)
    files = sorted(
        [*directory.glob("display_*.jsonl"), *directory.glob("display_*.jsonl.zip")], reverse=True
    )
    seen = set()
    for path in files:
        if len(records) >= limit:
            break
        try:
            chunks = []
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        if name.endswith("/"):
                            continue
                        with archive.open(name) as stream:
                            chunks.extend(
                                deque(
                                    io.TextIOWrapper(stream, encoding="utf-8", errors="replace"),
                                    maxlen=limit,
                                )
                            )
            else:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    chunks = list(deque(stream, maxlen=limit))
            batch = []
            for line in reversed(chunks):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or not isinstance(record.get("raw"), str):
                        continue
                    identity = record.get("id")
                    if not isinstance(identity, str) or identity in seen:
                        continue
                    seen.add(identity)
                    batch.append(record)
                    if len(records) + len(batch) >= limit:
                        break
                except (ValueError, TypeError):
                    continue
            records.extendleft(batch)
        except (OSError, zipfile.BadZipFile):
            continue
    return list(records)


def match_display_records(entries: list[tuple], records: list[dict]):
    """Match metadata to original lines without duplicating either log channel."""
    from .localized_log import render_record

    indexed = defaultdict(deque)
    for record in records:
        time = str(record.get("time", ""))
        if "T" in time:
            time = time.split("T", 1)[1][:8]
        key = (
            time,
            record.get("level"),
            record.get("module"),
            record["raw"].splitlines()[0] if record["raw"].splitlines() else "",
        )
        indexed[key].append(record)
    i = 0
    while i < len(entries):
        time, level, module, raw = entries[i]
        candidates = indexed.get((time, level, module.split(":", 1)[0], raw))
        record = candidates.popleft() if candidates else None
        if record:
            lines = record["raw"].splitlines()
            # Only consume continuation lines when every line matches.
            following = entries[i + 1 : i + len(lines)]
            if len(following) == len(lines) - 1 and all(
                row[3] == line for row, line in zip(following, lines[1:], strict=True)
            ):
                i += len(lines) - 1
                yield (time, level, module, render_record(record)), record["raw"]
            else:
                yield (time, level, module, raw), raw
        else:
            yield (time, level, module, raw), raw
        i += 1
