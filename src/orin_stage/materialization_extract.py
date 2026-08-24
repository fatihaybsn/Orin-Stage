from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
import tarfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from .materialization_seed import SEED_FORMAT, SEED_FORMAT_VERSION


class MaterializationExtractionError(RuntimeError):
    """Raised when seed extraction or parity validation fails."""


@dataclass(frozen=True, slots=True)
class ExpectedPath:
    archive_name: str
    relative_path: str
    uid: int
    gid: int
    mode: int
    kind: str
    linkname: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    path_count: int
    uid_parity: int
    gid_parity: int
    mode_parity: int
    hardlink_parity: int
    symlink_parity: int
    staging_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExtractionReport:
        try:
            return cls(
                path_count=int(value["path_count"]),
                uid_parity=int(value["uid_parity"]),
                gid_parity=int(value["gid_parity"]),
                mode_parity=int(value["mode_parity"]),
                hardlink_parity=int(value["hardlink_parity"]),
                symlink_parity=int(value["symlink_parity"]),
                staging_path=str(value["staging_path"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterializationExtractionError(
                "podman unshare worker returned a malformed report"
            ) from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MaterializationExtractionError(
            f"cannot read seed archive for SHA-256 verification: {exc}"
        ) from exc
    return digest.hexdigest()


def _load_seed(
    seed_dir: Path,
    *,
    verify_sha256: bool = True,
) -> tuple[Path, str]:
    metadata_path = seed_dir / "seed.json"
    if not metadata_path.is_file():
        raise MaterializationExtractionError(
            f"seed metadata does not exist: {metadata_path}"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaterializationExtractionError(
            f"cannot read seed metadata: {exc}"
        ) from exc
    if not isinstance(metadata, dict):
        raise MaterializationExtractionError("seed.json is not a JSON object")
    if metadata.get("format") != SEED_FORMAT:
        raise MaterializationExtractionError("seed.json has an unsupported format")
    if metadata.get("format_version") != SEED_FORMAT_VERSION:
        raise MaterializationExtractionError(
            "seed.json has an unsupported format version"
        )

    archive_name = metadata.get("archive")
    expected_sha256 = metadata.get("seed_sha256")
    if (
        not isinstance(archive_name, str)
        or not archive_name
        or Path(archive_name).name != archive_name
    ):
        raise MaterializationExtractionError("seed.json has an invalid archive name")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise MaterializationExtractionError("seed.json has an invalid seed SHA-256")

    archive_path = seed_dir / archive_name
    if not archive_path.is_file():
        raise MaterializationExtractionError(
            f"seed archive does not exist: {archive_path}"
        )
    if verify_sha256:
        actual_sha256 = _file_sha256(archive_path)
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise MaterializationExtractionError(
                "seed SHA-256 mismatch: extraction was not started"
            )
    return archive_path, expected_sha256


def _relative_archive_path(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise MaterializationExtractionError(
            f"unsafe path in seed archive: {name!r}"
        )
    parts = tuple(part for part in path.parts if part not in ("", "."))
    return "/".join(parts) if parts else "."


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return "regular file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    raise MaterializationExtractionError(
        f"unsupported member type in seed archive: {member.name!r}"
    )


def _read_expected_paths(archive_path: Path) -> dict[str, ExpectedPath]:
    expected: dict[str, ExpectedPath] = {}
    try:
        with tarfile.open(archive_path, "r:") as archive:
            for member in archive:
                relative = _relative_archive_path(member.name)
                if relative in expected:
                    raise MaterializationExtractionError(
                        f"duplicate path in seed archive: {member.name!r}"
                    )
                expected[relative] = ExpectedPath(
                    archive_name=member.name,
                    relative_path=relative,
                    uid=member.uid,
                    gid=member.gid,
                    mode=member.mode,
                    kind=_member_kind(member),
                    linkname=(
                        member.linkname
                        if member.islnk() or member.issym()
                        else None
                    ),
                )
    except (OSError, tarfile.TarError) as exc:
        raise MaterializationExtractionError(
            f"cannot read seed tar headers: {exc}"
        ) from exc
    if not expected:
        raise MaterializationExtractionError("seed archive is empty")
    return expected


def _content_entry(
    entry: ExpectedPath,
    expected: Mapping[str, ExpectedPath],
) -> ExpectedPath:
    visited: set[str] = set()
    current = entry
    while current.kind == "hardlink":
        if current.relative_path in visited:
            raise MaterializationExtractionError("hardlink cycle in seed archive")
        visited.add(current.relative_path)
        if current.linkname is None:
            raise MaterializationExtractionError(
                f"hardlink has no target: {current.archive_name!r}"
            )
        target = _relative_archive_path(current.linkname)
        try:
            current = expected[target]
        except KeyError as exc:
            raise MaterializationExtractionError(
                f"hardlink target is missing from seed archive: {current.linkname!r}"
            ) from exc
    return current


def _filesystem_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "unsupported"


def _staging_path(root: Path, relative: str) -> Path:
    return root if relative == "." else root.joinpath(*relative.split("/"))


def _validate_extraction(
    expected: Mapping[str, ExpectedPath],
    staging_root: Path,
) -> ExtractionReport:
    hardlinks = 0
    symlinks = 0
    for relative, entry in expected.items():
        path = _staging_path(staging_root, relative)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MaterializationExtractionError(
                f"path parity failed for {relative!r}: {exc}"
            ) from exc

        content = _content_entry(entry, expected)
        actual_kind = _filesystem_kind(metadata.st_mode)
        if actual_kind != content.kind:
            raise MaterializationExtractionError(
                f"type parity failed for {relative!r}: "
                f"expected {content.kind}, got {actual_kind}"
            )
        if metadata.st_uid != content.uid:
            raise MaterializationExtractionError(
                f"UID parity failed for {relative!r}: "
                f"expected {content.uid}, got {metadata.st_uid}"
            )
        if metadata.st_gid != content.gid:
            raise MaterializationExtractionError(
                f"GID parity failed for {relative!r}: "
                f"expected {content.gid}, got {metadata.st_gid}"
            )
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if actual_mode != content.mode:
            raise MaterializationExtractionError(
                f"mode parity failed for {relative!r}: "
                f"expected {content.mode:o}, got {actual_mode:o}"
            )

        if content.kind == "symlink":
            symlinks += 1
            actual_target = os.readlink(path)
            if actual_target != content.linkname:
                raise MaterializationExtractionError(
                    f"symlink parity failed for {relative!r}: "
                    f"expected {content.linkname!r}, got {actual_target!r}"
                )

        if entry.kind == "hardlink":
            hardlinks += 1
            if entry.linkname is None:
                raise MaterializationExtractionError(
                    f"hardlink has no target: {entry.archive_name!r}"
                )
            target = _staging_path(
                staging_root, _relative_archive_path(entry.linkname)
            )
            try:
                target_metadata = target.lstat()
            except OSError as exc:
                raise MaterializationExtractionError(
                    f"hardlink target is missing for {relative!r}: {exc}"
                ) from exc
            if (metadata.st_dev, metadata.st_ino) != (
                target_metadata.st_dev,
                target_metadata.st_ino,
            ):
                raise MaterializationExtractionError(
                    f"hardlink parity failed for {relative!r}"
                )

    count = len(expected)
    return ExtractionReport(
        path_count=count,
        uid_parity=count,
        gid_parity=count,
        mode_parity=count,
        hardlink_parity=hardlinks,
        symlink_parity=symlinks,
        staging_path=str(staging_root),
    )


def _tar_extract_command(
    tar_binary: str,
    archive_path: Path,
    staging_root: Path,
) -> list[str]:
    return [
        tar_binary,
        "--extract",
        "--numeric-owner",
        "--same-owner",
        "--same-permissions",
        "--delay-directory-restore",
        "--file",
        str(archive_path),
        "--directory",
        str(staging_root),
    ]


def extract_and_validate_in_namespace(
    seed_dir: Path,
    staging_root: Path,
    *,
    tar_binary: str = "tar",
    extract: bool = True,
) -> ExtractionReport:
    archive_path, _ = _load_seed(seed_dir)
    expected = _read_expected_paths(archive_path)
    if extract:
        environment = os.environ.copy()
        environment.pop("TAR_OPTIONS", None)
        try:
            completed = subprocess.run(
                _tar_extract_command(tar_binary, archive_path, staging_root),
                check=True,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                detail = f": {exc.stderr.decode(errors='replace').strip()}"
            raise MaterializationExtractionError(
                f"GNU tar extraction failed{detail}"
            ) from exc
        if completed.stdout:
            raise MaterializationExtractionError("GNU tar produced unexpected output")
    return _validate_extraction(expected, staging_root)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run_podman_worker(
    seed_dir: Path,
    staging_root: Path,
    *,
    validate_only: bool,
    podman_binary: str,
    python_binary: str,
    runner: Runner,
) -> ExtractionReport:
    source_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(source_root)
    )
    command = [
        podman_binary,
        "unshare",
        python_binary,
        "-m",
        "orin_stage.materialization_extract",
        "--seed-dir",
        str(seed_dir),
        "--staging-root",
        str(staging_root),
    ]
    if validate_only:
        command.append("--validate-only")
    try:
        completed = runner(
            command,
            check=True,
            text=True,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = f": {exc.stderr.strip()}"
        operation = "validation" if validate_only else "extraction/validation"
        raise MaterializationExtractionError(
            f"podman unshare {operation} failed; staging kept at "
            f"{staging_root}{detail}"
        ) from exc

    try:
        raw_report = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MaterializationExtractionError(
            "podman unshare worker returned invalid JSON"
        ) from exc
    if not isinstance(raw_report, dict):
        raise MaterializationExtractionError(
            "podman unshare worker returned a malformed report"
        )
    report = ExtractionReport.from_dict(raw_report)
    if report.staging_path != str(staging_root):
        raise MaterializationExtractionError(
            "podman unshare worker reported an unexpected staging path"
        )
    return report


def extract_materialization_seed(
    data_root: Path,
    target_dir: Path,
    *,
    podman_binary: str = "podman",
    python_binary: str = sys.executable,
    runner: Runner = subprocess.run,
) -> ExtractionReport:
    data = Path(data_root).expanduser().resolve()
    target = Path(target_dir).expanduser().resolve()
    if not data.is_dir():
        raise MaterializationExtractionError(f"data root does not exist: {data}")
    if target.parent != data / "targets" or not target.is_dir():
        raise MaterializationExtractionError(
            f"target directory is not under {data / 'targets'}: {target}"
        )

    seed_dir = target / "materialization"
    _load_seed(seed_dir, verify_sha256=False)

    staging_dir = data / "staging"
    if staging_dir.is_symlink() or (staging_dir.exists() and not staging_dir.is_dir()):
        raise MaterializationExtractionError(
            f"staging path is not a real directory: {staging_dir}"
        )
    staging_dir.mkdir(mode=0o755, exist_ok=True)
    attempt = staging_dir / uuid.uuid4().hex
    staging_root = attempt / "root"
    staging_root.mkdir(parents=True, mode=0o755)

    return _run_podman_worker(
        seed_dir,
        staging_root,
        validate_only=False,
        podman_binary=podman_binary,
        python_binary=python_binary,
        runner=runner,
    )


def validate_materialization_staging(
    seed_dir: Path,
    staging_root: Path,
    *,
    podman_binary: str = "podman",
    python_binary: str = sys.executable,
    runner: Runner = subprocess.run,
) -> ExtractionReport:
    seed = Path(seed_dir).expanduser().resolve()
    root = Path(staging_root).expanduser().resolve()
    _load_seed(seed, verify_sha256=False)
    if not root.is_dir() or root.is_symlink():
        raise MaterializationExtractionError(
            f"staging root is not a real directory: {root}"
        )
    return _run_podman_worker(
        seed,
        root,
        validate_only=True,
        podman_binary=podman_binary,
        python_binary=python_binary,
        runner=runner,
    )


def _worker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = extract_and_validate_in_namespace(
            args.seed_dir.resolve(),
            args.staging_root.resolve(),
            extract=not args.validate_only,
        )
    except MaterializationExtractionError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
