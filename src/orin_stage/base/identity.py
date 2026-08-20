from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Iterable, Mapping
from typing import Any

from orin_stage.acquisition.artifact_verification import (
    CONSTRUCTION_ARTIFACT_KINDS,
    VerifiedAcquisitionArtifact,
)


BASE_IDENTITY_POLICY_ID = "base-identity-v1"
BASE_IDENTITY_POLICY_VERSION = 1


class BaseIdentityError(ValueError):
    """Raised when immutable-base identity inputs are incomplete or invalid."""


def _require_sha256(name: str, value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in string.hexdigits for char in normalized):
        raise BaseIdentityError(f"{name} must be a 64-character SHA-256 hex digest")
    return normalized


def _require_mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise BaseIdentityError(f"canonical target lock is missing mapping {key!r}")
    return value


def build_base_target_projection(target_lock: Mapping[str, Any]) -> dict[str, object]:
    """Project one canonical target lock onto base-affecting target state only.

    Acquisition provenance, SDK Manager/response-file identity, validation policy,
    QEMU evidence, hardware-profile metadata and licensing remain in the canonical
    lock but deliberately do not participate in immutable-base identity.
    """

    target = _require_mapping(target_lock, "target")
    packages = _require_mapping(target_lock, "construction_packages")
    seed = _require_mapping(packages, "seed")
    package_entries = packages.get("packages")
    if not isinstance(package_entries, list):
        raise BaseIdentityError("canonical target lock is missing construction package list")

    required_target = (
        "canonical_id",
        "jetpack_version",
        "l4t_version",
        "ubuntu_suite",
        "target_abi",
        "debian_architecture",
        "repository_platform",
    )
    projected_target: dict[str, object] = {}
    for key in required_target:
        value = target.get(key)
        if not isinstance(value, str) or not value:
            raise BaseIdentityError(f"canonical target lock target.{key} is missing")
        projected_target[key] = value

    for key in ("name", "version", "architecture"):
        value = seed.get(key)
        if not isinstance(value, str) or not value:
            raise BaseIdentityError(f"construction package seed {key!r} is missing")

    normalized_packages: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in package_entries:
        if not isinstance(item, Mapping):
            raise BaseIdentityError("construction package entries must be mappings")
        normalized: dict[str, str] = {}
        for key in ("name", "version", "architecture", "operation", "sha256"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                raise BaseIdentityError(f"construction package field {key!r} is missing")
            normalized[key] = value
        normalized["sha256"] = _require_sha256(
            f"construction package {normalized['name']!r} sha256", normalized["sha256"]
        )
        package_key = (normalized["name"], normalized["architecture"])
        if package_key in seen:
            raise BaseIdentityError(
                "duplicate construction package identity: "
                f"{normalized['name']}:{normalized['architecture']}"
            )
        seen.add(package_key)
        normalized_packages.append(normalized)

    normalized_packages.sort(key=lambda item: (item["name"], item["architecture"]))
    return {
        "policy_id": BASE_IDENTITY_POLICY_ID,
        "policy_version": BASE_IDENTITY_POLICY_VERSION,
        "target": projected_target,
        "construction_packages": {
            "seed": {
                "name": str(seed["name"]),
                "version": str(seed["version"]),
                "architecture": str(seed["architecture"]),
            },
            "packages": normalized_packages,
        },
    }


def build_base_target_projection_digest(target_lock: Mapping[str, Any]) -> str:
    payload = build_base_target_projection(target_lock)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_base_digest(
    *,
    base_target_projection_digest: str,
    construction_recipe_digest: str,
    artifacts: Iterable[VerifiedAcquisitionArtifact],
) -> str:
    """Build the path-independent identity of one immutable base.

    The canonical lock can contain acquisition/execution/validation evidence that
    must not force a base rebuild. Only its ``base-identity-v1`` projection enters
    this digest, together with the construction recipe and verified construction
    artifact bytes.
    """

    projection_digest = _require_sha256(
        "base_target_projection_digest", base_target_projection_digest
    )
    recipe_digest = _require_sha256(
        "construction_recipe_digest", construction_recipe_digest
    )

    by_kind: dict[str, str] = {}
    for artifact in artifacts:
        if artifact.kind in by_kind:
            raise BaseIdentityError(
                f"duplicate construction artifact kind: {artifact.kind!r}"
            )
        by_kind[artifact.kind] = _require_sha256(
            f"artifact {artifact.kind!r} sha256", artifact.sha256
        )

    expected_kinds = frozenset(CONSTRUCTION_ARTIFACT_KINDS)
    actual_kinds = frozenset(by_kind)
    if actual_kinds != expected_kinds:
        missing = sorted(expected_kinds - actual_kinds)
        unexpected = sorted(actual_kinds - expected_kinds)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        raise BaseIdentityError(
            "base identity requires the exact construction artifact set"
            + (f" ({', '.join(details)})" if details else "")
        )

    payload = {
        "schema_version": 1,
        "base_identity_policy": BASE_IDENTITY_POLICY_ID,
        "base_target_projection_digest": projection_digest,
        "construction_recipe_digest": recipe_digest,
        "artifacts": [
            {"kind": kind, "sha256": by_kind[kind]} for kind in sorted(by_kind)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
