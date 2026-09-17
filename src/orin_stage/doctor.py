from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .acquisition.sdk_manager import (
    SdkManagerClient,
    SdkManagerError,
    SdkManagerTimeoutError,
)
from .build_toolchain import (
    BuildToolchainError,
    BuildToolchainManager,
    BuildToolchainNotFoundError,
)


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    status: CheckStatus
    name: str
    detail: str
    fix: str | None = None
    action: str | None = None
    hint: str | None = None
    error: str | None = None


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


class DiskUsageResult(Protocol):
    free: int


DiskUsage = Callable[[Path], DiskUsageResult]


OS_RELEASE_PATH = Path("/etc/os-release")
SUBUID_PATH = Path("/etc/subuid")
SUBGID_PATH = Path("/etc/subgid")
BINFMT_ROOT = Path("/proc/sys/fs/binfmt_misc")
QEMU_STATIC_PATH = Path("/usr/bin/qemu-aarch64-static")
MIN_SUBID_COUNT = 65_536

DATA_ROOT_ACTION = (
    "Choose a real writable directory with --data-root PATH, or correct the "
    "directory permissions."
)
PODMAN_FIX = "sudo apt update && sudo apt install -y podman"
UIDMAP_FIX = "sudo apt update && sudo apt install -y uidmap"
QEMU_STATIC_FIX = (
    "sudo apt update && sudo apt install --reinstall -y qemu-user-static"
)
BINFMT_FIXES = {
    "22.04": (
        "sudo apt update && sudo apt install --reinstall -y qemu-user-static "
        "binfmt-support && sudo update-binfmts --import qemu-aarch64 && sudo "
        "update-binfmts --enable qemu-aarch64"
    ),
    "24.04": (
        "sudo apt update && sudo apt install --reinstall -y qemu-user-static && "
        "sudo systemctl restart systemd-binfmt.service"
    ),
}


def _host_os(system: str) -> DoctorCheck:
    if system == "Linux":
        return DoctorCheck(CheckStatus.PASS, "Host OS", system)
    return DoctorCheck(
        CheckStatus.FAIL,
        "Host OS",
        f"{system} (Linux required)",
        action="Linux is required. Use a supported Ubuntu 22.04 or 24.04 host.",
    )


def _host_architecture(machine: str) -> DoctorCheck:
    if machine.lower() in {"x86_64", "amd64"}:
        return DoctorCheck(CheckStatus.PASS, "Host architecture", machine)
    return DoctorCheck(
        CheckStatus.FAIL,
        "Host architecture",
        f"{machine} (x86_64 required)",
        action="An x86_64/amd64 host is required.",
    )


