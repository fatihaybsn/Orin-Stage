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

    SDK Manager's primary current catalog is queried first, followed by its
    ``--show-all-versions`` current catalog and finally the archived catalog.
    The exact successful visibility scope is retained so download execution can
    use the same SDK Manager flag. A target mismatch in any scope that contains
    the requested release is surfaced immediately rather than hidden by a wider
    fallback.
    """

    sdk_manager_version = client.version()

    primary_releases = parse_jetson_query_output(
        client.query_jetson(primary_only=True)
    )
    try:
        verified = verify_catalog_target_advertised(
            target,
            primary_releases,
            required_sdk_manager_target=required_sdk_manager_target,
        )
    except LookupError:
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
            query_source = "current-all"
    else:
        query_source = "current"

    return SdkManagerDiscovery(
        sdk_manager_version=sdk_manager_version,
        query_source=query_source,
        target=verified,
    )
