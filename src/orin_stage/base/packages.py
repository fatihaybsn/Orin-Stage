from __future__ import annotations

import hashlib
import os
import re
import subprocess
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import json_digest
from .chroot import Arm64ConstructionChroot, ChrootError


class PackageResolutionError(RuntimeError):
    """Raised when the exact construction package transaction cannot be frozen."""


_NVIDIA_REPOSITORY = "https://repo.download.nvidia.com/jetson/"
_OFFICIAL_NVIDIA_SOURCE = "nvidia-l4t-apt-source.list"
_CONSTRUCTION_NVIDIA_SOURCE = "orin-stage-construction.list"
_DISABLED_NVIDIA_SOURCE = ".nvidia-l4t-apt-source.list.orin-stage-disabled"
_UNRESOLVED_PLACEHOLDER = re.compile(r"<[A-Za-z_][A-Za-z0-9_-]*>")
_REMOVAL_FORBIDDEN_ERROR = "Packages need to be removed but remove is disabled"
_DENY_ALL_REMOVAL_POLICY_VERSION = "deny-all-v1"


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
class PackageRemovalPolicy:
    version: str
    jetpack_version: str
    l4t_version: str
    allowed_removal_set: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("package removal policy version must not be empty")
        if self.allowed_removal_set != tuple(sorted(set(self.allowed_removal_set))):
            raise ValueError("allowed package removal set must be unique and sorted")

    def validate_target(self, target: ResolvedCatalogTarget) -> None:
        jetpack = str(target.record["release"]["jetpack"]["version"])
        l4t = str(target.record["release"]["l4t"]["version"])
        if (jetpack, l4t) != (self.jetpack_version, self.l4t_version):
            raise PackageResolutionError(
                f"package removal policy {self.version!r} applies only to "
                f"JetPack {self.jetpack_version} / L4T {self.l4t_version}"
            )


@dataclass(frozen=True, slots=True)
class PackageTransactionEvidence:
    packages_removed: tuple[str, ...]
    removal_policy_version: str
    allowed_removal_set: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "packages_removed": list(self.packages_removed),
            "removal_policy_version": self.removal_policy_version,
            "allowed_removal_set": list(self.allowed_removal_set),
        }


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
    removal_policy: PackageRemovalPolicy | None = None
    packages_removed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "seed": asdict(self.seed),
            "repositories": {
                "base": self.repository_base,
                "suites": list(self.repository_suites),
            },
            "packages": [asdict(package) for package in self.packages],
            "package_removal": {
                "packages_removed": list(self.packages_removed),
                "removal_policy_version": (
                    self.removal_policy.version
                    if self.removal_policy is not None
                    else _DENY_ALL_REMOVAL_POLICY_VERSION
                ),
                "allowed_removal_set": (
                    list(self.removal_policy.allowed_removal_set)
                    if self.removal_policy is not None
                    else []
                ),
            },
        }

    def digest(self) -> str:
        return json_digest(self.to_dict())


_SIMULATED_INSTALL = re.compile(
    r"^Inst\s+(?P<name>\S+?)(?::(?P<name_arch>[^\s]+))?"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?\s+"
    r"\((?P<version>\S+)(?:\s+.*?)?\s+\[(?P<architecture>[^\]]+)\]\)$"
)
_SIMULATED_REMOVE = re.compile(r"^Remv\s+(?P<name>\S+)")
_APT_TRANSACTION_SECTIONS = {
    "The following NEW packages will be installed:": "packages_to_install",
    "The following packages will be REMOVED:": "packages_to_remove",
    "The following packages will be upgraded:": "packages_to_upgrade",
    "The following packages will be DOWNGRADED:": "packages_to_downgrade",
}


@dataclass(frozen=True, slots=True)
class AptSimulationDiagnostic:
    packages_to_install: tuple[str, ...]
    packages_to_remove: tuple[str, ...]
    packages_to_upgrade: tuple[str, ...]
    packages_to_downgrade: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AptSimulationResult:
    transaction: tuple[tuple[str, str, str, str], ...]
    diagnostic: AptSimulationDiagnostic


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


