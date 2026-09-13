from __future__ import annotations

import copy
from typing import Mapping

from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import json_digest
from .packages import PackageRemovalPolicy, PackageSeed


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
JP61_CANONICAL_ID = "nvidia.jetpack-6.1.jetson-linux-36.4"
JP61_SEED_PROFILE_VERSION = "jp6.1-exact-nvidia-meta-closure-v1"
JP61_EXACT_META_SEED_NAMES = (
    "nvidia-jetpack",
    "nvidia-jetpack-runtime",
    "nvidia-jetpack-dev",
    "nvidia-container",
    "nvidia-cuda",
    "nvidia-cuda-dev",
    "nvidia-cudnn9",
    "nvidia-cudnn9-dev",
    "nvidia-cupva",
    "nvidia-nsight-graphics",
    "nvidia-nsight-systems",
    "nvidia-opencv",
    "nvidia-opencv-dev",
    "nvidia-tensorrt",
    "nvidia-tensorrt-dev",
    "nvidia-vpi",
    "nvidia-vpi-dev",
)
JP61_ADDITIONAL_EXACT_SEEDS = (
    ("nvidia-container-toolkit-base", "1.14.2-1", "arm64"),
    ("libnvidia-container-tools", "1.14.2-1", "arm64"),
    ("nvidia-container-toolkit", "1.14.2-1", "arm64"),
    ("libnvidia-container1", "1.14.2-1", "arm64"),
    ("pva-allow-2", "2.0.0~rc3", "all"),
)
JP61_REMOVAL_POLICY_VERSION = "jp6.1-opencv-replacement-v1"
JP61_ALLOWED_REMOVAL_SET = JP60_ALLOWED_REMOVAL_SET
JP62_CANONICAL_ID = "nvidia.jetpack-6.2.jetson-linux-36.4.3"
JP62_SEED_PROFILE_VERSION = "jp6.2-exact-nvidia-meta-closure-v1"
JP62_EXACT_META_SEED_NAMES = (
    "nvidia-jetpack",
    "nvidia-jetpack-runtime",
    "nvidia-jetpack-dev",
    "nvidia-container",
    "nvidia-cuda",
    "nvidia-cuda-dev",
    "nvidia-cudnn",
    "nvidia-cudnn-dev",
    "nvidia-cupva",
    "nvidia-nsight-graphics",
    "nvidia-nsight-systems",
    "nvidia-opencv",
    "nvidia-opencv-dev",
    "nvidia-tensorrt",
    "nvidia-tensorrt-dev",
    "nvidia-vpi",
    "nvidia-vpi-dev",
)
JP62_REMOVAL_POLICY_VERSION = "jp6.2-opencv-replacement-v1"
JP62_ALLOWED_REMOVAL_SET = JP60_ALLOWED_REMOVAL_SET
JP621_CANONICAL_ID = "nvidia.jetpack-6.2.1.jetson-linux-36.4.4"
JP621_SEED_PROFILE_VERSION = "jp6.2.1-exact-nvidia-meta-closure-v1"
JP621_EXACT_META_SEED_NAMES = JP62_EXACT_META_SEED_NAMES
JP621_REMOVAL_POLICY_VERSION = "jp6.2.1-opencv-replacement-v1"
JP621_ALLOWED_REMOVAL_SET = JP60_ALLOWED_REMOVAL_SET
JP622_CANONICAL_ID = "nvidia.jetpack-6.2.2.jetson-linux-36.5.0"
JP622_SEED_PROFILE_VERSION = "jp6.2.2-exact-nvidia-meta-closure-v1"
JP622_EXACT_META_SEED_NAMES = JP62_EXACT_META_SEED_NAMES
JP622_REMOVAL_POLICY_VERSION = "jp6.2.2-opencv-replacement-v1"
JP622_ALLOWED_REMOVAL_SET = (
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
        (JP61_CANONICAL_ID, "6.1", "36.4"): (
            JP61_REMOVAL_POLICY_VERSION,
            JP61_ALLOWED_REMOVAL_SET,
        ),
        (JP62_CANONICAL_ID, "6.2", "36.4.3"): (
            JP62_REMOVAL_POLICY_VERSION,
            JP62_ALLOWED_REMOVAL_SET,
        ),
        (JP621_CANONICAL_ID, "6.2.1", "36.4.4"): (
            JP621_REMOVAL_POLICY_VERSION,
            JP621_ALLOWED_REMOVAL_SET,
        ),
        (JP622_CANONICAL_ID, "6.2.2", "36.5.0"): (
            JP622_REMOVAL_POLICY_VERSION,
            JP622_ALLOWED_REMOVAL_SET,
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


def package_seed_names_for_target(target: ResolvedCatalogTarget) -> tuple[str, ...]:
    """Return the exact meta-package roots required by a release contract.

    JP6.1, JP6.2, JP6.2.1, and JP6.2.2 share rolling repository suites with adjacent
    releases. Their top-level meta-packages therefore need the runtime and
    development meta packages pinned explicitly to the catalog's exact build.
    Other validated releases retain their established single-seed transaction.
    """

    if (
        target.canonical_id,
        target.jetpack_version,
        target.l4t_version,
    ) == (JP61_CANONICAL_ID, "6.1", "36.4"):
        return JP61_EXACT_META_SEED_NAMES
    if (
        target.canonical_id,
        target.jetpack_version,
        target.l4t_version,
    ) == (JP62_CANONICAL_ID, "6.2", "36.4.3"):
        return JP62_EXACT_META_SEED_NAMES
    if (
        target.canonical_id,
        target.jetpack_version,
        target.l4t_version,
    ) == (JP621_CANONICAL_ID, "6.2.1", "36.4.4"):
        return JP621_EXACT_META_SEED_NAMES
    if (
        target.canonical_id,
        target.jetpack_version,
        target.l4t_version,
    ) == (JP622_CANONICAL_ID, "6.2.2", "36.5.0"):
        return JP622_EXACT_META_SEED_NAMES
    return (str(target.record["packages"]["meta_package"]["name"]),)


def package_seeds_for_target(target: ResolvedCatalogTarget) -> tuple[PackageSeed, ...]:
    """Return exact APT roots without changing other releases' seed semantics."""

    metadata = target.record["packages"]["meta_package"]
    version = str(metadata["version_build"])
    architecture = str(metadata["architecture"])
    seeds = tuple(
        PackageSeed(name, version, architecture)
        for name in package_seed_names_for_target(target)
    )
    if target.canonical_id == JP61_CANONICAL_ID:
        seeds += tuple(PackageSeed(*item) for item in JP61_ADDITIONAL_EXACT_SEEDS)
    return seeds


def construction_recipe_for_target(
    target: ResolvedCatalogTarget,
) -> Mapping[str, object]:
    """Build the JP6 family recipe with the target's narrow release policy."""

    recipe = copy.deepcopy(_CONSTRUCTION_RECIPE_V1)
    package_configuration = recipe["package_configuration"]
    assert isinstance(package_configuration, dict)
    policy = package_removal_policy_for_target(target)
    seeds = package_seeds_for_target(target)
    if len(seeds) > 1:
        if target.canonical_id == JP61_CANONICAL_ID:
            seed_profile_version = JP61_SEED_PROFILE_VERSION
        elif target.canonical_id == JP621_CANONICAL_ID:
            seed_profile_version = JP621_SEED_PROFILE_VERSION
        elif target.canonical_id == JP622_CANONICAL_ID:
            seed_profile_version = JP622_SEED_PROFILE_VERSION
        else:
            seed_profile_version = JP62_SEED_PROFILE_VERSION
        package_configuration["exact_meta_package_seed_profile"] = {
            "version": seed_profile_version,
            "scope": {
                "jetpack_version": target.jetpack_version,
                "l4t_version": target.l4t_version,
            },
            "packages": [
                {
                    "name": seed.name,
                    "version": seed.version,
                    "architecture": seed.architecture,
                }
                for seed in seeds
            ],
            "version_source": "official-repository-exact-dependency-closure",
        }
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
    if target.canonical_id in {JP60_CANONICAL_ID, JP62_CANONICAL_ID, JP621_CANONICAL_ID}:
        package_configuration["offline_l4t_preinstall"] = {
            "scope": {
                "jetpack_version": target.jetpack_version,
                "l4t_version": target.l4t_version,
            },
            "vendor_marker": (
                "/opt/nvidia/l4t-packages/"
                ".nv-l4t-disable-boot-fw-update-in-preinstall"
            ),
            "lifetime": "construction-chroot-only",
        }
    return recipe


def target_requires_offline_l4t_preinstall(target: ResolvedCatalogTarget) -> bool:
    """Return whether the target recipe specifies the offline L4T preinstall marker."""

    recipe = construction_recipe_for_target(target)
    package_configuration = recipe.get("package_configuration", {})
    return (
        isinstance(package_configuration, dict)
        and "offline_l4t_preinstall" in package_configuration
    )


def construction_recipe_digest_for_target(target: ResolvedCatalogTarget) -> str:
    return json_digest(construction_recipe_for_target(target))
