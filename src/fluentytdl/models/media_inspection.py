"""Read-only inspection results; deliberately independent of download outcomes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class InspectionResult:
    path: str
    file: dict[str, Any] = field(default_factory=dict)
    read_at: str = ""
    status: str = "ready"
    fields: list[dict[str, Any]] = field(default_factory=list)
    streams: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    reader_versions: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def report(self, *, include_path: bool = False, include_raw: bool = False) -> dict:
        data = deepcopy(asdict(self))
        data["schema_version"] = 1
        if not include_path:
            data.pop("path")
        if not include_raw:
            data.pop("raw")
            data.pop("tags")
            for section in (*data["streams"], *data["chapters"]):
                section.pop("tags", None)
        elif not include_path:
            data["raw"].get("format", {}).pop("filename", None)
        return data
