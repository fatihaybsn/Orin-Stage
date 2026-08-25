from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.workspace_manager import (
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceNotFoundError,
)


TARGET_LOCK_DIGEST = "a" * 64
BASE_DIGEST = "b" * 64
WORKSPACE_ID = "0123456789abcdef0123456789abcdef"


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def _workspace(data_root: Path, *, name: str = "demo", generation: int = 0) -> Path:
    path = data_root / "workspaces" / WORKSPACE_ID
    (path / "root").mkdir(parents=True)
    (path / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_ID,
                "workspace_name": name,
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
                "generation": generation,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_open_workspace_by_id(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    path = _workspace(data_root, generation=3)

    record = WorkspaceManager(data_root).open(WORKSPACE_ID)

    assert record.workspace_id == WORKSPACE_ID
    assert record.workspace_name == "demo"
    assert record.generation == 3
    assert record.workspace_path == path
    assert record.root_path == path / "root"


def test_open_workspace_by_name(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    _workspace(data_root, name="jp623-demo")

    record = WorkspaceManager(data_root).open("jp623-demo")

    assert record.workspace_id == WORKSPACE_ID


def test_open_missing_workspace_is_explicit(tmp_path: Path) -> None:
    manager = WorkspaceManager(_data_root(tmp_path))

    with pytest.raises(WorkspaceNotFoundError, match="workspace not found"):
        manager.open("missing")


def test_open_rejects_workspace_id_directory_mismatch(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    path = _workspace(data_root)
    metadata_path = path / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["workspace_id"] = "different-id"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(WorkspaceManagerError, match="does not match"):
        WorkspaceManager(data_root).open(WORKSPACE_ID)


def test_open_rejects_missing_workspace_root(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    path = _workspace(data_root)
    (path / "root").rmdir()

    with pytest.raises(WorkspaceManagerError, match="workspace root"):
        WorkspaceManager(data_root).open(WORKSPACE_ID)


def test_locked_reopens_same_workspace_under_kernel_lock(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    _workspace(data_root)
    manager = WorkspaceManager(data_root)

    with manager.locked("demo") as record:
        assert record.workspace_id == WORKSPACE_ID
        assert (data_root / "state" / "workspace-locks" / f"{WORKSPACE_ID}.lock").is_file()


def _target(data_root: Path) -> Path:
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
    return target


def test_reset_replaces_tree_and_increments_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module
    from orin_stage.materialization_extract import ExtractionReport

    data_root = _data_root(tmp_path)
    _target(data_root)
    path = _workspace(data_root, generation=4)
    (path / "root" / "old").write_text("old", encoding="utf-8")

    def fake_extract(data: Path, target: Path, **kwargs: object) -> ExtractionReport:
        root = data / "staging" / ("c" * 32) / "root"
        root.mkdir(parents=True)
        (root / "new").write_text("new", encoding="utf-8")
        return ExtractionReport(1, 1, 1, 1, 0, 0, str(root))

    def fake_remove(tree: Path, **kwargs: object) -> None:
        import shutil
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "extract_materialization_seed", fake_extract)
    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)

    record = WorkspaceManager(data_root).reset("demo")

    assert record.generation == 5
    assert (record.root_path / "new").read_text(encoding="utf-8") == "new"
    assert not (record.root_path / "old").exists()
    assert not (data_root / "staging" / ("c" * 32)).exists()


def test_reset_refuses_different_base(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    target = _target(data_root)
    _workspace(data_root)
    seed_path = target / "materialization" / "seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    seed["base_digest"] = "d" * 64
    seed_path.write_text(json.dumps(seed), encoding="utf-8")

    with pytest.raises(WorkspaceManagerError, match="base no longer matches"):
        WorkspaceManager(data_root).reset("demo")


def test_remove_unpublishes_workspace_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module

    data_root = _data_root(tmp_path)
    path = _workspace(data_root)
    observed: list[bool] = []

    def fake_remove(tree: Path, **kwargs: object) -> None:
        import shutil
        observed.append(path.exists())
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)

    removed = WorkspaceManager(data_root).remove("demo")

    assert removed.workspace_id == WORKSPACE_ID
    assert observed == [False]
    assert not path.exists()
    with pytest.raises(WorkspaceNotFoundError):
        WorkspaceManager(data_root).open("demo")


def test_recover_staging_only_removes_workspace_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module

    data_root = _data_root(tmp_path)
    workspace_attempt = data_root / "staging" / ("e" * 32)
    remove_attempt = data_root / "staging" / (
        f".workspace-remove-{WORKSPACE_ID}-{'f' * 32}"
    )
    base_attempt = data_root / "staging" / ".base-jp623-keep"
    for path in (workspace_attempt, remove_attempt, base_attempt):
        path.mkdir(parents=True)

    def fake_remove(tree: Path, **kwargs: object) -> None:
        import shutil
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)

    removed = WorkspaceManager(data_root).recover_staging()

    assert set(removed) == {workspace_attempt, remove_attempt}
    assert base_attempt.is_dir()


def test_reset_disk_full_before_publish_keeps_old_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module
    from orin_stage.materialization_extract import ExtractionReport

    data_root = _data_root(tmp_path)
    _target(data_root)
    path = _workspace(data_root, generation=2)
    (path / "root" / "old").write_text("keep", encoding="utf-8")

    def fake_extract(data: Path, target: Path, **kwargs: object) -> ExtractionReport:
        root = data / "staging" / ("1" * 32) / "root"
        root.mkdir(parents=True)
        (root / "new").write_text("candidate", encoding="utf-8")
        return ExtractionReport(1, 1, 1, 1, 0, 0, str(root))

    def disk_full(path: Path, value: object) -> None:
        raise WorkspaceManagerError("No space left on device")

    monkeypatch.setattr(module, "extract_materialization_seed", fake_extract)
    monkeypatch.setattr(module, "_write_json_exclusive", disk_full)

    with pytest.raises(WorkspaceManagerError, match="No space left"):
        WorkspaceManager(data_root).reset("demo")

    record = WorkspaceManager(data_root).open("demo")
    assert record.generation == 2
    assert (record.root_path / "old").read_text(encoding="utf-8") == "keep"
    assert not (record.root_path / "new").exists()


def test_sigkill_after_atomic_reset_publish_leaves_valid_final_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import shutil
    import signal
    import subprocess
    import sys
    import time

    import orin_stage.workspace_manager as module

    data_root = _data_root(tmp_path)
    path = _workspace(data_root, generation=6)
    (path / "root" / "old").write_text("old", encoding="utf-8")

    staging = data_root / "staging" / ("2" * 32)
    (staging / "root").mkdir(parents=True)
    (staging / "root" / "new").write_text("new", encoding="utf-8")
    metadata = json.loads((path / "workspace.json").read_text(encoding="utf-8"))
    metadata["generation"] = 7
    (staging / "workspace.json").write_text(json.dumps(metadata), encoding="utf-8")
    marker = tmp_path / "exchanged"

    code = """
import pathlib, sys, time
from orin_stage.workspace_manager import _rename_exchange
source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
marker = pathlib.Path(sys.argv[3])
_rename_exchange(source, destination)
marker.write_text('done', encoding='utf-8')
time.sleep(60)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(staging), str(path), str(marker)],
        env=env,
    )
    try:
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            if child.poll() is not None:
                raise AssertionError("reset publish child exited before marker")
            time.sleep(0.02)
        assert marker.exists()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    record = WorkspaceManager(data_root).open("demo")
    assert record.generation == 7
    assert (record.root_path / "new").read_text(encoding="utf-8") == "new"
    assert (staging / "root" / "old").read_text(encoding="utf-8") == "old"

    def fake_remove(tree: Path, **kwargs: object) -> None:
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)
    recovered = WorkspaceManager(data_root).recover_staging()
    assert staging in recovered
    assert not staging.exists()
