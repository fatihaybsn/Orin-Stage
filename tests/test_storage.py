from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orin_stage.storage import (
    DeletionBlockedError,
    DeletionConfirmationRequired,
    StorageError,
    StorageManager,
)


TARGET = "a" * 64
BASE = "b" * 64
WORKSPACE_ID = "0123456789abcdef0123456789abcdef"


def _root(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _target(data: Path, *, digest: str = TARGET) -> Path:
    target = data / "targets" / digest
    (target / "base").mkdir(parents=True)
    (target / "lock.json").write_text(
        json.dumps({"target": {"canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"}}),
        encoding="utf-8",
    )
    _write_bytes(target / "base" / "root-file", 2048)
    _write_bytes(target / "materialization" / "seed.tar", 4096)
    return target


def _workspace(data: Path, *, name: str = "demo", target: str = TARGET) -> Path:
    workspace = data / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    _write_bytes(workspace / "root" / "state", 3072)
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_ID,
                "workspace_name": name,
                "target_lock_digest": target,
                "base_digest": BASE,
                "generation": 0,
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_status_separates_the_four_mvp_storage_categories(tmp_path: Path) -> None:
    data = _root(tmp_path)
    _target(data)
    _workspace(data)
    _write_bytes(data / "sdkm" / "downloads" / "artifact.tbz2", 1024)
    _write_bytes(data / "build" / "outputs" / "app", 1536)

    status = StorageManager(data).status()

    assert status.sdkm_cache_bytes > 0
    assert status.base_bytes > 0
    assert status.workspace_bytes > 0
    assert status.build_output_bytes > 0
    assert len(status.bases) == 1
    assert status.bases[0].identifier == TARGET
    assert status.bases[0].label == "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    assert len(status.workspaces) == 1
    assert status.workspaces[0].label == "demo"
    assert status.tracked_bytes == (
        status.sdkm_cache_bytes
        + status.base_bytes
        + status.workspace_bytes
        + status.build_output_bytes
    )


def test_status_counts_materialization_storage_with_published_base(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)

    status = StorageManager(data).status()

    assert status.base_bytes == status.bases[0].bytes_used
    assert status.bases[0].path == target
    assert status.base_bytes >= 4096


def test_status_missing_categories_are_zero(tmp_path: Path) -> None:
    status = StorageManager(_root(tmp_path)).status()

    assert status.sdkm_cache_bytes == 0
    assert status.base_bytes == 0
    assert status.workspace_bytes == 0
    assert status.build_output_bytes == 0
    assert status.tracked_bytes == 0


def test_workspace_remove_requires_explicit_confirmation(tmp_path: Path) -> None:
    data = _root(tmp_path)
    workspace = _workspace(data)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        StorageManager(data).remove_workspace("demo")

    assert workspace.is_dir()


def test_confirmed_workspace_remove_uses_workspace_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as workspace_module

    data = _root(tmp_path)
    workspace = _workspace(data)

    def remove_tree(path: Path, **kwargs: object) -> None:
        shutil.rmtree(path)

    monkeypatch.setattr(workspace_module, "_remove_tree_in_namespace", remove_tree)

    plan = StorageManager(data).remove_workspace("demo", confirmation=WORKSPACE_ID)

    assert plan.kind == "workspace"
    assert plan.identifier == WORKSPACE_ID
    assert not workspace.exists()


def test_base_plan_reports_dependent_workspace(tmp_path: Path) -> None:
    data = _root(tmp_path)
    _target(data)
    _workspace(data, name="project-a")

    plan = StorageManager(data).plan_base_remove(TARGET)

    assert not plan.allowed
    assert plan.blocked_by == ("project-a",)


def test_base_remove_is_blocked_while_workspace_references_it(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)
    _workspace(data)

    with pytest.raises(DeletionBlockedError, match="still referenced"):
        StorageManager(data).remove_base(TARGET, confirmation=TARGET)

    assert target.is_dir()


def test_base_remove_requires_confirmation_even_without_workspace(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        StorageManager(data).remove_base(TARGET)

    assert target.is_dir()


def test_confirmed_unreferenced_base_remove_deletes_target_tree(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)

    plan = StorageManager(data).remove_base(TARGET, confirmation=TARGET)

    assert plan.allowed
    assert plan.path == target
    assert not target.exists()


def test_sdkm_cache_remove_requires_confirmation_and_keeps_cache(tmp_path: Path) -> None:
    data = _root(tmp_path)
    artifact = data / "sdkm" / "downloads" / "artifact.tbz2"
    _write_bytes(artifact, 1024)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        StorageManager(data).remove_sdkm_cache()

    assert artifact.is_file()


def test_confirmed_sdkm_cache_remove_only_clears_download_bytes(tmp_path: Path) -> None:
    data = _root(tmp_path)
    artifact = data / "sdkm" / "downloads" / "artifact.tbz2"
    receipt = data / "sdkm" / "receipts" / TARGET / "receipt.json"
    response = data / "sdkm" / "responses" / "jp623.json"
    _write_bytes(artifact, 1024)
    _write_bytes(receipt, 10)
    _write_bytes(response, 10)

    plan = StorageManager(data).remove_sdkm_cache(confirmation="sdkm-downloads")

    assert plan.kind == "sdkm-cache"
    assert plan.bytes_used > 0
    assert not artifact.exists()
    assert (data / "sdkm" / "downloads").is_dir()
    assert receipt.is_file()
    assert response.is_file()


def test_base_remove_fails_closed_when_workspace_metadata_is_missing(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)
    (data / "workspaces" / "unknown" / "root").mkdir(parents=True)

    with pytest.raises(StorageError, match="cannot prove base is unused"):
        StorageManager(data).remove_base(TARGET, confirmation=TARGET)

    assert target.is_dir()
