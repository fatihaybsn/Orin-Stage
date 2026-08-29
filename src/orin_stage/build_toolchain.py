from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, ContextManager, Iterator, Mapping, Protocol

from .build_identity import (
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
    JP6_BOOTLIN_TOOLCHAIN_PREFIX,
    JP6_BUILD_IDENTITY,
)


BUILD_TOOLCHAIN_RECEIPT_FORMAT_VERSION = 1
_DOWNLOAD_TIMEOUT_SECONDS = 60
_STAGING_PREFIX = ".jp6-toolchain-"


class BuildToolchainError(RuntimeError):
    """Base error for the managed JP6 Bootlin toolchain."""


class BuildToolchainNotFoundError(BuildToolchainError):
    """Raised when the exact managed toolchain has not been acquired."""


@dataclass(frozen=True, slots=True)
class BuildToolchainRecord:
    build_identity_digest: str
    toolchain_path: Path
    root_path: Path
    receipt_path: Path
    reused: bool


class ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...


UrlOpen = Callable[..., ContextManager[ReadableResponse]]
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _expected_receipt() -> dict[str, object]:
    return {
        "format_version": BUILD_TOOLCHAIN_RECEIPT_FORMAT_VERSION,
        "build_identity_digest": JP6_BUILD_IDENTITY.digest(),
        "build_identity": JP6_BUILD_IDENTITY.to_dict(),
        "archive": {
            "url": JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
            "filename": JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME,
            "sha256": JP6_BUILD_IDENTITY.toolchain_archive_sha256,
        },
        "toolchain": {
            "prefix": JP6_BOOTLIN_TOOLCHAIN_PREFIX,
            "gcc_version": JP6_BUILD_IDENTITY.gcc_version,
            "binutils_version": JP6_BUILD_IDENTITY.binutils_version,
        },
        "root": "root",
    }


