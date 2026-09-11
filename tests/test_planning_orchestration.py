from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
from orin_stage.planning.models import ArtifactIndex, PlanArtifactStatus
from orin_stage.planning.orchestration import ReleaseEnsureError, ensure_jp6_release
from orin_stage.planning.planner import (
    BasePlanStatus,
    PlannedArtifact,
    ReleasePlan,
    ResolvedSoftwareTarget,
)
from orin_stage.planning import orchestration as orchestration_module


CATALOG_PATHS = builtin_catalog_paths()
PROFILE = "orin-nx-16gb-p3767-0000-on-p3768-0000"
SDKM_TARGET = "JETSON_ORIN_NX_TARGETS"


def _resolver() -> TargetResolver:
    return TargetResolver(
        CATALOG_PATHS.targets_dir,
        CATALOG_PATHS.schema_path,
    )


def _target():
    return _resolver().resolve("jetson-orin@jp6.2.3")


def _discovery() -> SdkManagerDiscovery:
    target = _target()
    return SdkManagerDiscovery(
        sdk_manager_version="2.4.1.13536",
        query_source="current",
        target=VerifiedSdkManagerTarget(
            canonical_id=target.canonical_id,
            jetpack_version="6.2.3",
            sdk_manager_display_label="JetPack 6.2.3",
            sdk_manager_target=SDKM_TARGET,
        ),
    )


def _plan(
    statuses: tuple[PlanArtifactStatus, PlanArtifactStatus],
    *,
    base_status: BasePlanStatus,
) -> ReleasePlan:
    target = _target()
    inputs = target.record["construction_inputs"]
    artifacts = tuple(
        PlannedArtifact(
            kind=kind,
            role=f"construction-{kind.replace('_', '-')}",
            filename=str(inputs[kind]["filename"]),
            expected_sha256=digest,
            status=status,
            size=size if status is not PlanArtifactStatus.SDKM_DECISION else None,
            reason="test evidence",
        )
        for kind, digest, size, status in (
            ("bsp", "a" * 64, 100, statuses[0]),
            ("sample_rootfs", "b" * 64, 200, statuses[1]),
        )
    )
    unknown = sum(status is PlanArtifactStatus.SDKM_DECISION for status in statuses)
    known_bytes = sum(
        artifact.size or 0
        for artifact in artifacts
        if artifact.status is PlanArtifactStatus.DOWNLOAD_REQUIRED
    )
    return ReleasePlan(
        software_target=ResolvedSoftwareTarget(
            selector=target.selector,
            canonical_id=target.canonical_id,
            jetpack_version="6.2.3",
            l4t_version="36.5.2",
            support_status=target.support_status,
            supported=target.is_supported,
        ),
        hardware_profile=PROFILE,
        artifacts=artifacts,
        verified_cached_count=sum(
            status is PlanArtifactStatus.VERIFIED_CACHED for status in statuses
        ),
        download_required_count=sum(
            status is PlanArtifactStatus.DOWNLOAD_REQUIRED for status in statuses
        ),
        sdkm_decision_count=unknown,
        known_download_bytes=known_bytes,
        unknown_download_artifact_count=unknown,
        download_total_complete=unknown == 0,
        base_status=base_status,
        base_reason="test base evidence",
        base_digest="c" * 64 if base_status is BasePlanStatus.BASE_REUSE else None,
        base_reference=(
            "/data/targets/existing"
            if base_status is BasePlanStatus.BASE_REUSE
            else None
        ),
    )


def _acquisition_result(receipt: Path):
    return SimpleNamespace(discovery=_discovery(), receipt_path=receipt)


def _forbidden(*args, **kwargs):
    raise AssertionError("unexpected external operation")


def test_exact_verified_acquisition_and_base_skip_sdkm_and_builder(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.BASE_REUSE,
    )
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: plan)
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: receipt,
    )
    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", _forbidden)
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", _forbidden)

    result = ensure_jp6_release(
        _target(),
        SdkManagerClient("unused"),
        data_root=tmp_path,
        base_builder=_forbidden,
    )

    assert not result.acquisition_invoked
    assert not result.builder_invoked
    assert result.final_plan.base_status is BasePlanStatus.BASE_REUSE


def test_acquisition_hit_with_missing_base_calls_builder_once(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    receipt = tmp_path / "receipt.json"
    builder_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    base_result = object()

    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: plan)
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: receipt,
    )
    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", _forbidden)

    def builder(*args, **kwargs):
        builder_calls.append((args, kwargs))
        return base_result

    result = ensure_jp6_release(
        _target(),
        SdkManagerClient("unused"),
        data_root=tmp_path,
        base_builder=builder,
    )

    assert not result.acquisition_invoked
    assert result.base_result is base_result
    assert len(builder_calls) == 1
    builder_args, builder_kwargs = builder_calls[0]
    assert builder_args[0].canonical_id == _target().canonical_id
    assert builder_kwargs["acquisition_receipt_path"] == receipt


