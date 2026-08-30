from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.base.lock import target_lock_digest, write_target_lock
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
from orin_stage.cli import build_parser, main
from orin_stage.materialization_seed import MaterializationSeedResult
from orin_stage.workspace_manager import (
    WorkspaceListEntry,
    WorkspaceManagerError,
    WorkspaceRecord,
)


SELECTOR = "jetson-orin@jp6.2.3"
TARGET_LOCK_DIGEST = "a" * 64
BASE_DIGEST = "b" * 64
WORKSPACE_ID = "0123456789abcdef0123456789abcdef"


def _normal_user(monkeypatch) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 1000)


def _resolver() -> TargetResolver:
    paths = builtin_catalog_paths()
    return TargetResolver(paths.targets_dir, paths.schema_path)


def _target(tmp_path: Path, *, with_seed: bool) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    target = data_root / "targets" / TARGET_LOCK_DIGEST
    target.mkdir(parents=True)
    (target / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
            }
        ),
        encoding="utf-8",
    )
    if with_seed:
        _publish_seed(target)
    return data_root, target


def _publish_seed(target: Path) -> MaterializationSeedResult:
    materialization = target / "materialization"
    materialization.mkdir(exist_ok=True)
    archive = materialization / "seed.tar"
    metadata = materialization / "seed.json"
    archive.write_bytes(b"seed")
    metadata.write_text(
        json.dumps(
            {
                "format": "gnu-tar",
                "format_version": 1,
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
                "seed_sha256": "c" * 64,
                "archive": "seed.tar",
            }
        ),
        encoding="utf-8",
    )
    return MaterializationSeedResult(archive, metadata, "c" * 64)


def _record(data_root: Path, name: str) -> WorkspaceRecord:
    workspace = data_root / "workspaces" / WORKSPACE_ID
    return WorkspaceRecord(
        workspace_id=WORKSPACE_ID,
        workspace_name=name,
        target_lock_digest=TARGET_LOCK_DIGEST,
        base_digest=BASE_DIGEST,
        generation=0,
        workspace_path=workspace,
        root_path=workspace / "root",
    )


def _fake_manager(monkeypatch, data_root: Path, target: Path, *, error=None):
    calls: list[tuple[Path, str]] = []

    class FakeManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def create(self, selected_target: Path, name: str) -> WorkspaceRecord:
            calls.append((selected_target, name))
            if error is not None:
                raise error
            return _record(data_root, name)

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeManager)
    monkeypatch.setattr(
        "orin_stage.cli._find_realized_target",
        lambda root, resolved: target,
    )
    return calls


