from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import orin_stage.build_toolchain as module
from orin_stage.build_identity import (
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
    JP6_BOOTLIN_TOOLCHAIN_PREFIX,
)
from orin_stage.build_toolchain import BuildToolchainError, BuildToolchainManager


def _add_file(
    bundle: tarfile.TarFile,
    name: str,
    content: bytes,
    *,
    mode: int = 0o644,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = mode
    bundle.addfile(member, io.BytesIO(content))


def _toolchain_archive() -> bytes:
    stream = io.BytesIO()
    prefix = "aarch64--glibc--stable-2022.08-1"
    with tarfile.open(fileobj=stream, mode="w:bz2") as bundle:
        _add_file(
            bundle,
            f"{prefix}/bin/{JP6_BOOTLIN_TOOLCHAIN_PREFIX}gcc",
            b"#!/bin/sh\nexit 0\n",
            mode=0o755,
        )
        _add_file(
            bundle,
            f"{prefix}/bin/{JP6_BOOTLIN_TOOLCHAIN_PREFIX}ld",
            b"#!/bin/sh\nexit 0\n",
            mode=0o755,
        )
    return stream.getvalue()


def _archive_with_member(member: tarfile.TarInfo, content: bytes = b"bad") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:bz2") as bundle:
        if member.isfile():
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        else:
            bundle.addfile(member)
    return stream.getvalue()


def _patch_archive_identity(
    monkeypatch: pytest.MonkeyPatch,
    archive: bytes,
) -> None:
    monkeypatch.setattr(
        module,
        "JP6_BUILD_IDENTITY",
        replace(
            module.JP6_BUILD_IDENTITY,
            toolchain_archive_sha256=hashlib.sha256(archive).hexdigest(),
        ),
    )


def _urlopen(archive: bytes, calls: list[tuple[str, int]]):
    def open_archive(url: str, *, timeout: int):
        calls.append((url, timeout))
        return io.BytesIO(archive)

    return open_archive


def _version_runner(
    command: tuple[str, ...],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    executable = Path(command[0]).name
    if executable.endswith("gcc"):
        return subprocess.CompletedProcess(command, 0, "11.3.0\n", "")
    if executable.endswith("ld"):
        return subprocess.CompletedProcess(
            command,
            0,
            "GNU ld (GNU Binutils) 2.38\nCopyright\n",
            "",
        )
    raise AssertionError(f"unexpected version command: {command}")


def _ensure(
    manager: BuildToolchainManager,
    archive: bytes,
    calls: list[tuple[str, int]],
):
    return manager.ensure(
        urlopen=_urlopen(archive, calls),  # type: ignore[arg-type]
        runner=_version_runner,
    )


def test_valid_toolchain_is_published_under_build_identity_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")
    calls: list[tuple[str, int]] = []

    record = _ensure(manager, archive, calls)

    assert not record.reused
    assert record.toolchain_path == (
        manager.data_root / "build" / "toolchains" / manager.build_identity_digest
    )
    assert record.root_path == record.toolchain_path / "root"
    assert record.receipt_path == record.toolchain_path / "receipt.json"
    assert (record.root_path / "bin" / f"{JP6_BOOTLIN_TOOLCHAIN_PREFIX}gcc").is_file()
    assert (record.root_path / "bin" / f"{JP6_BOOTLIN_TOOLCHAIN_PREFIX}ld").is_file()
    receipt = json.loads(record.receipt_path.read_text(encoding="utf-8"))
    assert receipt["build_identity_digest"] == manager.build_identity_digest
    assert receipt["archive"] == {
        "filename": JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME,
        "sha256": hashlib.sha256(archive).hexdigest(),
        "url": JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
    }
    assert calls == [(JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL, 60)]


def test_valid_receipt_and_root_are_reused_without_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")
    calls: list[tuple[str, int]] = []
    first = _ensure(manager, archive, calls)

    def forbidden_download(*args: object, **kwargs: object):
        raise AssertionError("cache reuse must not download")

    second = manager.ensure(
        urlopen=forbidden_download,  # type: ignore[arg-type]
        runner=_version_runner,
    )

    assert not first.reused
    assert second.reused
    assert second.root_path == first.root_path
    assert len(calls) == 1


def test_sha_mismatch_leaves_no_published_toolchain(tmp_path: Path) -> None:
    manager = BuildToolchainManager(tmp_path / "data")
    calls: list[tuple[str, int]] = []

    with pytest.raises(BuildToolchainError, match="SHA-256 mismatch"):
        _ensure(manager, b"not the official archive", calls)

    assert not manager.toolchain_path.exists()
    assert list((manager.data_root / "build" / "staging").iterdir()) == []


def test_corrupt_archive_leaves_no_published_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = b"correct digest but not a bzip2 tar"
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")

    with pytest.raises(BuildToolchainError, match="extract"):
        _ensure(manager, archive, [])

    assert not manager.toolchain_path.exists()


@pytest.mark.parametrize("attack", ["path", "symlink"])
def test_archive_escape_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attack: str,
) -> None:
    if attack == "path":
        member = tarfile.TarInfo("../escaped")
    else:
        member = tarfile.TarInfo("toolchain/escape")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../escaped"
    archive = _archive_with_member(member)
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")

    with pytest.raises(BuildToolchainError, match="escapes extraction root"):
        _ensure(manager, archive, [])

    assert not (tmp_path / "escaped").exists()
    assert not manager.toolchain_path.exists()


@pytest.mark.parametrize(("wrong_tool", "expected_error"), [("gcc", "GCC"), ("ld", "Binutils")])
def test_wrong_compiler_or_binutils_version_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wrong_tool: str,
    expected_error: str,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")

    def wrong_version(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        executable = Path(command[0]).name
        if executable.endswith(wrong_tool):
            output = "99.0.0\n" if wrong_tool == "gcc" else "GNU ld 99.0\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        return _version_runner(command, **kwargs)

    with pytest.raises(BuildToolchainError, match=f"{expected_error} version mismatch"):
        manager.ensure(
            urlopen=_urlopen(archive, []),  # type: ignore[arg-type]
            runner=wrong_version,
        )

    assert not manager.toolchain_path.exists()


def test_concurrent_ensure_downloads_and_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    data_root = tmp_path / "data"
    calls: list[tuple[str, int]] = []

    def acquire():
        return _ensure(BuildToolchainManager(data_root), archive, calls)

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _index: acquire(), range(2)))

    assert sorted(record.reused for record in records) == [False, True]
    assert len(calls) == 1
    assert records[0].toolchain_path == records[1].toolchain_path


