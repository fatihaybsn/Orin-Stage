from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Sequence, TextIO


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = REPO_ROOT / "release" / "dependencies"
DEFAULT_MANIFEST = DEPENDENCIES / "cargo-sources.lock.json"
DEFAULT_CACHE = DEPENDENCIES / "cargo-downloads"
DEFAULT_VENDOR = DEPENDENCIES / "generated" / "cargo-vendor"
DEFAULT_CONFIG = DEPENDENCIES / "generated" / ".cargo" / "config.toml"
DEFAULT_INVENTORY = DEPENDENCIES / "CARGO_THIRD_PARTY.json"
DEFAULT_VENDOR_LOCK = DEPENDENCIES / "cargo-vendor.lock.json"
CRATES_IO_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
SOURCE_FIELDS = {
    "name",
    "version",
    "source",
    "filename",
    "url",
    "sha256",
    "consumers",
}
LOCK_FIELDS = {
    "consumer",
    "sdist_filename",
    "sdist_sha256",
    "member",
    "lock_filename",
    "lock_sha256",
    "registry_packages",
    "local_packages",
}
CONSUMERS = {"maturin", "rpds-py"}
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")


class CargoSourceError(RuntimeError):
    """Raised when locked Cargo source acquisition is unsafe or inconsistent."""


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    source: str
    sha256: str
    consumer: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.name, self.version, self.source


@dataclass(frozen=True)
class LockAnalysis:
    registry: tuple[LockedPackage, ...]
    local_count: int


@dataclass(frozen=True)
class CargoSource:
    name: str
    version: str
    source: str
    filename: str
    url: str
    sha256: str
    consumers: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.name, self.version, self.source

    @property
    def vendor_directory(self) -> str:
        return f"{self.name}-{self.version}"


@dataclass(frozen=True)
class FetchResult:
    entry: CargoSource
    path: Path
    size: int
    reused: bool


Opener = Callable[..., BinaryIO]


def _toml_string(line: str, key: str) -> str | None:
    match = re.fullmatch(rf"{re.escape(key)}\s*=\s*(\"(?:[^\"\\]|\\.)*\")", line)
    if match is None:
        return None
    value = json.loads(match.group(1))
    if not isinstance(value, str):
        raise CargoSourceError(f"Cargo metadata {key} is not a string")
    return value


