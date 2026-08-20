from __future__ import annotations

import subprocess
from pathlib import Path

from orin_stage.base import chroot as chroot_module
from orin_stage.base.chroot import Arm64ConstructionChroot


def test_chroot_construction_markers_are_temporary(tmp_path: Path, monkeypatch) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "sbin").mkdir(parents=True)
    (rootfs / "etc").mkdir(parents=True)
    original_resolv = rootfs / "etc" / "resolv.conf"
    original_resolv.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    host_resolv = tmp_path / "host-resolv.conf"
    host_resolv.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    qemu = tmp_path / "qemu-aarch64-static"
    qemu.write_bytes(b"qemu")
    qemu.chmod(0o755)
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()
    (binfmt / "qemu-aarch64").write_text(
        "enabled\ninterpreter /usr/bin/qemu-aarch64-static\n", encoding="utf-8"
    )
    calls: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(chroot_module.os, "geteuid", lambda: 0)
    with Arm64ConstructionChroot(
        rootfs,
        qemu_binary=qemu,
        runner=runner,
        host_resolv_conf=host_resolv,
        binfmt_root=binfmt,
    ) as chroot:
        assert (rootfs / "usr" / "bin" / "qemu-aarch64-static").is_file()
        assert (rootfs / "usr" / "sbin" / "policy-rc.d").is_file()
        assert original_resolv.read_text(encoding="utf-8") == "nameserver 1.1.1.1\n"
        chroot.run(("/usr/bin/apt-get", "-s", "install", "example"))

    assert not (rootfs / "usr" / "bin" / "qemu-aarch64-static").exists()
    assert not (rootfs / "usr" / "sbin" / "policy-rc.d").exists()
    assert original_resolv.read_text(encoding="utf-8") == "nameserver 127.0.0.53\n"
    assert any(call[0] == "mount" for call in calls)
    assert any(call[0] == "umount" for call in calls)
    assert any(
        call[:3] == ("chroot", str(rootfs), "/usr/bin/apt-get") for call in calls
    )


def test_chroot_requires_enabled_arm64_binfmt(tmp_path: Path, monkeypatch) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    qemu = tmp_path / "qemu-aarch64-static"
    qemu.write_bytes(b"qemu")
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()

    monkeypatch.setattr(chroot_module.os, "geteuid", lambda: 0)
    try:
        with Arm64ConstructionChroot(
            rootfs, qemu_binary=qemu, binfmt_root=binfmt
        ):
            raise AssertionError("unreachable")
    except chroot_module.ChrootError as exc:
        assert "binfmt_misc" in str(exc)


def test_failed_prepare_does_not_delete_existing_target_files(
    tmp_path: Path, monkeypatch
) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "sbin").mkdir(parents=True)
    (rootfs / "etc").mkdir(parents=True)
    existing_qemu = rootfs / "usr" / "bin" / "qemu-aarch64-static"
    existing_qemu.write_bytes(b"target-owned-qemu")
    existing_policy = rootfs / "usr" / "sbin" / "policy-rc.d"
    existing_policy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    existing_resolv = rootfs / "etc" / "resolv.conf"
    existing_resolv.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    host_qemu = tmp_path / "qemu-aarch64-static"
    host_qemu.write_bytes(b"host-qemu")
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()

    monkeypatch.setattr(chroot_module.os, "geteuid", lambda: 0)
    try:
        with Arm64ConstructionChroot(
            rootfs,
            qemu_binary=host_qemu,
            binfmt_root=binfmt,
        ):
            raise AssertionError("unreachable")
    except chroot_module.ChrootError as exc:
        assert "binfmt_misc" in str(exc)

    assert existing_qemu.read_bytes() == b"target-owned-qemu"
    assert existing_policy.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    assert existing_resolv.read_text(encoding="utf-8") == "nameserver 127.0.0.53\n"