def test_failed_final_publish_never_exposes_partial_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")
    real_replace = module.os.replace

    def fail_final_publish(source: Path, destination: Path) -> None:
        if Path(destination) == manager.toolchain_path:
            assert (Path(source) / "root").is_dir()
            assert (Path(source) / "receipt.json").is_file()
            assert not os.path.lexists(destination)
            raise OSError("simulated publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_final_publish)

    with pytest.raises(BuildToolchainError, match="simulated publish failure"):
        _ensure(manager, archive, [])

    assert not os.path.lexists(manager.toolchain_path)
    assert list((manager.data_root / "build" / "staging").iterdir()) == []


def test_final_destination_appears_only_after_complete_atomic_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _toolchain_archive()
    _patch_archive_identity(monkeypatch, archive)
    manager = BuildToolchainManager(tmp_path / "data")
    real_replace = module.os.replace
    final_publications: list[tuple[Path, Path]] = []

    def observe_publish(source: Path, destination: Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == manager.toolchain_path:
            assert not os.path.lexists(destination_path)
            assert (source_path / "root").is_dir()
            assert (source_path / "receipt.json").is_file()
            final_publications.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", observe_publish)

    record = _ensure(manager, archive, [])

    assert len(final_publications) == 1
    assert record.toolchain_path.is_dir()
    assert record.receipt_path.is_file()
