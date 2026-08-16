from __future__ import annotations

from dataclasses import dataclass

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from .sdk_manager import SdkManagerClient
from .sdk_manager_match import (
    VerifiedSdkManagerTarget,
    verify_catalog_target_advertised,
)
from .sdk_manager_query import parse_jetson_query_output


@dataclass(frozen=True, slots=True)
class SdkManagerDiscovery:
    """Normalized result of checking one catalog target against SDK Manager.

    This is still discovery evidence, not an acquisition receipt or target lock.
    Raw SDK Manager output is intentionally not stored because query output can
    contain login/user-facing information. Only the facts needed by later
    acquisition steps are retained.
    """

    sdk_manager_version: str
    query_source: str
    target: VerifiedSdkManagerTarget


def discover_catalog_target(
    client: SdkManagerClient,
    target: ResolvedCatalogTarget,
    *,
    required_sdk_manager_target: str,
) -> SdkManagerDiscovery:
    """Confirm that one exact catalog target is advertised by SDK Manager.

    Current releases are queried first. If the exact JetPack version is absent,
    the archived catalog is queried once as a fallback. If the release exists
    in the current catalog but does not advertise the required target id, that
    mismatch is surfaced immediately rather than hidden by an archive fallback.
    """

    sdk_manager_version = client.version()

    current_releases = parse_jetson_query_output(client.query_jetson())
    try:
        verified = verify_catalog_target_advertised(
            target,
            current_releases,
            required_sdk_manager_target=required_sdk_manager_target,
        )
    except LookupError:
        archived_releases = parse_jetson_query_output(
            client.query_jetson(archived=True)
        )
        verified = verify_catalog_target_advertised(
            target,
            archived_releases,
            required_sdk_manager_target=required_sdk_manager_target,
        )
        query_source = "archived"
    else:
        query_source = "current"

    return SdkManagerDiscovery(
        sdk_manager_version=sdk_manager_version,
        query_source=query_source,
        target=verified,
    )
