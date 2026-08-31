from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = REPO_ROOT / "release" / "dependencies"
RUNTIME_LOCK = DEPENDENCIES / "runtime.lock"
BUILD_LOCK = DEPENDENCIES / "build-tools.lock"
RUNTIME_SOURCES = DEPENDENCIES / "sources.lock.json"
BUILD_SOURCES = DEPENDENCIES / "build-sources.lock.json"
CARGO_SOURCES = DEPENDENCIES / "cargo-sources.lock.json"
CARGO_VENDOR_LOCK = DEPENDENCIES / "cargo-vendor.lock.json"
CARGO_LOCKS = (
    DEPENDENCIES / "cargo-locks" / "maturin-1.9.0.Cargo.lock",
    DEPENDENCIES / "cargo-locks" / "rpds-py-0.30.0.Cargo.lock",
)
DEFAULT_SOURCE_DIRECTORY = DEPENDENCIES / "downloads"
DEFAULT_VENDOR_DIRECTORY = DEPENDENCIES / "generated" / "cargo-vendor"
DEFAULT_OUTPUT_ROOT = DEPENDENCIES / "generated" / "wheelhouse"
LOCKED_INPUTS = (
    RUNTIME_LOCK,
    BUILD_LOCK,
    RUNTIME_SOURCES,
    BUILD_SOURCES,
    CARGO_SOURCES,
    CARGO_VENDOR_LOCK,
    *CARGO_LOCKS,
)
BUILD_ORDER = (
    "flit-core",
    "packaging",
    "setuptools",
    "pathspec",
    "tomli",
    "semantic-version",
    "calver",
    "setuptools-scm",
    "pluggy",
    "trove-classifiers",
    "hatchling",
    "setuptools-rust",
    "hatch-vcs",
    "hatch-fancy-pypi-readme",
    "maturin",
)
NATIVE_RUNTIME = {"pyyaml", "rpds-py"}
BUILD_ONLY = set(BUILD_ORDER)


class WheelhouseError(RuntimeError):
    """Raised when the exact offline wheel contract cannot be satisfied."""


@dataclass(frozen=True)
class Source:
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    build_backend: str | None
    build_requires: tuple[str, ...]


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    role: str
    python_tag: str
    abi_tag: str
    platform_tag: str


def canonicalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_exact_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(
            r"([a-z0-9]+(?:-[a-z0-9]+)*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)",
            line,
        )
        if match is None:
            raise WheelhouseError(f"lock entry is not exact: {line}")
        name, version = match.groups()
        if name in result:
            raise WheelhouseError(f"duplicate package in {path}: {name}")
        result[name] = version
    return result


def load_sources(path: Path) -> dict[str, Source]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WheelhouseError(f"cannot load source manifest {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("sources"), list)
    ):
        raise WheelhouseError(f"unsupported source manifest: {path}")
    result: dict[str, Source] = {}
    for raw in payload["sources"]:
        if not isinstance(raw, dict):
            raise WheelhouseError(f"malformed source entry in {path}")
        name = canonicalize_name(str(raw.get("name", "")))
        source = Source(
            name=name,
            version=str(raw.get("version", "")),
            filename=str(raw.get("filename", "")),
            sha256=str(raw.get("sha256", "")),
            size=raw.get("size", 0),
            build_backend=raw.get("build_backend"),
            build_requires=tuple(raw.get("build_requires", ())),
        )
        if (
            not name
            or name in result
            or not source.version
            or Path(source.filename).name != source.filename
            or re.fullmatch(r"[0-9a-f]{64}", source.sha256) is None
            or not isinstance(source.size, int)
            or source.size <= 0
        ):
            raise WheelhouseError(f"invalid source entry in {path}: {raw}")
        result[name] = source
    return result


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def verify_sources(
    locked: dict[str, str], sources: dict[str, Source], directory: Path
) -> None:
    if {name: source.version for name, source in sources.items()} != locked:
        raise WheelhouseError("source manifest does not match its exact package lock")
    for source in sources.values():
        archive = directory / source.filename
        try:
            actual_hash, actual_size = file_digest(archive)
        except OSError as exc:
            raise WheelhouseError(f"cannot read source {archive}: {exc}") from exc
        if (actual_hash, actual_size) != (source.sha256, source.size):
            raise WheelhouseError(
                f"source verification failed for {source.filename}: "
                f"expected {source.sha256}/{source.size}, "
                f"got {actual_hash}/{actual_size}"
            )


def snapshot_locks() -> dict[str, str]:
    return {str(path.relative_to(REPO_ROOT)): file_digest(path)[0] for path in LOCKED_INPUTS}