def parse_apt_simulation_diagnostic(output: str) -> AptSimulationDiagnostic:
    """Extract APT's package action lists from a non-mutating simulation."""

    packages: dict[str, set[str]] = {
        field: set() for field in _APT_TRANSACTION_SECTIONS.values()
    }
    active_field: str | None = None
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        section = _APT_TRANSACTION_SECTIONS.get(stripped)
        if section is not None:
            active_field = section
            continue
        if stripped.startswith("The following "):
            active_field = None
            continue
        if active_field is not None and raw_line[:1].isspace():
            for token in stripped.split():
                name = token.rstrip("*")
                if re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9+.-]*"
                    r"(?::[A-Za-z0-9][A-Za-z0-9+.-]*)?",
                    name,
                ):
                    packages[active_field].add(name)
            continue
        if stripped:
            active_field = None

        remove_match = _SIMULATED_REMOVE.match(stripped)
        if remove_match is not None:
            packages["packages_to_remove"].add(remove_match.group("name"))

    transaction = parse_apt_simulation(output)
    classified_installs = (
        packages["packages_to_install"]
        | packages["packages_to_upgrade"]
        | packages["packages_to_downgrade"]
    )
    for name, _version, architecture, operation in transaction:
        rendered_name = f"{name}:{architecture}" if architecture != "all" else name
        if name in classified_installs or rendered_name in classified_installs:
            continue
        packages[f"packages_to_{operation}"].add(rendered_name)

    return AptSimulationDiagnostic(
        packages_to_install=tuple(sorted(packages["packages_to_install"])),
        packages_to_remove=tuple(sorted(packages["packages_to_remove"])),
        packages_to_upgrade=tuple(sorted(packages["packages_to_upgrade"])),
        packages_to_downgrade=tuple(sorted(packages["packages_to_downgrade"])),
    )


def _format_package_list(packages: Sequence[str]) -> str:
    if not packages:
        return "- (none)"
    return "\n".join(f"- {package}" for package in packages)


def _removal_diagnostic_error(report: AptSimulationDiagnostic) -> str:
    return "\n".join(
        (
            "JetPack package transaction requires removals.",
            "",
            "packages_to_install:",
            _format_package_list(report.packages_to_install),
            "",
            "packages_to_remove:",
            _format_package_list(report.packages_to_remove),
            "",
            "packages_to_upgrade:",
            _format_package_list(report.packages_to_upgrade),
            "",
            "packages_to_downgrade:",
            _format_package_list(report.packages_to_downgrade),
            "",
            "Installation was NOT performed because Orin Stage currently forbids package removals.",
        )
    )


def _unexpected_removal_error(
    report: AptSimulationDiagnostic,
    policy: PackageRemovalPolicy,
) -> str:
    unexpected = tuple(
        sorted(set(report.packages_to_remove) - set(policy.allowed_removal_set))
    )
    return "\n".join(
        (
            f"APT transaction violates package removal policy {policy.version}.",
            "",
            "Unexpected packages requested for removal:",
            _format_package_list(unexpected),
            "",
            "packages_to_remove:",
            _format_package_list(report.packages_to_remove),
            "",
            "allowed_removal_set:",
            _format_package_list(policy.allowed_removal_set),
            "",
            "Installation was NOT performed.",
        )
    )


def _validate_removal_report(
    report: AptSimulationDiagnostic,
    policy: PackageRemovalPolicy | None,
) -> None:
    if not report.packages_to_remove:
        return
    if policy is None:
        raise PackageResolutionError(_removal_diagnostic_error(report))
    if not set(report.packages_to_remove).issubset(policy.allowed_removal_set):
        raise PackageResolutionError(_unexpected_removal_error(report, policy))


def _simulation_command(
    apt_specs: Sequence[str],
    *,
    no_remove: bool,
) -> tuple[str, ...]:
    options = (
        "/usr/bin/apt-get",
        "-s",
        "-o",
        "Debug::NoLocking=true",
    )
    if no_remove:
        options += ("--no-remove",)
    return (*options, "install", *apt_specs)


