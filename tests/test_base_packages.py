from __future__ import annotations

import copy
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from orin_stage.base import packages as packages_module
from orin_stage.base.chroot import ChrootError
from orin_stage.base.packages import (
    ConstructionPackageSet,
    LockedPackage,
    PackageResolutionError,
    PackageRemovalPolicy,
    PackageSeed,
    _simulate,
    _simulate_transaction,
    _deb_control,
    install_locked_package_set,
    parse_apt_simulation,
    parse_apt_simulation_diagnostic,
    render_temporary_nvidia_sources,
    use_canonical_nvidia_construction_sources,
    validate_final_nvidia_sources,
)
from orin_stage.base.recipe import (
    JP623_ALLOWED_REMOVAL_SET,
    JP623_REMOVAL_POLICY_VERSION,
)
from orin_stage.catalog.resolver import TargetResolver


REPO_ROOT = Path(__file__).resolve().parents[1]


def _target():
    resolver = TargetResolver(
        REPO_ROOT / "catalog" / "targets",
        REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    return resolver.resolve("jetson-orin@jp6.2.3")


def _removal_policy(
    allowed: tuple[str, ...] = JP623_ALLOWED_REMOVAL_SET,
) -> PackageRemovalPolicy:
    return PackageRemovalPolicy(
        version=JP623_REMOVAL_POLICY_VERSION,
        jetpack_version="6.2.3",
        l4t_version="36.5.2",
        allowed_removal_set=allowed,
    )


def _simulation_with_removals(packages: tuple[str, ...]) -> str:
    rendered = " ".join(packages)
    removal_rows = "\n".join(f"Remv {package} [1.0]" for package in packages)
    return (
        "The following NEW packages will be installed:\n"
        "  nvidia-opencv\n"
        "The following packages will be REMOVED:\n"
        f"  {rendered}\n"
        "Inst nvidia-opencv (1.0 NVIDIA:repo [arm64])\n"
        f"{removal_rows}\n"
    )


def test_parse_apt_simulation_freezes_install_and_upgrade_operations() -> None:
    output = """
Reading package lists... Done
Inst cuda-cudart-12-6 (12.6.77-1 NVIDIA:repo [arm64])
Inst nvidia-l4t-core [36.5.1-20260101000000] (36.5.2-20260716114719 NVIDIA:repo [arm64])
Conf cuda-cudart-12-6 (12.6.77-1 NVIDIA:repo [arm64])
"""

    assert parse_apt_simulation(output) == (
        ("cuda-cudart-12-6", "12.6.77-1", "arm64", "install"),
        (
            "nvidia-l4t-core",
            "36.5.2-20260716114719",
            "arm64",
            "upgrade",
        ),
    )


def test_parse_apt_simulation_rejects_unknown_inst_format() -> None:
    with pytest.raises(PackageResolutionError, match="cannot parse"):
        parse_apt_simulation("Inst impossible-format")


def test_parse_apt_diagnostic_classifies_every_transaction_operation() -> None:
    output = """
The following NEW packages will be installed:
  cuda-new
The following packages will be REMOVED:
  nvidia-obsolete old-lib:arm64
The following packages will be upgraded:
  nvidia-upgrade
The following packages will be DOWNGRADED:
  nvidia-downgrade
Inst cuda-new (1.0 NVIDIA:repo [arm64])
Inst nvidia-upgrade [1.0] (2.0 NVIDIA:repo [arm64])
Inst nvidia-downgrade [2.0] (1.0 NVIDIA:repo [arm64])
Remv nvidia-obsolete [1.0]
Remv old-lib:arm64 [1.0]
"""

    diagnostic = parse_apt_simulation_diagnostic(output)

    assert diagnostic.packages_to_install == ("cuda-new",)
    assert diagnostic.packages_to_remove == ("nvidia-obsolete", "old-lib:arm64")
    assert diagnostic.packages_to_upgrade == ("nvidia-upgrade",)
    assert diagnostic.packages_to_downgrade == ("nvidia-downgrade",)


def test_no_remove_failure_runs_read_only_diagnostic_and_reports_exact_packages() -> None:
    diagnostic_output = """
The following NEW packages will be installed:
  cuda-new
The following packages will be REMOVED:
  package-a package-b:arm64
The following packages will be upgraded:
  nvidia-upgrade
The following packages will be DOWNGRADED:
  nvidia-downgrade
Inst cuda-new (1.0 NVIDIA:repo [arm64])
Inst nvidia-upgrade [1.0] (2.0 NVIDIA:repo [arm64])
Inst nvidia-downgrade [2.0] (1.0 NVIDIA:repo [arm64])
Remv package-a [1.0]
Remv package-b:arm64 [1.0]
"""

    class Chroot:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], bool]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append((rendered, check))
            if "--no-remove" in rendered:
                raise ChrootError(
                    "ARM64 chroot command failed (100): apt-get\n"
                    "E: Packages need to be removed but remove is disabled."
                )
            return subprocess.CompletedProcess(rendered, 0, diagnostic_output, "")

    chroot = Chroot()
    seed = PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64")

    with pytest.raises(PackageResolutionError) as raised:
        _simulate(chroot, seed)  # type: ignore[arg-type]

    message = str(raised.value)
    assert "packages_to_install:\n- cuda-new" in message
    assert "packages_to_remove:\n- package-a\n- package-b:arm64" in message
    assert "packages_to_upgrade:\n- nvidia-upgrade" in message
    assert "packages_to_downgrade:\n- nvidia-downgrade" in message
    assert "Installation was NOT performed" in message
    assert len(chroot.calls) == 2
    normal, diagnostic = chroot.calls
    assert "--no-remove" in normal[0]
    assert "--no-remove" not in diagnostic[0]
    assert diagnostic[1] is False
    assert all("-s" in command for command, _check in chroot.calls)
    assert all("Debug::NoLocking=true" in command for command, _check in chroot.calls)


