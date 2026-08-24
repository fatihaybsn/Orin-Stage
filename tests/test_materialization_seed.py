from __future__ import annotations

import hashlib
import json
import os
import tarfile
from pathlib import Path

import pytest

from orin_stage.materialization_seed import (
    MaterializationSeedError,
    create_materialization_seed,
)


TARGET_LOCK_DIGEST = "a" * 64
BASE_DIGEST = "b" * 64


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "targets" / TARGET_LOCK_DIGEST
    base = target / "base"
    base.mkdir(parents=True)
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
    return target


def _member(archive: Path, suffix: str) -> tarfile.TarInfo:
    with tarfile.open(archive, "r:") as handle:
        return next(
            member for member in handle.getmembers() if member.name.endswith(suffix)
        )


def test_seed_and_minimal_metadata_are_created_with_matching_sha256(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    base = target / "base"
    (base / "payload").write_bytes(b"payload\x00bytes")
    for runtime in ("proc", "sys", "dev"):
        runtime_dir = base / runtime
        runtime_dir.mkdir()
        (runtime_dir / "runtime-only").write_text("excluded", encoding="utf-8")

    result = create_materialization_seed(target)

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    actual_sha256 = hashlib.sha256(result.archive_path.read_bytes()).hexdigest()
    assert metadata == {
        "format": "gnu-tar",
        "format_version": 1,
        "target_lock_digest": TARGET_LOCK_DIGEST,
        "base_digest": BASE_DIGEST,
        "seed_sha256": actual_sha256,
        "archive": "seed.tar",
    }
    assert result.seed_sha256 == actual_sha256
    with tarfile.open(result.archive_path, "r:") as handle:
        names = {member.name.removeprefix("./") for member in handle.getmembers()}
        payload = next(
            member for member in handle.getmembers() if member.name.endswith("payload")
        )
        archived_payload = handle.extractfile(payload)
        assert archived_payload is not None
        assert archived_payload.read() == b"payload\x00bytes"
    payload_stat = (base / "payload").stat()
    assert payload.uid == payload_stat.st_uid
    assert payload.gid == payload_stat.st_gid
    assert payload.mode == (payload_stat.st_mode & 0o7777)
    assert {"proc", "sys", "dev"}.issubset(names)
    assert not any(name.endswith("runtime-only") for name in names)


def test_seed_preserves_hardlink_relationship(tmp_path: Path) -> None:
    target = _target(tmp_path)
    first = target / "base" / "first"
    second = target / "base" / "second"
    first.write_text("shared inode", encoding="utf-8")
    os.link(first, second)

    archive = create_materialization_seed(target).archive_path

    members = [_member(archive, "first"), _member(archive, "second")]
    assert sum(member.isreg() for member in members) == 1
    assert sum(member.islnk() for member in members) == 1


def test_seed_does_not_dereference_symlinks(tmp_path: Path) -> None:
    target = _target(tmp_path)
    (target / "base" / "destination").write_text("content", encoding="utf-8")
    (target / "base" / "link").symlink_to("destination")

    archive = create_materialization_seed(target).archive_path

    link = _member(archive, "link")
    assert link.issym()
    assert link.linkname == "destination"


@pytest.mark.parametrize("metadata_kind", ["xattr", "sparse", "fifo"])
def test_seed_rejects_unsupported_metadata(
    tmp_path: Path,
    metadata_kind: str,
) -> None:
    target = _target(tmp_path)
    path = target / "base" / metadata_kind
    if metadata_kind == "xattr":
        path.touch()
        os.setxattr(path, "user.orin_stage_test", b"present")
    elif metadata_kind == "sparse":
        with path.open("wb") as handle:
            handle.seek(1024 * 1024)
            handle.write(b"x")
    else:
        os.mkfifo(path)

    with pytest.raises(
        MaterializationSeedError,
        match="unsupported materialization metadata",
    ):
        create_materialization_seed(target)

    assert not (target / "materialization").exists()