def test_workspace_help_contains_list_create_reset_and_remove(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["workspace", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for command in ("list", "create", "reset", "remove"):
        assert command in output
    for excluded in ("open", "shell", "run", "build", "inspect", "storage"):
        assert excluded not in output


def test_workspace_list_is_empty_without_creating_data_root(
    capsys,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "missing"

    assert main(["--data-root", str(data_root), "workspace", "list"]) == 0
    assert not data_root.exists()
    assert capsys.readouterr().out.strip().split() == [
        "NAME",
        "ID",
        "JETPACK",
        "GENERATION",
    ]


def test_workspace_list_formats_deterministic_entries(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FakeManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def list_workspaces(self):
            return (
                WorkspaceListEntry("a" * 32, "alpha", "6.2.3", 0),
                WorkspaceListEntry("b" * 32, "zeta", "6.2.3", 4),
            )

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeManager)

    assert main(["--data-root", str(data_root), "workspace", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[1].split() == ["alpha", "a" * 32, "6.2.3", "0"]
    assert lines[2].split() == ["zeta", "b" * 32, "6.2.3", "4"]


def test_workspace_list_reports_corrupt_published_metadata(
    capsys,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    (workspace / "workspace.json").write_text("{broken", encoding="utf-8")

    assert main(["--data-root", str(data_root), "workspace", "list"]) == 1
    error = capsys.readouterr().err
    assert "cannot read workspace metadata" in error
    assert "Traceback" not in error


def test_workspace_create_requires_validation_pending_flag(
    monkeypatch,
    capsys,
) -> None:
    _normal_user(monkeypatch)
    monkeypatch.setattr(
        "orin_stage.cli._find_realized_target",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("target discovery must not run")
        ),
    )

    assert main(["workspace", "create", "--target", SELECTOR, "--name", "demo"]) == 1
    error = capsys.readouterr().err
    assert "validation-pending" in error
    assert "--allow-validation-pending" in error


def test_workspace_create_rejects_root_invocation(monkeypatch, capsys) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 0)

    assert (
        main(
            [
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == (
        "error: Run ostg workspace create as your normal user.\n"
        "Orin Stage requests sudo only when materialization seed creation "
        "is required.\n"
    )


def test_workspace_create_rejects_unavailable_target_even_with_flag(
    monkeypatch,
    capsys,
) -> None:
    _normal_user(monkeypatch)

    assert (
        main(
            [
                "workspace",
                "create",
                "--target",
                "jetson-orin@jp6.0-dp",
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert "unavailable" in capsys.readouterr().err


def test_workspace_create_rejects_other_implemented_release(
    monkeypatch,
    capsys,
) -> None:
    _normal_user(monkeypatch)

    assert (
        main(
            [
                "workspace",
                "create",
                "--target",
                "jetson-orin@jp6.2",
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert "currently implemented only for JP6.2.3" in capsys.readouterr().err


def test_workspace_create_without_ensured_base_is_explicit_and_offline(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("acquisition must not run")

    monkeypatch.setattr(SdkManagerClient, "version", forbidden)
    monkeypatch.setattr(SdkManagerClient, "query_jetson", forbidden)
    monkeypatch.setattr("orin_stage.cli.ensure_jp623_release", forbidden)
    data_root = tmp_path / "missing"

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "Target is not ensured. Run:" in error
    assert f"ostg target ensure {SELECTOR}" in error
    assert not data_root.exists()


def test_realized_target_discovery_uses_exact_lock_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import orin_stage.cli as cli_module

    data_root = tmp_path / "data"
    targets = data_root / "targets"
    targets.mkdir(parents=True)

    def publish(canonical_id: str) -> Path:
        lock = {
            "schema_version": 1,
            "target": {
                "canonical_id": canonical_id,
                "jetpack_version": "6.2.3",
            },
        }
        digest = target_lock_digest(lock)
        target = targets / digest
        target.mkdir()
        write_target_lock(target / "lock.json", lock)
        return target

    publish("different.target")
    expected = publish("nvidia.jetpack-6.2.3.jetson-linux-36.5.2")
    monkeypatch.setattr(
        cli_module,
        "base_directory_is_reusable",
        lambda candidate: True,
    )

    assert (
        cli_module._find_realized_target(
            data_root,
            _resolver().resolve(SELECTOR),
        )
        == expected
    )


def test_workspace_create_reuses_valid_seed_without_sudo(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root, target = _target(tmp_path, with_seed=True)
    calls = _fake_manager(monkeypatch, data_root, target)
    monkeypatch.setattr(
        "orin_stage.cli.create_materialization_seed_with_sudo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sudo must not run for a complete seed")
        ),
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 0
    )
    assert calls == [(target, "demo")]
    output = capsys.readouterr().out
    assert "Generation:       0" in output
    assert "Materialization:  reused" in output


def test_workspace_create_builds_missing_seed_once_then_uses_exact_identity(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root, target = _target(tmp_path, with_seed=False)
    manager_calls = _fake_manager(monkeypatch, data_root, target)
    seed_calls: list[tuple[Path, Path]] = []

    def privileged_seed(
        selected_target: Path,
        *,
        data_root: Path,
    ) -> MaterializationSeedResult:
        seed_calls.append((selected_target, data_root))
        return _publish_seed(selected_target)

    monkeypatch.setattr(
        "orin_stage.cli.create_materialization_seed_with_sudo",
        privileged_seed,
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "created",
                "--allow-validation-pending",
            ]
        )
        == 0
    )
    assert seed_calls == [(target, data_root)]
    assert manager_calls == [(target, "created")]
    record = _record(data_root, "created")
    assert record.target_lock_digest == TARGET_LOCK_DIGEST
    assert record.base_digest == BASE_DIGEST
    assert "Materialization:  created" in capsys.readouterr().out


def test_workspace_create_rejects_partial_seed_without_sudo(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root, target = _target(tmp_path, with_seed=False)
    materialization = target / "materialization"
    materialization.mkdir()
    (materialization / "seed.tar").write_bytes(b"partial")
    _fake_manager(monkeypatch, data_root, target)
    monkeypatch.setattr(
        "orin_stage.cli.create_materialization_seed_with_sudo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("partial seed must not be overwritten")
        ),
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert "seed is incomplete" in capsys.readouterr().err


def test_workspace_create_surfaces_duplicate_name_error(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root, target = _target(tmp_path, with_seed=True)
    _fake_manager(
        monkeypatch,
        data_root,
        target,
        error=WorkspaceManagerError("workspace name already exists: demo"),
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "workspace",
                "create",
                "--target",
                SELECTOR,
                "--name",
                "demo",
                "--allow-validation-pending",
            ]
        )
        == 1
    )
    assert "workspace name already exists" in capsys.readouterr().err
