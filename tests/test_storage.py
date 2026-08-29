from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orin_stage.storage import (
    DeletionBlockedError,
    DeletionConfirmationRequired,
    StorageError,
    StorageManager,
    _allocated_shifted_tree_bytes,
    _allocated_tree_bytes,
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


def _in_process_measurement(
    command: tuple[str, ...],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    if command[:2] == ("podman", "unshare"):
        measured_path = Path(command[-1])
    else:
        data_root = Path(command[command.index("--data-root") + 1])
        digest = command[command.index("--target-digest") + 1]
        measured_path = data_root / "targets" / digest
        if "orin_stage.privileged_storage_delete" in command:
            shutil.rmtree(measured_path)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"removed": True, "target_lock_digest": digest}
                ),
                stderr="",
            )
    bytes_used = _allocated_tree_bytes(measured_path)
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps({"bytes_used": bytes_used}),
        stderr="",
    )


def _manager(data: Path, **kwargs: object) -> StorageManager:
    return StorageManager(data, runner=_in_process_measurement, **kwargs)


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

    status = _manager(data).status()

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

    status = _manager(data).status()

    assert status.base_bytes == status.bases[0].bytes_used
    assert status.bases[0].path == target
    assert status.base_bytes >= 4096


def test_status_missing_categories_are_zero(tmp_path: Path) -> None:
    status = _manager(_root(tmp_path)).status()

    assert status.sdkm_cache_bytes == 0
    assert status.base_bytes == 0
    assert status.workspace_bytes == 0
    assert status.build_output_bytes == 0
    assert status.tracked_bytes == 0


def test_workspace_remove_requires_explicit_confirmation(tmp_path: Path) -> None:
    data = _root(tmp_path)
    workspace = _workspace(data)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        _manager(data).remove_workspace("demo")

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

    plan = _manager(data).remove_workspace("demo", confirmation=WORKSPACE_ID)

    assert plan.kind == "workspace"
    assert plan.identifier == WORKSPACE_ID
    assert not workspace.exists()


def test_base_plan_reports_dependent_workspace(tmp_path: Path) -> None:
    data = _root(tmp_path)
    _target(data)
    _workspace(data, name="project-a")

    plan = _manager(data).plan_base_remove(TARGET)

    assert not plan.allowed
    assert plan.blocked_by == ("project-a",)


def test_base_remove_is_blocked_while_workspace_references_it(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)
    _workspace(data)

    with pytest.raises(DeletionBlockedError, match="still referenced"):
        _manager(data).remove_base(TARGET, confirmation=TARGET)

    assert target.is_dir()


