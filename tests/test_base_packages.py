from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.base.chroot import ChrootError
from orin_stage.base.packages import (
    ConstructionPackageSet,
    LockedPackage,
    PackageResolutionError,
    PackageSeed,
    _simulate,
    _deb_control,
    parse_apt_simulation,
    parse_apt_simulation_diagnostic,
    render_temporary_nvidia_sources,
)
from orin_stage.catalog.resolver import TargetResolver


REPO_ROOT = Path(__file__).resolve().parents[1]


def _target():
    resolver = TargetResolver(
        REPO_ROOT / "catalog" / "targets",
        REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    return resolver.resolve("jetson-orin@jp6.2.3")


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


def test_jp623_temporary_sources_are_common_and_t234_r365() -> None:
    rendered = render_temporary_nvidia_sources(_target())

    assert "https://repo.download.nvidia.com/jetson/common r36.5 main" in rendered
    assert "https://repo.download.nvidia.com/jetson/t234 r36.5 main" in rendered


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