def _metadata(archive: zipfile.ZipFile) -> email.message.Message:
    names = [
        name
        for name in archive.namelist()
        if name.endswith(".dist-info/METADATA") and name.count("/") == 1
    ]
    if len(names) != 1:
        raise WheelhouseError("wheel does not contain exactly one METADATA file")
    return email.parser.BytesParser().parsebytes(archive.read(names[0]))


def inspect_wheel(path: Path, role: str) -> Wheel:
    parts = path.name[:-4].rsplit("-", 3)
    if len(parts) != 4 or not path.name.endswith(".whl"):
        raise WheelhouseError(f"invalid wheel filename: {path.name}")
    _prefix, python_tag, abi_tag, platform_tag = parts
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = _metadata(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelhouseError(f"invalid wheel archive {path}: {exc}") from exc
    name = canonicalize_name(str(metadata.get("Name", "")))
    version = str(metadata.get("Version", ""))
    digest, size = file_digest(path)
    return Wheel(
        name=name,
        version=version,
        filename=path.name,
        sha256=digest,
        size=size,
        role=role,
        python_tag=python_tag,
        abi_tag=abi_tag,
        platform_tag=platform_tag,
    )


def validate_wheel_set(
    directory: Path, expected: dict[str, str], role: str
) -> tuple[Wheel, ...]:
    wheels = tuple(
        sorted(
            (inspect_wheel(path, role) for path in directory.glob("*.whl")),
            key=lambda wheel: (wheel.name, wheel.filename),
        )
    )
    identities = [(wheel.name, wheel.version) for wheel in wheels]
    if len(identities) != len(set(identities)):
        raise WheelhouseError(f"duplicate {role} wheel identity")
    if {wheel.name: wheel.version for wheel in wheels} != expected:
        raise WheelhouseError(f"{role} wheel set does not match its exact lock")
    if role == "runtime":
        leaked = BUILD_ONLY & {wheel.name for wheel in wheels}
        if leaked:
            raise WheelhouseError(f"build-only wheels leaked into runtime: {leaked}")
        by_name = {wheel.name: wheel for wheel in wheels}
        for name in NATIVE_RUNTIME & set(by_name):
            wheel = by_name[name]
            if wheel.abi_tag == "none" or wheel.platform_tag == "any":
                raise WheelhouseError(f"native package produced a pure wheel: {name}")
    return wheels


def verify_orin_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata = _metadata(archive)
        entry_points = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        entry_point_text = (
            archive.read(entry_points[0]).decode("utf-8")
            if len(entry_points) == 1
            else ""
        )
    if canonicalize_name(str(metadata.get("Name", ""))) != "orin-stage":
        raise WheelhouseError("Orin Stage wheel has the wrong project name")
    if metadata.get("Version") != "0.1.0":
        raise WheelhouseError("Orin Stage wheel version is not 0.1.0")
    required = {
        "orin_stage/__init__.py",
        "orin_stage/catalog/data/schema/target.schema.json",
    }
    if not required <= names:
        raise WheelhouseError("Orin Stage wheel lacks package or catalog schema")
    targets = {
        name
        for name in names
        if name.startswith("orin_stage/catalog/data/targets/")
        and name.endswith(".yaml")
    }
    hardware = {
        name
        for name in names
        if name.startswith("orin_stage/catalog/data/hardware/")
        and name.endswith(".yaml")
    }
    if len(targets) != 7 or len(hardware) != 2:
        raise WheelhouseError("Orin Stage wheel has an incomplete catalog")
    if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
        raise WheelhouseError("Orin Stage wheel lacks LICENSE")
    if len(entry_points) != 1 or "ostg = orin_stage.cli:main" not in entry_point_text:
        raise WheelhouseError("Orin Stage wheel lacks entry point metadata")


def _run(command: Sequence[str], *, environment: dict[str, str], cwd: Path | None = None) -> None:
    print(f"RUN {shlex.join(command)}", flush=True)
    try:
        subprocess.run(
            list(command),
            check=True,
            cwd=cwd,
            env=environment,
            shell=False,
        )
    except subprocess.CalledProcessError as exc:
        raise WheelhouseError(
            f"command failed with status {exc.returncode}: {shlex.join(command)}"
        ) from exc


def _find_new_wheel(
    directory: Path, before: set[Path], name: str, version: str, role: str
) -> Path:
    candidates = set(directory.glob("*.whl")) - before
    matches = [
        path
        for path in candidates
        if (inspect_wheel(path, role).name, inspect_wheel(path, role).version)
        == (name, version)
    ]
    if len(matches) != 1:
        raise WheelhouseError(
            f"build for {name}=={version} produced {len(matches)} matching wheels"
        )
    return matches[0]


def _build_one(
    python: Path,
    source: Source,
    source_directory: Path,
    output_directory: Path,
    role: str,
    environment: dict[str, str],
) -> Path:
    before = set(output_directory.glob("*.whl"))
    requirements = ", ".join(source.build_requires) or "in-tree/self-contained"
    print(
        f"BUILD {source.name}=={source.version} role={role} "
        f"backend={source.build_backend or 'legacy setuptools'} requires=[{requirements}]",
        flush=True,
    )
    _run(
        (
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-build-isolation",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(output_directory),
            str(source_directory / source.filename),
        ),
        environment=environment,
    )
    return _find_new_wheel(
        output_directory, before, source.name, source.version, role
    )


def _install_build_wheel(
    python: Path, wheel: Path, environment: dict[str, str]
) -> None:
    _run(
        (
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            "--force-reinstall",
            str(wheel),
        ),
        environment=environment,
    )


def _copy_project(destination: Path) -> None:
    destination.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / filename, destination / filename)
    shutil.copytree(REPO_ROOT / "src", destination / "src")