def parse_cargo_lock(path: Path, consumer: str) -> LockAnalysis:
    if consumer not in CONSUMERS:
        raise CargoSourceError(f"unsupported Cargo consumer: {consumer}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CargoSourceError(f"cannot read Cargo.lock: {exc}") from exc

    packages: list[LockedPackage] = []
    local_count = 0
    current: dict[str, str] | None = None

    def finish() -> None:
        nonlocal current, local_count
        if current is None:
            return
        name = current.get("name")
        version = current.get("version")
        if not name or not version:
            raise CargoSourceError("Cargo.lock package lacks name or version")
        source = current.get("source")
        if source is None:
            local_count += 1
        elif source.startswith("git+"):
            raise CargoSourceError(
                f"Git dependency is not supported: {name} {version} {source}"
            )
        elif source.startswith("registry+"):
            if source != CRATES_IO_SOURCE:
                raise CargoSourceError(f"unsupported registry source: {source}")
            checksum = current.get("checksum")
            if checksum is None or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                raise CargoSourceError(
                    f"registry package lacks a valid checksum: {name} {version}"
                )
            packages.append(
                LockedPackage(name, version, source, checksum, consumer)
            )
        else:
            raise CargoSourceError(f"unsupported Cargo source: {source}")
        current = None

    for raw_line in lines:
        line = raw_line.strip()
        if line == "[[package]]":
            finish()
            current = {}
            continue
        if current is None:
            continue
        for key in ("name", "version", "source", "checksum"):
            value = _toml_string(line, key)
            if value is not None:
                current[key] = value
                break
    finish()
    identities = [package.identity for package in packages]
    if len(identities) != len(set(identities)):
        raise CargoSourceError(f"Cargo.lock contains duplicate packages: {path}")
    return LockAnalysis(tuple(packages), local_count)


def merge_lock_sources(analyses: Iterable[LockAnalysis]) -> tuple[CargoSource, ...]:
    merged: dict[tuple[str, str, str], tuple[str, set[str]]] = {}
    for analysis in analyses:
        for package in analysis.registry:
            previous = merged.get(package.identity)
            if previous is None:
                merged[package.identity] = (package.sha256, {package.consumer})
            else:
                checksum, consumers = previous
                if checksum != package.sha256:
                    raise CargoSourceError(
                        f"checksum conflict for {package.name} {package.version}"
                    )
                consumers.add(package.consumer)

    entries = []
    for (name, version, source), (checksum, consumers) in sorted(merged.items()):
        filename = f"{name}-{version}.crate"
        entries.append(
            CargoSource(
                name=name,
                version=version,
                source=source,
                filename=filename,
                url=f"https://static.crates.io/crates/{name}/{filename}",
                sha256=checksum,
                consumers=tuple(sorted(consumers)),
            )
        )
    return tuple(entries)


def _validate_source(raw: object) -> CargoSource:
    if not isinstance(raw, dict) or set(raw) != SOURCE_FIELDS:
        raise CargoSourceError("cargo source manifest contains malformed entries")
    if any(
        not isinstance(raw[field], str)
        for field in SOURCE_FIELDS - {"consumers"}
    ):
        raise CargoSourceError("cargo source manifest contains invalid field types")
    consumers = raw["consumers"]
    if (
        not isinstance(consumers, list)
        or not consumers
        or consumers != sorted(consumers)
        or len(consumers) != len(set(consumers))
        or any(consumer not in CONSUMERS for consumer in consumers)
    ):
        raise CargoSourceError("cargo source manifest contains invalid consumers")
    entry = CargoSource(
        name=raw["name"],
        version=raw["version"],
        source=raw["source"],
        filename=raw["filename"],
        url=raw["url"],
        sha256=raw["sha256"],
        consumers=tuple(consumers),
    )
    if NAME_PATTERN.fullmatch(entry.name) is None:
        raise CargoSourceError(f"invalid crate name: {entry.name}")
    if VERSION_PATTERN.fullmatch(entry.version) is None:
        raise CargoSourceError(f"invalid crate version: {entry.version}")
    if entry.source != CRATES_IO_SOURCE:
        raise CargoSourceError(f"unsupported crate registry: {entry.source}")
    if entry.filename != f"{entry.name}-{entry.version}.crate":
        raise CargoSourceError(f"non-deterministic crate filename: {entry.filename}")
    expected_url = f"https://static.crates.io/crates/{entry.name}/{entry.filename}"
    if entry.url != expected_url:
        raise CargoSourceError(f"crate URL is not official: {entry.url}")
    if re.fullmatch(r"[0-9a-f]{64}", entry.sha256) is None:
        raise CargoSourceError(f"invalid crate SHA256: {entry.name}")
    return entry


def load_manifest(path: Path) -> tuple[CargoSource, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CargoSourceError(f"cannot read cargo source manifest: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "cargo_locks", "sources"}
        or payload["schema_version"] != 1
        or not isinstance(payload["cargo_locks"], list)
        or not isinstance(payload["sources"], list)
    ):
        raise CargoSourceError("cargo source manifest has an unsupported structure")
    for record in payload["cargo_locks"]:
        if not isinstance(record, dict) or set(record) != LOCK_FIELDS:
            raise CargoSourceError("cargo source manifest has malformed lock records")
    entries = tuple(_validate_source(raw) for raw in payload["sources"])
    identities = [entry.identity for entry in entries]
    filenames = [entry.filename for entry in entries]
    if identities != sorted(identities):
        raise CargoSourceError("cargo source entries are not sorted")
    if len(identities) != len(set(identities)) or len(filenames) != len(set(filenames)):
        raise CargoSourceError("cargo source manifest contains duplicates")
    return entries


def _digest(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _verify(entry: CargoSource, path: Path) -> int:
    try:
        actual, size = _digest(path)
    except OSError as exc:
        raise CargoSourceError(f"cannot read crate archive {path}: {exc}") from exc
    if actual != entry.sha256:
        raise CargoSourceError(
            f"crate verification failed for {entry.filename}: "
            f"expected={entry.sha256} actual={actual}"
        )
    return size


def _fetch_one(
    entry: CargoSource, cache: Path, opener: Opener
) -> FetchResult:
    final_path = cache / entry.filename
    if final_path.exists():
        if not final_path.is_file() or final_path.is_symlink():
            raise CargoSourceError(f"crate cache entry is not a regular file: {final_path}")
        return FetchResult(entry, final_path, _verify(entry, final_path), True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{entry.filename}.", suffix=".partial", dir=cache
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        request = urllib.request.Request(
            entry.url, headers={"User-Agent": "orin-stage-cargo-source-fetch/0.1"}
        )
        with opener(request, timeout=60) as response, temporary_path.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        size = _verify(entry, temporary_path)
        os.replace(temporary_path, final_path)
        return FetchResult(entry, final_path, size, False)
    except CargoSourceError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise CargoSourceError(f"cannot download {entry.filename}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def fetch_sources(
    manifest_path: Path,
    cache_directory: Path,
    *,
    workers: int = 1,
    opener: Opener = urllib.request.urlopen,
    output: TextIO = sys.stdout,
) -> tuple[FetchResult, ...]:
    entries = load_manifest(manifest_path)
    cache = cache_directory.expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if workers < 1:
        raise CargoSourceError("worker count must be positive")
    if workers == 1:
        results = [_fetch_one(entry, cache, opener) for entry in entries]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda entry: _fetch_one(entry, cache, opener), entries))
    for result in results:
        action = "REUSE" if result.reused else "FETCH"
        print(
            f"{action} {result.entry.filename} expected={result.entry.sha256} "
            f"actual={result.entry.sha256} MATCH size={result.size}",
            file=output,
        )
    return tuple(results)


def _safe_members(archive: tarfile.TarFile, expected_root: str) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise CargoSourceError(f"unsafe path in crate archive: {member.name}")
        if path.parts[0] != expected_root:
            raise CargoSourceError(f"unexpected crate archive root: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise CargoSourceError(f"unsupported crate archive member: {member.name}")
    return members


def _extract_crate(entry: CargoSource, archive_path: Path, destination: Path) -> None:
    root = entry.vendor_directory
    package_directory = destination / root
    package_directory.mkdir()
    file_hashes: dict[str, str] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in _safe_members(archive, root):
                relative = PurePosixPath(member.name).relative_to(root)
                if not relative.parts:
                    continue
                target = package_directory.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if target.exists():
                    raise CargoSourceError(
                        f"duplicate crate archive member: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise CargoSourceError(
                        f"cannot extract crate member: {member.name}"
                    )
                hasher = hashlib.sha256()
                with source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        hasher.update(chunk)
                        output.write(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                file_hashes[relative.as_posix()] = hasher.hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise CargoSourceError(f"cannot extract {entry.filename}: {exc}") from exc
    if "Cargo.toml" not in file_hashes:
        raise CargoSourceError(f"crate lacks Cargo.toml: {entry.filename}")
    checksum = json.dumps(
        {"files": dict(sorted(file_hashes.items())), "package": entry.sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (package_directory / ".cargo-checksum.json").write_bytes(checksum)


def _tree_digest(directory: Path) -> tuple[str, int, int]:
    hasher = hashlib.sha256()
    count = 0
    total_size = 0
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise CargoSourceError(f"vendor tree contains a symlink: {path}")
        relative = path.relative_to(directory).as_posix()
        digest, size = _digest(path)
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(bytes.fromhex(digest))
        hasher.update(b"\n")
        count += 1
        total_size += size
    return hasher.hexdigest(), count, total_size


def verify_vendor(entries: Sequence[CargoSource], vendor: Path) -> tuple[str, int, int]:
    expected = {entry.vendor_directory: entry for entry in entries}
    actual = {path.name for path in vendor.iterdir() if path.is_dir()}
    if actual != set(expected):
        raise CargoSourceError("vendor tree crate set does not match manifest")
    for directory_name, entry in expected.items():
        package_directory = vendor / directory_name
        checksum_path = package_directory / ".cargo-checksum.json"
        try:
            checksum = json.loads(checksum_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CargoSourceError(f"invalid vendor checksum: {directory_name}") from exc
        if checksum.get("package") != entry.sha256 or not isinstance(checksum.get("files"), dict):
            raise CargoSourceError(f"vendor package checksum mismatch: {directory_name}")
        actual_files: dict[str, str] = {}
        for path in sorted(item for item in package_directory.rglob("*") if item.is_file()):
            relative = path.relative_to(package_directory).as_posix()
            if relative == ".cargo-checksum.json":
                continue
            actual_files[relative] = _digest(path)[0]
        if actual_files != checksum["files"]:
            raise CargoSourceError(f"vendor file checksum mismatch: {directory_name}")
    return _tree_digest(vendor)


def _package_metadata(cargo_toml: Path) -> tuple[str | None, str | None, str | None]:
    section = ""
    values: dict[str, str] = {}
    for raw_line in cargo_toml.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section != "[package]":
            continue
        for key in ("license", "license-file", "repository"):
            value = _toml_string(line, key)
            if value is not None:
                values[key] = value
                break
    return values.get("license"), values.get("license-file"), values.get("repository")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _atomic_json(path: Path, payload: object) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")
    _atomic_bytes(path, data)


def materialize_vendor(
    manifest_path: Path,
    cache_directory: Path,
    vendor_directory: Path,
    config_path: Path,
    inventory_path: Path,
    vendor_lock_path: Path,
) -> dict[str, object]:
    entries = load_manifest(manifest_path)
    cache = cache_directory.resolve()
    vendor = vendor_directory.resolve()
    if vendor.exists():
        raise CargoSourceError(f"vendor destination already exists: {vendor}")
    vendor.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".cargo-vendor.", suffix=".partial", dir=vendor.parent))
    try:
        for entry in entries:
            archive = cache / entry.filename
            _verify(entry, archive)
            _extract_crate(entry, archive, stage)
        digest, file_count, total_size = verify_vendor(entries, stage)
        os.replace(stage, vendor)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    config = (
        '[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
        '[source.vendored-sources]\ndirectory = "cargo-vendor"\n'
    )
    _atomic_bytes(config_path, config.encode("utf-8"))
    config_sha256 = hashlib.sha256(config.encode("utf-8")).hexdigest()

    inventory_sources = []
    missing_license = []
    for entry in entries:
        license_expression, license_file, repository = _package_metadata(
            vendor / entry.vendor_directory / "Cargo.toml"
        )
        if license_expression is None and license_file is None:
            missing_license.append(entry.vendor_directory)
        inventory_sources.append(
            {
                "name": entry.name,
                "version": entry.version,
                "source": entry.source,
                "license": license_expression,
                "license_file": license_file,
                "repository": repository,
                "consumers": list(entry.consumers),
            }
        )
    _atomic_json(
        inventory_path,
        {"schema_version": 1, "sources": inventory_sources},
    )
    summary: dict[str, object] = {
        "schema_version": 1,
        "crate_count": len(entries),
        "file_count": file_count,
        "uncompressed_content_bytes": total_size,
        "tree_sha256": digest,
        "config_sha256": config_sha256,
        "missing_license_metadata": missing_license,
    }
    _atomic_json(vendor_lock_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lock, fetch, and vendor exact Cargo sources.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    fetch.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    fetch.add_argument("--workers", type=int, default=8)
    vendor = subparsers.add_parser("vendor")
    vendor.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    vendor.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    vendor.add_argument("--output", type=Path, default=DEFAULT_VENDOR)
    vendor.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    vendor.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    vendor.add_argument("--vendor-lock", type=Path, default=DEFAULT_VENDOR_LOCK)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify.add_argument("--vendor", type=Path, default=DEFAULT_VENDOR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "fetch":
            results = fetch_sources(args.manifest, args.cache, workers=args.workers)
            print(f"TOTAL crates={len(results)} compressed_bytes={sum(r.size for r in results)}")
        elif args.command == "vendor":
            summary = materialize_vendor(
                args.manifest,
                args.cache,
                args.output,
                args.config,
                args.inventory,
                args.vendor_lock,
            )
            print(json.dumps(summary, sort_keys=True))
        else:
            entries = load_manifest(args.manifest)
            digest, count, size = verify_vendor(entries, args.vendor)
            print(f"VERIFIED tree_sha256={digest} files={count} bytes={size}")
    except CargoSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
