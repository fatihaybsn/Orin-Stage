from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.base._json import write_json_atomic
from orin_stage.base.lock import target_lock_digest, write_target_lock
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


def _listing_target(data_root: Path) -> tuple[str, str]:
    lock = {
        "schema_version": 1,
        "target": {
            "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
            "jetpack_version": "6.2.3",
        },
    }
    lock_digest = target_lock_digest(lock)
    base_digest = "d" * 64
    target = data_root / "targets" / lock_digest
    target.mkdir(parents=True)
    write_target_lock(target / "lock.json", lock)
    write_json_atomic(
        target / "receipt.json",
        {
            "schema_version": 1,
            "target_lock_digest": lock_digest,
            "base_digest": base_digest,
        },
    )
    return lock_digest, base_digest


def _listed_workspace(
    data_root: Path,
    *,
    workspace_id: str,
    name: str,
    generation: int,
    target_lock_digest_value: str,
    base_digest: str,
) -> None:
    workspace = data_root / "workspaces" / workspace_id
    (workspace / "root").mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": workspace_id,
                "workspace_name": name,
                "target_lock_digest": target_lock_digest_value,
                "base_digest": base_digest,
                "generation": generation,
            }
        ),
        encoding="utf-8",
    )


def test_list_workspaces_is_empty_without_published_directory(tmp_path: Path) -> None:
    manager = WorkspaceManager(_data_root(tmp_path))

    assert manager.list_workspaces() == ()


def test_list_workspaces_validates_and_sorts_by_name_then_id(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    lock_digest, base_digest = _listing_target(data_root)
    _listed_workspace(
        data_root,
        workspace_id="b" * 32,
        name="zeta",
        generation=4,
        target_lock_digest_value=lock_digest,
        base_digest=base_digest,
    )
    _listed_workspace(
        data_root,
        workspace_id="a" * 32,
        name="alpha",
        generation=2,
        target_lock_digest_value=lock_digest,
        base_digest=base_digest,
    )

    entries = WorkspaceManager(data_root).list_workspaces()

    assert [entry.workspace_name for entry in entries] == ["alpha", "zeta"]
    assert [entry.jetpack_version for entry in entries] == ["6.2.3", "6.2.3"]
    assert [entry.generation for entry in entries] == [2, 4]


def test_list_workspaces_rejects_corrupt_published_metadata(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    workspace = data_root / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    (workspace / "workspace.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(WorkspaceManagerError, match="cannot read workspace metadata"):
        WorkspaceManager(data_root).list_workspaces()


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


def _operation_receipts(data_root: Path) -> list[dict[str, object]]:
    operations = data_root / "state" / "operations"
    if not operations.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(operations.glob("*.json"))
    ]


def test_create_writes_completed_operation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module
    from orin_stage.materialization_extract import ExtractionReport
    from orin_stage.workspace_publish import WorkspacePublishResult

    data_root = _data_root(tmp_path)
    target = _target(data_root)

    def fake_publish(data: Path, selected_target: Path, name: str) -> WorkspacePublishResult:
        assert selected_target == target
        path = _workspace(data, name=name)
        return WorkspacePublishResult(
            workspace_path=path,
            workspace_id=WORKSPACE_ID,
            extraction_report=ExtractionReport(1, 1, 1, 1, 0, 0, str(path / "root")),
            reused_staging=False,
        )

    monkeypatch.setattr(module, "publish_materialization_workspace", fake_publish)

    record = WorkspaceManager(data_root).create(target, "created")

    assert record.workspace_id == WORKSPACE_ID
    assert record.target_lock_digest == TARGET_LOCK_DIGEST
    assert record.base_digest == BASE_DIGEST
    assert record.generation == 0
    receipts = _operation_receipts(data_root)
    assert len(receipts) == 1
    assert receipts[0]["operation"] == "create"
    assert receipts[0]["phase"] == "completed"
    assert receipts[0]["workspace_id"] == WORKSPACE_ID
    assert receipts[0]["generation_after"] == 0


def test_mutable_run_holds_workspace_identity_and_advances_generation(tmp_path: Path) -> None:
    import subprocess

    data_root = _data_root(tmp_path)
    path = _workspace(data_root, generation=2)
    observed: list[tuple[Path, tuple[str, ...]]] = []

    class FakeExecutor:
        def run(self, root: Path, command: object, *, runner: object) -> subprocess.CompletedProcess[str]:
            observed.append((root, tuple(command)))
            return subprocess.CompletedProcess(tuple(command), 0, "ok", "")

    completed = WorkspaceManager(data_root).run(
        "demo",
        ("/bin/true",),
        executor=FakeExecutor(),  # type: ignore[arg-type]
    )

    assert completed.stdout == "ok"
    assert observed == [(path / "root", ("/bin/true",))]
    assert WorkspaceManager(data_root).open("demo").generation == 3


def test_build_uses_locked_generation_without_advancing_it(tmp_path: Path) -> None:
    import subprocess

    data_root = _data_root(tmp_path)
    path = _workspace(data_root, generation=8)
    repository = tmp_path / "repo"
    toolchain = tmp_path / "toolchain"
    repository.mkdir()
    toolchain.mkdir()
    observed: list[Path] = []

    class FakeBuildRunner:
        def run(
            self,
            root: Path,
            repository_root: Path,
            toolchain_root: Path,
            command: object,
            *,
            runner: object,
        ) -> subprocess.CompletedProcess[str]:
            observed.append(root)
            assert repository_root == repository
            assert toolchain_root == toolchain
            return subprocess.CompletedProcess(tuple(command), 0, "built", "")

    completed = WorkspaceManager(data_root).build(
        "demo",
        repository,
        toolchain,
        ("make",),
        build_runner=FakeBuildRunner(),  # type: ignore[arg-type]
    )

    assert completed.stdout == "built"
    assert observed == [path / "root"]
    assert WorkspaceManager(data_root).open("demo").generation == 8


def test_reset_completed_receipt_records_generation_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module
    from orin_stage.materialization_extract import ExtractionReport

    data_root = _data_root(tmp_path)
    _target(data_root)
    _workspace(data_root, generation=10)

    def fake_extract(data: Path, target: Path, **kwargs: object) -> ExtractionReport:
        root = data / "staging" / ("3" * 32) / "root"
        root.mkdir(parents=True)
        return ExtractionReport(1, 1, 1, 1, 0, 0, str(root))

    def fake_remove(tree: Path, **kwargs: object) -> None:
        import shutil
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "extract_materialization_seed", fake_extract)
    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)

    WorkspaceManager(data_root).reset("demo")

    receipts = _operation_receipts(data_root)
    assert len(receipts) == 1
    assert receipts[0]["operation"] == "reset"
    assert receipts[0]["phase"] == "completed"
    assert receipts[0]["generation_before"] == 10
    assert receipts[0]["generation_after"] == 11


