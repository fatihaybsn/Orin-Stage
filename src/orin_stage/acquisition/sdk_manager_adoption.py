from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from .acquisition_receipt import (
    build_acquisition_digest,
    hash_metadata_files,
    make_receipt,
    receipt_is_cache_hit,
    write_receipt_atomic,
)
from .artifact_verification import verify_catalog_construction_artifacts
from .sdk_manager import SdkManagerClient
from .sdk_manager_acquisition import (
    AcquisitionResult,
    _acquisition_lock,
    _publish_staging_directory,
    _remove_abandoned_publish_dirs,
    _safe_filename,
)
from .sdk_manager_discovery import discover_catalog_target
from .sdk_manager_manifest import copy_sdk_manager_reference_files
from .sdk_manager_response import render_response_file, write_response_file_atomic
from .sdk_manager_role import JP6_DEVELOPER_ROLE_V1, SdkManagerComponentRole


class SdkManagerAdoptionError(RuntimeError):
    """Raised when an existing SDK Manager download cannot be adopted safely."""


def _require_exact_jp623(target: ResolvedCatalogTarget) -> None:
    release = target.record["release"]
    if (
        target.selector != "jetson-orin@jp6.2.3"
        or str(release["jetpack"]["version"]) != "6.2.3"
        or str(release["l4t"]["version"]) != "36.5.2"
    ):
        raise SdkManagerAdoptionError(
            "adoption requires the exact jetson-orin@jp6.2.3 / L4T 36.5.2 target"
        )


def _link_or_copy(source: Path, destination: Path) -> str:
    """Prefer a zero-extra-data hard link, copying only across filesystems."""

    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(source, destination)
        return "copy-cross-filesystem"
    return "hardlink"


def _write_adoption_evidence(
    destination: Path,
    *,
    transfer_methods: dict[str, str],
    sdk_manager_reference_included: bool,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "source": "existing-sdk-manager-download-folder",
        "transfer_methods": transfer_methods,
        "sdk_manager_reference_included": sdk_manager_reference_included,
    }
    (destination / "adoption.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def adopt_sdk_manager_acquisition(
    client: SdkManagerClient,
    target: ResolvedCatalogTarget,
    *,
    required_sdk_manager_target: str,
    data_root: Path,
    existing_download_folder: Path,
    role: SdkManagerComponentRole = JP6_DEVELOPER_ROLE_V1,
    now: Callable[[], datetime] | None = None,
    sdk_manager_state_root: Path | None = None,
) -> AcquisitionResult:
    """Adopt already-downloaded JP6.2.3 artifacts without invoking downloadonly.

    The exact catalog SHA-1 values are checked at the source and again in the
    atomically staged managed location. Same-filesystem files are hard-linked;
    an actual copy is used only when the source is on another filesystem.
    """

    _require_exact_jp623(target)
    root = Path(data_root)
    source_root = Path(existing_download_folder)
    if not root.is_absolute():
        raise ValueError("Orin Stage data root must be absolute")
    if not source_root.is_absolute():
        raise ValueError("existing SDK Manager download folder must be absolute")
    root = root.resolve()
    source_root = source_root.resolve()

    # Fail before mutating managed state if either official artifact is absent,
    # ambiguous, or differs from NVIDIA's catalog checksum.
    source_artifacts = verify_catalog_construction_artifacts(
        target,
        download_root=source_root,
    )

    sdkm_root = root / "sdkm"
    downloads = sdkm_root / "downloads"
    responses = sdkm_root / "responses"
    receipts = sdkm_root / "receipts"
    downloads.mkdir(parents=True, exist_ok=True)
    responses.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)

    discovery = discover_catalog_target(
        client,
        target,
        required_sdk_manager_target=required_sdk_manager_target,
    )
    response_content = render_response_file(discovery, role, download_folder=downloads)
    response_path = responses / (
        f"{_safe_filename(target.canonical_id)}--{_safe_filename(role.role_id)}.ini"
    )
    response_file = write_response_file_atomic(
        response_path,
        response_content,
        role=role,
    )

    acquisition_digest = build_acquisition_digest(discovery, response_file, role)
    receipt_dir = receipts / acquisition_digest
    receipt_path = receipt_dir / "receipt.json"
    if receipt_is_cache_hit(
        receipt_path,
        expected_digest=acquisition_digest,
        download_root=downloads,
    ):
        return AcquisitionResult(
            discovery, response_file, receipt_path, acquisition_digest, True, None
        )

    lock_path = sdkm_root / ".acquisition.lock"
    with _acquisition_lock(lock_path):
        if receipt_is_cache_hit(
            receipt_path,
            expected_digest=acquisition_digest,
            download_root=downloads,
        ):
            return AcquisitionResult(
                discovery, response_file, receipt_path, acquisition_digest, True, None
            )

        _remove_abandoned_publish_dirs(receipts, acquisition_digest)
        token = uuid.uuid4().hex
        artifact_staging = sdkm_root / f".adoption-artifacts-{token}"
        receipt_staging = receipts / f".staging-{acquisition_digest}-{token}"
        managed_name = f"adopted-{_safe_filename(target.canonical_id)}"
        managed_artifacts = downloads / managed_name
        artifact_staging.mkdir(parents=True, exist_ok=False)
        receipt_staging.mkdir(parents=True, exist_ok=False)

        try:
            transfer_methods: dict[str, str] = {}
            for artifact in source_artifacts:
                source = source_root / artifact.relative_path
                destination = artifact_staging / artifact.filename
                transfer_methods[artifact.kind] = _link_or_copy(source, destination)

            staged = verify_catalog_construction_artifacts(
                target,
                download_root=artifact_staging,
            )
            staged_by_kind = {item.kind: item for item in staged}
            for source_item in source_artifacts:
                staged_item = staged_by_kind[source_item.kind]
                if (
                    source_item.size != staged_item.size
                    or source_item.sha256 != staged_item.sha256
                ):
                    raise SdkManagerAdoptionError(
                        f"artifact changed while being adopted: {source_item.filename}"
                    )

            metadata = receipt_staging / "metadata"
            state_root = (
                Path(sdk_manager_state_root).expanduser()
                if sdk_manager_state_root is not None
                else Path.home() / ".nvsdkm"
            )
            reference_included = False
            if state_root.exists():
                copy_sdk_manager_reference_files(
                    state_root,
                    discovery,
                    destination=metadata / "sdkmanager-reference",
                )
                reference_included = True
            _write_adoption_evidence(
                metadata,
                transfer_methods=transfer_methods,
                sdk_manager_reference_included=reference_included,
            )

            _publish_staging_directory(artifact_staging, managed_artifacts)
            artifacts = tuple(
                replace(item, relative_path=str(Path(managed_name) / item.filename))
                for item in staged
            )
            receipt = make_receipt(
                discovery,
                role,
                response_file,
                download_root=downloads,
                artifacts=artifacts,
                sdk_manager_metadata=hash_metadata_files(metadata),
                now=now,
            )
            write_receipt_atomic(receipt_staging / "receipt.json", receipt)
            _publish_staging_directory(receipt_staging, receipt_dir)
        except Exception:
            shutil.rmtree(artifact_staging, ignore_errors=True)
            shutil.rmtree(receipt_staging, ignore_errors=True)
            raise

    return AcquisitionResult(
        discovery, response_file, receipt_path, acquisition_digest, False, receipt
    )
