#!/usr/bin/env python3
"""Materialize deterministic Debian upstream and ``deps`` source components."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = REPO_ROOT / "release" / "dependencies"
VERSION = "0.1.0"
PACKAGE = "orin-stage"
ROOT_NAME = f"{PACKAGE}-{VERSION}"
DEFAULT_SOURCE_CACHE = DEPENDENCIES / "downloads"
DEFAULT_VENDOR = DEPENDENCIES / "generated" / "cargo-vendor"
DEFAULT_CARGO_CONFIG = DEPENDENCIES / "generated" / ".cargo" / "config.toml"
DEFAULT_OUTPUT = DEPENDENCIES / "generated" / "debian-source"
RELEASE_HELPERS = (
    Path("tools/release/prepare_debian_source.py"),
    Path("tools/release/write_debian_substvars.py"),
)


class DebianSourceError(RuntimeError):
    """Raised when the exact source-package input contract is violated."""


def _load(name: str, filename: str):
    path = REPO_ROOT / "tools" / "release" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise DebianSourceError(f"cannot load release helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise DebianSourceError(f"non-exact lock entry: {line}")
        name, version = line.split("==", 1)
        if not name or not version or name in result:
            raise DebianSourceError(f"invalid lock entry: {line}")
        result[name] = version
    return result


def _verified_sdists(manifest_path: Path, lock_path: Path, source_cache: Path) -> tuple[Path, ...]:
    fetcher = _load("orin_stage_dependency_fetch", "fetch_dependency_sources.py")
    try:
        entries = fetcher.load_manifest(manifest_path)
    except Exception as exc:
        raise DebianSourceError(f"cannot read source manifest {manifest_path}: {exc}") from exc
    locked = _read_lock(lock_path)
    if {entry.name: entry.version for entry in entries} != locked:
        raise DebianSourceError(f"manifest does not exactly match {lock_path.name}")
    result: list[Path] = []
    for entry in entries:
        path = source_cache / entry.filename
        try:
            fetcher._verify(entry, path)
        except Exception as exc:
            raise DebianSourceError(f"sdist verification failed for {entry.filename}: {exc}") from exc
        result.append(path)
    return tuple(result)


def verify_inputs(source_cache: Path, vendor: Path, cargo_config: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    runtime = _verified_sdists(
        DEPENDENCIES / "sources.lock.json", DEPENDENCIES / "runtime.lock", source_cache
    )
    build = _verified_sdists(
        DEPENDENCIES / "build-sources.lock.json", DEPENDENCIES / "build-tools.lock", source_cache
    )
    cargo = _load("orin_stage_cargo_fetch", "fetch_cargo_sources.py")
    try:
        entries = cargo.load_manifest(DEPENDENCIES / "cargo-sources.lock.json")
        tree_sha256, file_count, content_bytes = cargo.verify_vendor(entries, vendor)
        expected = json.loads((DEPENDENCIES / "cargo-vendor.lock.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise DebianSourceError(f"Cargo vendor verification failed: {exc}") from exc
    config_sha256 = _digest(cargo_config)[0]
    actual = {
        "crate_count": len(entries),
        "file_count": file_count,
        "uncompressed_content_bytes": content_bytes,
        "tree_sha256": tree_sha256,
        "config_sha256": config_sha256,
    }
    for field, value in actual.items():
        if expected.get(field) != value:
            raise DebianSourceError(
                f"cargo-vendor.lock mismatch for {field}: {value!r} != {expected.get(field)!r}"
            )
    return runtime, build


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def _tracked_files() -> tuple[Path, ...]:
    completed = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "ls-files", "-z"),
        check=True,
        capture_output=True,
    )
    paths = tuple(Path(item.decode("utf-8")) for item in completed.stdout.split(b"\0") if item)
    if not paths:
        raise DebianSourceError("git tracked source set is empty")
    return paths


def _copy_project(destination: Path, *, include_debian: bool) -> None:
    files = set(_tracked_files()) | set(RELEASE_HELPERS)
    if include_debian:
        debian = REPO_ROOT / "debian"
        if not debian.is_dir():
            raise DebianSourceError("debian packaging directory is absent")
        files.update(path.relative_to(REPO_ROOT) for path in debian.rglob("*") if path.is_file())
    for relative in sorted(files):
        if relative.parts[0] == "debian" and not include_debian:
            continue
        source = REPO_ROOT / relative
        if not source.is_file() or source.is_symlink():
            raise DebianSourceError(f"tracked source is not a regular file: {relative}")
        _copy_file(source, destination / relative)


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def _write_tarball(output: Path, root: Path, members: Iterable[Path], *, prefix: str = "") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:xz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(members, key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise DebianSourceError(f"source component contains symlink: {path}")
            relative = path.relative_to(root).as_posix()
            arcname = f"{prefix}{relative}" if prefix else relative
            archive.add(path, arcname=arcname, recursive=False, filter=_tar_filter)


def _all_component_members(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in root.rglob("*") if path.is_file())


def materialize(
    *,
    source_cache: Path,
    vendor: Path,
    cargo_config: Path,
    output: Path,
) -> Path:
    output = output.resolve()
    if output.exists():
        raise DebianSourceError(f"output directory already exists: {output}")
    runtime_sdists, build_sdists = verify_inputs(source_cache, vendor, cargo_config)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", suffix=".partial", dir=output.parent))
    try:
        project = stage / ROOT_NAME
        project.mkdir()
        _copy_project(project, include_debian=True)
        deps = project / "deps"
        for dirname, sources in (("runtime-sdists", runtime_sdists), ("build-sdists", build_sdists)):
            target = deps / dirname
            target.mkdir(parents=True)
            for source in sources:
                _copy_file(source, target / source.name)
        shutil.copytree(vendor, deps / "cargo-vendor", symlinks=False)
        _copy_file(cargo_config, deps / "cargo-config.toml")

        orig_root = stage / "orig"
        orig_root.mkdir()
        _copy_project(orig_root / ROOT_NAME, include_debian=False)
        _write_tarball(
            stage / f"{PACKAGE}_{VERSION}.orig.tar.xz",
            orig_root / ROOT_NAME,
            _all_component_members(orig_root / ROOT_NAME),
            prefix=f"{ROOT_NAME}/",
        )
        _write_tarball(
            stage / f"{PACKAGE}_{VERSION}.orig-deps.tar.xz",
            deps,
            _all_component_members(deps),
        )
        shutil.rmtree(orig_root)
        os.replace(stage, output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-cache", type=Path, default=DEFAULT_SOURCE_CACHE)
    parser.add_argument("--vendor", type=Path, default=DEFAULT_VENDOR)
    parser.add_argument("--cargo-config", type=Path, default=DEFAULT_CARGO_CONFIG)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = materialize(
            source_cache=args.source_cache,
            vendor=args.vendor,
            cargo_config=args.cargo_config,
            output=args.output,
        )
    except DebianSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"COMPLETE source_root={output / ROOT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
