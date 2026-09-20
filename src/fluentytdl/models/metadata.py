"""Task-scoped metadata contracts; no application or UI dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any

METADATA_POLICY = "__fluentytdl_metadata_policy"


@dataclass(frozen=True)
class MetadataPolicy:
    enabled: bool
    chapter_enabled: bool
    schema_version: int = 1
    profile: str = "accurate_v1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MetadataPolicy:
        if value.get("schema_version") != 1 or value.get("profile") != "accurate_v1":
            raise ValueError("unsupported_metadata_policy")
        if type(value.get("enabled")) is not bool or type(value.get("chapter_enabled")) is not bool:
            raise ValueError("invalid_metadata_policy")
        return cls(enabled=value["enabled"], chapter_enabled=value["chapter_enabled"])


@dataclass
class NormalizedMetadata:
    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    issues: dict[str, str] = field(default_factory=dict)


@dataclass
class MetadataReport:
    artifact_id: str
    expected: set[str] = field(default_factory=set)
    actual: set[str] = field(default_factory=set)
    fields: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    code: str = "verified"

    @property
    def missing(self) -> set[str]:
        return self.expected - self.actual

    def tokens(self, fields: set[str]) -> set[str]:
        return {f"embedded:metadata:{self.reference}:{key}" for key in fields}

    @property
    def reference(self) -> str:
        # Manifest IDs can contain user titles. Telemetry uses an opaque reference.
        return sha256(self.artifact_id.encode("utf-8")).hexdigest()[:16]
