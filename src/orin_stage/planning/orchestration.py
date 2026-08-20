from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from orin_stage.acquisition.acquisition_receipt import (
    AcquisitionReceiptError,
    load_receipt,
    receipt_is_cache_hit,
)
from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.acquisition.sdk_manager_acquisition import (
    AcquisitionResult,
    ensure_sdk_manager_acquisition,
)
from orin_stage.base.construction import BaseBuildResult, ensure_jp623_base
from orin_stage.catalog.resolver import ResolvedCatalogTarget, TargetResolver

from .artifact_index import build_artifact_index, rebuild_artifact_index
from .models import ArtifactIndex, PlanArtifactStatus
from .planner import (
    BasePlanStatus,
    ReleasePlan,
    normalize_sdk_manager_manifest,
    plan_release,
)


class ReleaseEnsureError(RuntimeError):
    """Raised when verified inputs cannot be established for base construction."""


@dataclass(frozen=True, slots=True)
class ReleaseEnsureResult:
    target: ResolvedCatalogTarget
    hardware_profile: str
    initial_plan: ReleasePlan
    final_plan: ReleasePlan
    acquisition_receipt_path: Path
    acquisition_result: AcquisitionResult | None
    base_result: BaseBuildResult | None

    @property
    def acquisition_invoked(self) -> bool:
        return self.acquisition_result is not None

    @property
    def builder_invoked(self) -> bool:
        return self.base_result is not None


def _base_directories(data_root: Path) -> tuple[Path, ...]:
    targets_root = data_root / "targets"
    if not targets_root.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(targets_root.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    )


def _require_jp623_target(target: ResolvedCatalogTarget) -> None:
    release = target.record["release"]
    if (
        str(release["jetpack"]["version"]) != "6.2.3"
        or str(release["l4t"]["version"]) != "36.5.2"
    ):
        raise ReleaseEnsureError(
            "release orchestration currently supports only JetPack 6.2.3 / L4T 36.5.2"
        )


def _plan_inputs_are_verified(plan: ReleasePlan) -> bool:
    return all(
        artifact.status is PlanArtifactStatus.VERIFIED_CACHED
        for artifact in plan.artifacts
    )