def test_successful_no_remove_simulation_behavior_is_unchanged() -> None:
    output = "Inst cuda-cudart-12-6 (12.6.77-1 NVIDIA:repo [arm64])\n"

    class Chroot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            return subprocess.CompletedProcess(rendered, 0, output, "")

    chroot = Chroot()
    transaction = _simulate(  # type: ignore[arg-type]
        chroot,
        PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
    )

    assert transaction == (("cuda-cudart-12-6", "12.6.77-1", "arm64", "install"),)
    assert len(chroot.calls) == 1
    assert "--no-remove" in chroot.calls[0]


def test_jp623_policy_keeps_normal_no_removal_path() -> None:
    output = "Inst nvidia-opencv (1.0 NVIDIA:repo [arm64])\n"

    class Chroot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            return subprocess.CompletedProcess(rendered, 0, output, "")

    chroot = Chroot()
    result = _simulate_transaction(  # type: ignore[arg-type]
        chroot,
        ("nvidia-jetpack:arm64=6.2.3+b81",),
        removal_policy=_removal_policy(),
    )

    assert result.diagnostic.packages_to_remove == ()
    assert result.transaction == (("nvidia-opencv", "1.0", "arm64", "install"),)
    assert len(chroot.calls) == 1
    assert "--no-remove" in chroot.calls[0]


def test_jp623_removal_policy_rejects_other_releases() -> None:
    target = _target()
    record = copy.deepcopy(target.record)
    record["release"]["jetpack"]["version"] = "6.3.0"

    with pytest.raises(PackageResolutionError, match="applies only"):
        _removal_policy().validate_target(replace(target, record=record))