def _write_manifest(path: Path, series: str, wheels: Iterable[Wheel]) -> None:
    records = [
        {
            "name": wheel.name,
            "version": wheel.version,
            "filename": wheel.filename,
            "sha256": wheel.sha256,
            "size": wheel.size,
            "role": wheel.role,
            "python_tag": wheel.python_tag,
            "abi_tag": wheel.abi_tag,
            "platform_tag": wheel.platform_tag,
        }
        for wheel in sorted(wheels, key=lambda item: (item.role, item.name))
    ]
    payload = {"schema_version": 1, "series": series, "wheels": records}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def verify_wheel_manifest(root: Path, series: str) -> tuple[Wheel, ...]:
    path = root / "WHEELS.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WheelhouseError(f"cannot load wheel manifest {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "series", "wheels"}
        or payload["schema_version"] != 1
        or payload["series"] != series
        or not isinstance(payload["wheels"], list)
    ):
        raise WheelhouseError("wheel manifest has an unsupported structure")
    expected_fields = {
        "name",
        "version",
        "filename",
        "sha256",
        "size",
        "role",
        "python_tag",
        "abi_tag",
        "platform_tag",
    }
    wheels: list[Wheel] = []
    for raw in payload["wheels"]:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise WheelhouseError("wheel manifest contains a malformed record")
        string_fields = expected_fields - {"size"}
        if (
            any(not isinstance(raw[field], str) for field in string_fields)
            or not isinstance(raw["size"], int)
            or isinstance(raw["size"], bool)
            or raw["filename"] != Path(raw["filename"]).name
        ):
            raise WheelhouseError("wheel manifest contains invalid field values")
        if raw["role"] not in {"build", "runtime"}:
            raise WheelhouseError("wheel manifest contains an invalid role")
        wheel_path = root / raw["role"] / raw["filename"]
        actual = inspect_wheel(wheel_path, raw["role"])
        recorded = Wheel(**raw)
        if actual != recorded:
            raise WheelhouseError(f"wheel manifest mismatch: {raw['filename']}")
        wheels.append(recorded)
    if [(wheel.role, wheel.name) for wheel in wheels] != sorted(
        (wheel.role, wheel.name) for wheel in wheels
    ):
        raise WheelhouseError("wheel manifest records are not sorted")
    build_lock = read_exact_lock(BUILD_LOCK)
    runtime_lock = read_exact_lock(RUNTIME_LOCK)
    runtime_lock["orin-stage"] = "0.1.0"
    validate_wheel_set(root / "build", build_lock, "build")
    validate_wheel_set(root / "runtime", runtime_lock, "runtime")
    orin = [wheel for wheel in wheels if wheel.name == "orin-stage"]
    if len(orin) != 1:
        raise WheelhouseError("wheel manifest lacks exact Orin Stage identity")
    verify_orin_wheel(root / "runtime" / orin[0].filename)
    return tuple(wheels)