def _simulate_transaction(
    chroot: Arm64ConstructionChroot,
    apt_specs: Sequence[str],
    *,
    removal_policy: PackageRemovalPolicy | None,
) -> _AptSimulationResult:
    environment = {"DEBIAN_FRONTEND": "noninteractive"}
    try:
        completed = chroot.run(
            _simulation_command(apt_specs, no_remove=True),
            env=environment,
        )
    except ChrootError as exc:
        if _REMOVAL_FORBIDDEN_ERROR not in str(exc):
            raise
        diagnostic = chroot.run(
            _simulation_command(apt_specs, no_remove=False),
            check=False,
            env=environment,
        )
        if diagnostic.returncode != 0:
            detail = diagnostic.stderr.strip() or diagnostic.stdout.strip()
            raise PackageResolutionError(
                "APT removal diagnostic simulation failed without changing the rootfs: "
                f"{detail}"
            ) from exc
        report = parse_apt_simulation_diagnostic(diagnostic.stdout)
        try:
            _validate_removal_report(report, removal_policy)
        except PackageResolutionError as policy_error:
            raise policy_error from exc
        return _AptSimulationResult(
            transaction=parse_apt_simulation(diagnostic.stdout),
            diagnostic=report,
        )

    report = parse_apt_simulation_diagnostic(completed.stdout)
    _validate_removal_report(report, removal_policy)
    return _AptSimulationResult(
        transaction=parse_apt_simulation(completed.stdout),
        diagnostic=report,
    )


def _simulate(
    chroot: Arm64ConstructionChroot,
    seed: PackageSeed,
) -> tuple[tuple[str, str, str, str], ...]:
    return _simulate_transaction(
        chroot,
        (seed.apt_spec,),
        removal_policy=None,
    ).transaction


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


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise PackageResolutionError(f"temporary APT source path already exists: {temporary}")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _active_apt_source_paths(rootfs: Path) -> tuple[Path, ...]:
    apt = Path(rootfs) / "etc" / "apt"
    paths: list[Path] = []
    main = apt / "sources.list"
    if main.is_file() or main.is_symlink():
        paths.append(main)
    source_directory = apt / "sources.list.d"
    if source_directory.is_dir():
        paths.extend(sorted(source_directory.glob("*.list")))
        paths.extend(sorted(source_directory.glob("*.sources")))
    return tuple(paths)


def _source_has_nvidia_or_placeholder(content: str) -> bool:
    return _NVIDIA_REPOSITORY in content or _UNRESOLVED_PLACEHOLDER.search(content) is not None


def _without_conflicting_source_entries(path: Path, content: str) -> str:
    if path.suffix == ".sources":
        stanzas = re.split(r"(\n[ \t]*\n)", content)
        kept: list[str] = []
        for part in stanzas:
            if not part.strip() or not _source_has_nvidia_or_placeholder(part):
                kept.append(part)
        return "".join(kept)

    kept_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        active = line.lstrip()
        if not active.startswith("#") and _source_has_nvidia_or_placeholder(line):
            continue
        if _UNRESOLVED_PLACEHOLDER.search(line):
            continue
        kept_lines.append(line)
    return "".join(kept_lines)


