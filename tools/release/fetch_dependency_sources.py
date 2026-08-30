from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Sequence, TextIO
from urllib.parse import unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "release" / "dependencies" / "sources.lock.json"
SOURCE_FIELDS = {
    "name",
    "version",
    "filename",
    "url",
    "sha256",
    "size",
    "requires_python",
    "license",
    "license_files",
    "build_backend",
    "build_requires",
    "runtime_role",
}
STRING_FIELDS = SOURCE_FIELDS - {"size", "license_files", "build_requires"}
SOURCE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".zip")


class SourceAcquisitionError(RuntimeError):
    """Raised when a locked dependency source cannot be safely acquired."""


@dataclass(frozen=True)
class SourceEntry:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int
    requires_python: str
    license: str
    license_files: list[str]
    build_backend: str
    build_requires: list[str]
    runtime_role: str


Opener = Callable[..., BinaryIO]


def _validate_entry(raw: object) -> SourceEntry:
    if not isinstance(raw, dict) or set(raw) != SOURCE_FIELDS:
        raise SourceAcquisitionError("source manifest contains malformed entries")
    if any(not isinstance(raw[field], str) for field in STRING_FIELDS):
        raise SourceAcquisitionError("source manifest contains invalid field types")
    if not isinstance(raw["size"], int) or isinstance(raw["size"], bool):
        raise SourceAcquisitionError("source manifest contains an invalid source size")
    for field in ("license_files", "build_requires"):
        if not isinstance(raw[field], list) or any(
            not isinstance(item, str) or not item for item in raw[field]
        ):
            raise SourceAcquisitionError(
                f"source manifest contains an invalid {field} list"
            )

    entry = SourceEntry(**raw)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", entry.name):
        raise SourceAcquisitionError(f"source package name is not canonical: {entry.name}")
    if not entry.version:
        raise SourceAcquisitionError(f"source package version is empty: {entry.name}")
    if (
        entry.filename != Path(entry.filename).name
        or not entry.filename.endswith(SOURCE_SUFFIXES)
        or entry.filename.endswith(".whl")
    ):
        raise SourceAcquisitionError(
            f"source filename is not a supported sdist: {entry.filename}"
        )
    parsed = urlparse(entry.url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or PurePosixPath(unquote(parsed.path)).name != entry.filename
    ):
        raise SourceAcquisitionError(
            f"source URL is not an official PyPI file URL: {entry.url}"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", entry.sha256):
        raise SourceAcquisitionError(f"source SHA256 is invalid: {entry.name}")
    if entry.size <= 0:
        raise SourceAcquisitionError(f"source size is invalid: {entry.name}")
    if (
        not entry.requires_python
        or not entry.license
        or not entry.license_files
        or not entry.build_backend
        or not entry.build_requires
    ):
        raise SourceAcquisitionError(
            f"source metadata is incomplete for package: {entry.name}"
        )
    if entry.runtime_role not in {"direct", "transitive"}:
        raise SourceAcquisitionError(f"runtime role is invalid: {entry.name}")
    return entry


def load_manifest(path: Path) -> tuple[SourceEntry, ...]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceAcquisitionError(f"cannot read source manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "sources"}
        or manifest["schema_version"] != 1
        or not isinstance(manifest["sources"], list)
    ):
        raise SourceAcquisitionError("source manifest has an unsupported structure")

    entries = tuple(_validate_entry(raw) for raw in manifest["sources"])
    names = [entry.name for entry in entries]
    filenames = [entry.filename for entry in entries]
    if len(names) != len(set(names)) or len(filenames) != len(set(filenames)):
        raise SourceAcquisitionError("source manifest contains duplicate entries")
    if names != sorted(names):
        raise SourceAcquisitionError("source manifest entries are not sorted")
    return entries


def _digest(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _verify(entry: SourceEntry, path: Path) -> str:
    actual_sha256, actual_size = _digest(path)
    if actual_sha256 != entry.sha256 or actual_size != entry.size:
        raise SourceAcquisitionError(
            f"source verification failed for {entry.filename}: "
            f"expected sha256={entry.sha256} size={entry.size}, "
            f"got sha256={actual_sha256} size={actual_size}"
        )
    return actual_sha256


def fetch_sources(
    manifest_path: Path,
    output_directory: Path,
    *,
    opener: Opener = urllib.request.urlopen,
    output: TextIO = sys.stdout,
) -> tuple[Path, ...]:
    entries = load_manifest(manifest_path)
    destination = output_directory.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    acquired: list[Path] = []

    for entry in entries:
        final_path = destination / entry.filename
        if final_path.exists():
            if not final_path.is_file() or final_path.is_symlink():
                raise SourceAcquisitionError(
                    f"source destination is not a regular file: {final_path}"
                )
            actual_sha256 = _verify(entry, final_path)
            print(
                f"REUSE {entry.filename} expected={entry.sha256} "
                f"actual={actual_sha256} MATCH",
                file=output,
            )
            acquired.append(final_path)
            continue

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{entry.filename}.",
            suffix=".partial",
            dir=destination,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            request = urllib.request.Request(
                entry.url,
                headers={"User-Agent": "orin-stage-source-fetch/0.1"},
            )
            with opener(request, timeout=60) as response, temporary_path.open(
                "wb"
            ) as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
            actual_sha256 = _verify(entry, temporary_path)
            os.replace(temporary_path, final_path)
        except SourceAcquisitionError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise SourceAcquisitionError(
                f"cannot download source {entry.filename}: {exc}"
            ) from exc
        finally:
            temporary_path.unlink(missing_ok=True)

        print(
            f"FETCH {entry.filename} expected={entry.sha256} "
            f"actual={actual_sha256} MATCH",
            file=output,
        )
        acquired.append(final_path)

    return tuple(acquired)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch and SHA256-verify locked PyPI runtime source archives."
    )
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fetch_sources(args.manifest, args.output_directory)
    except SourceAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
