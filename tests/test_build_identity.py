from orin_stage.build_identity import (
    BUILD_IDENTITY_SCHEMA_VERSION,
    JP6_BOOTLIN_BINUTILS_VERSION,
    JP6_BOOTLIN_GCC_VERSION,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
    JP6_BOOTLIN_TOOLCHAIN_PREFIX,
    JP6_BUILD_IDENTITY,
    JP6_BUILD_IMAGE,
    BuildIdentity,
    BuildPackageIdentity,
)


def test_jp6_build_identity_records_proven_step6_inputs() -> None:
    assert JP6_BUILD_IDENTITY.image == JP6_BUILD_IMAGE
    assert JP6_BUILD_IDENTITY.toolchain_archive_sha256 == (
        JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256
    )
    assert JP6_BUILD_IDENTITY.gcc_version == JP6_BOOTLIN_GCC_VERSION == "11.3.0"
    assert JP6_BUILD_IDENTITY.binutils_version == JP6_BOOTLIN_BINUTILS_VERSION == "2.38"
    assert JP6_BUILD_IDENTITY.nvidia_cross_packages == ()
    assert JP6_BOOTLIN_TOOLCHAIN_PREFIX == "aarch64-buildroot-linux-gnu-"
    assert JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME == (
        "aarch64--glibc--stable-2022.08-1.tar.bz2"
    )
    assert JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL == (
        "https://developer.nvidia.com/downloads/embedded/l4t/r36_release_v3.0/"
        "toolchain/aarch64--glibc--stable-2022.08-1.tar.bz2"
    )


def test_build_identity_digest_is_deterministic_and_order_independent() -> None:
    first = BuildIdentity(
        image="example@sha256:" + "1" * 64,
        toolchain_archive_sha256="2" * 64,
        gcc_version="11.3.0",
        binutils_version="2.38",
        nvidia_cross_packages=(
            BuildPackageIdentity("z", "2", "all"),
            BuildPackageIdentity("a", "1", "amd64"),
        ),
    )
    second = BuildIdentity(
        image=first.image,
        toolchain_archive_sha256=first.toolchain_archive_sha256,
        gcc_version=first.gcc_version,
        binutils_version=first.binutils_version,
        nvidia_cross_packages=tuple(reversed(first.nvidia_cross_packages)),
    )

    assert first.to_dict() == second.to_dict()
    assert first.digest() == second.digest()
    assert len(first.digest()) == 64


def test_build_identity_digest_changes_with_cross_package_version() -> None:
    base = BuildIdentity(
        image="example@sha256:" + "1" * 64,
        toolchain_archive_sha256="2" * 64,
        gcc_version="11.3.0",
        binutils_version="2.38",
        nvidia_cross_packages=(BuildPackageIdentity("cuda", "1", "all"),),
    )
    changed = BuildIdentity(
        image=base.image,
        toolchain_archive_sha256=base.toolchain_archive_sha256,
        gcc_version=base.gcc_version,
        binutils_version=base.binutils_version,
        nvidia_cross_packages=(BuildPackageIdentity("cuda", "2", "all"),),
    )

    assert base.digest() != changed.digest()


def test_build_identity_schema_version_is_explicit() -> None:
    assert JP6_BUILD_IDENTITY.schema_version == BUILD_IDENTITY_SCHEMA_VERSION == 1
