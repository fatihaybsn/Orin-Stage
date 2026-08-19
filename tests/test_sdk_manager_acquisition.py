from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orin_stage.acquisition import sdk_manager_acquisition as acquisition_module
from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.acquisition.sdk_manager_acquisition import ensure_sdk_manager_acquisition
from orin_stage.catalog.resolver import TargetResolver


ROOT = Path(__file__).resolve().parents[1]


class FakeSdkManagerClient(SdkManagerClient):
    def version(self) -> str:
        return "2.4.1.13536"

    def query_jetson(self, *, archived: bool = False) -> str:
        assert archived is False
        return """
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
"""


def _target():
    resolver = TargetResolver(
        targets_dir=ROOT / "catalog" / "targets",
        schema_path=ROOT / "catalog" / "schema" / "target.schema.json",
    )
    return resolver.resolve("jetson-orin@jp6.2.3")


def _write_sdkm_state(root: Path) -> Path:
    state = root / "sdkm-state"
    sw = state / "dist" / "sdkml3_jetpack_623.json"
    hw = state / "hwdata" / "families" / "series" / "orin-nx.json"
    sw.parent.mkdir(parents=True)
    hw.parent.mkdir(parents=True)
    sw.write_text(
        json.dumps({"release": {"releaseVersion": "6.2.3", "targetHW": ["JETSON_ORIN_NX_TARGETS"]}}),
        encoding="utf-8",
    )
    hw.write_text(
        json.dumps({"hw": {"nx": {"id": "JETSON_ORIN_NX_TARGETS"}}}),
        encoding="utf-8",
    )
    return state


def _fake_verified_artifacts(download_root: Path) -> tuple[VerifiedAcquisitionArtifact, ...]:
    specs = (
        ("bsp", "fake-bsp.tbz2", b"fake verified bsp bytes"),
        ("sample_rootfs", "fake-rootfs.tbz2", b"fake verified rootfs bytes"),
    )
    verified: list[VerifiedAcquisitionArtifact] = []
    for kind, filename, content in specs:
        (download_root / filename).write_bytes(content)
        sha1 = hashlib.sha1(content).hexdigest()
        verified.append(
            VerifiedAcquisitionArtifact(
                kind=kind,
                filename=filename,
                relative_path=filename,
                size=len(content),
                sha1=sha1,
                sha256=hashlib.sha256(content).hexdigest(),
                official_sha1=sha1,
            )
        )
    return tuple(verified)


def test_end_to_end_acquisition_publishes_receipt_and_then_hits_cache(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path.resolve() / "data"
    sdkm_state = _write_sdkm_state(tmp_path.resolve())

    def fake_verify(target, *, download_root: Path):
        return _fake_verified_artifacts(download_root)

    monkeypatch.setattr(
        "orin_stage.acquisition.sdk_manager_acquisition.verify_catalog_construction_artifacts",
        fake_verify,
    )

    executions = 0

    def fake_execute(plan) -> None:
        nonlocal executions
        executions += 1
        plan.metadata_directory.mkdir(parents=True, exist_ok=True)
        (plan.metadata_directory / "sdkm-export.ini").write_text(
            "exported=true\n", encoding="utf-8"
        )

    first = ensure_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target(),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        execute=fake_execute,
        now=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
        sdk_manager_state_root=sdkm_state,
    )

    assert first.cache_hit is False
    assert first.receipt is not None
    assert first.receipt_path.is_file()
    assert first.response_file.path.is_file()
    assert executions == 1

    second = ensure_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target(),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        execute=fake_execute,
        sdk_manager_state_root=sdkm_state,
    )

    assert second.cache_hit is True
    assert executions == 1


def test_corrupted_cache_forces_download_again(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path.resolve() / "data"
    sdkm_state = _write_sdkm_state(tmp_path.resolve())

    def fake_verify(target, *, download_root: Path):
        return _fake_verified_artifacts(download_root)

    monkeypatch.setattr(
        "orin_stage.acquisition.sdk_manager_acquisition.verify_catalog_construction_artifacts",
        fake_verify,
    )

    executions = 0

    def fake_execute(plan) -> None:
        nonlocal executions
        executions += 1
        plan.metadata_directory.mkdir(parents=True, exist_ok=True)
        (plan.metadata_directory / "sdkm-export.ini").write_text(
            f"run={executions}\n", encoding="utf-8"
        )

    first = ensure_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target(),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        execute=fake_execute,
        sdk_manager_state_root=sdkm_state,
    )
    (data_root / "sdkm" / "downloads" / "fake-bsp.tbz2").write_bytes(b"bad")

    second = ensure_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target(),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        execute=fake_execute,
        sdk_manager_state_root=sdkm_state,
    )

    assert first.cache_hit is False
    assert second.cache_hit is False
    assert executions == 2



def test_concurrent_acquisition_executes_sdk_manager_only_once(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path.resolve() / "data"
    sdkm_state = _write_sdkm_state(tmp_path.resolve())

    def fake_verify(target, *, download_root: Path):
        return _fake_verified_artifacts(download_root)

    monkeypatch.setattr(
        acquisition_module,
        "verify_catalog_construction_artifacts",
        fake_verify,
    )

    original_cache_check = acquisition_module.receipt_is_cache_hit
    first_check_threads: set[int] = set()
    first_checks_done = threading.Event()
    state_lock = threading.Lock()

    def synchronized_cache_check(*args, **kwargs) -> bool:
        thread_id = threading.get_ident()
        with state_lock:
            is_first_check = thread_id not in first_check_threads
            if is_first_check:
                first_check_threads.add(thread_id)
        result = original_cache_check(*args, **kwargs)
        if is_first_check:
            with state_lock:
                if len(first_check_threads) == 2:
                    first_checks_done.set()
        return result

    monkeypatch.setattr(
        acquisition_module,
        "receipt_is_cache_hit",
        synchronized_cache_check,
    )

    executions = 0

    def fake_execute(plan) -> None:
        nonlocal executions
        with state_lock:
            executions += 1
        assert first_checks_done.wait(timeout=5)
        plan.metadata_directory.mkdir(parents=True, exist_ok=True)
        (plan.metadata_directory / "sdkm-export.ini").write_text(
            "exported=true\n", encoding="utf-8"
        )

    def acquire():
        return ensure_sdk_manager_acquisition(
            FakeSdkManagerClient(),
            _target(),
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
            data_root=data_root,
            execute=fake_execute,
            sdk_manager_state_root=sdkm_state,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(acquire), executor.submit(acquire)]
        results = [future.result(timeout=10) for future in futures]

    assert executions == 1
    assert sorted(result.cache_hit for result in results) == [False, True]
    assert results[0].receipt_path == results[1].receipt_path


def test_failed_acquisition_does_not_publish_partial_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path.resolve() / "data"

    def fake_execute(plan) -> None:
        plan.metadata_directory.mkdir(parents=True, exist_ok=True)
        (plan.metadata_directory / "partial.ini").write_text(
            "partial=true\n", encoding="utf-8"
        )
        raise RuntimeError("simulated SDK Manager failure")

    with pytest.raises(RuntimeError, match="simulated SDK Manager failure"):
        ensure_sdk_manager_acquisition(
            FakeSdkManagerClient(),
            _target(),
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
            data_root=data_root,
            execute=fake_execute,
        )

    receipts = data_root / "sdkm" / "receipts"
    assert not list(receipts.glob(".staging-*"))
    assert not list(receipts.glob(".stale-*"))
    assert not list(receipts.glob("*/receipt.json"))
