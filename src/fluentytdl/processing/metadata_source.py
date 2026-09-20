"""Bounded, attempt-scoped yt-dlp metadata output protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SOURCE_FIELDS = (
    "id",
    "extractor_key",
    "filepath",
    "webpage_url",
    "title",
    "description",
    "media_type",
    "upload_date",
    "release_date",
    "release_year",
    "artist",
    "artists",
    "creator",
    "creators",
    "channel",
    "channel_id",
    "uploader",
    "track",
    "album",
    "album_artist",
    "album_artists",
    "genre",
    "genres",
    "categories",
    "tags",
    "composer",
    "composers",
    "copyright",
    "license",
    "track_number",
    "disc_number",
    "series",
    "season_number",
    "episode_number",
    "episode_id",
)
SOURCE_TEMPLATE = "after_move:%(.{" + ",".join(SOURCE_FIELDS) + "})j"


def read_source(path: str, observed_paths: list[str]) -> dict[str, Any] | None:
    """A worker downloads one video; reject ambiguous identities, not just the last row."""
    file = Path(path)
    if not file.is_file() or file.stat().st_size > 8 * 1024 * 1024:
        return None
    allowed = {os.path.normcase(os.path.realpath(p)) for p in observed_paths}
    records: list[dict[str, Any]] = []
    with file.open("rb") as stream:
        while line := stream.readline(2 * 1024 * 1024 + 1):
            if len(line) > 2 * 1024 * 1024:
                return None
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                return None
            if not isinstance(row, dict):
                return None
            if not all(
                isinstance(row.get(k), str) and row[k] for k in ("id", "extractor_key", "filepath")
            ):
                return None
            reported = os.path.normcase(os.path.realpath(row["filepath"]))
            if reported not in allowed:
                return None
            records.append({k: row[k] for k in SOURCE_FIELDS if k in row})
    if not records or len({(r["extractor_key"], r["id"]) for r in records}) != 1:
        return None
    # Conflicting source records cannot silently overwrite each other.
    fields = {k: v for k, v in records[0].items() if k != "filepath"}
    if any({k: v for k, v in r.items() if k != "filepath"} != fields for r in records[1:]):
        return None
    return records[0]