def _receipt_matches_plan(
    path: Path,
    *,
    data_root: Path,
    target: ResolvedCatalogTarget,
    required_sdk_manager_target: str,
    plan: ReleasePlan,
) -> bool:
    receipt_path = Path(path).resolve()
    receipts_root = (data_root / "sdkm" / "receipts").resolve()
    try:
        receipt_path.relative_to(receipts_root)
        receipt = load_receipt(receipt_path)
    except (ValueError, AcquisitionReceiptError):
        return False

    digest = receipt.get("acquisition_digest")
    download_root = (data_root / "sdkm" / "downloads").resolve()
    if (
        receipt_path.name != "receipt.json"
        or not isinstance(digest, str)
        or receipt_path.parent.name != digest
        or receipt.get("canonical_id") != target.canonical_id
        or receipt.get("sdk_manager_target") != required_sdk_manager_target
        or receipt.get("download_root") != str(download_root)
        or not receipt_is_cache_hit(
            receipt_path,
            expected_digest=digest,
            download_root=download_root,
        )
    ):
        return False

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    by_kind: dict[str, Mapping[str, object]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            return False
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in by_kind:
            return False
        by_kind[kind] = item

    for expected in plan.artifacts:
        item = by_kind.get(expected.kind)
        if (
            item is None
            or expected.expected_sha256 is None
            or item.get("filename") != expected.filename
            or str(item.get("sha256", "")).lower() != expected.expected_sha256.lower()
        ):
            return False
    return True


def _find_verified_acquisition_receipt(
    data_root: Path,
    target: ResolvedCatalogTarget,
    *,
    required_sdk_manager_target: str,
    plan: ReleasePlan,
    preferred: Path | None = None,
) -> Path | None:
    receipts_root = data_root / "sdkm" / "receipts"
    candidates: list[Path] = []
    if preferred is not None:
        candidates.append(Path(preferred))
    if receipts_root.is_dir():
        candidates.extend(sorted(receipts_root.glob("*/receipt.json")))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _receipt_matches_plan(
            resolved,
            data_root=data_root,
            target=target,
            required_sdk_manager_target=required_sdk_manager_target,
            plan=plan,
        ):
            return resolved
    return None


def _manifest_after_acquisition(
    manifest: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    normalized = normalize_sdk_manager_manifest(manifest)
    if normalized is None:
        return None
    kinds = {artifact.kind for artifact in normalized.artifacts}
    if (
        not normalized.schema_known
        or normalized.unknown_artifact_kinds
        or not {"bsp", "sample_rootfs"}.issubset(kinds)
    ):
        return None
    return manifest


def ensure_jp623_release(
    resolver: TargetResolver,
    client: SdkManagerClient,
    *,
    selector: str,
    hardware_profile: str,
    required_sdk_manager_target: str,
    data_root: Path,
    sdk_manager_manifest: Mapping[str, object] | None = None,
    qemu_binary: Path = Path("/usr/bin/qemu-aarch64-static"),
    sdk_manager_state_root: Path | None = None,
) -> ReleaseEnsureResult:
    """Resolve, plan, acquire when needed, replan, then reuse or build JP6.2.3."""

    root = Path(data_root).expanduser().resolve()
    target = resolver.resolve(selector)
    _require_jp623_target(target)
    index: ArtifactIndex = build_artifact_index(root)
    initial_plan = plan_release(
        target,
        hardware_profile=hardware_profile,
        artifact_index=index,
        data_root=root,
        sdk_manager_manifest=sdk_manager_manifest,
        base_directories=_base_directories(root),
    )
    receipt_path = _find_verified_acquisition_receipt(
        root,
        target,
        required_sdk_manager_target=required_sdk_manager_target,
        plan=initial_plan,
    )

    acquisition_result: AcquisitionResult | None = None
    final_plan = initial_plan
    if not _plan_inputs_are_verified(initial_plan) or receipt_path is None:
        acquisition_result = ensure_sdk_manager_acquisition(
            client,
            target,
            required_sdk_manager_target=required_sdk_manager_target,
            data_root=root,
            sdk_manager_state_root=sdk_manager_state_root,
        )
        index = rebuild_artifact_index(root)
        final_plan = plan_release(
            target,
            hardware_profile=hardware_profile,
            artifact_index=index,
            data_root=root,
            sdk_manager_discovery=acquisition_result.discovery,
            sdk_manager_manifest=_manifest_after_acquisition(sdk_manager_manifest),
            base_directories=_base_directories(root),
        )
        receipt_path = _find_verified_acquisition_receipt(
            root,
            target,
            required_sdk_manager_target=required_sdk_manager_target,
            plan=final_plan,
            preferred=acquisition_result.receipt_path,
        )

    if not _plan_inputs_are_verified(final_plan) or receipt_path is None:
        raise ReleaseEnsureError(
            "SDK Manager acquisition completed without a fully verified exact artifact set"
        )

    if final_plan.base_status is BasePlanStatus.BASE_REUSE:
        return ReleaseEnsureResult(
            target=target,
            hardware_profile=hardware_profile,
            initial_plan=initial_plan,
            final_plan=final_plan,
            acquisition_receipt_path=receipt_path,
            acquisition_result=acquisition_result,
            base_result=None,
        )

    base_result = ensure_jp623_base(
        target,
        acquisition_receipt_path=receipt_path,
        data_root=root,
        qemu_binary=Path(qemu_binary).expanduser().resolve(),
    )
    return ReleaseEnsureResult(
        target=target,
        hardware_profile=hardware_profile,
        initial_plan=initial_plan,
        final_plan=final_plan,
        acquisition_receipt_path=receipt_path,
        acquisition_result=acquisition_result,
        base_result=base_result,
    )
