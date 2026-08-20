from __future__ import annotations

import os
from pathlib import Path

import pytest

from orin_stage.base.packages import ConstructionPackageSet, LockedPackage, PackageSeed
from orin_stage.base.validation import (
    BaseValidationError,
    RuntimeValidationSnapshot,
    build_final_manifest,
)


def _elf(machine: int) -> bytes:
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def _package_set() -> ConstructionPackageSet:
    return ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage("nvidia-jetpack", "6.2.3+b81", "arm64", "install", "jp.deb", "a" * 64),
        ),
    )


def _runtime() -> RuntimeValidationSnapshot:
    return RuntimeValidationSnapshot(
        dpkg_packages=(
            {
                "name": "nvidia-jetpack",
                "version": "6.2.3+b81",
                "architecture": "arm64",
                "status": "installed",
            },
        ),
        alternatives=(),
        ld_cache=("1 libs found in cache",),
    )


def test_final_manifest_records_aarch64_symlink_xattr_and_hardlink_state(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    binary = rootfs / "usr" / "bin" / "demo"
    binary.write_bytes(_elf(183) + b"payload")
    hardlink = rootfs / "usr" / "bin" / "demo-hard"
    os.link(binary, hardlink)
    symlink = rootfs / "usr" / "bin" / "demo-link"
    symlink.symlink_to("demo")
    try:
        os.setxattr(binary, "user.orin_stage_test", b"yes")
        has_xattr = True
    except OSError:
        has_xattr = False

    manifest = build_final_manifest(
        rootfs,
        base_digest="b" * 64,
        package_set=_package_set(),
        runtime=_runtime(),
    )

    assert manifest["elf"]["files"][0]["machine"] == 183  # type: ignore[index]
    assert {"path": "usr/bin/demo-link", "target": "demo"} in manifest["symlinks"]  # type: ignore[operator]
    assert ["usr/bin/demo", "usr/bin/demo-hard"] in manifest["hardlink_groups"]  # type: ignore[operator]
    if has_xattr:
        assert any(item["path"] == "usr/bin/demo" for item in manifest["xattrs"])  # type: ignore[index]


def test_final_manifest_rejects_x86_64_elf(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "host-binary").write_bytes(_elf(62))

    with pytest.raises(BaseValidationError, match="host x86_64 ELF"):
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(),
        )


def test_final_manifest_allows_non_host_firmware_elf(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    firmware = rootfs / "lib" / "firmware" / "device-fw.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(_elf(243))  # EM_RISCV: device firmware, not a host binary.

    manifest = build_final_manifest(
        rootfs,
        base_digest="b" * 64,
        package_set=_package_set(),
        runtime=_runtime(),
    )

    assert {"path": "lib/firmware/device-fw.elf", "machine": 243} in manifest["elf"]["files"]  # type: ignore[index,operator]
