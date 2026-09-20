"""Read-back and protected-content checks, independent of writer exit status."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable

from .metadata_process import MetadataError, run_tool
from .metadata_writers import (
    COMMENT_FIELDS,
    ID3_FIELDS,
    MP4_FIELDS,
    comment_file,
    extension_key,
    load_id3,
    mp4_tags,
)


def probe_media(path: str, ffprobe: str, cancel: Callable[[], bool]) -> dict:
    try:
        return json.loads(
            run_tool(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-show_chapters",
                    "-show_data_hash",
                    "sha256",
                    "-of",
                    "json",
                    path,
                ],
                cancel,
                timeout=60,
            )
        )
    except (ValueError, TypeError) as exc:
        raise MetadataError("probe_failed") from exc


def container_kind(probe: dict, suffix: str) -> str:
    formats = set(probe.get("format", {}).get("format_name", "").split(","))
    if "mov" in formats:
        return "mp4"
    if "mp3" in formats:
        return "mp3"
    if "flac" in formats:
        return "flac"
    if "ogg" in formats:
        codecs = {
            s.get("codec_name")
            for s in probe.get("streams", [])
            if not s.get("disposition", {}).get("attached_pic")
        }
        if codecs <= {"opus"}:
            return "opus"
        if codecs <= {"vorbis"}:
            return "ogg"
    if "matroska" in formats and suffix in (".mkv", ".mka", ".webm"):
        return "matroska"
    raise MetadataError("unsupported_container")


def structure(probe: dict, owned_stream_tags: set[str] | None = None) -> tuple:
    """Do not compare file size, global bitrate or encoder bookkeeping after tagging."""
    fields = (
        "codec_name",
        "codec_type",
        "profile",
        "width",
        "height",
        "pix_fmt",
        "sample_rate",
        "channels",
        "channel_layout",
        "color_range",
        "color_space",
        "color_transfer",
        "color_primaries",
        "chroma_location",
        "sample_aspect_ratio",
        "r_frame_rate",
        "avg_frame_rate",
        "disposition",
        "side_data_list",
        "extradata_hash",
    )
    streams = []
    for stream in probe.get("streams", []):
        item = {k: stream[k] for k in fields if k in stream}
        item["tags"] = {
            k.lower(): v
            for k, v in stream.get("tags", {}).items()
            if k.lower() not in (owned_stream_tags or set())
            and k.lower()
            not in {"encoder", "duration", "bps", "number_of_bytes", "number_of_frames"}
        }
        streams.append(item)
    chapters = [
        {k: c.get(k) for k in ("start_time", "end_time", "tags")} for c in probe.get("chapters", [])
    ]
    return streams, chapters


def verify_structure(before: dict, after: dict, owned_stream_tags: set[str] | None = None) -> None:
    if structure(before, owned_stream_tags) != structure(after, owned_stream_tags):
        raise MetadataError("protected_streams_changed")
    if not before.get("streams") or not after.get("streams"):
        raise MetadataError("missing_media_stream")
    for left, right in zip(before["streams"], after["streams"], strict=True):
        for key in ("duration", "start_time"):
            if key in left and key in right and abs(float(left[key]) - float(right[key])) > 0.002:
                raise MetadataError("media_timeline_changed")


def native_protection(path: str, kind: str, values: dict[str, str]) -> dict[str, str]:
    """Covers unowned tags including complete cover bytes, not merely picture count."""
    if kind == "mp4":
        tags = mp4_tags(path)
        owned = {
            MP4_FIELDS[k][0] if k in MP4_FIELDS else f"----:com.fluentytdl:{k.upper()}"
            for k in values
        }
    elif kind == "mp3":
        tags = load_id3(path)
        owned = {
            ID3_FIELDS.get(k, "TDRC" if tags.version[1] == 4 else "TYER")
            if k == "year" or k in ID3_FIELDS
            else "COMM:FLUENTYTDL_SOURCE:eng"
            if k == "comment"
            else "TXXX:" + extension_key(k)
            for k in values
        }
    elif kind in ("flac", "ogg", "opus"):
        audio = comment_file(path, kind)
        tags = dict(audio.tags or {})
        if kind == "flac":
            tags["__pictures"] = [picture.write() for picture in audio.pictures]
        owned = {COMMENT_FIELDS.get(k, extension_key(k)).lower() for k in values}
    else:
        return {}
    return {
        k: hashlib.sha256(repr(v).encode("utf-8")).hexdigest()
        for k, v in tags.items()
        if k not in owned
    }


def mp4_protection(path: str) -> list[tuple[str, str]]:
    """Hash projection/HDR/unknown UUID atoms without reading large media payloads."""
    found: list[tuple[str, str]] = []
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"sv3d", b"proj"}
    visual = {b"avc1", b"avc3", b"hvc1", b"hev1", b"av01", b"vp09", b"encv"}
    audio = {b"mp4a", b"Opus", b"fLaC", b"ac-3", b"ec-3", b"enca"}
    protected = {
        b"uuid",
        b"sv3d",
        b"st3d",
        b"colr",
        b"mdcv",
        b"clli",
        b"SA3D",
        b"chan",
        b"avcC",
        b"hvcC",
        b"av1C",
        b"vpcC",
        b"dvcC",
        b"dvvC",
        b"dvwC",
        b"pasp",
        b"clap",
    }
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        total = stream.tell()

        def visit(start: int, end: int, depth: int = 0) -> None:
            if depth > 16:
                raise MetadataError("unsupported_mp4_structure")
            offset = start
            while offset < end:
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    raise MetadataError("invalid_mp4_structure")
                size, kind = struct.unpack(">I4s", header)
                head = 8
                if size == 1:
                    large = stream.read(8)
                    if len(large) != 8:
                        raise MetadataError("invalid_mp4_structure")
                    size = struct.unpack(">Q", large)[0]
                    head = 16
                if size == 0:
                    size = end - offset
                if size < head or offset + size > end:
                    raise MetadataError("invalid_mp4_structure")
                body, stop = offset + head, offset + size
                if kind in protected:
                    if size > 16 * 1024 * 1024:
                        raise MetadataError("unsupported_mp4_structure")
                    stream.seek(body)
                    found.append(
                        (kind.decode("ascii"), hashlib.sha256(stream.read(size - head)).hexdigest())
                    )
                elif kind in containers:
                    visit(body, stop, depth + 1)
                elif kind == b"stsd":
                    visit(body + 8, stop, depth + 1)
                elif kind in visual:
                    visit(body + 78, stop, depth + 1)
                elif kind in audio:
                    stream.seek(body + 8)
                    version_bytes = stream.read(2)
                    if len(version_bytes) != 2:
                        raise MetadataError("invalid_mp4_structure")
                    version = int.from_bytes(version_bytes, "big")
                    skip = {0: 28, 1: 44, 2: 64}.get(version)
                    if skip is None:
                        raise MetadataError("unsupported_mp4_structure")
                    visit(body + skip, stop, depth + 1)
                offset = stop

        visit(0, total)
    return found


def check_matroska_tags(path: str) -> None:
    """Reject tag structures that FFmpeg's flat dictionary cannot round-trip."""
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        total = stream.tell()

        def vint(identifier: bool = False) -> tuple[int, bool]:
            first = stream.read(1)
            if not first or not first[0]:
                raise MetadataError("unsupported_matroska_structure")
            mask, length = 128, 1
            while not (first[0] & mask):
                mask >>= 1
                length += 1
            tail = stream.read(length - 1)
            if len(tail) != length - 1:
                raise MetadataError("unsupported_matroska_structure")
            value = int.from_bytes(
                bytes([first[0] if identifier else first[0] & (mask - 1)]) + tail, "big"
            )
            return value, not identifier and value == (1 << (7 * length)) - 1

        def children(start: int, end: int):
            offset = start
            while offset < end:
                stream.seek(offset)
                element, _ = vint(True)
                size, unknown = vint()
                body = stream.tell()
                stop = end if unknown else body + size
                if stop > end or stop <= offset or (unknown and element != 0x18538067):
                    raise MetadataError("unsupported_matroska_structure")
                yield element, body, stop
                offset = stop

        def data(start: int, end: int) -> bytes:
            if end - start > 2 * 1024 * 1024:
                raise MetadataError("unsupported_matroska_tags")
            stream.seek(start)
            return stream.read(end - start)

        all_names: set[tuple[int | None, bytes]] = set()

        def tag(start: int, end: int) -> None:
            seen: set[bytes] = set()
            track = None
            for element, body, stop in children(start, end):
                if element == 0x63C0:  # Targets: global or one track, default level 50.
                    for key, a, b in children(body, stop):
                        value = data(a, b)
                        if key == 0x68CA and int.from_bytes(value, "big") == 50:
                            continue
                        if key == 0x63C5:  # TrackUID; preserved via stream metadata.
                            if track is not None:
                                raise MetadataError("unsupported_matroska_targets")
                            track = int.from_bytes(value, "big") or None
                            continue
                        raise MetadataError("unsupported_matroska_targets")
                elif element == 0x67C8:  # SimpleTag
                    name = None
                    for key, a, b in children(body, stop):
                        if key == 0x45A3:
                            name = data(a, b).upper()
                        elif key == 0x4487:  # TagString
                            data(a, b)
                        elif key == 0x447A and data(a, b) == b"und":
                            continue
                        elif key == 0x4484 and int.from_bytes(data(a, b), "big") == 1:
                            continue
                        else:  # nested/binary/localized tags need a richer writer
                            raise MetadataError("unsupported_matroska_tags")
                    if not name or name in seen:
                        raise MetadataError("unsupported_matroska_tags")
                    seen.add(name)
                elif element not in (0xBF, 0xEC):  # CRC / padding
                    raise MetadataError("unsupported_matroska_tags")
            for name in seen:
                identity = (track, name)
                if identity in all_names:
                    raise MetadataError("unsupported_matroska_tags")
                all_names.add(identity)

        for element, start, end in children(0, total):
            if element != 0x18538067:  # Segment
                continue
            for child, body, stop in children(start, end):
                if child == 0x1254C367:  # Tags
                    for key, a, b in children(body, stop):
                        if key == 0x7373:
                            tag(a, b)
                        elif key not in (0xBF, 0xEC):
                            raise MetadataError("unsupported_matroska_tags")