@pytest.mark.parametrize(
    "packages_to_remove",
    [
        JP623_ALLOWED_REMOVAL_SET,
        ("libopencv-core-dev", "libopencv-viz-dev"),
    ],
    ids=("exact-allowlist", "allowlist-subset"),
)
def test_jp623_policy_accepts_exact_allowlist_and_subsets(
    packages_to_remove: tuple[str, ...],
) -> None:
    output = _simulation_with_removals(packages_to_remove)

    class Chroot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            if "--no-remove" in rendered:
                raise ChrootError(
                    "E: Packages need to be removed but remove is disabled."
                )
            return subprocess.CompletedProcess(rendered, 0, output, "")

    chroot = Chroot()
    result = _simulate_transaction(  # type: ignore[arg-type]
        chroot,
        ("nvidia-jetpack:arm64=6.2.3+b81",),
        removal_policy=_removal_policy(),
    )

    assert result.diagnostic.packages_to_remove == tuple(sorted(packages_to_remove))
    assert len(chroot.calls) == 2
    assert all("-s" in command for command in chroot.calls)
    assert "--no-remove" in chroot.calls[0]
    assert "--no-remove" not in chroot.calls[1]


@pytest.mark.parametrize(
    "packages_to_remove, unexpected",
    [
        (("libc6",), "libc6"),
        (("libopencv-core-dev", "systemd"), "systemd"),
    ],
    ids=("single-unexpected", "allowlist-plus-critical"),
)
def test_jp623_policy_rejects_any_package_outside_exact_allowlist(
    packages_to_remove: tuple[str, ...],
    unexpected: str,
) -> None:
    output = _simulation_with_removals(packages_to_remove)

    class Chroot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            if "--no-remove" in rendered:
                raise ChrootError(
                    "E: Packages need to be removed but remove is disabled."
                )
            return subprocess.CompletedProcess(rendered, 0, output, "")

    chroot = Chroot()
    with pytest.raises(PackageResolutionError, match=unexpected):
        _simulate_transaction(  # type: ignore[arg-type]
            chroot,
            ("nvidia-jetpack:arm64=6.2.3+b81",),
            removal_policy=_removal_policy(),
        )

    assert len(chroot.calls) == 2
    assert all("-s" in command for command in chroot.calls)


def test_real_transaction_runs_only_after_removal_validation(monkeypatch, tmp_path: Path) -> None:
    output = _simulation_with_removals(("systemd",))

    class Chroot:
        rootfs = tmp_path

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            if "--no-remove" in rendered:
                raise ChrootError(
                    "E: Packages need to be removed but remove is disabled."
                )
            return subprocess.CompletedProcess(rendered, 0, output, "")

    package_set = ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-opencv", "1.0", "arm64", "install", "opencv.deb", "a" * 64
            ),
        ),
        removal_policy=_removal_policy(),
        packages_removed=("libopencv-core-dev",),
    )
    archive_validation_called = False

    def verify(*args, **kwargs):
        nonlocal archive_validation_called
        archive_validation_called = True

    monkeypatch.setattr(packages_module, "verify_locked_package_archives", verify)
    chroot = Chroot()

    with pytest.raises(PackageResolutionError, match="systemd"):
        install_locked_package_set(chroot, package_set)  # type: ignore[arg-type]

    assert not archive_validation_called
    assert all("-s" in command for command in chroot.calls)


