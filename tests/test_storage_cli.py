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


def test_storage_help_contains_only_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["storage", "--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "status" in output
    for excluded in ("delete", "clean", "prune", "gc"):
        assert excluded not in output


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
