from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SEED_FORMAT = "gnu-tar"
SEED_FORMAT_VERSION = 1
SEED_ARCHIVE_NAME = "seed.tar"
SEED_METADATA_NAME = "seed.json"
RUNTIME_TREES = ("proc", "sys", "dev")


class MaterializationSeedError(RuntimeError):
    """Raised when a materialization seed cannot be produced safely."""


@dataclass(frozen=True, slots=True)
class MaterializationSeedResult:
    archive_path: Path
    metadata_path: Path
    seed_sha256: str


def _load_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaterializationSeedError(
            f"cannot read metadata file {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise MaterializationSeedError(f"metadata file is not a JSON object: {path}")
    return value


def _receipt_digest(receipt: Mapping[str, object], field: str) -> str:
    value = receipt.get(field)
    if not isinstance(value, str) or not value:
        raise MaterializationSeedError(f"receipt.json has invalid {field!r}")
    return value


def _display_path(base: Path, path: Path) -> str:
    relative = path.relative_to(base)
    return "/" if relative == Path(".") else f"/{relative}"


def _unsupported_metadata(base: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {
        "POSIX ACL": [],
        "xattr": [],
        "sparse regular file": [],
        "special file": [],
    }
    pending = [base]

    while pending:
        path = pending.pop()
        display = _display_path(base, path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MaterializationSeedError(
                f"cannot inspect base metadata at {display}: {exc}"
            ) from exc

        mode = metadata.st_mode
        if stat.S_ISREG(mode) and metadata.st_blocks * 512 < metadata.st_size:
            found["sparse regular file"].append(display)
        if any(
            predicate(mode)
            for predicate in (stat.S_ISBLK, stat.S_ISCHR, stat.S_ISFIFO, stat.S_ISSOCK)
        ):
            found["special file"].append(display)

        try:
            xattrs = os.listxattr(path, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise MaterializationSeedError(
                    f"cannot inspect base xattrs at {display}: {exc}"
                ) from exc
            xattrs = []
        if any(name.startswith("system.posix_acl_") for name in xattrs):
            found["POSIX ACL"].append(display)
        if xattrs:
            found["xattr"].append(display)

        if not stat.S_ISDIR(mode):
            continue
        try:
            entries = sorted(
                os.scandir(path), key=lambda entry: entry.name, reverse=True
            )
        except OSError as exc:
            raise MaterializationSeedError(
                f"cannot inspect base directory at {display}: {exc}"
            ) from exc
        pending.extend(Path(entry.path) for entry in entries)

    return {kind: paths for kind, paths in found.items() if paths}


def _check_supported_metadata(base: Path) -> None:
    unsupported = _unsupported_metadata(base)
    if not unsupported:
        return
    details = "; ".join(
        f"{kind}: {', '.join(paths[:3])}"
        for kind, paths in unsupported.items()
    )
    raise MaterializationSeedError(
        f"unsupported materialization metadata detected ({details})"
    )


def _require_gnu_tar(tar_binary: str) -> None:
    try:
        completed = subprocess.run(
            [tar_binary, "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MaterializationSeedError(f"cannot execute GNU tar: {exc}") from exc
    if "GNU tar" not in completed.stdout:
        raise MaterializationSeedError(f"{tar_binary!r} is not GNU tar")


def _seed_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tar_command(tar_binary: str, base: Path, archive: Path) -> list[str]:
    command = [
        tar_binary,
        "--create",
        "--format=gnu",
        "--numeric-owner",
        "--file",
        str(archive),
        "--directory",
        str(base),
    ]
    command.extend(f"--exclude=./{name}/*" for name in RUNTIME_TREES)
    command.append(".")
    return command


def create_materialization_seed(
    target_dir: Path,
    *,
    tar_binary: str = "tar",
) -> MaterializationSeedResult:
    target = Path(target_dir).expanduser().resolve()
    base = target / "base"
    manifest_path = target / "manifest.json"
    receipt_path = target / "receipt.json"
    if not target.is_dir():
        raise MaterializationSeedError(f"target directory does not exist: {target}")
    if not base.is_dir() or base.is_symlink():
        raise MaterializationSeedError(f"base is not a real directory: {base}")
    for path in (manifest_path, receipt_path):
        if not path.is_file():
            raise MaterializationSeedError(
                f"required metadata file does not exist: {path}"
            )

    receipt = _load_json_object(receipt_path)
    target_lock_digest = _receipt_digest(receipt, "target_lock_digest")
    base_digest = _receipt_digest(receipt, "base_digest")

    _check_supported_metadata(base)
    _require_gnu_tar(tar_binary)

    output_dir = target / "materialization"
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise MaterializationSeedError(
            f"materialization output is not a real directory: {output_dir}"
        )
    output_dir.mkdir(mode=0o755, exist_ok=True)
    archive_path = output_dir / SEED_ARCHIVE_NAME
    metadata_path = output_dir / SEED_METADATA_NAME
    for path in (archive_path, metadata_path):
        if os.path.lexists(path):
            raise MaterializationSeedError(
                f"refusing to overwrite existing output: {path}"
            )

    environment = os.environ.copy()
    environment.pop("TAR_OPTIONS", None)
    try:
        subprocess.run(
            _tar_command(tar_binary, base, archive_path),
            check=True,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        archive_path.unlink(missing_ok=True)
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = f": {exc.stderr.decode(errors='replace').strip()}"
        raise MaterializationSeedError(
            "GNU tar seed creation failed; reading the base may require root"
            f"{detail}"
        ) from exc

    seed_sha256 = _seed_sha256(archive_path)
    metadata = {
        "format": SEED_FORMAT,
        "format_version": SEED_FORMAT_VERSION,
        "target_lock_digest": target_lock_digest,
        "base_digest": base_digest,
        "seed_sha256": seed_sha256,
        "archive": SEED_ARCHIVE_NAME,
    }
    try:
        with metadata_path.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise MaterializationSeedError(f"cannot write seed metadata: {exc}") from exc

    try:
        archive_path.chmod(0o644)
        metadata_path.chmod(0o644)
        output_dir.chmod(0o755)
    except OSError as exc:
        raise MaterializationSeedError(
            f"cannot set materialization seed permissions: {exc}"
        ) from exc

    return MaterializationSeedResult(
        archive_path=archive_path,
        metadata_path=metadata_path,
        seed_sha256=seed_sha256,
    )
