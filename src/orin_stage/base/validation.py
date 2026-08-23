from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable, Mapping

from .chroot import Arm64ConstructionChroot
from .packages import ConstructionPackageSet, LockedPackage


BASE_VALIDATION_POLICY_ID = "base-validation-v2"
BASE_VALIDATION_POLICY_VERSION = 2
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


def _normalize_dpkg_path(value: str) -> str | None:
    """Normalize only unambiguous absolute dpkg payload paths."""

    if not value.startswith("/"):
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    normalized = str(path).lstrip("/")
    return normalized if normalized not in {"", "."} else None


def _installed_rows_by_name(
    runtime: RuntimeValidationSnapshot,
) -> dict[str, tuple[Mapping[str, str], ...]]:
    by_name: dict[str, list[Mapping[str, str]]] = {}
    for row in runtime.dpkg_packages:
        if row["status"] != "installed":
            continue
        by_name.setdefault(row["name"], []).append(row)
    return {
        name: tuple(sorted(rows, key=lambda item: item["architecture"]))
        for name, rows in by_name.items()
    }


def _owner_identity(
    info_stem: str,
    installed: Mapping[str, tuple[Mapping[str, str], ...]],
) -> tuple[str, str | None, str | None]:
    if ":" in info_stem:
        name, architecture = info_stem.rsplit(":", 1)
        row = next(
            (
                item
                for item in installed.get(name, ())
                if item["architecture"] == architecture
            ),
            None,
        )
        return name, row["version"] if row is not None else None, architecture

    rows = installed.get(info_stem, ())
    if len(rows) == 1:
        return info_stem, rows[0]["version"], rows[0]["architecture"]
    return info_stem, None, None


def _dpkg_owner_candidates(
    rootfs: Path,
    relative_path: str,
    runtime: RuntimeValidationSnapshot,
) -> tuple[tuple[str, str | None, str | None, Path], ...]:
    info_root = rootfs / "var" / "lib" / "dpkg" / "info"
    if not info_root.is_dir():
        return ()

    installed = _installed_rows_by_name(runtime)
    candidates: list[tuple[str, str | None, str | None, Path]] = []
    for list_path in sorted(info_root.glob("*.list")):
        if list_path.is_symlink() or not list_path.is_file():
            continue
        try:
            with list_path.open(
                "r", encoding="utf-8", errors="surrogateescape"
            ) as handle:
                owns_path = any(
                    _normalize_dpkg_path(entry.rstrip("\n")) == relative_path
                    for entry in handle
                )
        except OSError:
            continue
        if not owns_path:
            continue
        info_stem = list_path.name.removesuffix(".list")
        name, version, architecture = _owner_identity(info_stem, installed)
        candidates.append((name, version, architecture, list_path))
    return tuple(candidates)


def _dpkg_md5_entry(md5sums_path: Path, relative_path: str) -> str | None:
    if md5sums_path.is_symlink() or not md5sums_path.is_file():
        return None
    try:
        handle = md5sums_path.open(
            "r", encoding="utf-8", errors="surrogateescape"
        )
    except OSError:
        return None
    matches: list[str] = []
    with handle:
        for raw_line in handle:
            checksum, separator, payload_path = raw_line.rstrip("\n").partition("  ")
            if not separator or len(checksum) != 32:
                continue
            normalized = _normalize_dpkg_path(f"/{payload_path}")
            if normalized == relative_path:
                matches.append(checksum.lower())
    if len(matches) != 1:
        return None
    return matches[0]


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ForeignElfProvenance:
    path: str
    machine: int
    owners: tuple[tuple[str, str | None, str | None, Path], ...]
    locked_candidates: tuple[LockedPackage, ...]
    locked_package: LockedPackage | None
    expected_md5: str | None
    actual_md5: str
    failure: str | None

    @property
    def accepted(self) -> bool:
        return self.failure is None

    def evidence(self) -> dict[str, object]:
        if not self.accepted or len(self.owners) != 1 or self.locked_package is None:
            raise BaseValidationError("foreign ELF provenance evidence is incomplete")
        name, version, architecture, _list_path = self.owners[0]
        return {
            "path": self.path,
            "elf_machine": self.machine,
            "owner_package": name,
            "owner_version": version,
            "owner_architecture": architecture,
            "locked_deb_filename": self.locked_package.filename,
            "locked_deb_sha256": self.locked_package.sha256,
            "payload_checksum": self.expected_md5,
            "final_checksum": self.actual_md5,
            "decision": "accepted_exact_locked_package_payload",
        }

    def diagnostic(self) -> str:
        if not self.owners:
            owner_name = "NOT FOUND"
            owner_candidates = "(none)"
            installed_version = "NOT FOUND"
            installed_architecture = "NOT FOUND"
        elif len(self.owners) > 1:
            owner_name = "AMBIGUOUS"
            owner_candidates = ", ".join(
                f"{name}:{architecture or 'UNKNOWN'}"
                for name, _version, architecture, _list_path in self.owners
            )
            installed_version = ", ".join(
                f"{name}={version or 'NOT FOUND'}"
                for name, version, _architecture, _list_path in self.owners
            )
            installed_architecture = ", ".join(
                f"{name}:{architecture or 'NOT FOUND'}"
                for name, _version, architecture, _list_path in self.owners
            )
        else:
            name, version, architecture, _list_path = self.owners[0]
            owner_name = name
            owner_candidates = f"{name}:{architecture or 'UNKNOWN'}"
            installed_version = version or "NOT FOUND"
            installed_architecture = architecture or "NOT FOUND"

        if self.locked_candidates:
            locked_version = ", ".join(
                item.version for item in self.locked_candidates
            )
            locked_architecture = ", ".join(
                item.architecture for item in self.locked_candidates
            )
            locked_filename = ", ".join(
                item.filename or "NOT FOUND" for item in self.locked_candidates
            )
            locked_sha256 = ", ".join(
                item.sha256 or "NOT FOUND" for item in self.locked_candidates
            )
        else:
            locked_version = "NOT FOUND"
            locked_architecture = "NOT FOUND"
            locked_filename = "NOT FOUND"
            locked_sha256 = "NOT FOUND"

        return "\n".join(
            (
                "foreign ELF diagnostic:",
                f"foreign ELF path: {self.path}",
                f"ELF machine: {self.machine}",
                "",
                "dpkg owner:",
                f"- package name: {owner_name}",
                f"- candidates: {owner_candidates}",
                "",
                "installed package:",
                f"- version: {installed_version}",
                f"- architecture: {installed_architecture}",
                "",
                "exact ConstructionPackageSet match:",
                f"- match: {'yes' if self.locked_package is not None else 'no'}",
                f"- locked package version: {locked_version}",
                f"- locked package architecture: {locked_architecture}",
                f"- locked .deb filename: {locked_filename}",
                f"- locked .deb SHA256: {locked_sha256}",
                "",
                "dpkg md5sums:",
                f"- entry found: {'yes' if self.expected_md5 is not None else 'no'}",
                f"- expected MD5: {self.expected_md5 or 'NOT FOUND'}",
                f"- actual MD5: {self.actual_md5}",
                f"- match: {'yes' if self.expected_md5 == self.actual_md5 else 'no'}",
                "",
                f"decision: rejected ({self.failure or 'none'})",
            )
        )


