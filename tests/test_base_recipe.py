from __future__ import annotations

from orin_stage.base.recipe import construction_recipe_digest_v1, construction_recipe_v1


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
