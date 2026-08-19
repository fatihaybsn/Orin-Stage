from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Iterable

from orin_stage.acquisition.artifact_verification import (
    CONSTRUCTION_ARTIFACT_KINDS,
    VerifiedAcquisitionArtifact,
)


class BaseIdentityError(ValueError):
    """Raised when immutable-base identity inputs are incomplete or invalid."""


def _require_sha256(name: str, value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in string.hexdigits for char in normalized):
        raise BaseIdentityError(f"{name} must be a 64-character SHA-256 hex digest")
    return normalized


def build_base_digest(
    *,
    target_lock_digest: str,
    construction_recipe_digest: str,
    artifacts: Iterable[VerifiedAcquisitionArtifact],
) -> str:
    """Build the path-independent identity of one immutable base.

    ``acquisition_digest`` is deliberately not an input. It identifies the
    SDK Manager acquisition transaction and may change with acquisition-only
    details such as SDK Manager/response-file identity. A base is instead
    identified by the exact target lock, the construction recipe, and the
    verified byte identity of every construction artifact.
    """

    lock_digest = _require_sha256("target_lock_digest", target_lock_digest)
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
        "target_lock_digest": lock_digest,
        "construction_recipe_digest": recipe_digest,
        "artifacts": [
            {"kind": kind, "sha256": by_kind[kind]} for kind in sorted(by_kind)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
