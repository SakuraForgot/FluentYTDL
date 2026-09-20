"""Inspect selected local media without changing it or consulting download data."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from ..models.media_inspection import InspectionResult
from ..utils.paths import locate_runtime_tool
from .media_inspection_process import InspectionCancelled, InspectionError, run_reader
from .metadata_writers import COMMENT_FIELDS, ID3_FIELDS, MATROSKA_FIELDS, MP4_FIELDS

ALLOWED_FORMATS = {
    "mov",
    "matroska",
    "webm",
    "mp3",
    "flac",
    "ogg",
    "wav",
    "aac",
    "avi",
    "mpegts",
    "mpeg",
    "asf",
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def rate(value):
    try:
        rational = Fraction(str(value))
        return {"fraction": str(value), "fps": float(rational)} if rational > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def identity(path: str) -> dict:
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise InspectionError("not_file")
        return {
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev,
            "inode": info.st_ino,
        }
    except FileNotFoundError as exc:
        raise InspectionError("file_missing") from exc
    except PermissionError as exc:
        raise InspectionError("file_denied") from exc


def tool_path(configured: str = "") -> str:
    if configured:
        path = Path(configured)
        candidate = (path if path.is_dir() else path.parent) / "ffprobe.exe"
        if not path.exists() or not candidate.is_file():
            raise InspectionError("configured_probe_missing")
        return str(candidate.resolve())
    try:
        return str(locate_runtime_tool("ffmpeg/ffprobe.exe", "ffprobe.exe"))
    except FileNotFoundError as exc:
        raise InspectionError("tool_unavailable") from exc


def native_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--media-tags-worker"]
    return [
        sys.executable,
        str(Path(__file__).resolve().parents[3] / "main.py"),
        "--media-tags-worker",
    ]


def field(key, value, source, *, scope="container", native_key="", state=None, unit=""):
    return {
        "key": key,
        "value": value,
        "source": source,
        "scope": scope,
        "native_key": native_key,
        "state": state or ("present" if value is not None else "absent"),
        "unit": unit,
    }


def probe_tags(probe: dict) -> list[dict]:
    rows = []
    sections = [("container", probe.get("format", {}))]
    sections += [(f"stream:{s.get('index')}", s) for s in probe.get("streams", [])]
    sections += [(f"chapter:{c.get('id')}", c) for c in probe.get("chapters", [])]
    for scope, section in sections:
        for key, value in section.get("tags", {}).items():
            rows.append(
                {"scope": scope, "key": key, "values": [value], "reader": "probe", "binary": False}
            )
    return rows


def logical_tags(tags: list[dict]) -> list[dict]:
    aliases = {native.upper(): key for key, (native, _) in MP4_FIELDS.items()}
    for mapping in (COMMENT_FIELDS, MATROSKA_FIELDS, ID3_FIELDS):
        aliases.update({native.upper(): key for key, native in mapping.items()})
    aliases.update(
        {
            "TDRC": "date",
            "TYER": "year",
            "DATE": "date",
            "©DAY": "date",
            "DESCRIPTION": "description",
            "SYNOPSIS": "description",
        }
    )
    result = []
    for tag in tags:
        if tag.get("binary"):
            continue
        native = tag["key"]
        key = aliases.get(native.upper())
        extension = native.upper().removeprefix("TXXX:")
        if extension.startswith("----:COM.FLUENTYTDL:"):
            key = extension.split(":", 2)[2].lower()
        elif extension.startswith("FLUENTYTDL_"):
            key = extension.removeprefix("FLUENTYTDL_").lower()
        elif native.startswith("COMM:"):
            key = "comment"
        if key is None:
            continue
        values = tag["values"]
        if key in {"authors_json", "categories", "keywords"} and len(values) == 1:
            try:
                decoded = json.loads(values[0])
                if isinstance(decoded, list) and all(isinstance(v, str) for v in decoded):
                    values = decoded
            except (ValueError, TypeError):
                pass
        result.append(
            field(
                key,
                values,
                "native_tag" if tag["reader"] == "native" else "probe",
                scope=tag["scope"],
                native_key=native,
            )
        )
    # Preserve candidates and explicitly distinguish disagreements by key/scope.
    candidates = {}
    for item in result:
        candidates.setdefault(item["key"], set()).add(json.dumps(item["value"], ensure_ascii=False))
    for item in result:
        if len(candidates[item["key"]]) > 1:
            item["state"] = "conflict"
        if item["key"] == "artist":
            item["role"] = "unspecified"
        elif item["key"] in {"channel", "uploader", "authors_json", "author_role"}:
            item["role"] = "file_declared"
    return result


def build_result(path: str, file_info: dict, probe: dict) -> InspectionResult:
    fmt = probe.get("format", {})
    formats = set(str(fmt.get("format_name", "")).split(","))
    if not formats & ALLOWED_FORMATS:
        raise InspectionError("unsupported_media")
    streams = probe.get("streams", [])
    if not any(
        s.get("codec_type") in {"video", "audio"}
        and not s.get("disposition", {}).get("attached_pic")
        for s in streams
    ):
        raise InspectionError("unsupported_media")
    result = InspectionResult(
        path,
        file={"name": Path(path).name, **file_info},
        read_at=datetime.now(UTC).isoformat(),
        raw=probe,
    )
    duration = number(fmt.get("duration"))
    bitrate = number(fmt.get("bit_rate"))
    derived = bitrate is None and duration is not None
    if derived:
        bitrate = file_info["size"] * 8 / duration
    result.fields = [
        field("container", fmt.get("format_name"), "probe"),
        field("size", file_info["size"], "filesystem", unit="bytes"),
        field("duration", duration, "probe", unit="s"),
        field("total_bitrate", bitrate, "derived" if derived else "probe", unit="bps"),
    ]
    for stream in streams:
        result.streams.append(
            {
                **stream,
                "average_rate": rate(stream.get("avg_frame_rate")),
                "reference_rate": rate(stream.get("r_frame_rate")),
                "reported_frames": number(stream.get("nb_frames")),
                "reported_bitrate": number(stream.get("bit_rate")),
                "frame_rate_mode": "not_analyzed",
            }
        )
    result.chapters = probe.get("chapters", [])
    result.tags = probe_tags(probe)
    result.coverage = {"probe": "reader_projection", "native": "unsupported"}
    result.fields += logical_tags(result.tags)
    return result


def complete_fields(result):
    fields = logical_tags(result.tags)
    for key in ("title", "artist", "authors_json", "channel", "uploader", "date", "webpage_url"):
        if not any(item["key"] == key for item in fields):
            fields.append(field(key, None, "probe+native", state="not_returned"))
    result.fields = result.fields[:4] + fields


def inspect_media(
    path: str,
    cancel: Callable[[], bool],
    *,
    configured="",
    progress=lambda stage: None,
    partial=lambda result: None,
) -> InspectionResult:
    if not path:
        raise InspectionError("path_missing")
    if "://" in path or path.startswith(("\\\\", "//")):
        raise InspectionError("local_file_only")
    path = str(Path(path).absolute())
    before = identity(path)
    if not before["size"]:
        raise InspectionError("empty_file")
    probe_tool = tool_path(configured)
    progress("probing")
    try:
        probe = json.loads(
            run_reader(
                [
                    probe_tool,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    ",".join(sorted(ALLOWED_FORMATS)),
                    "-show_format",
                    "-show_streams",
                    "-show_chapters",
                    "-of",
                    "json",
                    path,
                ],
                cancel,
            )
        )
        if not isinstance(probe, dict):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise InspectionError("invalid_probe") from exc
    result = build_result(path, before, probe)
    try:
        result.reader_versions["ffprobe"] = (
            run_reader([probe_tool, "-version"], cancel, timeout=5, limit=65536)
            .decode("utf-8", errors="replace")
            .splitlines()[0]
        )
    except InspectionError:
        result.reader_versions["ffprobe"] = "unavailable"
    formats = set(probe.get("format", {}).get("format_name", "").split(","))
    kind = next((k for k in ("mp3", "flac") if k in formats), "")
    if "mov" in formats:
        kind = "mp4"
    elif "ogg" in formats:
        codecs = {s.get("codec_name") for s in result.streams if s.get("codec_type") == "audio"}
        kind = "opus" if codecs == {"opus"} else "ogg" if codecs == {"vorbis"} else ""
    complete_fields(result)
    partial(result)
    if kind:
        progress("reading_tags")
        try:
            native = json.loads(
                run_reader(
                    native_command(),
                    cancel,
                    request=json.dumps({"path": path, "kind": kind}).encode("utf-8"),
                    timeout=15,
                )
            )
            result.coverage["native"] = native["status"]
            if native.get("error"):
                result.issues.append(native["error"])
            else:
                result.tags += native["tags"]
                result.fields = result.fields[:4] + logical_tags(result.tags)
            from mutagen import version_string

            result.reader_versions["mutagen"] = version_string
        except (InspectionError, ValueError, KeyError, TypeError) as exc:
            result.issues.append(str(exc) if isinstance(exc, InspectionError) else "native_failed")
            result.coverage["native"] = "error"
    if cancel():
        raise InspectionCancelled()
    try:
        changed = identity(path) != before
    except (InspectionError, OSError):
        changed = True
    if changed:
        result.status = "changed"
        result.issues.append("file_changed")
    elif result.issues:
        result.status = "partial"
    complete_fields(result)
    return result


def export_report(result: InspectionResult, destination: str, **options) -> None:
    target = Path(destination)
    source = Path(result.path)
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and os.path.samefile(target, source)
    ):
        raise InspectionError("export_is_source")
    data = json.dumps(result.report(**options), ensure_ascii=False, indent=2, allow_nan=False)
    # Open without truncation, then check the opened identity too (covers hardlinks
    # and replacement between the path check and opening).
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if source.exists() and os.path.samestat(os.fstat(stream.fileno()), os.stat(source)):
            raise InspectionError("export_is_source")
        stream.write(data)
        stream.truncate()
