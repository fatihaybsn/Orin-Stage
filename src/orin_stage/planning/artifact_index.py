from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Mapping

from orin_stage.acquisition.acquisition_receipt import (
    AcquisitionReceiptError,
    load_receipt,
    receipt_is_cache_hit,
)
from orin_stage.acquisition.artifact_verification import (
    VerifiedAcquisitionArtifact,
    verify_receipt_artifact,
)

from .models import (
    ArtifactExpectation,
    ArtifactIndex,
    ArtifactIndexContent,
    ArtifactIndexRelationship,
    PlanArtifactStatus,
    PlanningModelError,
)


ARTIFACT_INDEX_RELATIVE_PATH = Path("acquisition") / "artifact-index.json"


class ArtifactIndexError(RuntimeError):
    """Raised when the receipt-derived artifact index cannot be rebuilt."""


def artifact_index_path(data_root: Path) -> Path:
    return Path(data_root).expanduser().resolve() / ARTIFACT_INDEX_RELATIVE_PATH


def _artifact_from_receipt(data: Mapping[str, object]) -> VerifiedAcquisitionArtifact:
    try:
        return VerifiedAcquisitionArtifact(
            kind=str(data["kind"]),
            filename=str(data["filename"]),
            relative_path=str(data["relative_path"]),
            size=int(data["size"]),
            sha1=str(data["sha1"]),
            sha256=str(data["sha256"]).lower(),
            official_sha1=str(data["official_sha1"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIndexError("malformed acquisition artifact in receipt") from exc


def _successful_receipts(data_root: Path) -> tuple[tuple[Path, Mapping[str, object]], ...]:
    receipts_root = data_root / "sdkm" / "receipts"
    download_root = data_root / "sdkm" / "downloads"
    if not receipts_root.is_dir() or not download_root.is_dir():
        return ()

    successful: list[tuple[Path, Mapping[str, object]]] = []
    for path in sorted(receipts_root.glob("*/receipt.json")):
        try:
            receipt = load_receipt(path)
        except AcquisitionReceiptError:
            continue
        digest = receipt.get("acquisition_digest")
        if not isinstance(digest, str) or path.parent.name != digest:
            continue
        if receipt.get("download_root") != str(download_root):
            continue
        if not receipt_is_cache_hit(
            path,
            expected_digest=digest,
            download_root=download_root,
        ):
            continue
        successful.append((path, receipt))
    return tuple(successful)


def build_artifact_index(data_root: Path) -> ArtifactIndex:
    """Deterministically derive an index from currently valid Step 2 receipts."""

    root = Path(data_root).expanduser().resolve()
    grouped: dict[str, list[tuple[int, ArtifactIndexRelationship]]] = defaultdict(list)

    for receipt_path, receipt in _successful_receipts(root):
        artifacts = receipt.get("artifacts")
        groups = receipt.get("selected_groups")
        if not isinstance(artifacts, list) or not isinstance(groups, list) or not all(
            isinstance(group, str) for group in groups
        ):
            continue

        for item in artifacts:
            if not isinstance(item, Mapping):
                continue
            artifact = _artifact_from_receipt(item)
            local_path = (
                Path(str(receipt["download_root"])) / artifact.relative_path
            ).resolve()
            try:
                local_reference = str(local_path.relative_to(root))
                receipt_reference = str(receipt_path.relative_to(root))
            except ValueError as exc:
                raise ArtifactIndexError(
                    "receipt artifact paths must stay below ORIN_STAGE_DATA"
                ) from exc

            relationship = ArtifactIndexRelationship(
                source="sdk-manager",
                canonical_id=str(receipt["canonical_id"]),
                artifact_kind=artifact.kind,
                filename=artifact.filename,
                sdk_manager_target=str(receipt["sdk_manager_target"]),
                component_groups=tuple(sorted(groups)),
                local_path=local_reference,
                acquisition_digest=str(receipt["acquisition_digest"]),
                receipt_reference=receipt_reference,
            )
            grouped[artifact.sha256].append((artifact.size, relationship))

    contents: list[ArtifactIndexContent] = []
    for sha256 in sorted(grouped):
        rows = grouped[sha256]
        sizes = {size for size, _relationship in rows}
        if len(sizes) != 1:
            raise ArtifactIndexError(
                f"one whole-file SHA-256 identity has conflicting sizes: {sha256}"
            )
        relationships = tuple(
            sorted((relationship for _size, relationship in rows), key=lambda item: item.sort_key())
        )
        contents.append(
            ArtifactIndexContent(
                sha256=sha256,
                size=next(iter(sizes)),
                relationships=relationships,
            )
        )
    return ArtifactIndex(schema_version=1, contents=tuple(contents))


def canonical_artifact_index_json(index: ArtifactIndex) -> str:
    return json.dumps(
        index.to_dict(), sort_keys=True, separators=(",", ":")
    ) + "\n"


def write_artifact_index_atomic(data_root: Path, index: ArtifactIndex) -> Path:
    path = artifact_index_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_artifact_index_json(index)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def rebuild_artifact_index(data_root: Path) -> ArtifactIndex:
    index = build_artifact_index(data_root)
    write_artifact_index_atomic(data_root, index)
    return index


def load_artifact_index(data_root: Path) -> ArtifactIndex:
    path = artifact_index_path(data_root)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactIndexError(f"cannot load artifact index: {path}") from exc
    if not isinstance(data, Mapping):
        raise ArtifactIndexError("artifact index root must be an object")
    try:
        return ArtifactIndex.from_dict(data)
    except PlanningModelError as exc:
        raise ArtifactIndexError(str(exc)) from exc


def artifact_status(
    index: ArtifactIndex,
    expectation: ArtifactExpectation,
    *,
    data_root: Path,
) -> PlanArtifactStatus:
    """Check one expected artifact without treating the index as authority."""

    if not expectation.has_sufficient_evidence():
        return PlanArtifactStatus.SDKM_DECISION

    required_groups = frozenset(expectation.required_component_groups)
    for content in index.contents:
        if content.sha256 != expectation.expected_sha256.lower():
            continue
        if expectation.expected_size is not None and content.size != expectation.expected_size:
            continue
        for relationship in content.relationships:
            if (
                relationship.source != "sdk-manager"
                or relationship.canonical_id != expectation.canonical_id
                or relationship.artifact_kind != expectation.artifact_kind
                or relationship.filename != expectation.filename
                or relationship.sdk_manager_target != expectation.sdk_manager_target
                or not required_groups.issubset(relationship.component_groups)
            ):
                continue
            if verify_receipt_artifact(
                Path(data_root).expanduser().resolve(),
                relative_path=relationship.local_path,
                expected_size=content.size,
                expected_sha256=content.sha256,
            ):
                return PlanArtifactStatus.VERIFIED_CACHED
    return PlanArtifactStatus.DOWNLOAD_REQUIRED
