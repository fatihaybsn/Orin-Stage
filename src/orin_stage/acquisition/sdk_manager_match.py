from __future__ import annotations

from dataclasses import dataclass

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from .sdk_manager_query import (
    SdkManagerJetsonRelease,
    find_jetpack_release,
)


class SdkManagerTargetMismatchError(ValueError):
    """Raised when SDK Manager does not advertise the required target id."""

    def __init__(
        self,
        *,
        canonical_id: str,
        jetpack_version: str,
        required_target: str,
        advertised_targets: tuple[str, ...],
    ) -> None:
        self.canonical_id = canonical_id
        self.jetpack_version = jetpack_version
        self.required_target = required_target
        self.advertised_targets = advertised_targets
        super().__init__(
            f"SDK Manager advertises JetPack {jetpack_version} for "
            f"{advertised_targets!r}, not required target {required_target!r} "
            f"for catalog target {canonical_id!r}"
        )


@dataclass(frozen=True, slots=True)
class VerifiedSdkManagerTarget:
    """Normalized proof that one catalog target is advertised by SDK Manager.

    This is discovery evidence, not the final target lock. It records only the
    exact facts established by the query: catalog identity, JetPack version,
    NVIDIA's human-facing label and the exact SDK Manager target identifier.
    """

    canonical_id: str
    jetpack_version: str
    sdk_manager_display_label: str
    sdk_manager_target: str


def verify_catalog_target_advertised(
    target: ResolvedCatalogTarget,
    releases: tuple[SdkManagerJetsonRelease, ...],
    *,
    required_sdk_manager_target: str,
) -> VerifiedSdkManagerTarget:
    """Verify exact catalog JetPack identity against normalized SDKM query data.

    No fuzzy matching or target inference is performed. The catalog's exact
    JetPack version must be present and the caller-selected SDK Manager target
    id must be explicitly advertised for that exact version.

    The SDK Manager target id is intentionally supplied by the caller in this
    slice. Hardware-profile dereferencing/mapping is a later hardening concern;
    we do not silently infer a board identity from broad release-level catalog
    fields.
    """

    jetpack_version = target.record["release"]["jetpack"]["version"]
    release = find_jetpack_release(releases, jetpack_version)

    if required_sdk_manager_target not in release.targets:
        raise SdkManagerTargetMismatchError(
            canonical_id=target.canonical_id,
            jetpack_version=jetpack_version,
            required_target=required_sdk_manager_target,
            advertised_targets=release.targets,
        )

    return VerifiedSdkManagerTarget(
        canonical_id=target.canonical_id,
        jetpack_version=jetpack_version,
        sdk_manager_display_label=release.display_label,
        sdk_manager_target=required_sdk_manager_target,
    )
