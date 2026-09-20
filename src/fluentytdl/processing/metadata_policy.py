"""Freeze metadata intent once, before queue persistence."""

from __future__ import annotations

from typing import Any

from ..models.metadata import METADATA_POLICY, MetadataPolicy


def freeze_metadata_policy(opts: dict[str, Any], default: bool = True) -> MetadataPolicy:
    if METADATA_POLICY in opts:
        return MetadataPolicy.from_dict(opts[METADATA_POLICY])
    explicit = opts.get("addmetadata")
    if type(explicit) is bool:
        enabled = explicit
    else:
        enabled = any(
            isinstance(pp, dict) and pp.get("key") == "FFmpegMetadata"
            for pp in opts.get("postprocessors", [])
        ) or bool(default)
    # Fast paths have no media to tag. Preserve their independent preferences.
    enabled = enabled and not bool(opts.get("skip_download"))
    chapters = opts.get("embedchapters")
    if type(chapters) is not bool:
        chapters = enabled or bool(opts.get("sponsorblock_mark"))
    policy = MetadataPolicy(enabled=enabled, chapter_enabled=chapters)
    opts[METADATA_POLICY] = policy.as_dict()
    return policy


def configure_metadata(opts: dict[str, Any], default: bool = True) -> MetadataPolicy:
    policy = freeze_metadata_policy(opts, default)
    opts["postprocessors"] = [
        pp
        for pp in opts.get("postprocessors", [])
        if not (isinstance(pp, dict) and pp.get("key") == "FFmpegMetadata")
    ]
    # Only the finalizer owns text tags; chapters are a separate yt-dlp operation.
    opts["addmetadata"] = False
    opts["embedchapters"] = policy.chapter_enabled or bool(opts.get("sponsorblock_mark"))
    return policy
