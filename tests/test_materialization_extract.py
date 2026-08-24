from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orin_stage.materialization_extract import (
    ExtractionReport,
    MaterializationExtractionError,
    _read_expected_paths,
    _validate_extraction,
    extract_and_validate_in_namespace,
    extract_materialization_seed,
)
from orin_stage.materialization_seed import create_materialization_seed


TARGET_LOCK_DIGEST = "c" * 64
BASE_DIGEST = "d" * 64


def _seed_target(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    target = data_root / "targets" / TARGET_LOCK_DIGEST
    base = target / "base"
    directory = base / "directory"
    directory.mkdir(parents=True)
    directory.chmod(0o750)
    first = directory / "first"
    first.write_bytes(b"hardlinked payload")
    first.chmod(0o640)
    os.link(first, directory / "second")
    (base / "link").symlink_to("directory/first")
    (target / "manifest.json").write_text("{}\n", encoding="utf-8")
    (target / "receipt.json").write_text(
        json.dumps(
            {
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
            }
        ),
        encoding="utf-8",
    )
    create_materialization_seed(target)
    return data_root, target


def test_gnu_tar_extraction_and_header_parity_validation(tmp_path: Path) -> None:
    _, target = _seed_target(tmp_path)
    staging_root = tmp_path / "staging" / "attempt" / "root"
    staging_root.mkdir(parents=True)

    report = extract_and_validate_in_namespace(
        target / "materialization", staging_root
    )

    assert report == ExtractionReport(
        path_count=5,
        uid_parity=5,
        gid_parity=5,
        mode_parity=5,
        hardlink_parity=1,
        symlink_parity=1,
        staging_path=str(staging_root),
    )
    first = staging_root / "directory" / "first"
    second = staging_root / "directory" / "second"
    assert (first.stat().st_dev, first.stat().st_ino) == (
        second.stat().st_dev,
        second.stat().st_ino,
    )
    assert (staging_root / "link").is_symlink()
    assert os.readlink(staging_root / "link") == "directory/first"


def test_seed_sha_mismatch_fails_before_tar_extraction(tmp_path: Path) -> None:
    _, target = _seed_target(tmp_path)
    archive = target / "materialization" / "seed.tar"
    with archive.open("ab") as handle:
        handle.write(b"tampered")
    staging_root = tmp_path / "staging" / "root"
    staging_root.mkdir(parents=True)

    with pytest.raises(MaterializationExtractionError, match="SHA-256 mismatch"):
        extract_and_validate_in_namespace(target / "materialization", staging_root)

    assert not list(staging_root.iterdir())


def test_outer_operation_uses_one_podman_unshare_worker(tmp_path: Path) -> None:
    data_root, target = _seed_target(tmp_path)
    observed_command: list[str] = []

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed_command.extend(command)
        root = command[command.index("--staging-root") + 1]
        report = ExtractionReport(5, 5, 5, 5, 1, 1, root)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(report.to_dict()),
            stderr="",
        )

    report = extract_materialization_seed(data_root, target, runner=runner)

    assert observed_command[:2] == ["podman", "unshare"]
    assert observed_command[2:5] == [
        sys.executable,
        "-m",
        "orin_stage.materialization_extract",
    ]
    assert Path(report.staging_path).is_dir()
    assert Path(report.staging_path).parent.parent == data_root / "staging"


@pytest.mark.parametrize("broken_parity", ["mode", "hardlink", "symlink"])
def test_validator_rejects_staging_parity_mismatch(
    tmp_path: Path,
    broken_parity: str,
) -> None:
    _, target = _seed_target(tmp_path)
    staging_root = tmp_path / "staging" / "root"
    staging_root.mkdir(parents=True)
    seed_dir = target / "materialization"
    extract_and_validate_in_namespace(seed_dir, staging_root)
    first = staging_root / "directory" / "first"
    second = staging_root / "directory" / "second"
    link = staging_root / "link"

    if broken_parity == "mode":
        first.chmod(0o600)
    elif broken_parity == "hardlink":
        second.unlink()
        shutil.copyfile(first, second)
        second.chmod(0o640)
    else:
        link.unlink()
        link.symlink_to("wrong-target")

    expected = _read_expected_paths(seed_dir / "seed.tar")
    with pytest.raises(
        MaterializationExtractionError,
        match=f"{broken_parity} parity failed",
    ):
        _validate_extraction(expected, staging_root)
