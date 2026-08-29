from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orin_stage.base._json import write_json_atomic
from orin_stage.base.lock import target_lock_digest, write_target_lock
from orin_stage.build_identity import JP6_BUILD_IDENTITY
from orin_stage.cli import build_parser, main
from orin_stage.storage import DeletionPlan, _allocated_tree_bytes
from orin_stage.workspace_manager import WorkspaceManager


WORKSPACE_ID = "0123456789abcdef0123456789abcdef"
BASE_DIGEST = "b" * 64
CANONICAL_ID = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
PRIMARY_SELECTOR = "jetson-orin@jp6.2.3"


@pytest.fixture(autouse=True)
def _measure_test_trees_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orin_stage.storage._allocated_shifted_tree_bytes",
        lambda path, **_kwargs: _allocated_tree_bytes(path),
    )


def _published_workspace(
    tmp_path: Path,
    *,
    name: str = "jp623-demo",
    generation: int = 4,
) -> tuple[Path, Path, Path]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    lock = {
        "schema_version": 1,
        "target": {
            "canonical_id": CANONICAL_ID,
            "jetpack_version": "6.2.3",
            "l4t_version": "36.5.2",
            "jetson_linux_release_revision": "36.5.2",
        },
    }
    lock_digest = target_lock_digest(lock)
    target = data_root / "targets" / lock_digest
    (target / "base").mkdir(parents=True)
    (target / "base" / "base-file").write_bytes(b"base")
    write_target_lock(target / "lock.json", lock)
    write_json_atomic(
        target / "receipt.json",
        {
            "schema_version": 1,
            "target_lock_digest": lock_digest,
            "base_digest": BASE_DIGEST,
        },
    )

    workspace = data_root / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    (workspace / "root" / "state").write_bytes(b"workspace-state")
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_ID,
                "workspace_name": name,
                "target_lock_digest": lock_digest,
                "base_digest": BASE_DIGEST,
                "generation": generation,
            }
        ),
        encoding="utf-8",
    )
    return data_root, workspace, target


def test_top_level_help_contains_inspect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["--help"])

    assert captured.value.code == 0
    assert "inspect" in capsys.readouterr().out


def test_inspect_help_requires_workspace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["inspect", "--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "--workspace WORKSPACE" in output
    assert "JSON" not in output


@pytest.mark.parametrize("selector", ["jp623-demo", WORKSPACE_ID])
def test_inspect_resolves_workspace_by_name_or_id_and_shows_exact_identities(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    selector: str,
) -> None:
    data_root, workspace, target = _published_workspace(tmp_path)
    lock_digest = target.name

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                selector,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Workspace\n" in output
    assert "Name:        jp623-demo" in output
    assert f"ID:          {WORKSPACE_ID}" in output
    assert "Generation:  4" in output
    assert f"Root:        {workspace / 'root'}" in output
    assert "Size:" in output
    assert "Target\n" in output
    assert f"Canonical ID:        {PRIMARY_SELECTOR}" in output
    assert "JetPack:             6.2.3" in output
    assert "Jetson Linux/L4T:    36.5.2" in output
    assert "Support status:      validation-pending" in output
    assert f"Target lock digest:  {lock_digest}" in output
    assert "Base\n" in output
    assert f"Digest:  {BASE_DIGEST}" in output
    assert f"Path:    {target / 'base'}" in output
    assert "Build\n" in output
    assert "GCC:                 11.3.0" in output
    assert "Binutils:            2.38" in output
    assert f"Toolchain identity:  {JP6_BUILD_IDENTITY.digest()}" in output
    assert "Managed toolchain:   not acquired" in output
    assert "ARM64 userspace:    QEMU linux-user / CPU-only" in output
    assert "Hardware fidelity:  matching physical Orin required" in output


def test_inspect_size_comes_from_storage_manager(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace, _target = _published_workspace(tmp_path)

    class FakeStorageManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def plan_workspace_remove(self, selector: str) -> DeletionPlan:
            assert selector == WORKSPACE_ID
            return DeletionPlan(
                kind="workspace",
                identifier=WORKSPACE_ID,
                path=workspace,
                bytes_used=1536,
            )

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "jp623-demo",
            ]
        )
        == 0
    )
    assert "Size:        1.5 KiB" in capsys.readouterr().out


def test_inspect_base_mismatch_fails_closed_without_traceback(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace, _target = _published_workspace(tmp_path)
    metadata_path = workspace / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["base_digest"] = "c" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "jp623-demo",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: workspace/base identity mismatch\n"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("corrupt", ["target", "base"])
def test_inspect_corrupt_target_or_base_metadata_returns_one(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    corrupt: str,
) -> None:
    data_root, _workspace, target = _published_workspace(tmp_path)
    metadata = target / ("lock.json" if corrupt == "target" else "receipt.json")
    metadata.write_text("{broken", encoding="utf-8")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "jp623-demo",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert error.startswith("error: ")
    assert "Traceback" not in error


def test_inspect_target_identity_mismatch_returns_one(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace, target = _published_workspace(tmp_path)
    lock = json.loads((target / "lock.json").read_text(encoding="utf-8"))
    lock["target"]["l4t_version"] = "36.5.0"
    new_digest = target_lock_digest(lock)
    replacement = data_root / "targets" / new_digest
    target.rename(replacement)
    write_target_lock(replacement / "lock.json", lock)
    receipt_path = replacement / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["target_lock_digest"] = new_digest
    write_json_atomic(receipt_path, receipt)
    workspace_metadata = workspace / "workspace.json"
    metadata = json.loads(workspace_metadata.read_text(encoding="utf-8"))
    metadata["target_lock_digest"] = new_digest
    workspace_metadata.write_text(json.dumps(metadata), encoding="utf-8")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "jp623-demo",
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == "error: workspace/target identity mismatch\n"


def test_inspect_is_read_only_and_uses_only_toolchain_inspect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root, workspace, _target = _published_workspace(tmp_path, generation=12)
    inspect_calls: list[Path] = []

    class ReadOnlyToolchainManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def inspect(self) -> object:
            inspect_calls.append(data_root)
            return SimpleNamespace(root_path=data_root / "build" / "toolchain" / "root")

        def ensure(self) -> object:
            raise AssertionError("inspect must not ensure or acquire a toolchain")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("inspect must not start acquisition or SDK Manager")

    monkeypatch.setattr(
        "orin_stage.cli.BuildToolchainManager",
        ReadOnlyToolchainManager,
    )
    monkeypatch.setattr("orin_stage.cli.ensure_jp623_release", forbidden)
    monkeypatch.setattr("orin_stage.cli.SdkManagerClient", forbidden)

    def snapshot() -> tuple[tuple[str, int, int, int], ...]:
        return tuple(
            sorted(
                (
                    str(path.relative_to(data_root)),
                    path.lstat().st_mode,
                    path.lstat().st_size,
                    path.lstat().st_mtime_ns,
                )
                for path in data_root.rglob("*")
            )
        )

    before = snapshot()
    workspace_metadata_before = (workspace / "workspace.json").read_bytes()

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "jp623-demo",
            ]
        )
        == 0
    )

    assert inspect_calls == [data_root]
    assert snapshot() == before
    assert (workspace / "workspace.json").read_bytes() == workspace_metadata_before
    assert WorkspaceManager(data_root).open("jp623-demo").generation == 12
    assert "Managed toolchain:   ready" in capsys.readouterr().out


def test_inspect_unknown_workspace_is_short_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "inspect",
                "--workspace",
                "missing",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert error == "error: workspace not found: missing\n"
    assert "Traceback" not in error
