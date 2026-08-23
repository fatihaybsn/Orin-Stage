from __future__ import annotations

import json
from pathlib import Path

from orin_stage.base.identity import build_base_target_projection_digest
from orin_stage.base.lock import build_canonical_target_lock, target_lock_digest
from orin_stage.base.packages import ConstructionPackageSet, LockedPackage, PackageSeed
from orin_stage.base.recipe import construction_recipe_digest_v1
from orin_stage.base.validation import (
    BASE_VALIDATION_POLICY_ID,
    BASE_VALIDATION_POLICY_VERSION,
)
from orin_stage.catalog.resolver import TargetResolver


REPO_ROOT = Path(__file__).resolve().parents[1]


def _target():
    resolver = TargetResolver(
        REPO_ROOT / "catalog" / "targets",
        REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    return resolver.resolve("jetson-orin@jp6.2.3")


def _package_set() -> ConstructionPackageSet:
    return ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-jetpack",
                "6.2.3+b81",
                "arm64",
                "install",
                "nvidia-jetpack.deb",
                "c" * 64,
            ),
        ),
    )


def _receipt(path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "schema_version": 1,
        "acquisition_digest": "1" * 64,
        "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        "sdk_manager_version": "2.4.1.13536",
        "sdk_manager_query_source": "current",
        "jetpack_version": "6.2.3",
        "sdk_manager_target": "JETSON_ORIN_NX_TARGETS",
        "role_id": "jp6-developer-v1",
        "role_digest": "2" * 64,
        "response_file_sha256": "3" * 64,
        "artifacts": [
            {
                "kind": "bsp",
                "filename": "Jetson_Linux_R36.5.2_aarch64.tbz2",
                "relative_path": "bsp.tbz2",
                "size": 10,
                "sha1": "4" * 40,
                "official_sha1": "4" * 40,
                "sha256": "5" * 64,
            },
            {
                "kind": "sample_rootfs",
                "filename": "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2",
                "relative_path": "rootfs.tbz2",
                "size": 20,
                "sha1": "6" * 40,
                "official_sha1": "6" * 40,
                "sha256": "7" * 64,
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_canonical_lock_keeps_provenance_but_projection_excludes_it(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    receipt_path = data_root / "sdkm" / "receipts" / "a" / "receipt.json"
    receipt = _receipt(receipt_path)
    lock = build_canonical_target_lock(
        _target(),
        acquisition_receipt=receipt,
        acquisition_receipt_path=receipt_path,
        data_root=data_root,
        package_set=_package_set(),
        construction_recipe_digest=construction_recipe_digest_v1(),
        qemu_version="qemu-aarch64 version 8.2",
    )

    assert lock["acquisition"]["sdk_manager_version"] == "2.4.1.13536"  # type: ignore[index]
    assert lock["construction"]["qemu"]["version"] == "qemu-aarch64 version 8.2"  # type: ignore[index]
    assert lock["validation"] == {
        "policy_id": BASE_VALIDATION_POLICY_ID,
        "policy_version": BASE_VALIDATION_POLICY_VERSION,
    }
    assert lock["declared_environment"]["exact_cross_packages"]["status"] == "deferred-to-step6-build-capsule"  # type: ignore[index]
    assert len(target_lock_digest(lock)) == 64

    changed = json.loads(json.dumps(lock))
    changed["acquisition"]["sdk_manager_version"] = "2.5.0"
    changed["construction"]["qemu"]["version"] = "qemu-aarch64 version 9.0"
    assert build_base_target_projection_digest(lock) == build_base_target_projection_digest(changed)
    assert target_lock_digest(lock) != target_lock_digest(changed)