def _read_regular_source(path: Path) -> str:
    if path.is_symlink():
        raise PackageResolutionError(f"refusing symbolic-link APT source during construction: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PackageResolutionError(f"cannot read APT source file: {path}") from exc


def validate_final_nvidia_sources(rootfs: Path, target: ResolvedCatalogTarget) -> None:
    """Require one canonical final NVIDIA source set and no unresolved placeholder."""

    expected = tuple(render_temporary_nvidia_sources(target).splitlines())
    active_lines: list[str] = []
    for path in _active_apt_source_paths(rootfs):
        content = _read_regular_source(path)
        if _UNRESOLVED_PLACEHOLDER.search(content):
            raise PackageResolutionError(f"unresolved placeholder remains in APT source: {path}")
        if path.suffix == ".sources" and _NVIDIA_REPOSITORY in content:
            raise PackageResolutionError(
                f"unexpected deb822 NVIDIA source remains alongside canonical source: {path}"
            )
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and _NVIDIA_REPOSITORY in stripped:
                active_lines.append(stripped)

    source_directory = Path(rootfs) / "etc" / "apt" / "sources.list.d"
    if source_directory.is_dir():
        for path in source_directory.iterdir():
            if path.is_file() and not path.is_symlink():
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise PackageResolutionError(f"cannot audit final APT source file: {path}") from exc
                if _UNRESOLVED_PLACEHOLDER.search(content):
                    raise PackageResolutionError(
                        f"unresolved placeholder remains in final APT configuration: {path}"
                    )

    if tuple(active_lines) != expected:
        raise PackageResolutionError(
            "final NVIDIA APT sources are not the exact canonical common + platform set: "
            f"{active_lines!r}"
        )
    temporary = Path(rootfs) / "etc" / "apt" / "sources.list.d" / _CONSTRUCTION_NVIDIA_SOURCE
    if temporary.exists() or temporary.is_symlink():
        raise PackageResolutionError(f"construction-only APT source remains in final rootfs: {temporary}")


class NvidiaConstructionSources(AbstractContextManager[Path]):
    """Make catalog repositories authoritative for APT, then publish canonical final sources."""

    def __init__(self, rootfs: Path, target: ResolvedCatalogTarget) -> None:
        self.rootfs = Path(rootfs)
        self.target = target
        source_directory = self.rootfs / "etc" / "apt" / "sources.list.d"
        self.official_path = source_directory / _OFFICIAL_NVIDIA_SOURCE
        self.construction_path = source_directory / _CONSTRUCTION_NVIDIA_SOURCE
        self.disabled_path = source_directory / _DISABLED_NVIDIA_SOURCE
        self._entered = False

    def _canonicalize_final_sources(self) -> None:
        self.construction_path.unlink(missing_ok=True)
        self.disabled_path.unlink(missing_ok=True)
        if self.official_path.is_symlink():
            self.official_path.unlink()
        _write_text_atomic(self.official_path, render_temporary_nvidia_sources(self.target))
        validate_final_nvidia_sources(self.rootfs, self.target)

    def __enter__(self) -> Path:
        source_directory = self.official_path.parent
        source_directory.mkdir(parents=True, exist_ok=True)
        if self.construction_path.exists() or self.construction_path.is_symlink():
            raise PackageResolutionError(
                f"temporary construction source already exists: {self.construction_path}"
            )
        if self.disabled_path.exists() or self.disabled_path.is_symlink():
            raise PackageResolutionError(
                f"disabled NVIDIA source backup already exists: {self.disabled_path}"
            )

        try:
            if self.official_path.exists() or self.official_path.is_symlink():
                if self.official_path.is_symlink():
                    raise PackageResolutionError(
                        f"refusing symbolic-link NVIDIA APT source: {self.official_path}"
                    )
                os.replace(self.official_path, self.disabled_path)

            for path in _active_apt_source_paths(self.rootfs):
                if path in {self.construction_path, self.official_path}:
                    continue
                content = _read_regular_source(path)
                filtered = _without_conflicting_source_entries(path, content)
                if filtered != content:
                    _write_text_atomic(path, filtered)

            write_temporary_nvidia_sources(self.rootfs, self.target)
            self._entered = True
            return self.construction_path
        except Exception:
            self._canonicalize_final_sources()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._canonicalize_final_sources()
        self._entered = False
        return False


def use_canonical_nvidia_construction_sources(
    rootfs: Path, target: ResolvedCatalogTarget
) -> NvidiaConstructionSources:
    return NvidiaConstructionSources(rootfs, target)


def resolve_construction_package_set(
    chroot: Arm64ConstructionChroot,
    target: ResolvedCatalogTarget,
    *,
    removal_policy: PackageRemovalPolicy | None = None,
    runner=subprocess.run,
) -> ConstructionPackageSet:
    """Resolve, download and byte-lock the exact transaction for nvidia-jetpack."""

    seed = package_seed_from_target(target)
    if removal_policy is not None:
        removal_policy.validate_target(target)
    chroot.run(("/usr/bin/apt-get", "update"), env={"DEBIAN_FRONTEND": "noninteractive"})
    simulation = _simulate_transaction(
        chroot,
        (seed.apt_spec,),
        removal_policy=removal_policy,
    )
    transaction = simulation.transaction
    if not transaction:
        raise PackageResolutionError(
            "nvidia-jetpack exact seed produced an empty construction transaction"
        )

    exact_specs = [
        f"{name}={version}" if arch == "all" else f"{name}:{arch}={version}"
        for name, version, arch, _operation in transaction
    ]
    download_options = [
        "/usr/bin/apt-get",
        "--download-only",
        "--yes",
    ]
    if not simulation.diagnostic.packages_to_remove:
        download_options.append("--no-remove")
    chroot.run(
        (*download_options, "install", *exact_specs),
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
        removal_policy=removal_policy,
        packages_removed=simulation.diagnostic.packages_to_remove,
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


def _installed_package_names(chroot: Arm64ConstructionChroot) -> frozenset[str]:
    completed = chroot.run(
        (
            "/usr/bin/dpkg-query",
            "--show",
            "--showformat=${Package}\\t${Status}\\n",
        )
    )
    installed: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        name, separator, status = raw_line.partition("\t")
        if not separator or not name:
            raise PackageResolutionError(
                f"cannot parse dpkg installed-package snapshot line: {raw_line!r}"
            )
        if status == "install ok installed":
            installed.add(name)
    return frozenset(installed)


def _validate_actual_removals(
    *,
    before: frozenset[str],
    after: frozenset[str],
    expected: Sequence[str],
) -> tuple[str, ...]:
    actual = tuple(sorted(before - after))
    expected_set = set(expected)
    unexpected = tuple(sorted(set(actual) - expected_set))
    missing = tuple(sorted(expected_set - set(actual)))
    if unexpected or missing:
        raise PackageResolutionError(
            "installed package removal set differs from the validated APT simulation.\n\n"
            "unexpected_packages_removed:\n"
            f"{_format_package_list(unexpected)}\n\n"
            "expected_packages_not_removed:\n"
            f"{_format_package_list(missing)}"
        )
    return actual


def install_locked_package_set(
    chroot: Arm64ConstructionChroot,
    package_set: ConstructionPackageSet,
    *,
    runner=subprocess.run,
) -> PackageTransactionEvidence:
    """Install only after the current apt simulation exactly matches the frozen lock."""

    specs = tuple(package.apt_spec for package in package_set.packages)
    current = _simulate_transaction(
        chroot,
        specs,
        removal_policy=package_set.removal_policy,
    )
    expected = tuple(
        (package.name, package.version, package.architecture, package.operation)
        for package in package_set.packages
    )
    if current.transaction != expected:
        raise PackageResolutionError(
            "APT transaction changed after package lock was created; refusing construction"
        )
    if current.diagnostic.packages_to_remove != package_set.packages_removed:
        raise PackageResolutionError(
            "APT package removal set changed after package lock was created; "
            "refusing construction"
        )

    verify_locked_package_archives(chroot.rootfs, package_set, runner=runner)
    before = _installed_package_names(chroot)
    install_options = [
        "/usr/bin/apt-get",
        "--no-download",
        "--yes",
    ]
    if not package_set.packages_removed:
        install_options.append("--no-remove")
    chroot.run(
        (*install_options, "install", *specs),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
    after = _installed_package_names(chroot)
    actual_removed = _validate_actual_removals(
        before=before,
        after=after,
        expected=package_set.packages_removed,
    )
    missing_installed = tuple(
        sorted({package.name for package in package_set.packages} - set(after))
    )
    if missing_installed:
        raise PackageResolutionError(
            "validated replacement transaction did not leave every locked package installed.\n\n"
            "missing_locked_packages:\n"
            f"{_format_package_list(missing_installed)}"
        )
    if package_set.removal_policy is None:
        if actual_removed:
            raise PackageResolutionError(
                "packages were removed without an explicit package removal policy"
            )
        policy_version = _DENY_ALL_REMOVAL_POLICY_VERSION
        allowed_removal_set: tuple[str, ...] = ()
    else:
        policy_version = package_set.removal_policy.version
        allowed_removal_set = package_set.removal_policy.allowed_removal_set
    return PackageTransactionEvidence(
        packages_removed=actual_removed,
        removal_policy_version=policy_version,
        allowed_removal_set=allowed_removal_set,
    )


def clean_package_archives(chroot: Arm64ConstructionChroot) -> None:
    chroot.run(
        ("/usr/bin/apt-get", "clean"),
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
