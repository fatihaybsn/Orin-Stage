from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Mapping, Sequence


class ChrootError(RuntimeError):
    """Raised when the ARM64 construction chroot cannot be prepared or executed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class Arm64ConstructionChroot(AbstractContextManager["Arm64ConstructionChroot"]):
    """Minimal QEMU-backed chroot used only while constructing the ARM64 base."""

    def __init__(
        self,
        rootfs: Path,
        *,
        qemu_binary: Path = Path("/usr/bin/qemu-aarch64-static"),
        runner: Runner = subprocess.run,
        host_resolv_conf: Path = Path("/etc/resolv.conf"),
        binfmt_root: Path = Path("/proc/sys/fs/binfmt_misc"),
    ) -> None:
        self.rootfs = Path(rootfs)
        self.qemu_binary = Path(qemu_binary)
        self.runner = runner
        self.host_resolv_conf = Path(host_resolv_conf)
        self.binfmt_root = Path(binfmt_root)
        self._mounted: list[Path] = []
        self._resolv_state: tuple[str, object] | None = None
        self._qemu_created = False
        self._policy_rcd_created = False
        self._prepared = False

    @property
    def qemu_guest_path(self) -> str:
        return "/usr/bin/qemu-aarch64-static"

    def _host_run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        completed = self.runner(
            tuple(str(part) for part in command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise ChrootError(
                f"construction host command failed ({completed.returncode}): "
                f"{' '.join(str(part) for part in command)}\n{completed.stderr}"
            )
        return completed


    def _require_binfmt(self) -> None:
        candidates = (
            self.binfmt_root / "qemu-aarch64",
            self.binfmt_root / "qemu-aarch64-static",
        )
        for path in candidates:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if content.splitlines() and content.splitlines()[0].strip() == "enabled":
                return
        raise ChrootError(
            "ARM64 binfmt_misc support is not enabled; install/enable "
            "qemu-user-static with binfmt-support before base construction"
        )

    def _prepare_resolv_conf(self) -> None:
        target = self.rootfs / "etc" / "resolv.conf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not self.host_resolv_conf.is_file():
            raise ChrootError(f"host resolver file is missing: {self.host_resolv_conf}")

        if target.is_symlink():
            self._resolv_state = ("symlink", os.readlink(target))
            target.unlink()
        elif target.exists():
            backup = target.with_name(".resolv.conf.orin-stage-backup")
            if backup.exists() or backup.is_symlink():
                raise ChrootError(f"construction resolver backup already exists: {backup}")
            os.replace(target, backup)
            self._resolv_state = ("file", backup)
        else:
            self._resolv_state = ("missing", None)

        shutil.copyfile(self.host_resolv_conf, target)

    def _restore_resolv_conf(self) -> None:
        if self._resolv_state is None:
            return
        target = self.rootfs / "etc" / "resolv.conf"
        target.unlink(missing_ok=True)
        kind, value = self._resolv_state
        if kind == "symlink":
            target.symlink_to(str(value))
        elif kind == "file":
            os.replace(Path(value), target)
        elif kind != "missing":
            raise ChrootError(f"unknown resolver restore state: {kind}")
        self._resolv_state = None

    def _prepare_qemu(self) -> None:
        if not self.qemu_binary.is_absolute() or not self.qemu_binary.is_file():
            raise ChrootError(f"qemu-aarch64-static is missing: {self.qemu_binary}")
        destination = self.rootfs / self.qemu_guest_path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ChrootError(
                f"construction QEMU path already exists in target rootfs: {destination}"
            )
        self._qemu_created = True
        shutil.copy2(self.qemu_binary, destination)

    def _prepare_policy_rcd(self) -> None:
        path = self.rootfs / "usr" / "sbin" / "policy-rc.d"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            raise ChrootError(
                f"construction service-start policy path already exists: {path}"
            )
        self._policy_rcd_created = True
        path.write_text("#!/bin/sh\nexit 101\n", encoding="utf-8")
        path.chmod(0o755)

    def _mount(self, command: Sequence[str], target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        self._host_run(command)
        self._mounted.append(target)

    def __enter__(self) -> "Arm64ConstructionChroot":
        if os.geteuid() != 0:
            raise ChrootError("base construction chroot requires root privileges")
        if not self.rootfs.is_absolute() or not self.rootfs.is_dir():
            raise ChrootError(f"target rootfs must be an existing absolute directory: {self.rootfs}")

        try:
            self._require_binfmt()
            self._prepare_qemu()
            self._prepare_policy_rcd()
            self._prepare_resolv_conf()
            self._mount(
                ("mount", "-t", "proc", "proc", str(self.rootfs / "proc")),
                self.rootfs / "proc",
            )
            self._mount(
                ("mount", "-t", "sysfs", "sysfs", str(self.rootfs / "sys")),
                self.rootfs / "sys",
            )
            self._mount(
                ("mount", "--bind", "/dev", str(self.rootfs / "dev")),
                self.rootfs / "dev",
            )
            self._mount(
                ("mount", "--bind", "/dev/pts", str(self.rootfs / "dev" / "pts")),
                self.rootfs / "dev" / "pts",
            )
        except Exception:
            self.close()
            raise

        self._prepared = True
        return self

    def run(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not self._prepared:
            raise ChrootError("construction chroot is not active")
        full_command = (
            "chroot",
            str(self.rootfs),
            *(str(part) for part in command),
        )
        process_env = os.environ.copy()
        process_env["LC_ALL"] = "C"
        if env:
            process_env.update(env)
        completed = self.runner(
            full_command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
        )
        if check and completed.returncode != 0:
            raise ChrootError(
                f"ARM64 chroot command failed ({completed.returncode}): "
                f"{' '.join(str(part) for part in command)}\n{completed.stderr}"
            )
        return completed

    def close(self) -> None:
        cleanup_errors: list[str] = []
        for mountpoint in reversed(self._mounted):
            completed = self.runner(
                ("umount", str(mountpoint)),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                cleanup_errors.append(
                    f"cannot unmount {mountpoint}: {completed.stderr.strip()}"
                )
        self._mounted.clear()

        if self._policy_rcd_created:
            policy = self.rootfs / "usr" / "sbin" / "policy-rc.d"
            policy.unlink(missing_ok=True)
            self._policy_rcd_created = False
        if self._qemu_created:
            qemu = self.rootfs / self.qemu_guest_path.lstrip("/")
            qemu.unlink(missing_ok=True)
            self._qemu_created = False
        try:
            self._restore_resolv_conf()
        except Exception as exc:  # pragma: no cover - best effort after primary failure
            cleanup_errors.append(str(exc))
        self._prepared = False
        if cleanup_errors:
            raise ChrootError("; ".join(cleanup_errors))

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.close()
        except ChrootError:
            if exc is None:
                raise
        return False


def read_qemu_version(
    qemu_binary: Path = Path("/usr/bin/qemu-aarch64-static"),
    *,
    runner: Runner = subprocess.run,
) -> str:
    path = Path(qemu_binary)
    completed = runner(
        (str(path), "--version"),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise ChrootError(f"cannot read QEMU version from {path}: {completed.stderr}")
    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not first_line:
        raise ChrootError(f"QEMU returned an empty version string: {path}")
    return first_line
