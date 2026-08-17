from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from .acquisition_receipt import (
    AcquisitionReceipt,
    build_acquisition_digest,
    hash_metadata_files,
    make_receipt,
    receipt_is_cache_hit,
    write_receipt_atomic,
)
from .artifact_verification import verify_catalog_construction_artifacts
from .sdk_manager import SdkManagerClient
from .sdk_manager_discovery import SdkManagerDiscovery, discover_catalog_target
from .sdk_manager_execution import (
    build_response_file_execution_plan,
    execute_downloadonly,
)
from .sdk_manager_manifest import copy_sdk_manager_reference_files
from .sdk_manager_response import (
    SdkManagerResponseFile,
    render_response_file,
    write_response_file_atomic,
)
from .sdk_manager_role import JP6_DEVELOPER_ROLE_V1, SdkManagerComponentRole


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    discovery: SdkManagerDiscovery
    response_file: SdkManagerResponseFile
    receipt_path: Path
    acquisition_digest: str
    cache_hit: bool
    receipt: AcquisitionReceipt | None


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ".-_" else "_" for char in value)


def ensure_sdk_manager_acquisition(
    client: SdkManagerClient,
    target: ResolvedCatalogTarget,
    *,
    required_sdk_manager_target: str,
    data_root: Path,
    role: SdkManagerComponentRole = JP6_DEVELOPER_ROLE_V1,
    execute: Callable[..., None] = execute_downloadonly,
    now: Callable[[], datetime] | None = None,
    sdk_manager_state_root: Path | None = None,
) -> AcquisitionResult:
    """Ensure one exact JP6 acquisition exists in the shared SDKM folder.

    The function first proves target availability through SDK Manager query,
    generates a deterministic response file, checks a previous receipt, and
    only invokes ``downloadonly`` when the verified receipt is absent/invalid.
    """

    root = Path(data_root)
    if not root.is_absolute():
        raise ValueError("Orin Stage data root must be absolute")

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

    response_content = render_response_file(
        discovery,
        role,
        download_folder=downloads,
    )
    response_path = responses / (
        f"{_safe_filename(target.canonical_id)}--{_safe_filename(role.role_id)}.ini"
    )
    response_file = write_response_file_atomic(
        response_path,
        response_content,
        role=role,
    )
    acquisition_digest = build_acquisition_digest(discovery, response_file, role)
    final_dir = receipts / acquisition_digest
    receipt_path = final_dir / "receipt.json"

    if receipt_is_cache_hit(
        receipt_path,
        expected_digest=acquisition_digest,
        download_root=downloads,
    ):
        return AcquisitionResult(
            discovery=discovery,
            response_file=response_file,
            receipt_path=receipt_path,
            acquisition_digest=acquisition_digest,
            cache_hit=True,
            receipt=None,
        )

    staging = receipts / f".staging-{acquisition_digest}-{uuid.uuid4().hex}"
    metadata = staging / "metadata"
    logs = staging / "logs"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        execution_plan = build_response_file_execution_plan(
            response_file,
            metadata_directory=metadata,
            logs_directory=logs,
            executable=client.executable,
        )
        execute(execution_plan)

        state_root = (
            Path(sdk_manager_state_root)
            if sdk_manager_state_root is not None
            else Path.home() / ".nvsdkm"
        )
        copy_sdk_manager_reference_files(
            state_root,
            discovery,
            destination=metadata / "sdkmanager-reference",
        )

        artifacts = verify_catalog_construction_artifacts(
            target,
            download_root=downloads,
        )
        metadata_files = hash_metadata_files(metadata)
        if not metadata_files:
            raise RuntimeError(
                "SDK Manager completed but did not export response metadata; "
                "acquisition receipt will not be published"
            )

        receipt = make_receipt(
            discovery,
            role,
            response_file,
            download_root=downloads,
            artifacts=artifacts,
            sdk_manager_metadata=metadata_files,
            now=now,
        )
        write_receipt_atomic(staging / "receipt.json", receipt)

        if final_dir.exists():
            shutil.rmtree(final_dir)
        os.replace(staging, final_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return AcquisitionResult(
        discovery=discovery,
        response_file=response_file,
        receipt_path=receipt_path,
        acquisition_digest=acquisition_digest,
        cache_hit=False,
        receipt=receipt,
    )