def _parse_os_release(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _host_distribution(path: Path) -> tuple[DoctorCheck, str | None]:
    try:
        release = _parse_os_release(path)
    except (OSError, UnicodeError) as exc:
        return (
            DoctorCheck(
                CheckStatus.WARN,
                "Host distribution",
                f"unavailable: {exc}",
                action="Ubuntu 22.04 and 24.04 are the validated host releases.",
            ),
            None,
        )

    distro_id = release.get("ID", "unknown")
    version = release.get("VERSION_ID", "unknown")
    pretty_name = release.get("PRETTY_NAME") or f"{distro_id} {version}"
    status = (
        CheckStatus.INFO
        if distro_id.lower() == "ubuntu" and version in {"22.04", "24.04"}
        else CheckStatus.WARN
    )
    action = None
    if status is CheckStatus.WARN:
        action = "Ubuntu 22.04 and 24.04 are the validated host releases."
    ubuntu_version = version if distro_id.lower() == "ubuntu" else None
    return DoctorCheck(
        status,
        "Host distribution",
        pretty_name,
        action=action,
    ), ubuntu_version


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and not current.is_symlink():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _data_root(data_root: Path) -> tuple[DoctorCheck, Path]:
    if data_root.exists() or data_root.is_symlink():
        if data_root.is_symlink() or not data_root.is_dir():
            return (
                DoctorCheck(
                    CheckStatus.FAIL,
                    "Data root",
                    f"{data_root} is not a real directory",
                    action=DATA_ROOT_ACTION,
                ),
                _nearest_existing_path(data_root.parent),
            )
        if not os.access(data_root, os.W_OK):
            return (
                DoctorCheck(
                    CheckStatus.FAIL,
                    "Data root",
                    f"{data_root} is not writable",
                    action=DATA_ROOT_ACTION,
                ),
                data_root,
            )
        return DoctorCheck(CheckStatus.PASS, "Data root", str(data_root)), data_root

    parent = _nearest_existing_path(data_root.parent)
    if parent.is_symlink() or not parent.is_dir():
        return (
            DoctorCheck(
                CheckStatus.FAIL,
                "Data root",
                f"nearest existing parent is not a real directory: {parent}",
                action=DATA_ROOT_ACTION,
            ),
            parent,
        )
    if not os.access(parent, os.W_OK):
        return (
            DoctorCheck(
                CheckStatus.FAIL,
                "Data root",
                f"nearest existing parent is not writable: {parent}",
                action=DATA_ROOT_ACTION,
            ),
            parent,
        )
    return (
        DoctorCheck(
            CheckStatus.PASS,
            "Data root",
            f"{data_root} (creatable under {parent})",
        ),
        parent,
    )


def _free_disk(path: Path, disk_usage: DiskUsage) -> DoctorCheck:
    try:
        free = disk_usage(path).free
    except OSError as exc:
        return DoctorCheck(CheckStatus.INFO, "Free disk", f"unavailable: {exc}")
    return DoctorCheck(CheckStatus.INFO, "Free disk", f"{free / (1024**3):.1f} GiB")


def _run_probe(
    command: Sequence[str],
    *,
    runner: Runner,
) -> subprocess.CompletedProcess[str]:
    return runner(
        tuple(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _podman(which: Which, runner: Runner) -> tuple[DoctorCheck, str | None]:
    executable = which("podman")
    if executable is None:
        return DoctorCheck(
            CheckStatus.FAIL,
            "Podman",
            "not found",
            fix=PODMAN_FIX,
        ), None
    try:
        completed = _run_probe((executable, "--version"), runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            DoctorCheck(CheckStatus.FAIL, "Podman", f"cannot run: {exc}"),
            executable,
        )
    if completed.returncode != 0:
        return (
            DoctorCheck(
                CheckStatus.FAIL,
                "Podman",
                f"version command failed (exit {completed.returncode})",
            ),
            executable,
        )
    version = completed.stdout.strip().splitlines()
    detail = version[0] if version else "version command returned no output"
    if not version:
        return DoctorCheck(CheckStatus.FAIL, "Podman", detail), executable
    return DoctorCheck(CheckStatus.PASS, "Podman", detail), executable


def _mapping_helpers(which: Which) -> DoctorCheck:
    missing = [name for name in ("newuidmap", "newgidmap") if which(name) is None]
    if missing:
        return DoctorCheck(
            CheckStatus.FAIL,
            "Rootless helpers",
            f"missing: {', '.join(missing)}",
            fix=UIDMAP_FIX,
        )
    return DoctorCheck(CheckStatus.PASS, "Rootless helpers", "newuidmap, newgidmap")


def _sudo(which: Which, runner: Runner, username: str) -> DoctorCheck:
    executable = which("sudo")
    if executable is None:
        return DoctorCheck(
            CheckStatus.WARN,
            "sudo",
            "not found; required only for new base construction",
            action="Install the sudo package from an administrator/root session.",
        )
    try:
        completed = _run_probe((executable, "-n", "true"), runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            CheckStatus.WARN,
            "sudo",
            f"non-interactive probe failed: {exc}",
            action=(
                f"Ensure {username} is allowed to use sudo before running "
                "operations that require it."
            ),
        )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f"not usable non-interactively (exit {completed.returncode})"
        if stderr:
            detail = f"{detail}: {stderr.splitlines()[0]}"
        return DoctorCheck(
            CheckStatus.WARN,
            "sudo",
            detail,
            action=(
                f"Ensure {username} can use sudo; authenticate in an interactive "
                "session before operations that require it."
            ),
        )
    return DoctorCheck(CheckStatus.PASS, "sudo", executable)


def _has_subid_mapping(path: Path, username: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3 or parts[0] != username:
            continue
        try:
            start, count = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if start >= 0 and count >= MIN_SUBID_COUNT:
            return True
    return False


def _subid_mappings(
    username: str,
    subuid_path: Path,
    subgid_path: Path,
) -> DoctorCheck:
    missing: list[str] = []
    if not _has_subid_mapping(subuid_path, username):
        missing.append("subuid")
    if not _has_subid_mapping(subgid_path, username):
        missing.append("subgid")
    if missing:
        return DoctorCheck(
            CheckStatus.FAIL,
            "subuid/subgid",
            f"missing valid {', '.join(missing)} mapping for {username}",
            action=(
                "Configure non-overlapping subordinate UID/GID ranges for "
                f"{username} in /etc/subuid and /etc/subgid."
            ),
        )
    return DoctorCheck(CheckStatus.PASS, "subuid/subgid", f"configured for {username}")


def _podman_unshare(podman: str | None, runner: Runner) -> DoctorCheck:
    if podman is None:
        return DoctorCheck(
            CheckStatus.FAIL,
            "podman unshare",
            "Podman unavailable",
            hint=(
                "Rootless Podman could not create the user namespace. Check the "
                "error shown above."
            ),
        )
    try:
        completed = _run_probe((podman, "unshare", "true"), runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(CheckStatus.FAIL, "podman unshare", f"cannot run: {exc}")
    if completed.returncode != 0:
        return DoctorCheck(
            CheckStatus.FAIL,
            "podman unshare",
            f"failed (exit {completed.returncode})",
            hint=(
                "Rootless Podman could not create the user namespace. Check the "
                "error shown above."
            ),
            error=completed.stderr.strip() or None,
        )
    return DoctorCheck(CheckStatus.PASS, "podman unshare", "working")


def _arm64_binfmt(root: Path, ubuntu_version: str | None) -> DoctorCheck:
    candidates = (root / "qemu-aarch64", root / "qemu-aarch64-static")
    found_details: list[str] = []
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        enabled = bool(lines) and lines[0].strip() == "enabled"
        flags = ""
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            if separator and key.strip() == "flags":
                flags = value.strip()
                break
        if enabled and "F" in flags:
            return DoctorCheck(
                CheckStatus.PASS,
                "ARM64 binfmt",
                f"enabled, flags={flags} ({path.name})",
            )
        state = "enabled" if enabled else "disabled"
        found_details.append(f"{path.name}: {state}, flags={flags or '-'}")
    detail = "; ".join(found_details) if found_details else "entry not found"
    return DoctorCheck(
        CheckStatus.FAIL,
        "ARM64 binfmt",
        detail,
        fix=BINFMT_FIXES.get(ubuntu_version),
    )


def _qemu_static(path: Path, runner: Runner) -> DoctorCheck:
    if not path.is_file():
        return DoctorCheck(
            CheckStatus.WARN,
            "QEMU static",
            f"not found: {path}",
            fix=QEMU_STATIC_FIX,
        )
    try:
        completed = _run_probe((str(path), "--version"), runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            CheckStatus.WARN,
            "QEMU static",
            f"cannot run: {exc}",
            fix=QEMU_STATIC_FIX,
        )
    if completed.returncode != 0:
        return DoctorCheck(
            CheckStatus.WARN,
            "QEMU static",
            f"version command failed (exit {completed.returncode})",
            fix=QEMU_STATIC_FIX,
        )
    version = completed.stdout.strip().splitlines()
    if not version:
        return DoctorCheck(
            CheckStatus.WARN,
            "QEMU static",
            "empty version output",
            fix=QEMU_STATIC_FIX,
        )
    return DoctorCheck(CheckStatus.PASS, "QEMU static", version[0])


def _sdk_manager(client: SdkManagerClient) -> DoctorCheck:
    try:
        version = client.version(timeout_seconds=5.0).strip()
    except SdkManagerTimeoutError:
        return DoctorCheck(
            CheckStatus.WARN,
            "SDK Manager",
            "version probe timed out",
            action=(
                "Install NVIDIA SDK Manager from the official NVIDIA "
                "installation instructions."
            ),
        )
    except (SdkManagerError, OSError) as exc:
        return DoctorCheck(
            CheckStatus.WARN,
            "SDK Manager",
            f"unavailable: {exc}",
            action=(
                "Install NVIDIA SDK Manager from the official NVIDIA "
                "installation instructions."
            ),
        )
    if not version:
        return DoctorCheck(
            CheckStatus.WARN,
            "SDK Manager",
            "empty version output",
            action=(
                "Install NVIDIA SDK Manager from the official NVIDIA "
                "installation instructions."
            ),
        )
    return DoctorCheck(CheckStatus.PASS, "SDK Manager", version.splitlines()[0])


def _managed_build_toolchain(data_root: Path) -> DoctorCheck:
    try:
        record = BuildToolchainManager(data_root).inspect()
    except BuildToolchainNotFoundError:
        return DoctorCheck(
            CheckStatus.INFO,
            "Managed JP6 toolchain",
            "not acquired",
        )
    except BuildToolchainError as exc:
        detail = str(exc).splitlines()[0]
        return DoctorCheck(
            CheckStatus.WARN,
            "Managed JP6 toolchain",
            f"invalid: {detail}",
            action=(
                "The managed JP6 toolchain state is invalid; resolve the reported "
                "problem before using it."
            ),
        )
    return DoctorCheck(
        CheckStatus.PASS,
        "Managed JP6 toolchain",
        str(record.root_path),
    )


def _current_username() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, AttributeError, KeyError):
        import getpass

        return getpass.getuser()


def run_doctor(
    data_root: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    username: str | None = None,
    os_release_path: Path = OS_RELEASE_PATH,
    subuid_path: Path = SUBUID_PATH,
    subgid_path: Path = SUBGID_PATH,
    binfmt_root: Path = BINFMT_ROOT,
    qemu_static_path: Path = QEMU_STATIC_PATH,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
    disk_usage: DiskUsage = shutil.disk_usage,
    sdk_manager: SdkManagerClient | None = None,
) -> list[DoctorCheck]:
    """Inspect host prerequisites without changing host or project state."""
    resolved_data_root = Path(data_root).expanduser().resolve()
    data_root_check, disk_path = _data_root(resolved_data_root)
    podman_check, podman_executable = _podman(which, runner)
    current_username = username or _current_username()
    distribution_check, ubuntu_version = _host_distribution(os_release_path)

    return [
        _host_os(system or platform.system()),
        _host_architecture(machine or platform.machine()),
        distribution_check,
        data_root_check,
        _free_disk(disk_path, disk_usage),
        podman_check,
        _mapping_helpers(which),
        _sudo(which, runner, current_username),
        _subid_mappings(current_username, subuid_path, subgid_path),
        _podman_unshare(podman_executable, runner),
        _arm64_binfmt(binfmt_root, ubuntu_version),
        _qemu_static(qemu_static_path, runner),
        _managed_build_toolchain(resolved_data_root),
        _sdk_manager(sdk_manager or SdkManagerClient()),
    ]


def doctor_exit_code(checks: Sequence[DoctorCheck]) -> int:
    return 1 if any(check.status is CheckStatus.FAIL for check in checks) else 0


def format_report(checks: Sequence[DoctorCheck]) -> str:
    rows = list(checks)
    name_width = max((len(check.name) for check in rows), default=0)
    lines = ["Orin Stage Doctor", ""]
    for check in rows:
        lines.append(
            f"{check.status.value:<5} {check.name:<{name_width}}  {check.detail}"
        )
        if check.error:
            error_lines = check.error.splitlines()
            lines.append(f"      Error: {error_lines[0]}")
            lines.extend(f"             {line}" for line in error_lines[1:])
        for label, value in (
            ("Fix", check.fix),
            ("Action", check.action),
            ("Hint", check.hint),
        ):
            if value:
                lines.append(f"      {label}: {value}")
    counts = Counter(check.status for check in rows)
    lines.extend(
        (
            "",
            "Summary: "
            f"{counts[CheckStatus.PASS]} PASS, "
            f"{counts[CheckStatus.WARN]} WARN, "
            f"{counts[CheckStatus.FAIL]} FAIL",
        )
    )
    return "\n".join(lines)
