from __future__ import annotations

import string
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class PlanningModelError(ValueError):
    """Raised when persisted planning data is malformed."""


class PlanArtifactStatus(str, Enum):
    VERIFIED_CACHED = "verified-cached"
    DOWNLOAD_REQUIRED = "download-required"
    SDKM_DECISION = "sdkm-decision"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in string.hexdigits for character in value)


@dataclass(frozen=True, slots=True)
class ArtifactExpectation:
    canonical_id: str
    artifact_kind: str
    filename: str
    sdk_manager_target: str
    expected_sha256: str | None
    expected_size: int | None = None
    required_component_groups: tuple[str, ...] = ()

    def has_sufficient_evidence(self) -> bool:
        return (
            bool(self.canonical_id)
            and bool(self.artifact_kind)
            and bool(self.filename)
            and bool(self.sdk_manager_target)
            and isinstance(self.expected_sha256, str)
            and _is_sha256(self.expected_sha256)
            and (self.expected_size is None or self.expected_size >= 0)
        )


@dataclass(frozen=True, slots=True)
class ArtifactIndexRelationship:
    source: str
    canonical_id: str
    artifact_kind: str
    filename: str
    sdk_manager_target: str
    component_groups: tuple[str, ...]
    local_path: str
    acquisition_digest: str
    receipt_reference: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "canonical_id": self.canonical_id,
            "artifact_kind": self.artifact_kind,
            "filename": self.filename,
            "sdk_manager_target": self.sdk_manager_target,
            "component_groups": list(self.component_groups),
            "local_path": self.local_path,
            "acquisition_digest": self.acquisition_digest,
            "receipt_reference": self.receipt_reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ArtifactIndexRelationship":
        try:
            groups = data["component_groups"]
            if not isinstance(groups, list) or not all(
                isinstance(group, str) for group in groups
            ):
                raise TypeError
            values = {
                key: data[key]
                for key in (
                    "source",
                    "canonical_id",
                    "artifact_kind",
                    "filename",
                    "sdk_manager_target",
                    "local_path",
                    "acquisition_digest",
                    "receipt_reference",
                )
            }
            if not all(isinstance(value, str) and value for value in values.values()):
                raise TypeError
        except (KeyError, TypeError) as exc:
            raise PlanningModelError("malformed artifact index relationship") from exc
        return cls(
            source=str(values["source"]),
            canonical_id=str(values["canonical_id"]),
            artifact_kind=str(values["artifact_kind"]),
            filename=str(values["filename"]),
            sdk_manager_target=str(values["sdk_manager_target"]),
            component_groups=tuple(groups),
            local_path=str(values["local_path"]),
            acquisition_digest=str(values["acquisition_digest"]),
            receipt_reference=str(values["receipt_reference"]),
        )

    def sort_key(self) -> tuple[object, ...]:
        return (
            self.canonical_id,
            self.artifact_kind,
            self.filename,
            self.sdk_manager_target,
            self.component_groups,
            self.local_path,
            self.acquisition_digest,
            self.receipt_reference,
        )


@dataclass(frozen=True, slots=True)
class ArtifactIndexContent:
    sha256: str
    size: int
    relationships: tuple[ArtifactIndexRelationship, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "relationships": [item.to_dict() for item in self.relationships],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ArtifactIndexContent":
        try:
            sha256 = data["sha256"]
            size = data["size"]
            relationships = data["relationships"]
            if not isinstance(sha256, str) or not _is_sha256(sha256):
                raise TypeError
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise TypeError
            if not isinstance(relationships, list) or not relationships:
                raise TypeError
            parsed = tuple(
                ArtifactIndexRelationship.from_dict(item)
                for item in relationships
                if isinstance(item, Mapping)
            )
            if len(parsed) != len(relationships):
                raise TypeError
        except (KeyError, TypeError) as exc:
            raise PlanningModelError("malformed artifact index content") from exc
        return cls(sha256=sha256.lower(), size=size, relationships=parsed)


@dataclass(frozen=True, slots=True)
class ArtifactIndex:
    schema_version: int
    contents: tuple[ArtifactIndexContent, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contents": [content.to_dict() for content in self.contents],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ArtifactIndex":
        contents = data.get("contents")
        if data.get("schema_version") != 1 or not isinstance(contents, list):
            raise PlanningModelError("unsupported or malformed artifact index")
        parsed = tuple(
            ArtifactIndexContent.from_dict(item)
            for item in contents
            if isinstance(item, Mapping)
        )
        if len(parsed) != len(contents):
            raise PlanningModelError("malformed artifact index content")
        return cls(schema_version=1, contents=parsed)
