from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orin_stage.base import cleanup as cleanup_module
from orin_stage.base.cleanup import (
    JP623_CANONICAL_ID,
    inspect_jp623_base_attempts,
    remove_jp623_base_attempts,
    write_construction_lease,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "clean_jp623_base_attempts.py"


def _jp623_lock(path: Path, canonical_id: str = JP623_CANONICAL_ID) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "target": {"canonical_id": canonical_id}}),
        encoding="utf-8",
    )


def test_cleanup_tool_dry_run_then_apply_preserves_sdk_manager_cache(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    stale = data_root / "staging" / ".base-jp623-stale"
    stale.mkdir(parents=True)
    (stale / "partial").write_text("incomplete", encoding="utf-8")
    download = data_root / "sdkm" / "downloads" / "bsp.tbz2"
    receipt = data_root / "sdkm" / "receipts" / ("e" * 64) / "receipt.json"
    download.parent.mkdir(parents=True)
    receipt.parent.mkdir(parents=True)
    download.write_text("bsp", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")

    dry_run = subprocess.run(
        [sys.executable, str(TOOL), "--data-root", str(data_root), "--dry-run"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "WOULD-REMOVE" in dry_run.stdout
    assert stale.is_dir()

    applied = subprocess.run(
        [sys.executable, str(TOOL), "--data-root", str(data_root), "--apply"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "REMOVED" in applied.stdout
    assert not stale.exists()
    assert download.read_text(encoding="utf-8") == "bsp"
    assert receipt.read_text(encoding="utf-8") == "receipt"


def test_cleanup_protects_active_staging_and_valid_published_base(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    active = data_root / "staging" / ".base-jp623-active"
    active.mkdir(parents=True)
    write_construction_lease(active)
    valid = data_root / "targets" / ("a" * 64)
    valid.mkdir(parents=True)
    _jp623_lock(valid / "lock.json")
    monkeypatch.setattr(
        cleanup_module,
        "base_directory_is_reusable",
        lambda path: Path(path) == valid,
    )

    inspection = inspect_jp623_base_attempts(data_root)
    protected = {entry.path for entry in inspection.protected}
    assert active in protected
    assert valid in protected
    assert not inspection.removable
    assert not remove_jp623_base_attempts(data_root)
    assert active.is_dir()
    assert valid.is_dir()


def test_cleanup_detects_incomplete_jp623_target_but_ignores_other_target(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    incomplete = data_root / "targets" / ("1" * 64)
    other = data_root / "targets" / ("2" * 64)
    incomplete.mkdir(parents=True)
    other.mkdir(parents=True)
    _jp623_lock(incomplete / "lock.json")
    _jp623_lock(other / "lock.json", "nvidia.jetpack-6.2.2.jetson-linux-36.5")
    (incomplete / "manifest.json").write_text("{broken", encoding="utf-8")

    inspection = inspect_jp623_base_attempts(data_root)
    assert [entry.path for entry in inspection.removable] == [incomplete]
    assert "missing base/" in inspection.removable[0].reason
    assert "malformed manifest.json" in inspection.removable[0].reason

    removed = remove_jp623_base_attempts(data_root)
    assert [entry.path for entry in removed] == [incomplete]
    assert not incomplete.exists()
    assert other.is_dir()


def test_cleanup_does_not_follow_staging_or_target_symlinks(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside_staging = tmp_path / "outside-staging"
    outside_targets = tmp_path / "outside-targets"
    stale = outside_staging / ".base-jp623-stale"
    incomplete = outside_targets / ("3" * 64)
    stale.mkdir(parents=True)
    incomplete.mkdir(parents=True)
    _jp623_lock(incomplete / "lock.json")
    data_root.mkdir()
    (data_root / "staging").symlink_to(outside_staging, target_is_directory=True)
    (data_root / "targets").symlink_to(outside_targets, target_is_directory=True)

    assert not inspect_jp623_base_attempts(data_root).removable
    assert not remove_jp623_base_attempts(data_root)
    assert stale.is_dir()
    assert incomplete.is_dir()