def test_validated_removals_are_applied_and_post_verified(monkeypatch, tmp_path: Path) -> None:
    removed = "libopencv-core-dev"
    output = _simulation_with_removals((removed,))

    class Chroot:
        rootfs = tmp_path

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.snapshots = iter(
                (
                    f"base-files\tinstall ok installed\n{removed}\tinstall ok installed\n",
                    "base-files\tinstall ok installed\n"
                    "nvidia-opencv\tinstall ok installed\n",
                )
            )

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            if "-s" in rendered and "--no-remove" in rendered:
                raise ChrootError(
                    "E: Packages need to be removed but remove is disabled."
                )
            if "-s" in rendered:
                return subprocess.CompletedProcess(rendered, 0, output, "")
            if rendered[0] == "/usr/bin/dpkg-query":
                return subprocess.CompletedProcess(rendered, 0, next(self.snapshots), "")
            return subprocess.CompletedProcess(rendered, 0, "", "")

    monkeypatch.setattr(
        packages_module,
        "verify_locked_package_archives",
        lambda *args, **kwargs: None,
    )
    package_set = ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-opencv", "1.0", "arm64", "install", "opencv.deb", "a" * 64
            ),
        ),
        removal_policy=_removal_policy(),
        packages_removed=(removed,),
    )
    chroot = Chroot()

    evidence = install_locked_package_set(chroot, package_set)  # type: ignore[arg-type]

    assert evidence.packages_removed == (removed,)
    assert evidence.removal_policy_version == JP623_REMOVAL_POLICY_VERSION
    assert evidence.allowed_removal_set == JP623_ALLOWED_REMOVAL_SET
    real_installs = [
        command
        for command in chroot.calls
        if command[0] == "/usr/bin/apt-get" and "-s" not in command
    ]
    assert len(real_installs) == 1
    assert "--no-remove" not in real_installs[0]


def test_real_transaction_without_removals_keeps_no_remove_guard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = "Inst nvidia-opencv (1.0 NVIDIA:repo [arm64])\n"

    class Chroot:
        rootfs = tmp_path

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.snapshots = iter(
                (
                    "base-files\tinstall ok installed\n",
                    "base-files\tinstall ok installed\n"
                    "nvidia-opencv\tinstall ok installed\n",
                )
            )

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            self.calls.append(rendered)
            if "-s" in rendered:
                return subprocess.CompletedProcess(rendered, 0, output, "")
            if rendered[0] == "/usr/bin/dpkg-query":
                return subprocess.CompletedProcess(rendered, 0, next(self.snapshots), "")
            return subprocess.CompletedProcess(rendered, 0, "", "")

    monkeypatch.setattr(
        packages_module,
        "verify_locked_package_archives",
        lambda *args, **kwargs: None,
    )
    package_set = ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-opencv", "1.0", "arm64", "install", "opencv.deb", "a" * 64
            ),
        ),
        removal_policy=_removal_policy(),
    )
    chroot = Chroot()

    evidence = install_locked_package_set(chroot, package_set)  # type: ignore[arg-type]

    assert evidence.packages_removed == ()
    real_install = next(
        command
        for command in chroot.calls
        if command[0] == "/usr/bin/apt-get" and "-s" not in command
    )
    assert "--no-remove" in real_install


def test_post_install_unexpected_removal_fails_validation(monkeypatch, tmp_path: Path) -> None:
    removed = "libopencv-core-dev"
    output = _simulation_with_removals((removed,))

    class Chroot:
        rootfs = tmp_path

        def __init__(self) -> None:
            self.snapshots = iter(
                (
                    f"{removed}\tinstall ok installed\nsystemd\tinstall ok installed\n",
                    "",
                )
            )

        def run(self, command, *, check=True, env=None):
            rendered = tuple(command)
            if "-s" in rendered and "--no-remove" in rendered:
                raise ChrootError(
                    "E: Packages need to be removed but remove is disabled."
                )
            if "-s" in rendered:
                return subprocess.CompletedProcess(rendered, 0, output, "")
            if rendered[0] == "/usr/bin/dpkg-query":
                return subprocess.CompletedProcess(rendered, 0, next(self.snapshots), "")
            return subprocess.CompletedProcess(rendered, 0, "", "")

    monkeypatch.setattr(
        packages_module,
        "verify_locked_package_archives",
        lambda *args, **kwargs: None,
    )
    package_set = ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-opencv", "1.0", "arm64", "install", "opencv.deb", "a" * 64
            ),
        ),
        removal_policy=_removal_policy(),
        packages_removed=(removed,),
    )

    with pytest.raises(PackageResolutionError, match="systemd"):
        install_locked_package_set(Chroot(), package_set)  # type: ignore[arg-type]


