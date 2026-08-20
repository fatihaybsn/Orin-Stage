from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .chroot import Arm64ConstructionChroot
from .packages import ConstructionPackageSet


BASE_VALIDATION_POLICY_ID = "base-validation-v1"
BASE_VALIDATION_POLICY_VERSION = 1
_EM_X86_64 = 62
_EM_AARCH64 = 183
_SKIP_TOP_LEVEL = {"proc", "sys", "dev"}


class BaseValidationError(RuntimeError):
    """Raised when a constructed base fails the minimum official-base gate."""


@dataclass(frozen=True, slots=True)
class RuntimeValidationSnapshot:
    dpkg_packages: tuple[dict[str, str], ...]
    alternatives: tuple[dict[str, str], ...]
    ld_cache: tuple[str, ...]


def _parse_dpkg_state(output: str) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise BaseValidationError(f"cannot parse dpkg state line: {line!r}")
        name, version, architecture, status = fields
        rows.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "status": status,
            }
        )
    rows.sort(key=lambda item: (item["name"], item["architecture"]))
    return tuple(rows)


def _parse_alternatives(output: str) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 2)
        if len(fields) != 3:
            raise BaseValidationError(f"cannot parse alternatives line: {line!r}")
        name, mode, value = fields
        rows.append({"name": name, "mode": mode, "value": value})
    rows.sort(key=lambda item: item["name"])
    return tuple(rows)


def validate_runtime_state(
    chroot: Arm64ConstructionChroot,
    package_set: ConstructionPackageSet,
) -> RuntimeValidationSnapshot:
    audit = chroot.run(("/usr/bin/dpkg", "--audit"))
    if audit.stdout.strip():
        raise BaseValidationError(f"dpkg --audit reported problems:\n{audit.stdout}")

    dpkg = chroot.run(
        (
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\n",
        )
    )
    packages = _parse_dpkg_state(dpkg.stdout)
    by_key = {(row["name"], row["architecture"]): row for row in packages}
    for expected in package_set.packages:
        actual = by_key.get((expected.name, expected.architecture))
        if actual is None:
            raise BaseValidationError(f"locked package is not installed: {expected.apt_spec}")
        if actual["version"] != expected.version or actual["status"] != "installed":
            raise BaseValidationError(
                f"locked package state mismatch for {expected.apt_spec}: {actual}"
            )

    alternatives_result = chroot.run(("/usr/bin/update-alternatives", "--get-selections"))
    alternatives = _parse_alternatives(alternatives_result.stdout)
    for item in alternatives:
        value = item["value"]
        if value.startswith("/") and not (chroot.rootfs / value.lstrip("/")).exists():
            raise BaseValidationError(
                f"alternative {item['name']!r} selects a missing target: {value}"
            )

    chroot.run(("/sbin/ldconfig",))
    cache = chroot.run(("/sbin/ldconfig", "-p"))
    ld_cache = tuple(line for line in cache.stdout.splitlines() if line.strip())
    if not ld_cache:
        raise BaseValidationError("ldconfig cache is empty")

    return RuntimeValidationSnapshot(
        dpkg_packages=packages,
        alternatives=alternatives,
        ld_cache=ld_cache,
    )


def _iter_final_tree(rootfs: Path) -> Iterable[Path]:
    root = Path(rootfs)
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == root:
            dirnames[:] = [name for name in dirnames if name not in _SKIP_TOP_LEVEL]
        for name in sorted(dirnames):
            yield current_path / name
        for name in sorted(filenames):
            yield current_path / name


def _relative(rootfs: Path, path: Path) -> str:
    return str(path.relative_to(rootfs))


def _elf_machine(path: Path) -> int | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise BaseValidationError(f"cannot read file during ELF audit: {path}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    data_encoding = header[5]
    if data_encoding == 1:
        return int.from_bytes(header[18:20], "little")
    if data_encoding == 2:
        return int.from_bytes(header[18:20], "big")
    raise BaseValidationError(f"ELF file has unknown byte order: {path}")


def _xattrs(path: Path) -> dict[str, str]:
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise BaseValidationError(f"cannot read xattrs: {path}") from exc
    result: dict[str, str] = {}
    for name in sorted(names):
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            raise BaseValidationError(f"cannot read xattr {name!r}: {path}") from exc
        result[name] = value.hex()
    return result


def build_final_manifest(
    rootfs: Path,
    *,
    base_digest: str,
    package_set: ConstructionPackageSet,
    runtime: RuntimeValidationSnapshot,
) -> dict[str, object]:
    """Audit the final post-cleanup tree and return the configured-base manifest."""

    root = Path(rootfs)
    if not root.is_dir():
        raise BaseValidationError(f"base rootfs is missing: {root}")

    elf_rows: list[dict[str, object]] = []
    symlinks: list[dict[str, object]] = []
    xattrs: list[dict[str, object]] = []
    capabilities: list[dict[str, str]] = []
    hardlink_candidates: dict[tuple[int, int], list[str]] = {}

    for path in _iter_final_tree(root):
        rel = _relative(root, path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise BaseValidationError(f"cannot stat final base path: {path}") from exc

        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise BaseValidationError(f"cannot read symlink: {path}") from exc
            symlinks.append({"path": rel, "target": target})

        attrs = _xattrs(path)
        if attrs:
            xattrs.append({"path": rel, "values": attrs})
            capability = attrs.get("security.capability")
            if capability is not None:
                capabilities.append({"path": rel, "value": capability})

        if stat.S_ISREG(info.st_mode):
            machine = _elf_machine(path)
            if machine is not None:
                elf_rows.append({"path": rel, "machine": machine})
                if machine == _EM_X86_64:
                    raise BaseValidationError(
                        f"host x86_64 ELF remained in final ARM64 base: {rel}"
                    )
            if info.st_nlink > 1:
                hardlink_candidates.setdefault((info.st_dev, info.st_ino), []).append(rel)

    hardlinks = [
        sorted(paths)
        for paths in hardlink_candidates.values()
        if len(paths) > 1
    ]
    hardlinks.sort(key=lambda group: group[0])
    symlinks.sort(key=lambda item: str(item["path"]))
    xattrs.sort(key=lambda item: str(item["path"]))
    capabilities.sort(key=lambda item: item["path"])
    elf_rows.sort(key=lambda item: str(item["path"]))

    return {
        "schema_version": 1,
        "base_digest": base_digest,
        "validation_policy": {
            "id": BASE_VALIDATION_POLICY_ID,
            "version": BASE_VALIDATION_POLICY_VERSION,
        },
        "construction_package_set_digest": package_set.digest(),
        "dpkg_packages": list(runtime.dpkg_packages),
        "alternatives": list(runtime.alternatives),
        "ld_cache": list(runtime.ld_cache),
        "elf": {
            "target_machine": _EM_AARCH64,
            "forbidden_host_machine": _EM_X86_64,
            "files": elf_rows,
        },
        "symlinks": symlinks,
        "xattrs": xattrs,
        "capabilities": capabilities,
        "hardlink_groups": hardlinks,
    }