def _valid_locked_evidence(package: LockedPackage) -> bool:
    return bool(package.filename) and len(package.sha256) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in package.sha256
    )


def verify_foreign_elf_provenance(
    rootfs: Path,
    path: Path,
    relative_path: str,
    machine: int,
    package_set: ConstructionPackageSet,
    runtime: RuntimeValidationSnapshot,
) -> ForeignElfProvenance:
    owners = _dpkg_owner_candidates(rootfs, relative_path, runtime)
    actual_md5 = _md5(path)
    owner_names = {name for name, _version, _architecture, _path in owners}
    locked_candidates = tuple(
        package for package in package_set.packages if package.name in owner_names
    )
    locked_package = None
    expected_md5 = None
    failure = None

    if not owners:
        failure = "dpkg owner not found"
    elif len(owners) != 1:
        failure = "dpkg ownership is ambiguous"
    else:
        name, version, architecture, list_path = owners[0]
        if version is None or architecture is None:
            failure = "installed package identity is unavailable or ambiguous"
        else:
            locked_package = next(
                (
                    package
                    for package in locked_candidates
                    if package.version == version
                    and package.architecture == architecture
                ),
                None,
            )
            if locked_package is None:
                failure = "owner is not in the exact construction package set"
            elif not _valid_locked_evidence(locked_package):
                failure = "locked .deb filename or SHA256 evidence is missing"

        expected_md5 = _dpkg_md5_entry(
            list_path.with_name(
                f"{list_path.name.removesuffix('.list')}.md5sums"
            ),
            relative_path,
        )
        if failure is None and expected_md5 is None:
            failure = "dpkg md5sums entry not found or ambiguous"
        elif failure is None and expected_md5 != actual_md5:
            failure = "dpkg payload checksum mismatch"

    return ForeignElfProvenance(
        path=relative_path,
        machine=machine,
        owners=owners,
        locked_candidates=locked_candidates,
        locked_package=locked_package,
        expected_md5=expected_md5,
        actual_md5=actual_md5,
        failure=failure,
    )


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
    accepted_foreign_elf: list[dict[str, object]] = []
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
                    provenance = verify_foreign_elf_provenance(
                        root,
                        path,
                        rel,
                        machine,
                        package_set,
                        runtime,
                    )
                    if not provenance.accepted:
                        raise BaseValidationError(
                            "unverified x86_64 ELF remained in final ARM64 base: "
                            f"{rel}\n\n{provenance.diagnostic()}"
                        )
                    accepted_foreign_elf.append(provenance.evidence())
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
    accepted_foreign_elf.sort(key=lambda item: str(item["path"]))

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
            "provenance_required_machine": _EM_X86_64,
            "files": elf_rows,
            "accepted_foreign_files": accepted_foreign_elf,
        },
        "symlinks": symlinks,
        "xattrs": xattrs,
        "capabilities": capabilities,
        "hardlink_groups": hardlinks,
    }