def test_base_remove_requires_confirmation_even_without_workspace(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        _manager(data).remove_base(TARGET)

    assert target.is_dir()


def test_confirmed_unreferenced_base_remove_deletes_target_tree(tmp_path: Path) -> None:
    data = _root(tmp_path)
    target = _target(data)

    plan = _manager(data).remove_base(TARGET, confirmation=TARGET)

    assert plan.allowed
    assert plan.path == target
    assert not target.exists()


def test_sdkm_cache_remove_requires_confirmation_and_keeps_cache(tmp_path: Path) -> None:
    data = _root(tmp_path)
    artifact = data / "sdkm" / "downloads" / "artifact.tbz2"
    _write_bytes(artifact, 1024)

    with pytest.raises(DeletionConfirmationRequired, match="confirmation token"):
        _manager(data).remove_sdkm_cache()

    assert artifact.is_file()


def test_confirmed_sdkm_cache_remove_only_clears_download_bytes(tmp_path: Path) -> None:
    data = _root(tmp_path)
    artifact = data / "sdkm" / "downloads" / "artifact.tbz2"
    receipt = data / "sdkm" / "receipts" / TARGET / "receipt.json"
    response = data / "sdkm" / "responses" / "jp623.json"
    _write_bytes(artifact, 1024)
    _write_bytes(receipt, 10)
    _write_bytes(response, 10)

    plan = _manager(data).remove_sdkm_cache(confirmation="sdkm-downloads")

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
        _manager(data).remove_base(TARGET, confirmation=TARGET)

    assert target.is_dir()


@pytest.mark.parametrize("workspace_state", ("missing", "corrupt", "symlink"))
def test_base_remove_never_calls_privileged_delete_for_unprovable_workspace_state(
    tmp_path: Path,
    workspace_state: str,
) -> None:
    data = _root(tmp_path)
    target = _target(data)
    workspace = data / "workspaces" / "unknown"
    if workspace_state == "symlink":
        outside = tmp_path / "outside-workspace"
        outside.mkdir()
        workspace.parent.mkdir()
        workspace.symlink_to(outside, target_is_directory=True)
    else:
        workspace.mkdir(parents=True)
        if workspace_state == "corrupt":
            (workspace / "workspace.json").write_text("{broken", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _in_process_measurement(command, **kwargs)

    with pytest.raises(StorageError):
        StorageManager(data, runner=runner).remove_base(
            TARGET,
            confirmation=TARGET,
        )

    assert target.is_dir()
    assert all("orin_stage.privileged_storage_delete" not in call for call in commands)


def test_wrong_confirmation_never_calls_privileged_delete(tmp_path: Path) -> None:
    data = _root(tmp_path)
    _target(data)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _in_process_measurement(command, **kwargs)

    with pytest.raises(DeletionConfirmationRequired):
        StorageManager(data, runner=runner).remove_base(
            TARGET,
            confirmation="wrong",
        )

    assert all("orin_stage.privileged_storage_delete" not in call for call in commands)


def test_dependent_workspace_never_calls_privileged_delete(tmp_path: Path) -> None:
    data = _root(tmp_path)
    _target(data)
    _workspace(data)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _in_process_measurement(command, **kwargs)

    with pytest.raises(DeletionBlockedError):
        StorageManager(data, runner=runner).remove_base(
            TARGET,
            confirmation=TARGET,
        )

    assert all("orin_stage.privileged_storage_delete" not in call for call in commands)


def test_base_replan_and_privileged_delete_stay_under_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    data = _root(tmp_path)
    _target(data)
    lock_held = False
    calls: list[tuple[str, bool]] = []

    @contextmanager
    def lifecycle_lock(_manager: StorageManager):
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        operation = (
            "delete"
            if "orin_stage.privileged_storage_delete" in command
            else "measure"
        )
        calls.append((operation, lock_held))
        return _in_process_measurement(command, **kwargs)

    monkeypatch.setattr(StorageManager, "_workspace_lifecycle_lock", lifecycle_lock)

    StorageManager(data, runner=runner).remove_base(TARGET, confirmation=TARGET)

    assert calls == [("measure", False), ("measure", True), ("delete", True)]
    assert not lock_held


def test_allocated_tree_semantics_do_not_follow_symlinks_and_deduplicate_hardlinks(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    payload = tree / "payload"
    payload.write_bytes(b"x" * 8192)
    os.link(payload, tree / "hardlink")
    outside = tmp_path / "outside"
    outside.write_bytes(b"y" * 65536)
    symlink = tree / "outside-link"
    symlink.symlink_to(outside)

    def allocated(path: Path) -> int:
        stat = path.lstat()
        return stat.st_blocks * 512 if stat.st_blocks else stat.st_size

    expected = allocated(tree) + allocated(payload) + allocated(symlink)
    assert _allocated_tree_bytes(tree) == expected


def test_workspace_measurement_uses_podman_unshare_worker(tmp_path: Path) -> None:
    data = _root(tmp_path)
    workspace = _workspace(data)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":1234}',
            stderr="",
        )

    plan = StorageManager(data, runner=runner).plan_workspace_remove("demo")

    assert plan.bytes_used == 1234
    assert commands == [
        (
            "podman",
            "unshare",
            os.path.abspath(os.sys.executable),
            "-m",
            "orin_stage._storage_measure_worker",
            str(workspace),
        )
    ]


def test_base_plan_uses_privileged_measurement_not_podman_unshare(
    tmp_path: Path,
) -> None:
    data = _root(tmp_path)
    target = _target(data)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":5678}',
            stderr="",
        )

    plan = StorageManager(data, runner=runner).plan_base_remove(TARGET)

    assert plan.bytes_used == 5678
    assert commands[0][:2] == ("sudo", "--")
    assert "podman" not in commands[0]
    assert str(target) not in commands[0]
    assert commands[0][commands[0].index("--target-digest") + 1] == TARGET


def test_base_status_uses_the_same_privileged_measurement_path(
    tmp_path: Path,
) -> None:
    data = _root(tmp_path)
    _target(data)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":9012}',
            stderr="",
        )

    status = StorageManager(data, runner=runner).status()

    assert status.base_bytes == 9012
    assert status.bases[0].bytes_used == 9012
    assert len(commands) == 1
    assert commands[0][:2] == ("sudo", "--")


def test_shifted_measurement_parses_worker_result(tmp_path: Path) -> None:
    measured = tmp_path / "tree"

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":4096}',
            stderr="",
        )

    assert _allocated_shifted_tree_bytes(measured, runner=runner) == 4096


@pytest.mark.parametrize(
    "output",
    ["not-json", "[]", '{"bytes_used":true}', '{"bytes_used":-1}', '{"bytes_used":1,"extra":2}'],
)
def test_shifted_measurement_rejects_malformed_worker_output(
    tmp_path: Path,
    output: str,
) -> None:
    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(StorageError, match="shifted storage measurement returned invalid"):
        _allocated_shifted_tree_bytes(tmp_path, runner=runner)


def test_shifted_measurement_reports_podman_unshare_failure(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            125,
            stdout="",
            stderr="cannot enter user namespace\n",
        )

    with pytest.raises(
        StorageError,
        match=r"podman unshare storage measurement failed \(exit 125\)",
    ):
        _allocated_shifted_tree_bytes(tmp_path, runner=runner)


def test_shifted_measurement_keeps_venv_interpreter_symlink_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = tmp_path / "actual-python"
    actual.write_text("", encoding="utf-8")
    interpreter = tmp_path / "venv-python"
    interpreter.symlink_to(actual)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":0}',
            stderr="",
        )

    monkeypatch.setattr("orin_stage.storage.sys.executable", str(interpreter))

    assert _allocated_shifted_tree_bytes(tmp_path, runner=runner) == 0
    assert commands[0][2] == os.path.abspath(interpreter)
    assert commands[0][2] != str(interpreter.resolve())
