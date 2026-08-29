from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.cli import (
    _format_allocated_bytes,
    build_parser,
    main,
)
from orin_stage.storage import (
    DeletionBlockedError,
    DeletionPlan,
    StorageEntry,
    StorageError,
    StorageStatus,
    _allocated_tree_bytes,
)


BASE_A = "a" * 64
BASE_Z = "f" * 64
WORKSPACE_A = "0123456789abcdef0123456789abcdef"
WORKSPACE_Z = "fedcba9876543210fedcba9876543210"


def _status(tmp_path: Path) -> StorageStatus:
    return StorageStatus(
        sdkm_cache_bytes=1024,
        base_bytes=6144,
        workspace_bytes=12288,
        build_output_bytes=512,
        bases=(
            StorageEntry(
                kind="base",
                identifier=BASE_Z,
                label="z-target",
                path=tmp_path / "z-target",
                bytes_used=4096,
            ),
            StorageEntry(
                kind="base",
                identifier=BASE_A,
                label="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
                path=tmp_path / "a-target",
                bytes_used=2048,
            ),
        ),
        workspaces=(
            StorageEntry(
                kind="workspace",
                identifier=WORKSPACE_Z,
                label="z-workspace",
                path=tmp_path / "z-workspace",
                bytes_used=8192,
            ),
            StorageEntry(
                kind="workspace",
                identifier=WORKSPACE_A,
                label="a-workspace",
                path=tmp_path / "a-workspace",
                bytes_used=4096,
            ),
        ),
    )


def test_storage_help_contains_status_and_delete_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["storage", "--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "status" in output
    assert "delete" in output
    for excluded in ("clean", "prune", "gc"):
        assert excluded not in output


def test_storage_delete_help_contains_only_base_and_sdkm_cache(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["storage", "delete", "--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "base" in output
    assert "sdkm-cache" in output
    assert "workspace" not in output


def test_storage_delete_base_requires_digest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["storage", "delete", "base"])

    assert captured.value.code == 2
    assert "TARGET_DIGEST" in capsys.readouterr().err


def test_storage_status_uses_global_data_root_and_storage_manager_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "selected-data"
    data_root.mkdir()
    expected_status = _status(tmp_path)
    status_calls: list[Path] = []

    class FakeStorageManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root.resolve()

        def status(self) -> StorageStatus:
            status_calls.append(data_root)
            return expected_status

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "storage",
                "status",
            ]
        )
        == 0
    )
    assert status_calls == [data_root]
    assert capsys.readouterr().err == ""