def test_missing_artifact_uses_sdkm_once_rebuilds_replans_and_builds(
    tmp_path: Path, monkeypatch
) -> None:
    initial = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.DOWNLOAD_REQUIRED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    verified = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    plans = iter((initial, verified))
    receipts = iter((None, tmp_path / "published" / "receipt.json"))
    acquisition_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    rebuild_calls: list[Path] = []
    builder_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: next(plans))
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: next(receipts),
    )

    def acquire(*args, **kwargs):
        acquisition_calls.append((args, kwargs))
        return _acquisition_result(tmp_path / "published" / "receipt.json")

    def rebuild(root):
        rebuild_calls.append(root)
        return ArtifactIndex(1, ())

    def builder(*args, **kwargs):
        builder_calls.append(kwargs)
        return object()

    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", acquire)
    monkeypatch.setattr(orchestration_module, "rebuild_artifact_index", rebuild)
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", builder)

    result = ensure_jp6_release(
        _target(),
        SdkManagerClient("sdkmanager"),
        data_root=tmp_path,
    )

    assert result.acquisition_invoked
    assert len(acquisition_calls) == 1
    acquisition_args, acquisition_kwargs = acquisition_calls[0]
    assert acquisition_args[1].canonical_id == _target().canonical_id
    assert acquisition_kwargs["required_sdk_manager_target"] == SDKM_TARGET
    assert acquisition_kwargs["role"].role_id == "jp6-developer-v1"
    assert rebuild_calls == [tmp_path.resolve()]
    assert len(builder_calls) == 1


def test_unverified_hash_after_acquisition_blocks_builder(
    tmp_path: Path, monkeypatch
) -> None:
    missing = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.DOWNLOAD_REQUIRED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    still_invalid = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.DOWNLOAD_REQUIRED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    plans = iter((missing, still_invalid))
    receipts = iter((None, None))

    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(
        orchestration_module,
        "rebuild_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: next(plans))
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: next(receipts),
    )
    monkeypatch.setattr(
        orchestration_module,
        "ensure_sdk_manager_acquisition",
        lambda *a, **k: _acquisition_result(tmp_path / "receipt.json"),
    )
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", _forbidden)

    with pytest.raises(ReleaseEnsureError, match="fully verified"):
        ensure_jp6_release(
            _target(),
            SdkManagerClient("sdkmanager"),
            data_root=tmp_path,
        )


def test_unknown_manifest_uses_existing_sdkm_adapter_without_custom_downloader(
    tmp_path: Path, monkeypatch
) -> None:
    decision = _plan(
        (PlanArtifactStatus.SDKM_DECISION, PlanArtifactStatus.SDKM_DECISION),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    verified = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.BASE_REUSE,
    )
    plans = iter((decision, verified))
    receipts = iter((None, tmp_path / "receipt.json"))
    acquisition_calls: list[dict[str, object]] = []

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(
        orchestration_module,
        "rebuild_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: next(plans))
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: next(receipts),
    )

    def acquire(*args, **kwargs):
        acquisition_calls.append(kwargs)
        return _acquisition_result(tmp_path / "receipt.json")

    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", acquire)
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", _forbidden)

    result = ensure_jp6_release(
        _target(),
        SdkManagerClient("sdkmanager"),
        data_root=tmp_path,
        sdk_manager_manifest={"schema_version": 999},
    )

    assert len(acquisition_calls) == 1
    assert result.final_plan.base_status is BasePlanStatus.BASE_REUSE


def test_same_filename_with_different_sha_does_not_take_fast_hit(
    tmp_path: Path, monkeypatch
) -> None:
    hash_mismatch = _plan(
        (PlanArtifactStatus.DOWNLOAD_REQUIRED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
    )
    verified = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.BASE_REUSE,
    )
    plans = iter((hash_mismatch, verified))
    receipts = iter((None, tmp_path / "receipt.json"))
    calls = {"acquisition": 0}

    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(
        orchestration_module,
        "rebuild_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )
    monkeypatch.setattr(orchestration_module, "plan_release", lambda *a, **k: next(plans))
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *a, **k: next(receipts),
    )

    def acquire(*args, **kwargs):
        calls["acquisition"] += 1
        return _acquisition_result(tmp_path / "receipt.json")

    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", acquire)
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", _forbidden)

    ensure_jp6_release(
        _target(),
        SdkManagerClient("sdkmanager"),
        data_root=tmp_path,
    )

    assert calls["acquisition"] == 1


def test_other_jp6_target_uses_same_orchestration_path(
    tmp_path: Path, monkeypatch
) -> None:
    target = _resolver().resolve("jetson-orin@jp6.2")
    plan = _plan(
        (PlanArtifactStatus.VERIFIED_CACHED, PlanArtifactStatus.VERIFIED_CACHED),
        base_status=BasePlanStatus.BASE_REUSE,
    )
    receipt = tmp_path / "receipt.json"
    observed: list[object] = []
    monkeypatch.setattr(
        orchestration_module,
        "build_artifact_index",
        lambda root: ArtifactIndex(1, ()),
    )

    def plan_release(selected, **kwargs):
        observed.append((selected, kwargs["hardware_profile"]))
        return plan

    monkeypatch.setattr(orchestration_module, "plan_release", plan_release)
    monkeypatch.setattr(
        orchestration_module,
        "_find_verified_acquisition_receipt",
        lambda *args, **kwargs: receipt,
    )
    monkeypatch.setattr(orchestration_module, "ensure_sdk_manager_acquisition", _forbidden)
    monkeypatch.setattr(orchestration_module, "ensure_jp6_base", _forbidden)

    result = ensure_jp6_release(
        target,
        SdkManagerClient("sdkmanager"),
        data_root=tmp_path,
    )

    assert result.target is target
    assert observed == [(target, target.hardware_profile)]