def test_jp623_temporary_sources_are_common_and_t234_r365() -> None:
    rendered = render_temporary_nvidia_sources(_target())

    assert "https://repo.download.nvidia.com/jetson/common r36.5 main" in rendered
    assert "https://repo.download.nvidia.com/jetson/t234 r36.5 main" in rendered


def test_vendor_placeholder_and_duplicate_sources_are_disabled_during_construction(
    tmp_path: Path,
) -> None:
    rootfs = tmp_path / "rootfs"
    sources = rootfs / "etc" / "apt" / "sources.list.d"
    sources.mkdir(parents=True)
    official = sources / "nvidia-l4t-apt-source.list"
    official.write_text(
        "deb https://repo.download.nvidia.com/jetson/<SOC> r36.5 main\n"
        "deb https://repo.download.nvidia.com/jetson/common r36.5 main\n",
        encoding="utf-8",
    )
    duplicate = sources / "third-party.list"
    duplicate.write_text(
        "deb https://repo.download.nvidia.com/jetson/common r36.5 main\n"
        "deb https://example.invalid stable main\n",
        encoding="utf-8",
    )

    with use_canonical_nvidia_construction_sources(rootfs, _target()) as temporary:
        assert not official.exists()
        active = "".join(
            path.read_text(encoding="utf-8") for path in sorted(sources.glob("*.list"))
        )
        assert "<SOC>" not in active
        assert active.count("/jetson/common r36.5 main") == 1
        assert active.count("/jetson/t234 r36.5 main") == 1
        assert temporary.name == "orin-stage-construction.list"

    assert not (sources / "orin-stage-construction.list").exists()
    assert "<SOC>" not in "".join(
        path.read_text(encoding="utf-8") for path in sources.iterdir() if path.is_file()
    )
    assert "example.invalid" in duplicate.read_text(encoding="utf-8")
    validate_final_nvidia_sources(rootfs, _target())


def test_construction_source_cleanup_is_exception_safe(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    sources = rootfs / "etc" / "apt" / "sources.list.d"
    sources.mkdir(parents=True)
    (sources / "nvidia-l4t-apt-source.list").write_text(
        "deb https://repo.download.nvidia.com/jetson/<SOC> r36.5 main\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="package resolution failed"):
        with use_canonical_nvidia_construction_sources(rootfs, _target()):
            raise RuntimeError("package resolution failed")

    assert not (sources / "orin-stage-construction.list").exists()
    final = (sources / "nvidia-l4t-apt-source.list").read_text(encoding="utf-8")
    assert "<SOC>" not in final
    assert final.count("/jetson/common r36.5 main") == 1
    assert final.count("/jetson/t234 r36.5 main") == 1


def test_package_set_digest_is_order_sensitive_only_to_semantic_rows() -> None:
    seed = PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64")
    first = ConstructionPackageSet(
        seed=seed,
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage("a", "1", "arm64", "install", "a.deb", "a" * 64),
            LockedPackage("b", "2", "arm64", "install", "b.deb", "b" * 64),
        ),
    )
    second = ConstructionPackageSet(
        seed=seed,
        repository_base=first.repository_base,
        repository_suites=first.repository_suites,
        packages=first.packages,
    )

    assert first.digest() == second.digest()


def test_deb_control_parses_dpkg_deb_labeled_field_output(tmp_path: Path) -> None:
    archive = tmp_path / "package.deb"
    archive.write_bytes(b"placeholder")

    class Completed:
        returncode = 0
        stdout = "Package: example\nVersion: 1.2.3-1\nArchitecture: arm64\n"
        stderr = ""

    def runner(*args, **kwargs):
        return Completed()

    assert _deb_control(archive, runner=runner) == ("example", "1.2.3-1", "arm64")