def build_wheelhouse(
    *,
    series: str,
    system_python: Path,
    cargo: Path,
    rustc: Path,
    source_directory: Path,
    vendor_directory: Path,
    output_directory: Path,
) -> None:
    if output_directory.exists():
        raise WheelhouseError(f"output already exists: {output_directory}")
    runtime_lock = read_exact_lock(RUNTIME_LOCK)
    build_lock = read_exact_lock(BUILD_LOCK)
    runtime_sources = load_sources(RUNTIME_SOURCES)
    build_sources = load_sources(BUILD_SOURCES)
    verify_sources(runtime_lock, runtime_sources, source_directory)
    verify_sources(build_lock, build_sources, source_directory)
    if tuple(BUILD_ORDER) != tuple(name for name in BUILD_ORDER if name in build_lock):
        raise WheelhouseError("bootstrap order does not cover the build-tools lock")
    if set(BUILD_ORDER) != set(build_lock):
        raise WheelhouseError("bootstrap order does not exactly match build-tools.lock")
    if not vendor_directory.is_dir():
        raise WheelhouseError(f"Cargo vendor tree is absent: {vendor_directory}")
    before_locks = snapshot_locks()

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            suffix=".partial",
            dir=output_directory.parent,
        )
    )
    work = Path(tempfile.mkdtemp(prefix=f"orin-stage-{series}-wheel-build."))
    try:
        build_output = stage / "build"
        runtime_output = stage / "runtime"
        build_output.mkdir()
        runtime_output.mkdir()
        home = work / "home"
        cargo_home = work / "cargo-home"
        target = work / "cargo-target"
        toolchain_bin = work / "toolchain-bin"
        home.mkdir()
        cargo_home.mkdir()
        toolchain_bin.mkdir()
        (toolchain_bin / "cargo").symlink_to(cargo)
        (toolchain_bin / "rustc").symlink_to(rustc)
        cargo_config = (
            '[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
            f'[source.vendored-sources]\ndirectory = "{vendor_directory.resolve()}"\n\n'
            "[net]\noffline = true\n"
        )
        (cargo_home / "config.toml").write_text(cargo_config, encoding="utf-8")
        venv = work / "build-venv"
        base_environment = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_ROOT_USER_ACTION": "ignore",
            "CARGO": str(cargo),
            "RUSTC": str(rustc),
            "CARGO_HOME": str(cargo_home),
            "CARGO_TARGET_DIR": str(target),
            "CARGO_NET_OFFLINE": "true",
            "MATURIN_NO_INSTALL_RUST": "1",
            "MATURIN_SETUP_ARGS": "--no-default-features --offline",
            "MATURIN_PEP517_ARGS": "--locked --offline",
            "PYYAML_FORCE_CYTHON": "1",
        }
        _run(
            (str(system_python), "-m", "venv", "--system-site-packages", str(venv)),
            environment=base_environment,
        )
        python = venv / "bin" / "python"
        environment = dict(base_environment)
        environment["PATH"] = (
            f"{venv / 'bin'}:{toolchain_bin}:{base_environment['PATH']}"
        )

        for name in BUILD_ORDER:
            wheel = _build_one(
                python,
                build_sources[name],
                source_directory,
                build_output,
                "build",
                environment,
            )
            _install_build_wheel(python, wheel, environment)

        for name in runtime_lock:
            _build_one(
                python,
                runtime_sources[name],
                source_directory,
                runtime_output,
                "runtime",
                environment,
            )

        project = work / "project"
        _copy_project(project)
        before = set(runtime_output.glob("*.whl"))
        print("BUILD orin-stage==0.1.0 role=runtime backend=setuptools.build_meta", flush=True)
        _run(
            (
                str(python),
                "-m",
                "pip",
                "wheel",
                "--no-index",
                "--no-build-isolation",
                "--no-deps",
                "--no-cache-dir",
                "--wheel-dir",
                str(runtime_output),
                str(project),
            ),
            environment=environment,
        )
        orin_wheel = _find_new_wheel(
            runtime_output, before, "orin-stage", "0.1.0", "runtime"
        )
        verify_orin_wheel(orin_wheel)

        build_wheels = validate_wheel_set(build_output, build_lock, "build")
        runtime_expected = dict(runtime_lock)
        runtime_expected["orin-stage"] = "0.1.0"
        runtime_wheels = validate_wheel_set(
            runtime_output, runtime_expected, "runtime"
        )
        _write_manifest(stage / "WHEELS.json", series, (*build_wheels, *runtime_wheels))
        verify_wheel_manifest(stage, series)
        if snapshot_locks() != before_locks:
            raise WheelhouseError("a source lock changed during the build")
        os.replace(stage, output_directory)
        print(
            f"COMPLETE series={series} build_wheels={len(build_wheels)} "
            f"runtime_wheels={len(runtime_wheels)} locks=UNCHANGED",
            flush=True,
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the exact Orin Stage wheelhouses without network access."
    )
    parser.add_argument("--series", choices=("jammy", "noble"), required=True)
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    parser.add_argument("--cargo", type=Path, default=Path("/usr/bin/cargo-1.85"))
    parser.add_argument("--rustc", type=Path, default=Path("/usr/bin/rustc-1.85"))
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCE_DIRECTORY)
    parser.add_argument("--vendor", type=Path, default=DEFAULT_VENDOR_DIRECTORY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = (args.output_root / args.series).resolve()
        if args.verify_only:
            wheels = verify_wheel_manifest(output, args.series)
            print(f"VERIFIED series={args.series} wheels={len(wheels)}")
        else:
            build_wheelhouse(
                series=args.series,
                system_python=args.python.resolve(),
                cargo=args.cargo.resolve(),
                rustc=args.rustc.resolve(),
                source_directory=args.sources.resolve(),
                vendor_directory=args.vendor.resolve(),
                output_directory=output,
            )
    except WheelhouseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
