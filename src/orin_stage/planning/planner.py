from __future__ import annotations

import string
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from orin_stage.acquisition.artifact_verification import CONSTRUCTION_ARTIFACT_KINDS
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.base.lock import load_target_lock
from orin_stage.base.receipt import base_directory_is_reusable, load_base_receipt
from orin_stage.base.recipe import construction_recipe_digest_v1
from orin_stage.catalog.resolver import ResolvedCatalogTarget

from .artifact_index import artifact_status
from .models import (
    ArtifactExpectation,
    ArtifactIndex,
    ArtifactIndexContent,
    ArtifactIndexRelationship,
    PlanArtifactStatus,
)


class PlannerError(ValueError):
    """Raised when a release plan input is invalid."""


class BasePlanStatus(str, Enum):
    BASE_REUSE = "base-reuse"
    CONSTRUCTION_REQUIRED = "construction-required"


@dataclass(frozen=True, slots=True)
class ManifestArtifactEvidence:
    kind: str
    filename: str
    sha256: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class NormalizedSdkManagerManifest:
    schema_known: bool
    canonical_id: str | None
    sdk_manager_target: str | None
    artifacts: tuple[ManifestArtifactEvidence, ...]
    unknown_artifact_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedSoftwareTarget:
    selector: str
    canonical_id: str
    jetpack_version: str
    l4t_version: str
    support_status: str
    supported: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "canonical_id": self.canonical_id,
            "jetpack_version": self.jetpack_version,
            "l4t_version": self.l4t_version,
            "support_status": self.support_status,
            "supported": self.supported,
        }


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
    kind: str
    role: str
    filename: str
    expected_sha256: str | None
    status: PlanArtifactStatus
    size: int | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "role": self.role,
            "filename": self.filename,
            "expected_sha256": self.expected_sha256,
            "status": self.status.value,
            "size": self.size,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    software_target: ResolvedSoftwareTarget
    hardware_profile: str
    artifacts: tuple[PlannedArtifact, ...]
    verified_cached_count: int
    download_required_count: int
    sdkm_decision_count: int
    known_download_bytes: int
    unknown_download_artifact_count: int
    download_total_complete: bool
    base_status: BasePlanStatus
    base_reason: str
    base_digest: str | None
    base_reference: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "software_target": self.software_target.to_dict(),
            "hardware_profile": self.hardware_profile,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "artifact_status_counts": {
                PlanArtifactStatus.VERIFIED_CACHED.value: self.verified_cached_count,
                PlanArtifactStatus.DOWNLOAD_REQUIRED.value: self.download_required_count,
                PlanArtifactStatus.SDKM_DECISION.value: self.sdkm_decision_count,
            },
            "known_download_bytes": self.known_download_bytes,
            "unknown_download_artifact_count": self.unknown_download_artifact_count,
            "download_total_complete": self.download_total_complete,
            "base": {
                "status": self.base_status.value,
                "reason": self.base_reason,
                "digest": self.base_digest,
                "reference": self.base_reference,
            },
        }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
    )


def normalize_sdk_manager_manifest(
    data: Mapping[str, object] | None,
) -> NormalizedSdkManagerManifest | None:
    """Read only the narrow normalized evidence schema; never guess raw SDKM data."""

    if data is None:
        return None
    expected_top_level = {
        "schema_version",
        "canonical_id",
        "sdk_manager_target",
        "artifacts",
    }
    if set(data) != expected_top_level or data.get("schema_version") != 1:
        return NormalizedSdkManagerManifest(False, None, None, (), ())

    canonical_id = data.get("canonical_id")
    sdk_manager_target = data.get("sdk_manager_target")
    artifacts = data.get("artifacts")
    if (
        not isinstance(canonical_id, str)
        or not canonical_id
        or not isinstance(sdk_manager_target, str)
        or not sdk_manager_target
        or not isinstance(artifacts, list)
    ):
        return NormalizedSdkManagerManifest(False, None, None, (), ())

    parsed: dict[str, ManifestArtifactEvidence] = {}
    unknown: set[str] = set()
    expected_artifact_fields = {"kind", "filename", "sha256", "size"}
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        if kind in parsed or kind in unknown or set(item) != expected_artifact_fields:
            parsed.pop(kind, None)
            unknown.add(kind)
            continue
        filename = item.get("filename")
        sha256 = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(filename, str)
            or not filename
            or (sha256 is not None and not _is_sha256(sha256))
            or (
                size is not None
                and (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                )
            )
        ):
            unknown.add(kind)
            continue
        parsed[kind] = ManifestArtifactEvidence(
            kind=kind,
            filename=filename,
            sha256=sha256.lower() if isinstance(sha256, str) else None,
            size=size,
        )

    return NormalizedSdkManagerManifest(
        schema_known=True,
        canonical_id=canonical_id,
        sdk_manager_target=sdk_manager_target,
        artifacts=tuple(parsed[kind] for kind in sorted(parsed)),
        unknown_artifact_kinds=tuple(sorted(unknown)),
    )


