from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from orin_stage.base import validation as validation_module
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


def _package_set(
    *, filename: str = "jp.deb", sha256: str = "a" * 64
) -> ConstructionPackageSet:
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
                filename,
                sha256,
            ),
        ),
    )


def _runtime(
    *,
    name: str = "nvidia-jetpack",
    version: str = "6.2.3+b81",
    architecture: str = "arm64",
) -> RuntimeValidationSnapshot:
    return RuntimeValidationSnapshot(
        dpkg_packages=(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "status": "installed",
            },
        ),
        alternatives=(),
        ld_cache=("1 libs found in cache",),
    )


def _write_dpkg_owner(
    rootfs: Path,
    *,
    package: str,
    relative_path: str,
    expected_md5: str | None,
) -> None:
    info = rootfs / "var" / "lib" / "dpkg" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / f"{package}.list").write_text(
        f"/{relative_path}\n", encoding="utf-8"
    )
    if expected_md5 is not None:
        (info / f"{package}.md5sums").write_text(
            f"{expected_md5}  {relative_path}\n", encoding="utf-8"
        )


def _x86_rootfs(tmp_path: Path) -> tuple[Path, Path, str]:
    rootfs = tmp_path / "rootfs"
    relative_path = "opt/vendor/foreign.so"
    binary = rootfs / relative_path
    binary.parent.mkdir(parents=True)
    binary.write_bytes(_elf(62) + b"payload")
    return rootfs, binary, relative_path


def test_final_manifest_records_aarch64_symlink_xattr_and_hardlink_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(
        validation_module,
        "verify_foreign_elf_provenance",
        lambda *args, **kwargs: pytest.fail(
            "normal ARM64 ELF must not perform foreign provenance lookup"
        ),
    )
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

    with pytest.raises(BaseValidationError, match="unverified x86_64 ELF") as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(),
        )

    message = str(raised.value)
    assert "foreign ELF path: usr/bin/host-binary" in message
    assert "ELF machine: 62" in message
    assert "package name: NOT FOUND" in message
    assert "entry found: no" in message


def test_x86_64_exact_locked_payload_is_accepted_with_manifest_evidence(
    tmp_path: Path,
) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=actual_md5,
    )
    before = {
        str(path.relative_to(rootfs)): path.read_bytes()
        for path in rootfs.rglob("*")
        if path.is_file()
    }

    manifest = build_final_manifest(
        rootfs,
        base_digest="b" * 64,
        package_set=_package_set(),
        runtime=_runtime(),
    )

    assert manifest["elf"]["accepted_foreign_files"] == [  # type: ignore[index]
        {
            "path": relative_path,
            "elf_machine": 62,
            "owner_package": "nvidia-jetpack",
            "owner_version": "6.2.3+b81",
            "owner_architecture": "arm64",
            "locked_deb_filename": "jp.deb",
            "locked_deb_sha256": "a" * 64,
            "payload_checksum": actual_md5,
            "final_checksum": actual_md5,
            "decision": "accepted_exact_locked_package_payload",
        }
    ]
    after = {
        str(path.relative_to(rootfs)): path.read_bytes()
        for path in rootfs.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_x86_64_diagnostic_reports_ambiguous_owners(tmp_path: Path) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    for package in ("owner-a:arm64", "owner-b:arm64"):
        _write_dpkg_owner(
            rootfs,
            package=package,
            relative_path=relative_path,
            expected_md5=actual_md5,
        )
    runtime = RuntimeValidationSnapshot(
        dpkg_packages=(
            {
                "name": "owner-a",
                "version": "1",
                "architecture": "arm64",
                "status": "installed",
            },
            {
                "name": "owner-b",
                "version": "2",
                "architecture": "arm64",
                "status": "installed",
            },
        ),
        alternatives=(),
        ld_cache=("cache",),
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=runtime,
        )

    message = str(raised.value)
    assert "package name: AMBIGUOUS" in message
    assert "owner-a:arm64" in message
    assert "owner-b:arm64" in message


def test_x86_64_diagnostic_reports_owner_outside_exact_package_set(
    tmp_path: Path,
) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    _write_dpkg_owner(
        rootfs,
        package="other-package",
        relative_path=relative_path,
        expected_md5=actual_md5,
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(name="other-package", version="1.0"),
        )

    message = str(raised.value)
    assert "package name: other-package" in message
    assert "exact ConstructionPackageSet match:\n- match: no" in message
    assert "locked package version: NOT FOUND" in message


def test_x86_64_diagnostic_reports_installed_version_mismatch(
    tmp_path: Path,
) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=actual_md5,
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(version="6.2.2+b1"),
        )

    message = str(raised.value)
    assert "version: 6.2.2+b1" in message
    assert "exact ConstructionPackageSet match:\n- match: no" in message
    assert "locked package version: 6.2.3+b81" in message


def test_x86_64_diagnostic_reports_installed_architecture_mismatch(
    tmp_path: Path,
) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=actual_md5,
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(architecture="amd64"),
        )

    message = str(raised.value)
    assert "architecture: amd64" in message
    assert "exact ConstructionPackageSet match:\n- match: no" in message
    assert "locked package architecture: arm64" in message


@pytest.mark.parametrize(
    "filename, sha256",
    (("", "a" * 64), ("jp.deb", "")),
    ids=("missing-filename", "missing-sha256"),
)
def test_x86_64_diagnostic_rejects_missing_locked_deb_evidence(
    tmp_path: Path,
    filename: str,
    sha256: str,
) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=actual_md5,
    )

    with pytest.raises(BaseValidationError, match="locked .deb filename or SHA256"):
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(filename=filename, sha256=sha256),
            runtime=_runtime(),
        )


def test_x86_64_diagnostic_reports_missing_md5sums_entry(tmp_path: Path) -> None:
    rootfs, _binary, relative_path = _x86_rootfs(tmp_path)
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=None,
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(),
        )

    message = str(raised.value)
    assert "dpkg md5sums:\n- entry found: no" in message
    assert "expected MD5: NOT FOUND" in message
    assert "- match: no" in message


def test_x86_64_diagnostic_reports_md5_checksum_mismatch(tmp_path: Path) -> None:
    rootfs, binary, relative_path = _x86_rootfs(tmp_path)
    actual_md5 = hashlib.md5(binary.read_bytes()).hexdigest()
    expected_md5 = "0" * 32
    _write_dpkg_owner(
        rootfs,
        package="nvidia-jetpack",
        relative_path=relative_path,
        expected_md5=expected_md5,
    )

    with pytest.raises(BaseValidationError) as raised:
        build_final_manifest(
            rootfs,
            base_digest="b" * 64,
            package_set=_package_set(),
            runtime=_runtime(),
        )

    message = str(raised.value)
    assert f"expected MD5: {expected_md5}" in message
    assert f"actual MD5: {actual_md5}" in message
    assert "- match: no" in message


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
