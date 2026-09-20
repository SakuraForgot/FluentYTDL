"""Container adapters. All writes target a transaction-owned candidate."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from mutagen import id3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4Tags
from mutagen.mp4._atom import Atoms
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .metadata_process import MetadataCancelled, MetadataError, run_tool

MP4_FIELDS = {
    "title": ("©nam", "--title"),
    "artist": ("©ART", "--artist"),
    "album_artist": ("aART", "--albumArtist"),
    "album": ("©alb", "--album"),
    "year": ("©day", "--year"),
    "comment": ("©cmt", "--comment"),
    "description": ("ldes", "--longdesc"),
    "genre": ("©gen", "--genre"),
    "composer": ("©wrt", "--composer"),
    "copyright": ("cprt", "--copyright"),
    "track_number": ("trkn", "--tracknum"),
    "disc_number": ("disk", "--disk"),
    "series": ("tvsh", "--TVShowName"),
    "episode_id": ("tven", "--TVEpisode"),
    "season_number": ("tvsn", "--TVSeasonNum"),
    "episode_number": ("tves", "--TVEpisodeNum"),
}
ID3_FIELDS = {
    "title": "TIT2",
    "artist": "TPE1",
    "album_artist": "TPE2",
    "album": "TALB",
    "genre": "TCON",
    "composer": "TCOM",
    "copyright": "TCOP",
    "track_number": "TRCK",
    "disc_number": "TPOS",
}
COMMENT_FIELDS = {
    "title": "TITLE",
    "artist": "ARTIST",
    "album_artist": "ALBUMARTIST",
    "album": "ALBUM",
    "year": "DATE",
    "comment": "COMMENT",
    "description": "DESCRIPTION",
    "genre": "GENRE",
    "composer": "COMPOSER",
    "copyright": "COPYRIGHT",
    "track_number": "TRACKNUMBER",
    "disc_number": "DISCNUMBER",
}
MATROSKA_FIELDS = {**COMMENT_FIELDS, "track_number": "PART_NUMBER", "disc_number": "DISC_NUMBER"}


def matroska_tags(probe: dict) -> dict[str, str]:
    # FFmpeg demuxes Matroska PART_NUMBER under its generic "track" alias.
    return {
        "PART_NUMBER" if k.upper() == "TRACK" else k.upper(): str(v)
        for k, v in probe.get("format", {}).get("tags", {}).items()
    }


def extension_key(key: str) -> str:
    return "FLUENTYTDL_" + key.upper()


def supported_values(kind: str, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    selected, omitted = {}, {}
    for key, value in values.items():
        # AP's ordinary text fields have a 255-byte ceiling. Never silently truncate a title.
        if (
            kind == "mp4"
            and key in MP4_FIELDS
            and key != "description"
            and len(value.encode("utf-8")) > 255
        ):
            omitted[key] = "unsupported_size"
        else:
            selected[key] = value
    return selected, omitted


def write_mp4(
    source: str, output: str, values: dict[str, str], tool: str, cancel: Callable[[], bool]
) -> None:
    argv = [tool, source]
    for key, value in values.items():
        if key in MP4_FIELDS:
            argv.extend([MP4_FIELDS[key][1], value])
        else:
            argv.extend(["--rDNSatom", value, "name=" + key.upper(), "domain=com.fluentytdl"])
    # Force a new file: AP's in-place optimization can miscount padding after
    # artwork embedding ("insufficient space to retag ...", even on a copy).
    # Staging owns the empty output candidate and all adoption/cleanup operations.
    argv.extend(["--output", output])
    if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 > 30000:
        raise MetadataError("unsupported_command_size")
    run_tool(argv, cancel)
    if cancel():
        raise MetadataCancelled()
    if not Path(output).is_file() or Path(output).stat().st_size == 0:
        raise MetadataError("mp4_output_missing")


def mp4_tags(path: str) -> MP4Tags:
    # MP4() requires an audio track. Read atoms directly to support video-only MP4.
    with open(path, "rb") as stream:
        atoms = Atoms(stream)
        try:
            atoms.path(b"moov", b"udta", b"meta", b"ilst")
        except KeyError:
            return MP4Tags()
        return MP4Tags(atoms, stream)


def read_mp4(path: str, keys: dict[str, str]) -> dict[str, str]:
    tags = mp4_tags(path)
    found = {}
    for key in keys:
        native = MP4_FIELDS[key][0] if key in MP4_FIELDS else f"----:com.fluentytdl:{key.upper()}"
        values = tags.get(native, [])
        if not values:
            continue
        value = values[0]
        if key in ("track_number", "disc_number"):
            value = value[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        found[key] = str(value)
    return found


def load_id3(path: str) -> id3.ID3:
    try:
        return id3.ID3(path, translate=False)
    except id3.ID3NoHeaderError:
        return id3.ID3()


def write_id3(path: str, values: dict[str, str]) -> None:
    tags = load_id3(path)
    version = tags.version[1] if tags else 3
    if version not in (3, 4):
        raise MetadataError("unsupported_id3_version")
    for key, value in values.items():
        if key in ID3_FIELDS:
            tags.add(getattr(id3, ID3_FIELDS[key])(encoding=1, text=[value]))
        elif key == "year":
            tags.add((id3.TDRC if version == 4 else id3.TYER)(encoding=1, text=[value]))
        elif key == "comment":
            tags.add(id3.COMM(encoding=1, lang="eng", desc="FLUENTYTDL_SOURCE", text=[value]))
        else:
            tags.add(id3.TXXX(encoding=1, desc=extension_key(key), text=[value]))
    tags.save(path, v2_version=version, v1=1)


def read_id3(path: str, keys: dict[str, str]) -> dict[str, str]:
    tags = load_id3(path)
    found = {}
    for key in keys:
        if key in ID3_FIELDS:
            frame = ID3_FIELDS[key]
        elif key == "year":
            frame = "TDRC" if tags.version[1] == 4 else "TYER"
        elif key == "comment":
            frame = "COMM:FLUENTYTDL_SOURCE:eng"
        else:
            frame = "TXXX:" + extension_key(key)
        if frame in tags:
            found[key] = str(tags[frame])
    return found


def comment_file(path: str, kind: str):
    return {"flac": FLAC, "opus": OggOpus, "ogg": OggVorbis}[kind](path)


def write_comments(path: str, kind: str, values: dict[str, str]) -> None:
    audio = comment_file(path, kind)
    if audio.tags is None:
        audio.add_tags()
    for key, value in values.items():
        audio[COMMENT_FIELDS.get(key, extension_key(key))] = (
            json.loads(values["authors_json"])
            if key == "artist" and "authors_json" in values
            else [value]
        )
    audio.save()


def read_comments(path: str, kind: str, keys: dict[str, str]) -> dict[str, str]:
    audio = comment_file(path, kind)
    return {
        key: "; ".join(audio[native])
        for key in keys
        if (native := COMMENT_FIELDS.get(key, extension_key(key))) in audio
    }


def write_matroska(
    source: str,
    output: str,
    values: dict[str, str],
    original: dict,
    ffmpeg: str,
    cancel: Callable[[], bool],
) -> None:
    tags = matroska_tags(original)
    for key, value in values.items():
        native = MATROSKA_FIELDS.get(key, extension_key(key))
        # Do not leave conflicting case variants of a tag behind.
        tags = {k: v for k, v in tags.items() if k.lower() != native.lower()}
        tags[native] = value

    def escape(value: str) -> str:
        for char in ("\\", "=", ";", "#", "\n"):
            value = value.replace(char, "\\" + char)
        return value

    metadata_file = Path(output + ".ffmetadata")
    # FFmpeg's line-continuation reader treats even an escaped terminal backslash
    # as continuation. Pass these exceptional values as discrete argv entries.
    direct = {k: str(v) for k, v in tags.items() if str(v).endswith("\\") or "\r" in str(v)}
    metadata_file.write_text(
        ";FFMETADATA1\n"
        + "".join(f"{escape(k)}={escape(str(v))}\n" for k, v in tags.items() if k not in direct),
        encoding="utf-8",
        newline="\n",
    )
    argv = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source,
        "-f",
        "ffmetadata",
        "-i",
        str(metadata_file),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "1",
        "-map_chapters",
        "0",
    ]
    for key, value in direct.items():
        argv.extend(["-metadata", f"{key}={value}"])
    argv.append(output)
    if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 > 30000:
        raise MetadataError("unsupported_command_size")
    run_tool(argv, cancel)


def read_matroska(probe: dict, keys: dict[str, str]) -> dict[str, str]:
    tags = matroska_tags(probe)
    return {
        key: tags[native]
        for key in keys
        if (native := MATROSKA_FIELDS.get(key, extension_key(key))) in tags
    }
