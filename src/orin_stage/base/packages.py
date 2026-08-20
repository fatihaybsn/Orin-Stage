from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import json_digest
from .chroot import Arm64ConstructionChroot


class PackageResolutionError(RuntimeError):
    """Raised when the exact construction package transaction cannot be frozen."""


@dataclass(frozen=True, slots=True)
class PackageSeed:
    name: str
    version: str
    architecture: str

    @property
    def apt_spec(self) -> str:
        if self.architecture == "all":
            return f"{self.name}={self.version}"
        return f"{self.name}:{self.architecture}={self.version}"


@dataclass(frozen=True, slots=True)
class LockedPackage:
    name: str
    version: str
    architecture: str
    operation: str
    filename: str
    sha256: str

    @property
    def apt_spec(self) -> str:
        if self.architecture == "all":
            return f"{self.name}={self.version}"
        return f"{self.name}:{self.architecture}={self.version}"


@dataclass(frozen=True, slots=True)
class ConstructionPackageSet:
    seed: PackageSeed
    repository_base: str
    repository_suites: tuple[str, ...]
    packages: tuple[LockedPackage, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "seed": asdict(self.seed),
            "repositories": {
                "base": self.repository_base,
                "suites": list(self.repository_suites),
            },
            "packages": [asdict(package) for package in self.packages],
        }

    def digest(self) -> str:
        return json_digest(self.to_dict())


_SIMULATED_INSTALL = re.compile(
    r"^Inst\s+(?P<name>\S+?)(?::(?P<name_arch>[^\s]+))?"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?\s+"
    r"\((?P<version>\S+)(?:\s+.*?)?\s+\[(?P<architecture>[^\]]+)\]\)$"
)


def package_seed_from_target(target: ResolvedCatalogTarget) -> PackageSeed:
    metadata = target.record["packages"]["meta_package"]
    version = metadata["version_build"]
    if not isinstance(version, str) or not version:
        raise PackageResolutionError(
            "JP6.2.3 construction requires an exact meta-package version in the catalog"
        )
    return PackageSeed(
        name=str(metadata["name"]),
        version=version,
        architecture=str(metadata["architecture"]),
    )


def render_temporary_nvidia_sources(target: ResolvedCatalogTarget) -> str:
    repository = target.record["packages"]["repository"]
    base = str(repository["base"]).rstrip("/")
    lines: list[str] = []
    for suite in repository["suites"]:
        component, release, pocket = str(suite).split()
        lines.append(f"deb {base}/{component} {release} {pocket}")
    return "\n".join(lines) + "\n"


def parse_apt_simulation(output: str) -> tuple[tuple[str, str, str, str], ...]:
    """Return (name, version, architecture, operation) rows from apt simulation."""

    rows: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("Inst "):
            continue
        match = _SIMULATED_INSTALL.match(line)
        if match is None:
            raise PackageResolutionError(f"cannot parse apt simulation line: {line}")
        name = match.group("name")
        name_arch = match.group("name_arch")
        architecture = match.group("architecture")
        if name_arch and name_arch != architecture:
            raise PackageResolutionError(
                f"apt simulation architecture mismatch for {name}: "
                f"{name_arch!r} vs {architecture!r}"
            )
        key = (name, architecture)
        if key in seen:
            raise PackageResolutionError(
                f"apt simulation contains duplicate package transaction: {name}:{architecture}"
            )
        seen.add(key)
        operation = "upgrade" if match.group("old_version") else "install"
        rows.append((name, match.group("version"), architecture, operation))
    rows.sort(key=lambda row: (row[0], row[2]))
    return tuple(rows)