def _index_candidates(
    index: ArtifactIndex,
    target: ResolvedCatalogTarget,
    *,
    kind: str,
    filename: str,
    sdk_manager_target: str | None,
) -> tuple[tuple[ArtifactIndexContent, ArtifactIndexRelationship], ...]:
    candidates: list[tuple[ArtifactIndexContent, ArtifactIndexRelationship]] = []
    for content in index.contents:
        for relationship in content.relationships:
            if (
                relationship.source == "sdk-manager"
                and relationship.canonical_id == target.canonical_id
                and relationship.artifact_kind == kind
                and relationship.filename == filename
                and (
                    sdk_manager_target is None
                    or relationship.sdk_manager_target == sdk_manager_target
                )
            ):
                candidates.append((content, relationship))
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item[0].sha256,
                item[1].sdk_manager_target,
                item[1].receipt_reference,
            ),
        )
    )


def _valid_discovery_target(
    discovery: SdkManagerDiscovery | None,
    target: ResolvedCatalogTarget,
) -> str | None:
    if discovery is None:
        return None
    jetpack_version = str(target.record["release"]["jetpack"]["version"])
    if (
        discovery.target.canonical_id != target.canonical_id
        or discovery.target.jetpack_version != jetpack_version
    ):
        return None
    return discovery.target.sdk_manager_target


def _plan_artifact(
    target: ResolvedCatalogTarget,
    *,
    kind: str,
    index: ArtifactIndex,
    data_root: Path,
    sdk_manager_target: str | None,
    manifest: NormalizedSdkManagerManifest | None,
) -> PlannedArtifact:
    catalog_artifact = target.record["construction_inputs"][kind]
    filename = str(catalog_artifact["filename"])
    role = f"construction-{kind.replace('_', '-')}"

    manifest_artifact: ManifestArtifactEvidence | None = None
    manifest_unknown = False
    if manifest is not None:
        if not manifest.schema_known or manifest.canonical_id != target.canonical_id:
            manifest_unknown = True
        elif kind in manifest.unknown_artifact_kinds:
            manifest_unknown = True
        else:
            manifest_artifact = next(
                (item for item in manifest.artifacts if item.kind == kind),
                None,
            )
            if (
                manifest_artifact is not None
                and manifest_artifact.filename.casefold() != filename.casefold()
            ):
                manifest_artifact = None
                manifest_unknown = True

    candidates = _index_candidates(
        index,
        target,
        kind=kind,
        filename=filename,
        sdk_manager_target=sdk_manager_target,
    )
    candidate_hashes = {content.sha256 for content, _relationship in candidates}
    candidate_targets = {
        relationship.sdk_manager_target for _content, relationship in candidates
    }

    if manifest_unknown:
        return PlannedArtifact(
            kind=kind,
            role=role,
            filename=filename,
            expected_sha256=None,
            status=PlanArtifactStatus.SDKM_DECISION,
            size=None,
            reason=(
                "normalized manifest schema or artifact fields are unknown; "
                "SDK Manager must decide this artifact"
            ),
        )

    expected_sha256 = (
        manifest_artifact.sha256 if manifest_artifact is not None else None
    )
    size = manifest_artifact.size if manifest_artifact is not None else None
    if expected_sha256 is None and len(candidate_hashes) == 1:
        expected_sha256 = next(iter(candidate_hashes))
        matching_content = next(
            content for content, _relationship in candidates if content.sha256 == expected_sha256
        )
        if size is None:
            size = matching_content.size

    effective_sdk_manager_target = sdk_manager_target
    if effective_sdk_manager_target is None and len(candidate_targets) == 1:
        effective_sdk_manager_target = next(iter(candidate_targets))

    if expected_sha256 is None or effective_sdk_manager_target is None:
        return PlannedArtifact(
            kind=kind,
            role=role,
            filename=filename,
            expected_sha256=expected_sha256,
            status=PlanArtifactStatus.SDKM_DECISION,
            size=size,
            reason="exact whole-file identity or SDK Manager target evidence is unavailable",
        )

    expectation = ArtifactExpectation(
        canonical_id=target.canonical_id,
        artifact_kind=kind,
        filename=filename,
        sdk_manager_target=effective_sdk_manager_target,
        expected_sha256=expected_sha256,
        expected_size=size,
    )
    status = artifact_status(index, expectation, data_root=data_root)
    if status is PlanArtifactStatus.VERIFIED_CACHED:
        reason = "exact receipt relationship plus whole-file size/SHA-256 is verified"
    else:
        reason = "expected whole-file identity is not verified in the local acquisition cache"
    return PlannedArtifact(
        kind=kind,
        role=role,
        filename=filename,
        expected_sha256=expected_sha256,
        status=status,
        size=size,
        reason=reason,
    )


