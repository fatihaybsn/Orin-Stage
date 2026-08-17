from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from orin_stage.catalog.resolver import ResolvedCatalogTarget


class AcquisitionArtifactError(RuntimeError):
    """Base error for downloaded artifact verification."""


class AcquisitionArtifactMissingError(AcquisitionArtifactError):
    pass


class AcquisitionArtifactAmbiguousError(AcquisitionArtifactError):
    pass


class AcquisitionArtifactChecksumError(AcquisitionArtifactError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedAcquisitionArtifact:
    kind: str
    filename: str
    relative_path: str
    size: int
    sha1: str
    sha256: str
    official_sha1: str


def _hash_file(path: Path) -> tuple[str, str, int]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest(), size


def _find_exact_regular_file(root: Path, filename: str) -> Path:
    matches = [
        candidate
        for candidate in root.rglob(filename)
        if candidate.is_file() and not candidate.is_symlink()
    ]
    if not matches:
        raise AcquisitionArtifactMissingError(
            f"expected SDK Manager artifact is missing: {filename}"
        )
    if len(matches) != 1:
        relative = tuple(str(path.relative_to(root)) for path in matches)
        raise AcquisitionArtifactAmbiguousError(
            f"artifact filename {filename!r} is ambiguous under shared download root: "
            f"{relative!r}"
        )
    return matches[0]


def verify_catalog_construction_artifacts(
    target: ResolvedCatalogTarget,
    *,
    download_root: Path,
) -> tuple[VerifiedAcquisitionArtifact, ...]:
    """Verify the catalog-critical BSP and Sample RootFS in shared SDKM storage.

    NVIDIA's published SHA-1 proves equality with the official release artifact.
    Orin Stage's SHA-256 is recorded as the local exact-byte identity used by
    later cache/base reuse decisions.
    """

    root = Path(download_root)
    if not root.is_absolute():
        raise AcquisitionArtifactError("download root must be absolute")
    if not root.is_dir():
        raise AcquisitionArtifactMissingError(
            f"SDK Manager download root does not exist: {root}"
        )

    official = target.record["checksums"]["official"]
    if official["algorithm"] != "sha1":
        raise AcquisitionArtifactError("catalog official checksum algorithm is not sha1")

    construction = target.record["construction_inputs"]
    checksum_artifacts = official["artifacts"]
    verified: list[VerifiedAcquisitionArtifact] = []

    for kind in ("bsp", "sample_rootfs"):
        expected_filename = construction[kind]["filename"]
        checksum_entry = checksum_artifacts[kind]
        official_filename = checksum_entry["filename"]
        if official_filename.casefold() != expected_filename.casefold():
            raise AcquisitionArtifactError(
                f"catalog construction/checksum filename mismatch for {kind}: "
                f"{expected_filename!r} vs {official_filename!r}"
            )

        path = _find_exact_regular_file(root, expected_filename)
        sha1, sha256, size = _hash_file(path)
        expected_sha1 = checksum_entry["digest"].lower()
        if sha1 != expected_sha1:
            raise AcquisitionArtifactChecksumError(
                f"NVIDIA SHA-1 mismatch for {expected_filename}: "
                f"expected {expected_sha1}, got {sha1}"
            )

        verified.append(
            VerifiedAcquisitionArtifact(
                kind=kind,
                filename=expected_filename,
                relative_path=str(path.relative_to(root)),
                size=size,
                sha1=sha1,
                sha256=sha256,
                official_sha1=expected_sha1,
            )
        )

    return tuple(verified)


def verify_receipt_artifact(
    download_root: Path,
    *,
    relative_path: str,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    """Cheap cache-gate verification for one previously receipted artifact."""

    root = Path(download_root).resolve()
    unresolved = root / relative_path
    if unresolved.is_symlink():
        return False
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if not candidate.is_file():
        return False
    if candidate.stat().st_size != expected_size:
        return False

    sha256 = hashlib.sha256()
    with candidate.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest() == expected_sha256
