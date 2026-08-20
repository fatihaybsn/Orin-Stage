from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.base.packages import (
    ConstructionPackageSet,
    LockedPackage,
    PackageResolutionError,
    PackageSeed,
    _deb_control,
    parse_apt_simulation,
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
