from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orin_stage.cli import main
from orin_stage.materialization_extract import ExtractionReport
from orin_stage.storage import StorageManager
from orin_stage.workspace_manager import (
    WorkspaceManager,
    WorkspaceNotFoundError,
)


TARGET_LOCK_DIGEST = "a" * 64
BASE_DIGEST = "b" * 64
WORKSPACE_ID = "0123456789abcdef0123456789abcdef"


def _published_workspace(
    tmp_path: Path,
    *,
    generation: int = 0,
) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    target = data_root / "targets" / TARGET_LOCK_DIGEST
    materialization = target / "materialization"
    materialization.mkdir(parents=True)
    (materialization / "seed.json").write_text(
        json.dumps(
            {
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
            }
        ),
        encoding="utf-8",
    )

    workspace = data_root / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    (workspace / "root" / "state").write_bytes(b"workspace-state")
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_ID,
                "workspace_name": "demo",
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
                "generation": generation,
            }
        ),
        encoding="utf-8",
    )
    return data_root, workspace


def _command(data_root: Path, operation: str, *arguments: str) -> list[str]:
    return [
        "--data-root",
        str(data_root),
        "workspace",
        operation,
        "demo",
        *arguments,
    ]


def test_workspace_reset_without_confirmation_is_read_only_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace = _published_workspace(tmp_path)
    original_metadata = (workspace / "workspace.json").read_bytes()

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("reset must not run while showing the plan")

    monkeypatch.setattr(WorkspaceManager, "reset", forbidden)

    assert main(_command(data_root, "reset")) == 0

    output = capsys.readouterr().out
    assert "Workspace:     demo" in output
    assert f"Workspace ID:  {WORKSPACE_ID}" in output
    assert "Generation:    0 -> 1" in output
    assert f"Target lock:   {TARGET_LOCK_DIGEST}" in output
    assert f"Base digest:   {BASE_DIGEST}" in output
    assert "Current size:" in output
    assert "Action:        reset to the same immutable base" in output
    assert (
        f"ostg workspace reset demo --confirm {WORKSPACE_ID}" in output
    )
    assert (workspace / "workspace.json").read_bytes() == original_metadata
    assert (workspace / "root" / "state").read_bytes() == b"workspace-state"


def test_workspace_reset_plan_uses_storage_manager_measurement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, _workspace = _published_workspace(tmp_path)
    original = StorageManager.plan_workspace_remove
    calls: list[str] = []

    def tracked(self: StorageManager, selector: str):
        calls.append(selector)
        return original(self, selector)

    monkeypatch.setattr(StorageManager, "plan_workspace_remove", tracked)

    assert main(_command(data_root, "reset")) == 0
    assert calls == [WORKSPACE_ID]
    assert "Current size:" in capsys.readouterr().out


def test_workspace_reset_rejects_wrong_confirmation_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace = _published_workspace(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("reset must not run with the wrong confirmation")

    monkeypatch.setattr(WorkspaceManager, "reset", forbidden)

    assert main(_command(data_root, "reset", "--confirm", "wrong")) == 1
    error = capsys.readouterr().err
    assert "must exactly match" in error
    assert WORKSPACE_ID in error
    assert "Traceback" not in error
    assert WorkspaceManager(data_root).open("demo").generation == 0
    assert (workspace / "root" / "state").is_file()


def test_confirmed_workspace_reset_increments_generation_and_keeps_binding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import orin_stage.workspace_manager as workspace_module

    data_root, _workspace = _published_workspace(tmp_path, generation=3)

    def fake_extract(
        data: Path,
        target: Path,
        **kwargs: object,
    ) -> ExtractionReport:
        assert target == data_root / "targets" / TARGET_LOCK_DIGEST
        root = data / "staging" / ("c" * 32) / "root"
        root.mkdir(parents=True)
        (root / "fresh").write_text("fresh", encoding="utf-8")
        return ExtractionReport(1, 1, 1, 1, 0, 0, str(root))

    def remove_tree(path: Path, **kwargs: object) -> None:
        shutil.rmtree(path)

    monkeypatch.setattr(
        workspace_module,
        "extract_materialization_seed",
        fake_extract,
    )
    monkeypatch.setattr(
        workspace_module,
        "_remove_tree_in_namespace",
        remove_tree,
    )

    assert (
        main(_command(data_root, "reset", "--confirm", WORKSPACE_ID))
        == 0
    )

    record = WorkspaceManager(data_root).open("demo")
    assert record.generation == 4
    assert record.target_lock_digest == TARGET_LOCK_DIGEST
    assert record.base_digest == BASE_DIGEST
    assert (record.root_path / "fresh").read_text(encoding="utf-8") == "fresh"
    output = capsys.readouterr().out
    assert "Generation:    3 -> 4" in output
    assert "Action:        reset completed" in output


def test_workspace_remove_without_confirmation_only_shows_storage_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace = _published_workspace(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("remove must not run while showing the plan")

    monkeypatch.setattr(StorageManager, "remove_workspace", forbidden)

    assert main(_command(data_root, "remove")) == 0

    output = capsys.readouterr().out
    assert "Workspace:     demo" in output
    assert f"Workspace ID:  {WORKSPACE_ID}" in output
    assert f"Path:          {workspace}" in output
    assert "Current size:" in output
    assert "Action:        remove workspace" in output
    assert (
        f"ostg workspace remove demo --confirm {WORKSPACE_ID}" in output
    )
    assert workspace.is_dir()


def test_workspace_remove_rejects_wrong_confirmation_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace = _published_workspace(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("remove must not run with the wrong confirmation")

    monkeypatch.setattr(StorageManager, "remove_workspace", forbidden)

    assert main(_command(data_root, "remove", "--confirm", "wrong")) == 1
    error = capsys.readouterr().err
    assert "must exactly match" in error
    assert WORKSPACE_ID in error
    assert "Traceback" not in error
    assert workspace.is_dir()


def test_confirmed_workspace_remove_uses_storage_guard_and_unpublishes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import orin_stage.workspace_manager as workspace_module

    data_root, workspace = _published_workspace(tmp_path)
    original = StorageManager.remove_workspace
    calls: list[tuple[str, str | None]] = []

    def tracked(
        self: StorageManager,
        selector: str,
        *,
        confirmation: str | None = None,
    ):
        calls.append((selector, confirmation))
        return original(self, selector, confirmation=confirmation)

    def remove_tree(path: Path, **kwargs: object) -> None:
        shutil.rmtree(path)

    monkeypatch.setattr(StorageManager, "remove_workspace", tracked)
    monkeypatch.setattr(
        workspace_module,
        "_remove_tree_in_namespace",
        remove_tree,
    )

    assert (
        main(_command(data_root, "remove", "--confirm", WORKSPACE_ID))
        == 0
    )

    assert calls == [(WORKSPACE_ID, WORKSPACE_ID)]
    assert not workspace.exists()
    manager = WorkspaceManager(data_root)
    with pytest.raises(WorkspaceNotFoundError):
        manager.open("demo")
    assert manager.list_workspaces() == ()
    output = capsys.readouterr().out
    assert "Action:        removed" in output
    assert f"Workspace ID:  {WORKSPACE_ID}" in output