def test_reset_failure_before_publish_marks_receipt_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module
    from orin_stage.materialization_extract import ExtractionReport

    data_root = _data_root(tmp_path)
    _target(data_root)
    _workspace(data_root, generation=4)

    def fake_extract(data: Path, target: Path, **kwargs: object) -> ExtractionReport:
        root = data / "staging" / ("4" * 32) / "root"
        root.mkdir(parents=True)
        return ExtractionReport(1, 1, 1, 1, 0, 0, str(root))

    def disk_full(path: Path, value: object) -> None:
        raise WorkspaceManagerError("No space left on device")

    monkeypatch.setattr(module, "extract_materialization_seed", fake_extract)
    monkeypatch.setattr(module, "_write_json_exclusive", disk_full)

    with pytest.raises(WorkspaceManagerError, match="No space left"):
        WorkspaceManager(data_root).reset("demo")

    receipt = _operation_receipts(data_root)[0]
    assert receipt["operation"] == "reset"
    assert receipt["phase"] == "failed"
    assert WorkspaceManager(data_root).open("demo").generation == 4


def test_recovery_reconciles_reset_receipt_after_publish_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orin_stage.workspace_manager as module

    data_root = _data_root(tmp_path)
    _workspace(data_root, generation=7)
    abandoned_old_tree = data_root / "staging" / ("5" * 32)
    (abandoned_old_tree / "root").mkdir(parents=True)
    operation_id = "6" * 32
    operations = data_root / "state" / "operations"
    operations.mkdir(parents=True)
    (operations / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "operation_id": operation_id,
                "operation": "reset",
                "phase": "staged",
                "workspace_id": WORKSPACE_ID,
                "workspace_name": "demo",
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
                "generation_before": 6,
                "generation_after": 7,
                "final_path": str(data_root / "workspaces" / WORKSPACE_ID),
                "staging_path": str(abandoned_old_tree),
            }
        ),
        encoding="utf-8",
    )

    def fake_remove(tree: Path, **kwargs: object) -> None:
        import shutil
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)

    WorkspaceManager(data_root).recover_staging()

    receipt = json.loads((operations / f"{operation_id}.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "completed"
    assert receipt["recovered"] is True
    assert not abandoned_old_tree.exists()


def test_sigkill_after_create_publish_is_reconciled_from_receipt(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    data_root = _data_root(tmp_path)
    target = _target(data_root)
    marker = tmp_path / "create-published"

    code = f"""
import json, pathlib, time
import orin_stage.workspace_manager as module
from orin_stage.workspace_manager import WorkspaceManager
workspace_id = {WORKSPACE_ID!r}
target_digest = {TARGET_LOCK_DIGEST!r}
base_digest = {BASE_DIGEST!r}
marker = pathlib.Path({str(marker)!r})
def fake_publish(data_root, target_dir, workspace_name):
    path = pathlib.Path(data_root) / 'workspaces' / workspace_id
    (path / 'root').mkdir(parents=True)
    (path / 'workspace.json').write_text(json.dumps({{
        'format_version': 1,
        'workspace_id': workspace_id,
        'workspace_name': workspace_name,
        'target_lock_digest': target_digest,
        'base_digest': base_digest,
        'generation': 0,
    }}), encoding='utf-8')
    marker.write_text('done', encoding='utf-8')
    time.sleep(60)
module.publish_materialization_workspace = fake_publish
WorkspaceManager(pathlib.Path({str(data_root)!r})).create(pathlib.Path({str(target)!r}), 'demo')
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            if child.poll() is not None:
                raise AssertionError("create child exited before publish marker")
            time.sleep(0.02)
        assert marker.exists()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert WorkspaceManager(data_root).open("demo").workspace_id == WORKSPACE_ID
    receipt = _operation_receipts(data_root)[0]
    assert receipt["phase"] == "started"

    WorkspaceManager(data_root).recover_staging()

    receipt = _operation_receipts(data_root)[0]
    assert receipt["phase"] == "completed"
    assert receipt["recovered"] is True
    assert receipt["workspace_id"] == WORKSPACE_ID


def test_sigkill_after_remove_unpublish_is_cleaned_by_recovery(
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
    _workspace(data_root)
    marker = tmp_path / "remove-unpublished"

    code = f"""
import pathlib, time
import orin_stage.workspace_manager as module
from orin_stage.workspace_manager import WorkspaceManager
marker = pathlib.Path({str(marker)!r})
def hang_after_unpublish(path, **kwargs):
    marker.write_text(str(path), encoding='utf-8')
    time.sleep(60)
module._remove_tree_in_namespace = hang_after_unpublish
WorkspaceManager(pathlib.Path({str(data_root)!r})).remove('demo')
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            if child.poll() is not None:
                raise AssertionError("remove child exited before unpublish marker")
            time.sleep(0.02)
        assert marker.exists()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    with pytest.raises(WorkspaceNotFoundError):
        WorkspaceManager(data_root).open("demo")
    tombstones = list((data_root / "staging").glob(".workspace-remove-*"))
    assert len(tombstones) == 1

    def fake_remove(tree: Path, **kwargs: object) -> None:
        shutil.rmtree(tree)

    monkeypatch.setattr(module, "_remove_tree_in_namespace", fake_remove)
    WorkspaceManager(data_root).recover_staging()
    assert not tombstones[0].exists()


def test_mutable_shell_advances_generation_after_clean_exit(tmp_path: Path) -> None:
    import subprocess

    data_root = _data_root(tmp_path)
    _workspace(data_root, generation=1)

    class FakeExecutor:
        def shell(self, root: Path, *, runner: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(("/bin/bash",), 0, None, None)

    WorkspaceManager(data_root).shell(
        "demo",
        executor=FakeExecutor(),  # type: ignore[arg-type]
    )

    assert WorkspaceManager(data_root).open("demo").generation == 2


def test_failed_mutable_run_does_not_advance_generation(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    _workspace(data_root, generation=5)

    class FailingExecutor:
        def run(self, root: Path, command: object, *, runner: object) -> object:
            raise RuntimeError("command failed")

    with pytest.raises(RuntimeError, match="command failed"):
        WorkspaceManager(data_root).run(
            "demo",
            ("false",),
            executor=FailingExecutor(),  # type: ignore[arg-type]
        )

    assert WorkspaceManager(data_root).open("demo").generation == 5