def _load_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildToolchainError(
            f"cannot read managed toolchain receipt {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise BuildToolchainError("managed toolchain receipt is not a JSON object")
    return value


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BuildToolchainError(f"cannot write managed toolchain receipt: {exc}") from exc


def _ensure_real_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise BuildToolchainError(f"managed toolchain path is not a real directory: {path}")
    try:
        path.mkdir(parents=True, mode=0o755, exist_ok=True)
    except OSError as exc:
        raise BuildToolchainError(f"cannot create managed toolchain directory {path}: {exc}") from exc


def _archive_parts(name: str) -> tuple[str, ...]:
    archive_path = PurePosixPath(name)
    if archive_path.is_absolute():
        raise BuildToolchainError(f"toolchain archive contains an absolute path: {name}")
    parts = tuple(part for part in archive_path.parts if part not in {"", "."})
    if any(part == ".." for part in parts):
        raise BuildToolchainError(f"toolchain archive path escapes extraction root: {name}")
    return parts


def _link_parts(member_parts: tuple[str, ...], linkname: str, *, hard: bool) -> tuple[str, ...]:
    link_path = PurePosixPath(linkname)
    if link_path.is_absolute():
        raise BuildToolchainError(
            f"toolchain archive link has an absolute target: {linkname}"
        )
    resolved = [] if hard else list(member_parts[:-1])
    for part in link_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise BuildToolchainError(
                    f"toolchain archive link escapes extraction root: {linkname}"
                )
            resolved.pop()
        else:
            resolved.append(part)
    return tuple(resolved)


def _safe_parent(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts[:-1]:
        current = current / part
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise BuildToolchainError(
                    f"toolchain archive member has an unsafe parent: {'/'.join(parts)}"
                )
        else:
            current.mkdir(mode=0o755)
    return current


def _validate_archive_members(
    members: list[tarfile.TarInfo],
) -> dict[str, tuple[str, ...]]:
    paths: dict[str, tuple[str, ...]] = {}
    seen: set[tuple[str, ...]] = set()
    for member in members:
        parts = _archive_parts(member.name)
        paths[member.name] = parts
        if not parts:
            if member.isdir():
                continue
            raise BuildToolchainError("toolchain archive contains an unnamed member")
        if parts in seen:
            raise BuildToolchainError(
                f"toolchain archive contains a duplicate path: {member.name}"
            )
        seen.add(parts)
        if member.issym():
            _link_parts(parts, member.linkname, hard=False)
        elif member.islnk():
            _link_parts(parts, member.linkname, hard=True)
        elif not (member.isdir() or member.isfile()):
            raise BuildToolchainError(
                f"toolchain archive contains an unsupported member: {member.name}"
            )
    return paths


def _extract_archive(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:bz2") as bundle:
            members = bundle.getmembers()
            paths = _validate_archive_members(members)
            directory_modes: list[tuple[Path, int]] = []
            hardlinks: list[tuple[Path, tuple[str, ...], int]] = []

            for member in members:
                parts = paths[member.name]
                if not parts:
                    continue
                target = destination.joinpath(*parts)
                _safe_parent(destination, parts)
                mode = member.mode & 0o777

                if member.isdir():
                    if os.path.lexists(target):
                        if target.is_symlink() or not target.is_dir():
                            raise BuildToolchainError(
                                f"toolchain archive directory conflicts with a member: {member.name}"
                            )
                    else:
                        target.mkdir(mode=0o755)
                    directory_modes.append((target, mode))
                elif member.isfile():
                    source = bundle.extractfile(member)
                    if source is None:
                        raise BuildToolchainError(
                            f"cannot read toolchain archive member: {member.name}"
                        )
                    try:
                        with target.open("xb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                    finally:
                        source.close()
                    target.chmod(mode)
                elif member.issym():
                    _link_parts(parts, member.linkname, hard=False)
                    target.symlink_to(member.linkname)
                else:
                    hardlinks.append(
                        (target, _link_parts(parts, member.linkname, hard=True), mode)
                    )

            pending = hardlinks
            while pending:
                unresolved: list[tuple[Path, tuple[str, ...], int]] = []
                for target, source_parts, mode in pending:
                    source = destination.joinpath(*source_parts)
                    if not os.path.lexists(source):
                        unresolved.append((target, source_parts, mode))
                        continue
                    if source.is_symlink() or not source.is_file():
                        raise BuildToolchainError(
                            "toolchain archive hard link does not target a regular file"
                        )
                    os.link(source, target)
                    target.chmod(mode)
                if len(unresolved) == len(pending):
                    raise BuildToolchainError(
                        "toolchain archive contains an unresolved hard link"
                    )
                pending = unresolved

            for directory, mode in sorted(
                directory_modes,
                key=lambda item: len(item[0].parts),
                reverse=True,
            ):
                directory.chmod(mode)
    except BuildToolchainError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BuildToolchainError(f"cannot extract managed toolchain archive: {exc}") from exc


def _select_archive_root(extracted: Path) -> Path:
    entries = list(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return extracted


def _version_output(
    executable: Path,
    arguments: tuple[str, ...],
    *,
    runner: Runner,
) -> str:
    try:
        completed = runner(
            (str(executable), *arguments),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise BuildToolchainError(f"cannot run managed toolchain executable: {exc}") from exc
    if completed.returncode != 0:
        raise BuildToolchainError(
            f"managed toolchain version probe failed (exit {completed.returncode})"
        )
    return (completed.stdout or "").strip()


def _expected_executable(root: Path, name: str) -> Path:
    executable = root / "bin" / f"{JP6_BOOTLIN_TOOLCHAIN_PREFIX}{name}"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BuildToolchainError(
            f"managed toolchain is missing executable: {executable.relative_to(root)}"
        )
    try:
        executable.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BuildToolchainError(
            f"managed toolchain executable escapes its root: {executable.relative_to(root)}"
        ) from exc
    return executable


def _validate_root(root: Path, *, runner: Runner | None) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BuildToolchainError(f"managed toolchain root is invalid: {root}")
    gcc = _expected_executable(root, "gcc")
    ld = _expected_executable(root, "ld")
    if runner is None:
        return

    gcc_version = _version_output(gcc, ("-dumpfullversion",), runner=runner)
    if gcc_version != JP6_BUILD_IDENTITY.gcc_version:
        raise BuildToolchainError(
            "managed toolchain GCC version mismatch: "
            f"expected {JP6_BUILD_IDENTITY.gcc_version}, got {gcc_version or 'empty output'}"
        )

    binutils_output = _version_output(ld, ("--version",), runner=runner)
    first_line = binutils_output.splitlines()[0] if binutils_output else ""
    versions = re.findall(r"(?<![\d.])\d+\.\d+(?:\.\d+)*(?![\d.])", first_line)
    if JP6_BUILD_IDENTITY.binutils_version not in versions:
        raise BuildToolchainError(
            "managed toolchain Binutils version mismatch: "
            f"expected {JP6_BUILD_IDENTITY.binutils_version}, "
            f"got {first_line or 'empty output'}"
        )


@dataclass(frozen=True, slots=True)
class BuildToolchainManager:
    data_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_root", Path(self.data_root).expanduser().resolve())

    @property
    def build_identity_digest(self) -> str:
        return JP6_BUILD_IDENTITY.digest()

    @property
    def toolchains_dir(self) -> Path:
        return self.data_root / "build" / "toolchains"

    @property
    def toolchain_path(self) -> Path:
        return self.toolchains_dir / self.build_identity_digest

    def inspect(self) -> BuildToolchainRecord:
        """Inspect the managed toolchain without creating paths or running binaries."""
        return self._load(reused=True, runner=None)

    def ensure(
        self,
        *,
        urlopen: UrlOpen = urllib.request.urlopen,
        runner: Runner = subprocess.run,
    ) -> BuildToolchainRecord:
        """Acquire or reuse the exact rootless JP6 Bootlin toolchain."""
        try:
            return self._load(reused=True, runner=runner)
        except BuildToolchainNotFoundError:
            pass

        self._prepare_managed_directories()
        with self._lock():
            try:
                return self._load(reused=True, runner=runner)
            except BuildToolchainNotFoundError:
                return self._acquire(urlopen=urlopen, runner=runner)

    def _load(
        self,
        *,
        reused: bool,
        runner: Runner | None,
    ) -> BuildToolchainRecord:
        final = self.toolchain_path
        if not os.path.lexists(final):
            raise BuildToolchainNotFoundError(
                f"managed JP6 toolchain is not acquired: {final}"
            )
        if final.is_symlink() or not final.is_dir():
            raise BuildToolchainError(f"managed toolchain path is invalid: {final}")
        receipt_path = final / "receipt.json"
        receipt = _load_json_object(receipt_path)
        if receipt != _expected_receipt():
            raise BuildToolchainError("managed toolchain receipt does not match JP6 build identity")
        root = final / "root"
        _validate_root(root, runner=runner)
        return BuildToolchainRecord(
            build_identity_digest=self.build_identity_digest,
            toolchain_path=final,
            root_path=root,
            receipt_path=receipt_path,
            reused=reused,
        )

    def _prepare_managed_directories(self) -> None:
        _ensure_real_directory(self.data_root)
        _ensure_real_directory(self.data_root / "build")
        _ensure_real_directory(self.toolchains_dir)
        _ensure_real_directory(self.data_root / "build" / "staging")
        _ensure_real_directory(self.data_root / "build" / "locks")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = (
            self.data_root
            / "build"
            / "locks"
            / f"toolchain-{self.build_identity_digest}.lock"
        )
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise BuildToolchainError(f"cannot lock managed toolchain acquisition: {exc}") from exc

    def _acquire(self, *, urlopen: UrlOpen, runner: Runner) -> BuildToolchainRecord:
        staging = (
            self.data_root
            / "build"
            / "staging"
            / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
        )
        try:
            staging.mkdir(mode=0o755)
            archive = staging / JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_FILENAME
            self._download(archive, urlopen=urlopen)
            self._verify_archive(archive)

            extracted = staging / "extracted"
            extracted.mkdir(mode=0o755)
            _extract_archive(archive, extracted)
            archive_root = _select_archive_root(extracted)

            publish = staging / "publish"
            publish.mkdir(mode=0o755)
            root = publish / "root"
            os.replace(archive_root, root)
            _validate_root(root, runner=runner)
            _write_receipt(publish / "receipt.json", _expected_receipt())

            if os.path.lexists(self.toolchain_path):
                raise BuildToolchainError(
                    f"managed toolchain destination already exists: {self.toolchain_path}"
                )
            os.replace(publish, self.toolchain_path)
            return self._load(reused=False, runner=runner)
        except BuildToolchainError:
            raise
        except OSError as exc:
            raise BuildToolchainError(f"cannot publish managed toolchain: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _download(archive: Path, *, urlopen: UrlOpen) -> None:
        try:
            with urlopen(
                JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_URL,
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                with archive.open("xb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
        except OSError as exc:
            raise BuildToolchainError(f"cannot download managed toolchain: {exc}") from exc

    @staticmethod
    def _verify_archive(archive: Path) -> None:
        digest = hashlib.sha256()
        try:
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise BuildToolchainError(f"cannot read managed toolchain archive: {exc}") from exc
        actual = digest.hexdigest()
        expected = JP6_BUILD_IDENTITY.toolchain_archive_sha256
        if actual != expected:
            raise BuildToolchainError(
                f"managed toolchain SHA-256 mismatch: expected {expected}, got {actual}"
            )
