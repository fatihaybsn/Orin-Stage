from __future__ import annotations

import copy
from typing import Mapping

from ._json import json_digest


CONSTRUCTION_RECIPE_ID = "jp6-official-base-v1"
CONSTRUCTION_RECIPE_VERSION = 1
HOST_BUILDER_IMAGE = "docker.io/library/ubuntu:jammy-20260627"
JP623_REMOVAL_POLICY_VERSION = "jp6.2.3-opencv-replacement-v1"
JP623_ALLOWED_REMOVAL_SET = (
    "libopencv-core-dev",
    "libopencv-dnn-dev",
    "libopencv-flann-dev",
    "libopencv-imgcodecs-dev",
    "libopencv-imgproc-dev",
    "libopencv-ml-dev",
    "libopencv-photo-dev",
    "libopencv-shape-dev",
    "libopencv-video-dev",
    "libopencv-viz-dev",
)

# This descriptor is the semantic construction contract. Changing a step or a
# construction-affecting policy requires a new descriptor and therefore a new
# recipe digest. Host paths, timestamps, SDK Manager details and validation-only
# policy are intentionally absent.
_CONSTRUCTION_RECIPE_V1: dict[str, object] = {
    "schema_version": 1,
    "recipe_id": CONSTRUCTION_RECIPE_ID,
    "recipe_version": CONSTRUCTION_RECIPE_VERSION,
    "artifact_flow": [
        "extract_bsp",
        "extract_sample_rootfs",
        "run_l4t_flash_prerequisites",
        "run_apply_binaries",
    ],
    "extraction": {
        "bsp": "tar-xf",
        "sample_rootfs": "tar-xpf-numeric-owner-acls-xattrs",
    },
    "official_scripts": [
        "tools/l4t_flash_prerequisites.sh",
        "apply_binaries.sh",
    ],
    "host_script_execution": {
        "executor": "rootful-podman-disposable-builder",
        "image": HOST_BUILDER_IMAGE,
        "writable_host_bind": "Linux_for_Tegra-staging-only",
        "bootstrap_packages": ["sudo"],
    },
    "package_configuration": {
        "architecture": "arm64",
        "execution": "qemu-aarch64-static-binfmt-chroot",
        "guest_qemu_path": "/usr/bin/qemu-aarch64-static",
        "pseudo_filesystems": ["proc", "sysfs", "dev", "dev/pts"],
        "resolution": "apt-simulate-then-freeze-exact-transaction",
        "installation": "apt-exact-versions-from-verified-local-archives",
        "service_start_policy": "policy-rc.d-exit-101",
        "upgrade_policy": "no-upgrade-or-dist-upgrade",
        "removal_policy": {
            "version": JP623_REMOVAL_POLICY_VERSION,
            "scope": {
                "jetpack_version": "6.2.3",
                "l4t_version": "36.5.2",
            },
            "decision": "allow-subset-of-exact-set",
            "allowed_removal_set": list(JP623_ALLOWED_REMOVAL_SET),
            "pre_install_gate": "apt-simulation-exact-package-set",
            "post_install_audit": "dpkg-installed-set-exact-difference",
        },
        "nvidia_repository_sources": {
            "authority": "catalog-exact-common-plus-platform",
            "construction_file": "orin-stage-construction.list",
            "vendor_source_policy": "disable-during-construction-canonicalize-for-final-base",
            "unresolved_placeholder_policy": "reject",
            "duplicate_policy": "reject",
        },
    },
    "cleanup": [
        "apt-clean",
        "remove-construction-qemu",
        "remove-policy-rc.d",
        "restore-resolv-conf",
        "unmount-construction-pseudofs",
        "remove-construction-apt-source",
        "write-canonical-final-nvidia-apt-source",
    ],
}


def construction_recipe_v1() -> Mapping[str, object]:
    return copy.deepcopy(_CONSTRUCTION_RECIPE_V1)


def construction_recipe_digest_v1() -> str:
    return json_digest(_CONSTRUCTION_RECIPE_V1)
