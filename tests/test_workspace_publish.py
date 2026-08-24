from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.materialization_extract import (
    ExtractionReport,
    MaterializationExtractionError,
)
from orin_stage.workspace_publish import (
    WorkspacePublishError,
    publish_materialization_workspace,
)


TARGET_LOCK_DIGEST = "e" * 64
BASE_DIGEST = "f" * 64
WORKSPACE_ID = "0123456789abcdef0123456789abcdef"


def _target(tmp_path: Path) -> tuple[Path, Path]:
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
    return data_root, target


def _staging(data_root: Path, attempt: str = "attempt") -> Path:
    root = data_root / "staging" / attempt / "root"
    root.mkdir(parents=True)
    (root / "payload").write_text("workspace payload", encoding="utf-8")
    return root


def _report(root: Path) -> ExtractionReport:
    return ExtractionReport(2, 2, 2, 2, 0, 0, str(root))


def test_successful_staging_is_published_with_minimal_metadata(
    tmp_path: Path,
) -> None:
    data_root, target = _target(tmp_path)

    def extractor(data: Path, selected_target: Path) -> ExtractionReport:
        assert data == data_root
        assert selected_target == target
        return _report(_staging(data_root))

    result = publish_materialization_workspace(
        data_root,
        target,
        "developer-workspace",
        workspace_id_factory=lambda: WORKSPACE_ID,
        extractor=extractor,
    )

    expected_path = data_root / "workspaces" / WORKSPACE_ID
    assert result.workspace_path == expected_path
    assert not result.reused_staging
    assert (expected_path / "root" / "payload").read_text(encoding="utf-8") == (
        "workspace payload"
    )
    assert json.loads((expected_path / "workspace.json").read_text()) == {
        "format_version": 1,
        "workspace_id": WORKSPACE_ID,
        "workspace_name": "developer-workspace",
        "target_lock_digest": TARGET_LOCK_DIGEST,
        "base_digest": BASE_DIGEST,
        "generation": 0,
    }
    assert not (data_root / "staging" / "attempt").exists()


def test_existing_staging_can_be_revalidated_without_extraction(
    tmp_path: Path,
) -> None:
    data_root, target = _target(tmp_path)
    root = _staging(data_root)
    validated = False

    def extractor(data: Path, selected_target: Path) -> ExtractionReport:
        raise AssertionError("existing staging must not be extracted again")

    def validator(seed_dir: Path, staging_root: Path) -> ExtractionReport:
        nonlocal validated
        validated = True
        assert seed_dir == target / "materialization"
        assert staging_root == root
        return _report(root)

    result = publish_materialization_workspace(
        data_root,
        target,
        "reuse-me",
        staging_root=root,
        workspace_id_factory=lambda: WORKSPACE_ID,
        extractor=extractor,
        validator=validator,
    )

    assert validated
    assert result.reused_staging
    assert result.workspace_path.is_dir()


def test_parity_failure_does_not_publish_workspace(tmp_path: Path) -> None:
    data_root, target = _target(tmp_path)
    root = _staging(data_root)

    def extractor(data: Path, selected_target: Path) -> ExtractionReport:
        raise MaterializationExtractionError("mode parity failed")

    with pytest.raises(MaterializationExtractionError, match="parity failed"):
        publish_materialization_workspace(
            data_root,
            target,
            "must-not-publish",
            workspace_id_factory=lambda: WORKSPACE_ID,
            extractor=extractor,
        )

    assert root.is_dir()
    assert not (root.parent / "workspace.json").exists()
    assert not (data_root / "workspaces").exists()


def test_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    data_root, target = _target(tmp_path)
    destination = data_root / "workspaces" / WORKSPACE_ID
    destination.mkdir(parents=True)
    sentinel = destination / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    def extractor(data: Path, selected_target: Path) -> ExtractionReport:
        raise AssertionError("duplicate ID must fail before extraction")

    with pytest.raises(WorkspacePublishError, match="ID already exists"):
        publish_materialization_workspace(
            data_root,
            target,
            "duplicate-id",
            workspace_id_factory=lambda: WORKSPACE_ID,
            extractor=extractor,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_atomic_publish_does_not_replace_racing_destination(tmp_path: Path) -> None:
    data_root, target = _target(tmp_path)
    destination = data_root / "workspaces" / WORKSPACE_ID

    def extractor(data: Path, selected_target: Path) -> ExtractionReport:
        root = _staging(data_root)
        destination.mkdir(parents=True)
        (destination / "sentinel").write_text("keep", encoding="utf-8")
        return _report(root)

    with pytest.raises(WorkspacePublishError, match="destination already exists"):
        publish_materialization_workspace(
            data_root,
            target,
            "racing-destination",
            workspace_id_factory=lambda: WORKSPACE_ID,
            extractor=extractor,
        )

    assert (destination / "sentinel").read_text(encoding="utf-8") == "keep"
    assert (data_root / "staging" / "attempt" / "root").is_dir()


def test_existing_workspace_name_is_rejected(tmp_path: Path) -> None:
    data_root, target = _target(tmp_path)
    existing = data_root / "workspaces" / "another-id"
    existing.mkdir(parents=True)
    (existing / "workspace.json").write_text(
        json.dumps({"workspace_name": "already-named"}),
        encoding="utf-8",
    )

    with pytest.raises(WorkspacePublishError, match="name already exists"):
        publish_materialization_workspace(
            data_root,
            target,
            "already-named",
            workspace_id_factory=lambda: WORKSPACE_ID,
        )


@pytest.mark.parametrize(
    "workspace_name",
    ["", "   ", "..", "../escape", "nested/name", "name..suffix"],
)
def test_path_traversal_workspace_name_is_rejected(
    tmp_path: Path,
    workspace_name: str,
) -> None:
    with pytest.raises(WorkspacePublishError):
        publish_materialization_workspace(
            tmp_path / "data",
            tmp_path / "target",
            workspace_name,
        )