def _find_reusable_base(
    target: ResolvedCatalogTarget,
    artifacts: tuple[PlannedArtifact, ...],
    base_directories: Iterable[Path],
) -> tuple[str, str] | None:
    expected_hashes = {
        artifact.kind: artifact.expected_sha256
        for artifact in artifacts
        if artifact.expected_sha256 is not None
    }
    recipe_digest = construction_recipe_digest_v1()
    for directory in sorted((Path(path).resolve() for path in base_directories), key=str):
        if not base_directory_is_reusable(directory):
            continue
        try:
            lock = load_target_lock(directory / "lock.json")
            receipt = load_base_receipt(directory / "receipt.json")
            target_data = lock["target"]
            construction = lock["construction"]
            lock_artifacts = lock["artifacts"]
            if not all(
                isinstance(item, Mapping)
                for item in (target_data, construction, lock_artifacts)
            ):
                continue
            if target_data["canonical_id"] != target.canonical_id:
                continue
            if construction["recipe_digest"] != recipe_digest:
                continue
            if any(
                not isinstance(lock_artifacts.get(kind), Mapping)
                or lock_artifacts[kind].get("sha256") != sha256
                for kind, sha256 in expected_hashes.items()
            ):
                continue
            base_digest = receipt.get("base_digest")
            if isinstance(base_digest, str):
                return base_digest, str(directory)
        except (KeyError, OSError, TypeError, ValueError, RuntimeError):
            continue
    return None


def plan_release(
    target: ResolvedCatalogTarget,
    *,
    hardware_profile: str,
    artifact_index: ArtifactIndex,
    data_root: Path,
    sdk_manager_discovery: SdkManagerDiscovery | None = None,
    sdk_manager_manifest: Mapping[str, object] | None = None,
    base_directories: Iterable[Path] = (),
) -> ReleasePlan:
    """Build a deterministic read-only JP6 release plan from local evidence."""

    profiles = tuple(str(item) for item in target.record["target"]["hardware_profiles"])
    if hardware_profile not in profiles:
        raise PlannerError(
            f"hardware profile {hardware_profile!r} is not declared by {target.canonical_id}"
        )

    manifest = normalize_sdk_manager_manifest(sdk_manager_manifest)
    discovery_target = _valid_discovery_target(sdk_manager_discovery, target)
    manifest_target = (
        manifest.sdk_manager_target
        if manifest is not None
        and manifest.schema_known
        and manifest.canonical_id == target.canonical_id
        else None
    )
    sdk_manager_target = discovery_target or manifest_target
    evidence_conflict = (
        sdk_manager_discovery is not None and discovery_target is None
    ) or (
        discovery_target is not None
        and manifest_target is not None
        and discovery_target != manifest_target
    )
    if evidence_conflict:
        sdk_manager_target = None
        manifest = NormalizedSdkManagerManifest(False, None, None, (), ())

    artifacts = tuple(
        _plan_artifact(
            target,
            kind=kind,
            index=artifact_index,
            data_root=Path(data_root),
            sdk_manager_target=sdk_manager_target,
            manifest=manifest,
        )
        for kind in CONSTRUCTION_ARTIFACT_KINDS
    )
    counts = {
        status: sum(artifact.status is status for artifact in artifacts)
        for status in PlanArtifactStatus
    }
    known_download_bytes = sum(
        artifact.size
        for artifact in artifacts
        if artifact.status is PlanArtifactStatus.DOWNLOAD_REQUIRED
        and artifact.size is not None
    )
    unknown_download_artifact_count = sum(
        artifact.status is PlanArtifactStatus.SDKM_DECISION
        or (
            artifact.status is PlanArtifactStatus.DOWNLOAD_REQUIRED
            and artifact.size is None
        )
        for artifact in artifacts
    )

    reusable = _find_reusable_base(target, artifacts, base_directories)
    if reusable is None:
        base_status = BasePlanStatus.CONSTRUCTION_REQUIRED
        base_digest = None
        base_reference = None
        base_reason = "no matching validated immutable base was supplied"
    else:
        base_status = BasePlanStatus.BASE_REUSE
        base_digest, base_reference = reusable
        base_reason = "matching target, recipe, input hashes and frozen package state are validated"

    release = target.record["release"]
    software_target = ResolvedSoftwareTarget(
        selector=target.selector,
        canonical_id=target.canonical_id,
        jetpack_version=str(release["jetpack"]["version"]),
        l4t_version=str(release["l4t"]["version"]),
        support_status=target.support_status,
        supported=target.is_supported,
    )
    return ReleasePlan(
        software_target=software_target,
        hardware_profile=hardware_profile,
        artifacts=artifacts,
        verified_cached_count=counts[PlanArtifactStatus.VERIFIED_CACHED],
        download_required_count=counts[PlanArtifactStatus.DOWNLOAD_REQUIRED],
        sdkm_decision_count=counts[PlanArtifactStatus.SDKM_DECISION],
        known_download_bytes=known_download_bytes,
        unknown_download_artifact_count=unknown_download_artifact_count,
        download_total_complete=unknown_download_artifact_count == 0,
        base_status=base_status,
        base_reason=base_reason,
        base_digest=base_digest,
        base_reference=base_reference,
    )
