from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from orin_stage.acquisition.sdk_manager import SdkManagerNotFoundError
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
from orin_stage.cli import main
from orin_stage.planning.orchestration import (
    JP623_HARDWARE_PROFILE,
    JP623_QEMU_BINARY,
    JP623_SDK_MANAGER_TARGET,
)
from orin_stage.planning.planner import BasePlanStatus
from orin_stage.privileged_base import (
    PrivilegedBaseError,
    ensure_jp623_base_with_sudo,
)


SELECTOR = "jetson-orin@jp6.2.3"


def _resolver() -> TargetResolver:
    paths = builtin_catalog_paths()
    return TargetResolver(paths.targets_dir, paths.schema_path)


def _result(
    tmp_path: Path,
    *,
    acquisition_cache_hit: bool | None = None,
    base_reuse: bool,
) -> SimpleNamespace:
    target = _resolver().resolve(SELECTOR)
    base_directory = tmp_path / "targets" / ("c" * 64)
    acquisition = (
        None
        if acquisition_cache_hit is None
        else SimpleNamespace(cache_hit=acquisition_cache_hit)
    )
    if base_reuse:
        final_plan = SimpleNamespace(
            base_status=BasePlanStatus.BASE_REUSE,
            base_digest="d" * 64,
            base_reference=str(base_directory),
        )
        base_result = None
    else:
        final_plan = SimpleNamespace(
            base_status=BasePlanStatus.CONSTRUCTION_REQUIRED,
            base_digest=None,
            base_reference=None,
        )
        base_result = SimpleNamespace(
            cache_hit=False,
            base_digest="e" * 64,
            base_path=base_directory / "base",
        )
    return SimpleNamespace(
        target=target,
        acquisition_result=acquisition,
        final_plan=final_plan,
        base_result=base_result,
    )


def _normal_user(monkeypatch) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 1000)


def test_validation_pending_requires_explicit_flag(
    monkeypatch,
    capsys,
) -> None:
    _normal_user(monkeypatch)
    monkeypatch.setattr(
        "orin_stage.cli.ensure_jp623_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("orchestration must not run")
        ),
    )

    assert main(["target", "ensure", SELECTOR]) == 1
    error = capsys.readouterr().err
    assert "validation-pending" in error
    assert "--allow-validation-pending" in error
    assert "Traceback" not in error


def test_validation_pending_flag_accepts_jp623_and_reuses_base(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    calls: list[dict[str, object]] = []

    def ensure(*args, **kwargs):
        calls.append(kwargs)
        return _result(tmp_path, acquisition_cache_hit=None, base_reuse=True)

    monkeypatch.setattr("orin_stage.cli.ensure_jp623_release", ensure)

    assert (
        main(
            [
                "--data-root",
                str(tmp_path / "data"),
                "target",
                "ensure",
                SELECTOR,
                "--allow-validation-pending",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    assert calls[0]["hardware_profile"] == JP623_HARDWARE_PROFILE
    assert calls[0]["required_sdk_manager_target"] == JP623_SDK_MANAGER_TARGET
    assert calls[0]["qemu_binary"] == JP623_QEMU_BINARY
    assert calls[0]["base_builder"] is ensure_jp623_base_with_sudo
    output = capsys.readouterr().out
    assert "validation-pending (explicitly allowed)" in output
    assert "Acquisition:  cache-hit" in output
    assert "Base:         reused" in output


def test_supported_jp623_does_not_require_flag(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    target = replace(_resolver().resolve(SELECTOR), support_status="supported")

    class SupportedResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, selector: str):
            return replace(target, selector=selector)

    monkeypatch.setattr("orin_stage.cli.TargetResolver", SupportedResolver)

    def ensure(*args, **kwargs):
        result = _result(tmp_path, acquisition_cache_hit=True, base_reuse=True)
        result.target = target
        return result

    monkeypatch.setattr("orin_stage.cli.ensure_jp623_release", ensure)

    assert main(["target", "ensure", SELECTOR]) == 0
    assert "Status:       supported" in capsys.readouterr().out


def test_unavailable_target_is_rejected_even_with_flag(monkeypatch, capsys) -> None:
    _normal_user(monkeypatch)

    assert (
        main(
            [
                "target",
                "ensure",
                "jetson-orin@jp6.0-dp",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert "unavailable" in capsys.readouterr().err


def test_other_jp6_release_reports_not_implemented(monkeypatch, capsys) -> None:
    _normal_user(monkeypatch)

    assert main(["target", "ensure", "jetson-orin@jp6.2"]) == 1
    assert "currently implemented only for JP6.2.3" in capsys.readouterr().err


def test_unknown_selector_is_short_domain_error(monkeypatch, capsys) -> None:
    _normal_user(monkeypatch)

    assert main(["target", "ensure", "jetson-orin@jp6.9"]) == 1
    error = capsys.readouterr().err
    assert "Unknown exact target selector" in error
    assert "Traceback" not in error


def test_top_level_root_invocation_is_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 0)

    assert main(["target", "ensure", SELECTOR, "--allow-validation-pending"]) == 1
    error = capsys.readouterr().err
    assert "Run ostg target ensure as your normal user." in error
    assert "requests sudo only when base construction is required" in error


def test_sdk_manager_not_found_is_short_domain_error(monkeypatch, capsys) -> None:
    _normal_user(monkeypatch)
    monkeypatch.setattr(
        "orin_stage.cli.ensure_jp623_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SdkManagerNotFoundError("SDK Manager executable not found")
        ),
    )

    assert main(["target", "ensure", SELECTOR, "--allow-validation-pending"]) == 1
    error = capsys.readouterr().err
    assert "SDK Manager executable not found" in error
    assert "Traceback" not in error


def test_sudo_failure_is_short_domain_error(monkeypatch, capsys) -> None:
    _normal_user(monkeypatch)
    monkeypatch.setattr(
        "orin_stage.cli.ensure_jp623_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PrivilegedBaseError("sudo is not installed")
        ),
    )

    assert main(["target", "ensure", SELECTOR, "--allow-validation-pending"]) == 1
    error = capsys.readouterr().err
    assert error.strip() == "error: sudo is not installed"
    assert "Traceback" not in error


def test_download_and_construction_output(monkeypatch, capsys, tmp_path: Path) -> None:
    _normal_user(monkeypatch)
    monkeypatch.setattr(
        "orin_stage.cli.ensure_jp623_release",
        lambda *args, **kwargs: _result(
            tmp_path,
            acquisition_cache_hit=False,
            base_reuse=False,
        ),
    )

    assert main(["target", "ensure", SELECTOR, "--allow-validation-pending"]) == 0
    output = capsys.readouterr().out
    assert "Acquisition:  downloaded+verified" in output
    assert "Base:         constructed+validated" in output
