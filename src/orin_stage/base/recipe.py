from __future__ import annotations

import copy
from typing import Mapping

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import json_digest
from .packages import PackageRemovalPolicy


CONSTRUCTION_RECIPE_ID = "jp6-official-base-v1"
CONSTRUCTION_RECIPE_VERSION = 1
HOST_BUILDER_IMAGE = "docker.io/library/ubuntu:jammy-20260627"
JP60_CANONICAL_ID = "nvidia.jetpack-6.0.jetson-linux-36.3"
JP60_REMOVAL_POLICY_VERSION = "jp6.0-opencv-replacement-v1"
JP60_ALLOWED_REMOVAL_SET = (
    "libopencv-calib3d-dev",
    "libopencv-contrib-dev",
    "libopencv-core-dev",
    "libopencv-dnn-dev",
    "libopencv-features2d-dev",
    "libopencv-flann-dev",
    "libopencv-highgui-dev",
    "libopencv-imgcodecs-dev",
    "libopencv-imgproc-dev",
    "libopencv-ml-dev",
    "libopencv-objdetect-dev",
    "libopencv-photo-dev",
    "libopencv-shape-dev",
    "libopencv-stitching-dev",
    "libopencv-superres-dev",
    "libopencv-video-dev",
    "libopencv-videoio-dev",
    "libopencv-videostab-dev",
    "libopencv-viz-dev",
)
JP623_CANONICAL_ID = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
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


def package_removal_policy_for_target(
    target: ResolvedCatalogTarget,
) -> PackageRemovalPolicy | None:
    """Return only exact-release removal exceptions proven for this target.

    All other JP6 releases retain the family default: package removal is denied.
    Each exception remains deliberately narrow and auditable rather than
    becoming an implicit JP6-wide workaround.
    """

    policies = {
        (JP60_CANONICAL_ID, "6.0", "36.3"): (
            JP60_REMOVAL_POLICY_VERSION,
            JP60_ALLOWED_REMOVAL_SET,
        ),
        (JP623_CANONICAL_ID, "6.2.3", "36.5.2"): (
            JP623_REMOVAL_POLICY_VERSION,
            JP623_ALLOWED_REMOVAL_SET,
        ),
    }
    selected = policies.get(
        (target.canonical_id, target.jetpack_version, target.l4t_version)
    )
    if selected is None:
        return None
    version, allowed_removal_set = selected
    return PackageRemovalPolicy(
        version=version,
        jetpack_version=target.jetpack_version,
        l4t_version=target.l4t_version,
        allowed_removal_set=allowed_removal_set,
    )


def construction_recipe_for_target(
    target: ResolvedCatalogTarget,
) -> Mapping[str, object]:
    """Build the JP6 family recipe with the target's narrow release policy."""

    recipe = copy.deepcopy(_CONSTRUCTION_RECIPE_V1)
    package_configuration = recipe["package_configuration"]
    assert isinstance(package_configuration, dict)
    policy = package_removal_policy_for_target(target)
    if policy is None:
        package_configuration["removal_policy"] = {
            "version": "deny-all-v1",
            "scope": {"jetpack_family": "6.x"},
            "decision": "reject-any-removal",
            "allowed_removal_set": [],
            "pre_install_gate": "apt-simulation-exact-package-set",
            "post_install_audit": "dpkg-installed-set-exact-difference",
        }
    else:
        package_configuration["removal_policy"] = {
            "version": policy.version,
            "scope": {
                "jetpack_version": policy.jetpack_version,
                "l4t_version": policy.l4t_version,
            },
            "decision": "allow-subset-of-exact-set",
            "allowed_removal_set": list(policy.allowed_removal_set),
            "pre_install_gate": "apt-simulation-exact-package-set",
            "post_install_audit": "dpkg-installed-set-exact-difference",
        }
    if target.canonical_id == JP60_CANONICAL_ID:
        package_configuration["offline_l4t_preinstall"] = {
            "scope": {
                "jetpack_version": "6.0",
                "l4t_version": "36.3",
            },
            "vendor_marker": (
                "/opt/nvidia/l4t-packages/"
                ".nv-l4t-disable-boot-fw-update-in-preinstall"
            ),
            "lifetime": "construction-chroot-only",
        }
    return recipe


def construction_recipe_digest_for_target(target: ResolvedCatalogTarget) -> str:
    return json_digest(construction_recipe_for_target(target))