def _simulate(
    chroot: Arm64ConstructionChroot,
    seed: PackageSeed,
) -> tuple[tuple[str, str, str, str], ...]:
    completed = chroot.run(
        (
            "/usr/bin/apt-get",
            "-s",
            "-o",
            "Debug::NoLocking=true",
            "--no-remove",
            "install",
            seed.apt_spec,
        ),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
    return parse_apt_simulation(completed.stdout)


def _deb_control(
    path: Path,
    *,
    runner=subprocess.run,
) -> tuple[str, str, str]:
    completed = runner(
        ("dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise PackageResolutionError(
            f"cannot inspect downloaded Debian package {path}: {completed.stderr}"
        )
    fields: dict[str, str] = {}
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition(":")
        if not separator or key not in {"Package", "Version", "Architecture"}:
            raise PackageResolutionError(
                f"unexpected Debian control output for {path}: {completed.stdout!r}"
            )
        fields[key] = value.strip()
    if set(fields) != {"Package", "Version", "Architecture"} or any(
        not value for value in fields.values()
    ):
        raise PackageResolutionError(
            f"unexpected Debian control output for {path}: {completed.stdout!r}"
        )
    return fields["Package"], fields["Version"], fields["Architecture"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _index_downloaded_debs(
    archives: Path,
    *,
    runner=subprocess.run,
) -> Mapping[tuple[str, str, str], tuple[Path, str]]:
    indexed: dict[tuple[str, str, str], tuple[Path, str]] = {}
    for path in sorted(Path(archives).glob("*.deb")):
        if not path.is_file() or path.is_symlink():
            continue
        name, version, architecture = _deb_control(path, runner=runner)
        key = (name, version, architecture)
        if key in indexed:
            raise PackageResolutionError(
                f"multiple downloaded Debian archives provide {name}:{architecture}={version}"
            )
        indexed[key] = (path, _sha256(path))
    return indexed


def write_temporary_nvidia_sources(
    rootfs: Path, target: ResolvedCatalogTarget
) -> Path:
    source_path = (
        Path(rootfs)
        / "etc"
        / "apt"
        / "sources.list.d"
        / "orin-stage-construction.list"
    )
    if source_path.exists() or source_path.is_symlink():
        raise PackageResolutionError(
            f"temporary construction source already exists: {source_path}"
        )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(render_temporary_nvidia_sources(target), encoding="utf-8")
    return source_path


def resolve_construction_package_set(
    chroot: Arm64ConstructionChroot,
    target: ResolvedCatalogTarget,
    *,
    runner=subprocess.run,
) -> ConstructionPackageSet:
    """Resolve, download and byte-lock the exact transaction for nvidia-jetpack."""

    seed = package_seed_from_target(target)
    chroot.run(("/usr/bin/apt-get", "update"), env={"DEBIAN_FRONTEND": "noninteractive"})
    transaction = _simulate(chroot, seed)
    if not transaction:
        raise PackageResolutionError(
            "nvidia-jetpack exact seed produced an empty construction transaction"
        )

    exact_specs = [
        f"{name}={version}" if arch == "all" else f"{name}:{arch}={version}"
        for name, version, arch, _operation in transaction
    ]
    chroot.run(
        (
            "/usr/bin/apt-get",
            "--download-only",
            "--yes",
            "--no-remove",
            "install",
            *exact_specs,
        ),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )

    archives = chroot.rootfs / "var" / "cache" / "apt" / "archives"
    indexed = _index_downloaded_debs(archives, runner=runner)
    locked: list[LockedPackage] = []
    for name, version, architecture, operation in transaction:
        archive = indexed.get((name, version, architecture))
        if archive is None:
            raise PackageResolutionError(
                f"downloaded archive is missing for {name}:{architecture}={version}"
            )
        path, sha256 = archive
        locked.append(
            LockedPackage(
                name=name,
                version=version,
                architecture=architecture,
                operation=operation,
                filename=path.name,
                sha256=sha256,
            )
        )

    repository = target.record["packages"]["repository"]
    return ConstructionPackageSet(
        seed=seed,
        repository_base=str(repository["base"]),
        repository_suites=tuple(str(item) for item in repository["suites"]),
        packages=tuple(locked),
    )


def verify_locked_package_archives(
    rootfs: Path,
    package_set: ConstructionPackageSet,
    *,
    runner=subprocess.run,
) -> None:
    indexed = _index_downloaded_debs(
        Path(rootfs) / "var" / "cache" / "apt" / "archives",
        runner=runner,
    )
    for package in package_set.packages:
        archive = indexed.get((package.name, package.version, package.architecture))
        if archive is None:
            raise PackageResolutionError(
                f"locked Debian archive is missing for {package.apt_spec}"
            )
        path, actual = archive
        if path.name != package.filename or actual != package.sha256:
            raise PackageResolutionError(
                f"locked Debian archive changed for {package.apt_spec}"
            )


def install_locked_package_set(
    chroot: Arm64ConstructionChroot,
    package_set: ConstructionPackageSet,
    *,
    runner=subprocess.run,
) -> None:
    """Install only after the current apt simulation exactly matches the frozen lock."""

    current = _simulate(chroot, package_set.seed)
    expected = tuple(
        (package.name, package.version, package.architecture, package.operation)
        for package in package_set.packages
    )
    if current != expected:
        raise PackageResolutionError(
            "APT transaction changed after package lock was created; refusing construction"
        )

    verify_locked_package_archives(chroot.rootfs, package_set, runner=runner)
    specs = tuple(package.apt_spec for package in package_set.packages)
    chroot.run(
        (
            "/usr/bin/apt-get",
            "--no-download",
            "--yes",
            "--no-remove",
            "install",
            *specs,
        ),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )


def clean_package_archives(chroot: Arm64ConstructionChroot) -> None:
    chroot.run(
        ("/usr/bin/apt-get", "clean"),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
