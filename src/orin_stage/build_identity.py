from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


BUILD_IDENTITY_SCHEMA_VERSION = 1

JP6_BUILD_IMAGE = (
    "docker.io/library/ubuntu@"
    "sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc"
)
JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256 = (
    "8af54f268c462b2d0737df8789b5e35db03a2d1ecbec90e20948f66f9244fcdd"
)
JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL = (
    "https://developer.nvidia.com/downloads/embedded/l4t/r36_release_v3.0/"
    "toolchain/aarch64--glibc--stable-2022.08-1.tar.bz2"
)
JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME = (
    "aarch64--glibc--stable-2022.08-1.tar.bz2"
)
JP6_BOOTLIN_GCC_VERSION = "11.3.0"
JP6_BOOTLIN_BINUTILS_VERSION = "2.38"
JP6_BOOTLIN_TOOLCHAIN_PREFIX = "aarch64-buildroot-linux-gnu-"


@dataclass(frozen=True, slots=True, order=True)
class BuildPackageIdentity:
    """One exact package installed inside the host-native build capsule."""

    name: str
    version: str
    architecture: str


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    """Exact identity of host-native inputs consumed by one build capsule."""

    image: str
    toolchain_archive_sha256: str
    gcc_version: str
    binutils_version: str
    nvidia_cross_packages: tuple[BuildPackageIdentity, ...] = ()
    schema_version: int = BUILD_IDENTITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        packages = sorted(self.nvidia_cross_packages)
        return {
            "schema_version": self.schema_version,
            "image": self.image,
            "toolchain": {
                "archive_sha256": self.toolchain_archive_sha256,
                "gcc_version": self.gcc_version,
                "binutils_version": self.binutils_version,
            },
            "nvidia_cross_packages": [asdict(package) for package in packages],
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


JP6_BUILD_IDENTITY = BuildIdentity(
    image=JP6_BUILD_IMAGE,
    toolchain_archive_sha256=JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256,
    gcc_version=JP6_BOOTLIN_GCC_VERSION,
    binutils_version=JP6_BOOTLIN_BINUTILS_VERSION,
)
