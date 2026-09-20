"""Finalize requested tags once all media-changing features have completed."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..download.staging import StagingError
from ..models.metadata import MetadataPolicy, MetadataReport
from ..utils.paths import locate_runtime_tool
from .metadata_normalizer import normalize_metadata
from .metadata_process import MetadataCancelled, MetadataError
from .metadata_source import read_source
from .metadata_verifier import (
    check_matroska_tags,
    container_kind,
    mp4_protection,
    native_protection,
    probe_media,
    verify_structure,
)
from .metadata_writers import (
    COMMENT_FIELDS,
    MATROSKA_FIELDS,
    extension_key,
    matroska_tags,
    read_comments,
    read_id3,
    read_matroska,
    read_mp4,
    supported_values,
    write_comments,
    write_id3,
    write_matroska,
    write_mp4,
)


def _tool(name: str, configured: str = "") -> str:
    if configured:
        path = Path(configured)
        if path.is_dir():
            path /= f"{name}.exe"
        elif name != "ffmpeg":
            path = path.parent / f"{name}.exe"
        if path.is_file():
            return str(path)
        raise MetadataError("tool_unavailable")
    try:
        return str(locate_runtime_tool(f"{name}.exe", f"{name}/{name}.exe", f"ffmpeg/{name}.exe"))
    except FileNotFoundError as exc:
        raise MetadataError("tool_unavailable") from exc


def finalize_metadata(
    staging: Any,
    policy: MetadataPolicy,
    source_path: str,
    cancel: Callable[[], bool],
    *,
    ffmpeg_location: str = "",
    on_report: Callable[[MetadataReport], None] | None = None,
) -> list[MetadataReport]:
    if not policy.enabled:
        return []
    media = staging.manifest.kept("media")
    # Source identity is one-video-per-worker. Includes consumed parents so VR renames
    # and audio extraction can inherit the same download's source without stem matching.
    observed = [a.path for a in staging.manifest.observed() if a.kind == "media"]
    # Content replacement may have moved a parent to .internal; the authority file
    # still holds the exact original path reported by this attempt.
    observed.extend(staging.assert_inside(p) for p in staging.read_attempt_paths())
    try:
        source = read_source(source_path, observed)
    except OSError:
        source = None
    reports = []
    for artifact in media:
        if cancel():
            raise MetadataCancelled()
        report = MetadataReport(artifact.id)
        reports.append(report)
        if source is None:
            report.expected.add("source")
            report.code = "source_unavailable"
            if on_report:
                on_report(report)
            continue
        report.expected.add("source")
        report.actual.add("source")
        try:
            ffprobe = _tool("ffprobe", ffmpeg_location)
            before = probe_media(artifact.path, ffprobe, cancel)
            kind = container_kind(before, Path(artifact.path).suffix.lower())
            audio_only = not any(
                s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
                for s in before.get("streams", [])
            )
            normalized = normalize_metadata(source, audio_only=audio_only)
            report.sources.update(normalized.sources)
            values, unsupported = supported_values(kind, normalized.values)
            report.fields.update(normalized.issues)
            report.fields.update(unsupported)
            # Size failures are requested fields too; they must not disappear from expectation.
            report.expected.update(normalized.values)
            if not values:
                report.code = "no_fields"
                if on_report:
                    on_report(report)
                continue
            native_before = native_protection(artifact.path, kind, values)
            spatial_before = mp4_protection(artifact.path) if kind == "mp4" else []
            candidate = staging.reserve_workfile(
                "metadata",
                Path(artifact.path).suffix,
                seed_from=artifact.id if kind not in ("matroska", "mp4") else None,
            )
            if kind == "mp4":
                write_mp4(artifact.path, candidate, values, _tool("AtomicParsley"), cancel)
            elif kind == "mp3":
                write_id3(candidate, values)
            elif kind in ("flac", "ogg", "opus"):
                write_comments(candidate, kind, values)
            else:
                check_matroska_tags(artifact.path)
                write_matroska(
                    artifact.path,
                    candidate,
                    values,
                    before,
                    _tool("ffmpeg", ffmpeg_location),
                    cancel,
                )
            if cancel():
                raise MetadataCancelled()
            after = probe_media(candidate, ffprobe, cancel)
            # Vorbis/Opus comments are exposed by ffprobe as *stream* tags.
            owned_stream_tags = (
                {COMMENT_FIELDS.get(k, extension_key(k)).lower() for k in values}
                if kind in ("opus", "ogg")
                else set()
            )
            # ffprobe exposes these native Vorbis comments via generic aliases.
            for native, alias in {
                "albumartist": "album_artist",
                "tracknumber": "track",
                "discnumber": "disc",
                "description": "comment",
            }.items():
                if native in owned_stream_tags:
                    owned_stream_tags.add(alias)
            verify_structure(before, after, owned_stream_tags)
            if native_before != native_protection(candidate, kind, values):
                raise MetadataError("protected_tags_changed")
            if kind == "mp4" and spatial_before != mp4_protection(candidate):
                raise MetadataError("protected_spatial_metadata_changed")
            if kind == "mp4":
                found = read_mp4(candidate, values)
            elif kind == "mp3":
                found = read_id3(candidate, values)
            elif kind in ("flac", "ogg", "opus"):
                found = read_comments(candidate, kind, values)
            else:
                found = read_matroska(after, values)
                owned = {MATROSKA_FIELDS.get(k, extension_key(k)).upper() for k in values}
                old_tags = {
                    k.upper(): v
                    for k, v in matroska_tags(before).items()
                    if k.upper() not in owned | {"ENCODER"}
                }
                new_tags = matroska_tags(after)
                if any(new_tags.get(k) != v for k, v in old_tags.items()):
                    raise MetadataError("protected_tags_changed")
            mismatched = {k for k, v in values.items() if found.get(k) != v}
            if mismatched:
                report.fields.update(dict.fromkeys(mismatched, "verify_failed"))
                raise MetadataError("verify_failed")
            if cancel():
                raise MetadataCancelled()
            staging.replace_artifact_content(artifact.id, candidate, producer="MetadataFinalizer")
            # Record only after the transaction accepted this exact candidate.
            report.actual.update(k for k in values if normalized.issues.get(k) != "truncated")
            report.fields.update({k: "verified" for k in values if k not in normalized.issues})
            report.code = "partial" if report.missing else "verified"
        except (MetadataCancelled, StagingError):
            raise
        except MetadataError as exc:
            report.code = str(exc)
            if not report.missing and report.code != "unsupported_container":
                report.expected.add("write")
        except Exception:
            # No user text, filenames or raw library exceptions in metadata telemetry.
            report.code = "metadata_failed"
            report.expected.add("write")
        if on_report:
            on_report(report)
    return reports
