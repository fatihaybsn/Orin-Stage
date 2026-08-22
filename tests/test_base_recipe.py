from __future__ import annotations

from orin_stage.base._json import json_digest
from orin_stage.base.recipe import (
    JP623_ALLOWED_REMOVAL_SET,
    JP623_REMOVAL_POLICY_VERSION,
    construction_recipe_digest_v1,
    construction_recipe_v1,
)


def test_construction_recipe_v1_is_stable_and_copy_safe() -> None:
    first = construction_recipe_v1()
    second = construction_recipe_v1()
    first["artifact_flow"].append("unexpected")  # type: ignore[union-attr]

    assert "unexpected" not in second["artifact_flow"]
    assert len(construction_recipe_digest_v1()) == 64


def test_recipe_contains_only_step3_construction_semantics() -> None:
    recipe = construction_recipe_v1()
    encoded = repr(recipe)

    assert "l4t_flash_prerequisites" in encoded
    assert "apply_binaries" in encoded
    assert "qemu-aarch64-static-binfmt-chroot" in encoded
    assert "SDK Manager" not in encoded
    assert "validation" not in encoded


def test_jp623_recipe_contains_versioned_exact_opencv_removal_policy() -> None:
    recipe = construction_recipe_v1()
    package_configuration = recipe["package_configuration"]
    policy = package_configuration["removal_policy"]  # type: ignore[index]

    assert policy["version"] == JP623_REMOVAL_POLICY_VERSION  # type: ignore[index]
    assert policy["scope"] == {  # type: ignore[index]
        "jetpack_version": "6.2.3",
        "l4t_version": "36.5.2",
    }
    assert policy["decision"] == "allow-subset-of-exact-set"  # type: ignore[index]
    assert tuple(policy["allowed_removal_set"]) == JP623_ALLOWED_REMOVAL_SET  # type: ignore[index]


def test_recipe_digest_changes_when_removal_policy_changes() -> None:
    changed = construction_recipe_v1()
    package_configuration = changed["package_configuration"]
    policy = package_configuration["removal_policy"]  # type: ignore[index]
    policy["allowed_removal_set"].append("systemd")  # type: ignore[index,union-attr]

    assert json_digest(changed) != construction_recipe_digest_v1()
