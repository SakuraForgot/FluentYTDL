"""Deterministic semantic mapping. Never infer music from a filename or category."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models.metadata import NormalizedMetadata


def text(value: Any) -> str:
    return (
        value.replace("\0", "").strip()
        if isinstance(value, str) and value not in ("NA", "N/A")
        else ""
    )


def names(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else [value]
    return list(dict.fromkeys(v for item in items if (v := text(item))))


def valid_date(value: Any) -> str:
    value = text(value)
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def year(value: Any) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,4}", str(value)):
        return ""
    number = int(value)
    return f"{number:04d}" if 1 <= number <= 9999 else ""


def page_url(value: Any) -> str:
    try:
        url = urlsplit(text(value))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return ""
        if url.hostname.endswith(("googlevideo.com", "akamaized.net")):
            return ""
        pairs = parse_qsl(url.query, keep_blank_values=True)
        if any(
            k.lower() in {"signature", "sig", "token", "expire", "expires", "x-amz-signature"}
            for k, _ in pairs
        ):
            return ""
        pairs = [
            (k, v)
            for k, v in pairs
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "si"}
        ]
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(pairs), ""))
    except ValueError:
        return ""


def normalize_metadata(info: dict[str, Any], *, audio_only: bool = False) -> NormalizedMetadata:
    result = NormalizedMetadata()

    def put(key: str, value: str, source: str) -> None:
        if value:
            result.values[key] = value
            result.sources[key] = source

    # YouTube Music's explicit page context, or an explicit structured music type.
    music = info.get("media_type") == "music" or (
        urlsplit(page_url(info.get("webpage_url"))).hostname == "music.youtube.com"
        and bool(text(info.get("track")))
    )
    title_field = "track" if audio_only and music and text(info.get("track")) else "title"
    put("title", text(info.get(title_field)), title_field)
    author_fields = ("artists", "artist") if music else ()
    for source in (*author_fields, "creators", "creator", "channel", "uploader"):
        authors = names(info.get(source))
        if authors:
            put("artist", "; ".join(authors), source)
            put("authors_json", json.dumps(authors, ensure_ascii=False), source)
            put("author_role", "artist" if source in author_fields else source.rstrip("s"), source)
            break
    for source in ("upload_date", "release_date"):
        value = valid_date(info.get(source))
        put(source, value, source)
        if info.get(source) and not value:
            result.issues[source] = "invalid_source"
    release_year = year(info.get("release_year"))
    put("release_year", release_year, "release_year")
    release_date = result.values.get("release_date", "")
    if release_year and release_date and release_year != release_date[:4]:
        result.issues["release_year"] = "date_conflict"
    choices = ([("release_year", release_year)] if music else []) + [
        ("release_date", release_date[:4]),
        ("upload_date", result.values.get("upload_date", "")[:4]),
    ]
    for source, value in choices:
        if value:
            put("year", value, source)
            break
    put("comment", page_url(info.get("webpage_url")), "webpage_url")
    description = text(info.get("description"))
    if len(description.encode("utf-8")) > 16384:
        description = description.encode("utf-8")[:16384].decode("utf-8", errors="ignore")
        result.issues["description"] = "truncated"
    put("description", description, "description")
    for target, alternatives in {
        "genre": ("genres", "genre"),
        "album_artist": ("album_artists", "album_artist"),
        "composer": ("composers", "composer"),
    }.items():
        for source in alternatives:
            value = names(info.get(source))
            if value:
                put(target, "; ".join(value), source)
                break
    for key in (
        "album",
        "copyright",
        "license",
        "series",
        "episode_id",
        "channel_id",
        "extractor_key",
        "id",
    ):
        put(key, text(info.get(key)), key)
    for target, source in (("categories", "categories"), ("keywords", "tags")):
        values = names(info.get(source))
        if values:
            put(target, json.dumps(values, ensure_ascii=False), source)
    for key in ("track_number", "disc_number", "season_number", "episode_number"):
        raw = info.get(key)
        if (
            not isinstance(raw, bool)
            and re.fullmatch(r"[1-9]\d{0,4}", str(raw))
            and int(str(raw)) <= 65535
        ):
            put(key, str(raw), key)
        elif raw is not None:
            result.issues[key] = "invalid_source"
    return result