def test_storage_status_shows_totals_identities_and_deterministic_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    expected_status = _status(tmp_path)

    class FakeStorageManager:
        def __init__(self, _data_root: Path) -> None:
            pass

        def status(self) -> StorageStatus:
            return expected_status

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert main(["storage", "status"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("Orin Stage Storage\n\n")
    assert "SDK Manager cache  1.0 KiB" in output
    assert "Bases              6.0 KiB" in output
    assert "Workspaces         12.0 KiB" in output
    assert "Build outputs      512 B" in output
    assert "Tracked total      19.5 KiB" in output
    assert "TARGET" in output
    canonical_target = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    assert canonical_target in output
    assert BASE_A in output
    assert "z-target" in output
    assert BASE_Z in output
    assert "NAME" in output
    assert "a-workspace" in output
    assert WORKSPACE_A in output
    assert "z-workspace" in output
    assert WORKSPACE_Z in output
    assert output.index(canonical_target) < output.index("z-target")
    assert output.index("a-workspace") < output.index("z-workspace")


def test_storage_status_empty_state_is_zero_and_explicit(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "empty-data"
    data_root.mkdir()

    assert main(["--data-root", str(data_root), "storage", "status"]) == 0

    output = capsys.readouterr().out
    assert "SDK Manager cache  0 B" in output
    assert "Bases              0 B" in output
    assert "Workspaces         0 B" in output
    assert "Build outputs      0 B" in output
    assert "Tracked total      0 B" in output
    assert output.count("(none)") == 2


@pytest.mark.parametrize(
    ("value", "formatted"),
    (
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
    ),
)
def test_storage_byte_formatter_boundaries(value: int, formatted: str) -> None:
    assert _format_allocated_bytes(value) == formatted


def test_storage_status_corrupt_workspace_metadata_fails_closed(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / "workspaces" / WORKSPACE_A
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text("{broken", encoding="utf-8")

    assert main(["--data-root", str(data_root), "storage", "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "message",
    (
        "privileged storage measurement failed: base unreadable",
        "podman unshare storage measurement failed: workspace unreadable",
    ),
)
def test_storage_measurement_error_is_short_and_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    message: str,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FailingStorageManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root.resolve()

        def status(self) -> StorageStatus:
            raise StorageError(message)

    monkeypatch.setattr("orin_stage.cli.StorageManager", FailingStorageManager)

    assert main(["--data-root", str(data_root), "storage", "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {message}\n"
    assert "Traceback" not in captured.err


def test_storage_status_does_not_mutate_storage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import orin_stage.privileged_storage_measure as privileged_measure

    data_root = tmp_path / "data"
    target = data_root / "targets" / BASE_A
    (target / "base").mkdir(parents=True)
    (target / "base" / "base-file").write_bytes(b"base")
    (target / "materialization").mkdir()
    (target / "materialization" / "seed.tar").write_bytes(b"seed")
    (target / "lock.json").write_text(
        json.dumps({"target": {"canonical_id": "jetson-orin@jp6.2.3"}}),
        encoding="utf-8",
    )
    workspace = data_root / "workspaces" / WORKSPACE_A
    (workspace / "root").mkdir(parents=True)
    (workspace / "root" / "state").write_bytes(b"state")
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_A,
                "workspace_name": "demo",
                "target_lock_digest": BASE_A,
                "base_digest": "b" * 64,
                "generation": 7,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        privileged_measure,
        "measure_base_storage_with_sudo",
        lambda root, digest, **_kwargs: _allocated_tree_bytes(
            root / "targets" / digest
        ),
    )
    monkeypatch.setattr(
        "orin_stage.storage._allocated_shifted_tree_bytes",
        lambda path, **_kwargs: _allocated_tree_bytes(path),
    )

    def snapshot() -> tuple[tuple[str, int, int, int, int, int], ...]:
        return tuple(
            sorted(
                (
                    str(path.relative_to(data_root)),
                    path.lstat().st_mode,
                    path.lstat().st_uid,
                    path.lstat().st_gid,
                    path.lstat().st_size,
                    path.lstat().st_mtime_ns,
                )
                for path in data_root.rglob("*")
            )
        )

    before = snapshot()

    assert main(["--data-root", str(data_root), "storage", "status"]) == 0

    assert snapshot() == before
    assert "demo" in capsys.readouterr().out


def test_base_delete_plan_uses_global_data_root_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "selected-data"
    data_root.mkdir()
    target = data_root / "targets" / BASE_A
    plan = DeletionPlan(
        kind="base",
        identifier=BASE_A,
        path=target,
        bytes_used=1536,
    )
    calls: list[tuple[str, str]] = []

    class FakeStorageManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root.resolve()

        def plan_base_remove(self, digest: str) -> DeletionPlan:
            calls.append(("plan", digest))
            return plan

        def remove_base(self, *_args: object, **_kwargs: object) -> DeletionPlan:
            raise AssertionError("plan-only command must not remove")

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "storage",
                "delete",
                "base",
                BASE_A,
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert calls == [("plan", BASE_A)]
    assert "Type:          base" in captured.out
    assert f"Target ID:     {BASE_A}" in captured.out
    assert f"Path:          {target}" in captured.out
    assert "Current size:  1.5 KiB" in captured.out
    assert "Status:        ready" in captured.out
    assert "Action:        remove immutable target" in captured.out
    assert (
        f"ostg storage delete base {BASE_A} --confirm {BASE_A}"
        in captured.out
    )


def test_blocked_base_plan_shows_dependencies_without_continuation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FakeStorageManager:
        def __init__(self, _data_root: Path) -> None:
            pass

        def plan_base_remove(self, digest: str) -> DeletionPlan:
            return DeletionPlan(
                kind="base",
                identifier=digest,
                path=data_root / "targets" / digest,
                bytes_used=1024,
                blocked_by=("jp623-demo",),
            )

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert main(["--data-root", str(data_root), "storage", "delete", "base", BASE_A]) == 0

    captured = capsys.readouterr()
    assert "Status:        BLOCKED" in captured.out
    assert "Blocked by:    jp623-demo" in captured.out
    assert "Deletion blocked by workspace(s): jp623-demo" in captured.out
    assert "To continue:" not in captured.out
    assert "--confirm" not in captured.out


def test_invalid_base_digest_is_short_error(
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
                "storage",
                "delete",
                "base",
                "not-a-digest",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: target lock digest must be 64 lowercase hexadecimal characters\n"
    )
    assert "Traceback" not in captured.err


def test_wrong_base_confirmation_does_not_construct_storage_manager(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ForbiddenStorageManager:
        def __init__(self, _data_root: Path) -> None:
            raise AssertionError("StorageManager must not be called")

    monkeypatch.setattr("orin_stage.cli.StorageManager", ForbiddenStorageManager)

    assert (
        main(
            [
                "storage",
                "delete",
                "base",
                BASE_A,
                "--confirm",
                BASE_Z,
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: base deletion confirmation must exactly match "
        f"target ID {BASE_A}\n"
    )


def test_exact_base_confirmation_calls_remove_and_prints_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    calls: list[tuple[str, str | None]] = []

    class FakeStorageManager:
        def __init__(self, _data_root: Path) -> None:
            pass

        def remove_base(
            self,
            digest: str,
            *,
            confirmation: str | None,
        ) -> DeletionPlan:
            calls.append((digest, confirmation))
            return DeletionPlan(
                kind="base",
                identifier=digest,
                path=data_root / "targets" / digest,
                bytes_used=2048,
            )

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "storage",
                "delete",
                "base",
                BASE_A,
                "--confirm",
                BASE_A,
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert calls == [(BASE_A, BASE_A)]
    assert f"Target ID:     {BASE_A}" in captured.out
    assert "Removed size:  2.0 KiB" in captured.out
    assert "Action:        removed" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "returned_plan",
    (
        DeletionPlan("workspace", BASE_A, Path("/tmp/target"), 1),
        DeletionPlan("base", BASE_Z, Path("/tmp/target"), 1),
    ),
)
def test_base_remove_identity_mismatch_fails_without_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returned_plan: DeletionPlan,
) -> None:
    class FakeStorageManager:
        def __init__(self, _data_root: Path) -> None:
            pass

        def remove_base(self, *_args: object, **_kwargs: object) -> DeletionPlan:
            return returned_plan

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert main(["storage", "delete", "base", BASE_A, "--confirm", BASE_A]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inconsistent identity evidence" in captured.err
    assert "removed" not in captured.err


@pytest.mark.parametrize(
    "error",
    (
        DeletionBlockedError("base is still referenced by workspace(s): demo"),
        StorageError("privileged storage deletion failed: permission denied"),
    ),
)
def test_base_remove_domain_errors_are_short_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: RuntimeError,
) -> None:
    class FailingStorageManager:
        def __init__(self, _data_root: Path) -> None:
            pass

        def remove_base(self, *_args: object, **_kwargs: object) -> DeletionPlan:
            raise error

    monkeypatch.setattr("orin_stage.cli.StorageManager", FailingStorageManager)

    assert main(["storage", "delete", "base", BASE_A, "--confirm", BASE_A]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {error}\n"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("metadata_state", ("missing", "corrupt", "symlink"))
def test_base_plan_workspace_metadata_failures_are_short_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    metadata_state: str,
) -> None:
    import orin_stage.privileged_storage_measure as privileged_measure

    data_root = tmp_path / "data"
    target = data_root / "targets" / BASE_A
    target.mkdir(parents=True)
    workspace = data_root / "workspaces" / WORKSPACE_A
    if metadata_state == "symlink":
        outside = tmp_path / "outside-workspace"
        outside.mkdir()
        workspace.parent.mkdir()
        workspace.symlink_to(outside, target_is_directory=True)
    else:
        workspace.mkdir(parents=True)
        if metadata_state == "corrupt":
            (workspace / "workspace.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        privileged_measure,
        "measure_base_storage_with_sudo",
        lambda *_args, **_kwargs: 0,
    )

    assert main(["--data-root", str(data_root), "storage", "delete", "base", BASE_A]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert target.is_dir()


def test_sdkm_cache_plan_shows_token_path_size_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    downloads = data_root / "sdkm" / "downloads"
    calls: list[str] = []

    class FakeStorageManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root.resolve()

        def plan_sdkm_cache_remove(self) -> DeletionPlan:
            calls.append("plan")
            return DeletionPlan(
                kind="sdkm-cache",
                identifier="sdkm-downloads",
                path=downloads,
                bytes_used=4096,
            )

        def remove_sdkm_cache(self, **_kwargs: object) -> DeletionPlan:
            raise AssertionError("plan-only command must not remove")

    monkeypatch.setattr("orin_stage.cli.StorageManager", FakeStorageManager)

    assert main(["--data-root", str(data_root), "storage", "delete", "sdkm-cache"]) == 0

    captured = capsys.readouterr()
    assert calls == ["plan"]
    assert "Type:          sdkm-cache" in captured.out
    assert "Confirmation:  sdkm-downloads" in captured.out
    assert f"Path:          {downloads}" in captured.out
    assert "Current size:  4.0 KiB" in captured.out
    assert "Action:        clear SDK Manager download cache" in captured.out
    assert "ostg storage delete sdkm-cache --confirm sdkm-downloads" in captured.out
    assert captured.err == ""


def test_wrong_sdkm_confirmation_does_not_construct_storage_manager(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ForbiddenStorageManager:
        def __init__(self, _data_root: Path) -> None:
            raise AssertionError("StorageManager must not be called")

    monkeypatch.setattr("orin_stage.cli.StorageManager", ForbiddenStorageManager)

    assert main(["storage", "delete", "sdkm-cache", "--confirm", "wrong"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: SDK Manager cache deletion confirmation must exactly match "
        "sdkm-downloads\n"
    )


def test_exact_sdkm_confirmation_clears_only_downloads_and_keeps_evidence(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    artifact = data_root / "sdkm" / "downloads" / "artifact.tbz2"
    receipt = data_root / "sdkm" / "receipts" / BASE_A / "receipt.json"
    response = data_root / "sdkm" / "responses" / "jp623.json"
    artifact.parent.mkdir(parents=True)
    receipt.parent.mkdir(parents=True)
    response.parent.mkdir(parents=True)
    artifact.write_bytes(b"artifact")
    receipt.write_text("{}", encoding="utf-8")
    response.write_text("{}", encoding="utf-8")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "storage",
                "delete",
                "sdkm-cache",
                "--confirm",
                "sdkm-downloads",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Type:          sdkm-cache" in captured.out
    assert "Action:        removed" in captured.out
    assert captured.err == ""
    assert not artifact.exists()
    assert artifact.parent.is_dir()
    assert receipt.is_file()
    assert response.is_file()
    assert (data_root / "sdkm" / ".acquisition.lock").is_file()
