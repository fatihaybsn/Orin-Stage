from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from orin_stage.base import construction as construction_module
from orin_stage.base.construction import _extract_official_rootfs, ensure_jp623_base
from orin_stage.base.packages import (
    ConstructionPackageSet,
    LockedPackage,
    PackageSeed,
    PackageTransactionEvidence,
)
from orin_stage.base.validation import RuntimeValidationSnapshot
from orin_stage.base.recipe import (
    JP623_ALLOWED_REMOVAL_SET,
    JP623_REMOVAL_POLICY_VERSION,
)
from orin_stage.catalog import TargetResolver, builtin_catalog_paths


CATALOG_PATHS = builtin_catalog_paths()


def _target():
    resolver = TargetResolver(
        CATALOG_PATHS.targets_dir,
        CATALOG_PATHS.schema_path,
    )
    return resolver.resolve("jetson-orin@jp6.2.3")


def test_official_rootfs_commands_follow_nvidia_order(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    bsp = tmp_path / "bsp.tbz2"
    rootfs_tar = tmp_path / "rootfs.tbz2"
    bsp.write_bytes(b"bsp")
    rootfs_tar.write_bytes(b"rootfs")
    events: list[str] = []

    def runner(command, **kwargs):
        if tuple(command[:2]) == ("tar", "-xf"):
            events.append("extract_bsp")
            (work / "Linux_for_Tegra").mkdir()
        else:
            assert "--numeric-owner" in command
            events.append("extract_sample_rootfs")
        return subprocess.CompletedProcess(command, 0, "", "")

    class FakeSandbox:
        def run_official_l4t_scripts(self, l4t_root: Path, *, runner):
            assert l4t_root == work / "Linux_for_Tegra"
            events.extend(("l4t_flash_prerequisites", "apply_binaries"))
            return subprocess.CompletedProcess((), 0, "", "")

    l4t, rootfs = _extract_official_rootfs(
        work,
        bsp=bsp,
        sample_rootfs=rootfs_tar,
        host_sandbox=FakeSandbox(),
        runner=runner,
    )

    assert l4t == work / "Linux_for_Tegra"
    assert rootfs == l4t / "rootfs"
    assert events == [
        "extract_bsp",
        "extract_sample_rootfs",
        "l4t_flash_prerequisites",
        "apply_binaries",
    ]


def test_ensure_jp623_base_publishes_then_reuses_same_base(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    downloads = data_root / "sdkm" / "downloads"
    downloads.mkdir(parents=True)
    bsp = downloads / "bsp.tbz2"
    sample = downloads / "sample.tbz2"
    bsp.write_bytes(b"verified-bsp")
    sample.write_bytes(b"verified-rootfs")

    def artifact(kind: str, path: Path) -> dict[str, object]:
        content = path.read_bytes()
        sha1 = hashlib.sha1(content).hexdigest()
        return {
            "kind": kind,
            "filename": path.name,
            "relative_path": path.name,
            "size": len(content),
            "sha1": sha1,
            "official_sha1": sha1,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    acquisition_dir = data_root / "sdkm" / "receipts" / ("1" * 64)
    metadata_dir = acquisition_dir / "metadata"
    metadata_dir.mkdir(parents=True)
    metadata_file = metadata_dir / "sdkm.json"
    metadata_file.write_bytes(b"sdkm-metadata")
    acquisition_receipt_path = acquisition_dir / "receipt.json"
    acquisition_receipt = {
        "schema_version": 1,
        "acquisition_digest": "1" * 64,
        "created_at": "2026-08-19T00:00:00Z",
        "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        "sdk_manager_version": "2.4.1.13536",
        "sdk_manager_query_source": "current",
        "jetpack_version": "6.2.3",
        "sdk_manager_target": "JETSON_ORIN_NX_TARGETS",
        "role_id": "jp6-developer-v1",
        "role_digest": "2" * 64,
        "include_host": False,
        "selected_groups": [],
        "deselected_groups": [],
        "response_file_sha256": "3" * 64,
        "download_root": str(downloads),
        "artifacts": [artifact("bsp", bsp), artifact("sample_rootfs", sample)],
        "sdk_manager_metadata": [
            {
                "relative_path": "sdkm.json",
                "size": metadata_file.stat().st_size,
                "sha256": hashlib.sha256(metadata_file.read_bytes()).hexdigest(),
            }
        ],
        "license_handling": "sdk_manager_user_interaction",
    }
    acquisition_receipt_path.write_text(json.dumps(acquisition_receipt), encoding="utf-8")

    package_set = ConstructionPackageSet(
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
                "a" * 64,
            ),
        ),
    )
    runtime = RuntimeValidationSnapshot(
        dpkg_packages=(), alternatives=(), ld_cache=("cache",)
    )
    build_count = {"extract": 0}

    def fake_extract(work: Path, **kwargs):
        build_count["extract"] += 1
        l4t = work / "Linux_for_Tegra"
        rootfs = l4t / "rootfs"
        rootfs.mkdir(parents=True)
        (rootfs / "etc" / "apt" / "sources.list.d").mkdir(parents=True)
        (rootfs / "final-aarch64-tree").write_text("ok", encoding="utf-8")
        return l4t, rootfs

    class FakeChroot:
        def __init__(self, rootfs: Path, **kwargs):
            self.rootfs = Path(rootfs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(construction_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(construction_module, "_extract_official_rootfs", fake_extract)
    monkeypatch.setattr(construction_module, "read_qemu_version", lambda *a, **k: "qemu 8.2")
    monkeypatch.setattr(construction_module, "Arm64ConstructionChroot", FakeChroot)

    def fake_resolve(*args, removal_policy, **kwargs):
        assert removal_policy.version == JP623_REMOVAL_POLICY_VERSION
        assert removal_policy.allowed_removal_set == JP623_ALLOWED_REMOVAL_SET
        return replace(package_set, removal_policy=removal_policy)

    monkeypatch.setattr(
        construction_module,
        "resolve_construction_package_set",
        fake_resolve,
    )

    def fake_install(_chroot, resolved, **kwargs):
        assert resolved.removal_policy is not None
        return PackageTransactionEvidence(
            (),
            resolved.removal_policy.version,
            resolved.removal_policy.allowed_removal_set,
        )

    monkeypatch.setattr(
        construction_module,
        "install_locked_package_set",
        fake_install,
    )
    monkeypatch.setattr(construction_module, "validate_runtime_state", lambda *a, **k: runtime)
    monkeypatch.setattr(construction_module, "clean_package_archives", lambda *a, **k: None)
    monkeypatch.setattr(
        construction_module,
        "build_final_manifest",
        lambda rootfs, *, base_digest, package_set, runtime: {
            "schema_version": 1,
            "base_digest": base_digest,
        },
    )

    first = ensure_jp623_base(
        _target(),
        acquisition_receipt_path=acquisition_receipt_path,
        data_root=data_root,
        qemu_binary=tmp_path / "unused-qemu",
    )
    monkeypatch.setattr(construction_module.os, "geteuid", lambda: 1000)
    second = ensure_jp623_base(
        _target(),
        acquisition_receipt_path=acquisition_receipt_path,
        data_root=data_root,
        qemu_binary=tmp_path / "unused-qemu",
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.base_digest == second.base_digest
    assert first.base_path.is_dir()
    assert (first.base_path / "final-aarch64-tree").is_file()
    assert build_count["extract"] == 1
    receipt = json.loads(first.receipt_path.read_text(encoding="utf-8"))
    assert receipt["packages_removed"] == []
    assert receipt["removal_policy_version"] == JP623_REMOVAL_POLICY_VERSION
    assert receipt["allowed_removal_set"] == list(JP623_ALLOWED_REMOVAL_SET)


def test_ensure_rejects_acquisition_receipt_outside_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    downloads = data_root / "sdkm" / "downloads"
    downloads.mkdir(parents=True)
    outside = tmp_path / "outside-receipt.json"
    outside.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
                "download_root": str(downloads),
            }
        ),
        encoding="utf-8",
    )

    try:
        ensure_jp623_base(
            _target(),
            acquisition_receipt_path=outside,
            data_root=data_root,
        )
    except construction_module.BaseConstructionError as exc:
        assert "SDK Manager receipt root" in str(exc)
    else:
        raise AssertionError("outside acquisition receipt was accepted")


def test_ensure_requires_valid_published_step2_receipt(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    receipt_dir = data_root / "sdkm" / "receipts" / ("1" * 64)
    downloads = data_root / "sdkm" / "downloads"
    receipt_dir.mkdir(parents=True)
    downloads.mkdir(parents=True)
    receipt = receipt_dir / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "acquisition_digest": "1" * 64,
                "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
                "download_root": str(downloads),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(construction_module, "receipt_is_cache_hit", lambda *a, **k: False)

    try:
        ensure_jp623_base(
            _target(),
            acquisition_receipt_path=receipt,
            data_root=data_root,
        )
    except construction_module.BaseConstructionError as exc:
        assert "valid published Step 2 acquisition" in str(exc)
    else:
        raise AssertionError("invalid acquisition receipt was accepted")
