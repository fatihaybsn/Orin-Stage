#!/usr/bin/env python3
"""Stage and verify the installed private-runtime layout from a 6C wheelhouse.

This is release preparation tooling.  It never writes to the host ``/usr`` and
accepts only the exact runtime wheels already verified by 6C.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = REPO_ROOT / "release" / "dependencies"
RUNTIME_LOCK = DEPENDENCIES / "runtime.lock"
WHEELHOUSE_ROOT = DEPENDENCIES / "generated" / "wheelhouse"
DEFAULT_OUTPUT_ROOT = DEPENDENCIES / "generated" / "private-runtime"
RUNTIME_PREFIX = Path("/usr/lib/orin-stage/venv")
LAUNCHER_PATH = Path("/usr/bin/ostg")
BUILD_ONLY = {
    "calver",
    "cython",
    "flit-core",
    "hatch-fancy-pypi-readme",
    "hatch-vcs",
    "hatchling",
    "maturin",
    "packaging",
    "pathspec",
    "pluggy",
    "semantic-version",
    "setuptools-rust",
    "setuptools-scm",
    "tomli",
    "trove-classifiers",
}
BOOTSTRAP = {"pip", "setuptools"}


class RuntimeStageError(RuntimeError):
    """Raised when the installed-runtime contract cannot be proven."""


def canonicalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_exact_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)==([^\s]+)", line)
        if match is None:
            raise RuntimeStageError(f"lock entry is not exact: {line}")
        name, version = match.groups()
        if name in result:
            raise RuntimeStageError(f"duplicate package in {path}: {name}")
        result[name] = version
    return result


def _load_wheelhouse_module():
    path = REPO_ROOT / "tools" / "release" / "build_offline_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("orin_stage_wheelhouse", path)
    if spec is None or spec.loader is None:
        raise RuntimeStageError(f"cannot load 6C wheelhouse verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runtime_wheels(wheelhouse: Path, series: str) -> tuple[Path, ...]:
    """Return only the exact runtime wheels after the full 6C verification."""
    verifier = _load_wheelhouse_module()
    try:
        wheels = verifier.verify_wheel_manifest(wheelhouse, series)
    except Exception as exc:  # The verifier's domain exception is implementation detail.
        raise RuntimeStageError(f"6C wheelhouse verification failed: {exc}") from exc
    selected = tuple(
        wheelhouse / "runtime" / wheel.filename
        for wheel in wheels
        if wheel.role == "runtime"
    )
    expected = read_exact_lock(RUNTIME_LOCK)
    expected["orin-stage"] = "0.1.0"
    if len(selected) != len(expected):
        raise RuntimeStageError("runtime wheel count does not match runtime.lock")
    return selected


def launcher_text(runtime_python: Path) -> str:
    """Return the intentionally minimal, shell-free Python invocation wrapper."""
    return (
        "#!/bin/sh\n"
        f'exec "{runtime_python}" -I -m orin_stage.cli "$@"\n'
    )


def _run(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=check,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeStageError(f"cannot run {' '.join(command)}: {exc}") from exc


def _clean_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _distribution_payload(python: Path, environment: dict[str, str], cwd: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json\n"
        "print(json.dumps({d.metadata['Name'].lower().replace('_','-'): d.version "
        "for d in m.distributions() if d.metadata.get('Name')}, sort_keys=True))\n"
    )
    completed = _run((str(python), "-I", "-c", code), environment=environment, cwd=cwd)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeStageError("could not inspect installed distributions") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(name, str) and isinstance(version, str)
        for name, version in payload.items()
    ):
        raise RuntimeStageError("installed distribution report is malformed")
    return {canonicalize_name(name): version for name, version in payload.items()}


def validate_installed_distributions(installed: dict[str, str]) -> None:
    expected = read_exact_lock(RUNTIME_LOCK)
    expected["orin-stage"] = "0.1.0"
    actual_runtime = {name: installed.get(name) for name in expected}
    if actual_runtime != expected:
        raise RuntimeStageError(
            f"installed runtime package set differs from runtime.lock: {actual_runtime}"
        )
    unexpected = set(installed) - set(expected) - BOOTSTRAP
    if unexpected:
        raise RuntimeStageError(f"unexpected installed distribution(s): {sorted(unexpected)}")
    leaked = BUILD_ONLY & set(installed)
    if leaked:
        raise RuntimeStageError(f"build-only packages leaked into runtime: {sorted(leaked)}")


def _harden_tree(stage_root: Path, launcher: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeStageError("root-owned staging proof must run as uid 0")
    paths = sorted(stage_root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    paths.append(stage_root)
    for path in paths:
        if path.is_symlink():
            os.chown(path, 0, 0, follow_symlinks=False)
            continue
        os.chown(path, 0, 0)
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path == launcher or "/bin/" in str(path) else 0o644)


def validate_tree_permissions(stage_root: Path, *, owner_uid: int = 0) -> None:
    required = (
        stage_root / "usr" / "bin" / "ostg",
        stage_root / "usr" / "lib" / "orin-stage",
        stage_root / "usr" / "lib" / "orin-stage" / "venv",
    )
    for path in required:
        info = path.lstat()
        if info.st_uid != owner_uid:
            raise RuntimeStageError(f"not owned by uid {owner_uid}: {path}")
    for path in (stage_root, *stage_root.rglob("*")):
        info = path.lstat()
        # Symlink mode bits are not permissions on Linux; its protected parent
        # directory controls whether an unprivileged user can replace it.
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeStageError(f"runtime path is group/world writable: {path}")


def _assert_success(label: str, completed: subprocess.CompletedProcess[str]) -> None:
    if completed.returncode != 0:
        raise RuntimeStageError(
            f"{label} failed ({completed.returncode}): {completed.stderr.strip()}"
        )


def _assert_not_hijacked(
    launcher: Path,
    python: Path,
    environment: dict[str, str],
    cwd: Path,
    fake_root: Path,
) -> None:
    fake_package = fake_root / "orin_stage"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text(
        "raise RuntimeError('FAKE ORIN STAGE WAS IMPORTED')\n", encoding="utf-8"
    )
    version = _run(
        (str(python), "-I", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"),
        environment=environment,
        cwd=cwd,
    ).stdout.strip()
    user_site = (
        Path(environment["HOME"])
        / ".local"
        / "lib"
        / f"python{version}"
        / "site-packages"
        / "orin_stage"
    )
    user_site.mkdir(parents=True)
    (user_site / "__init__.py").write_text(
        "raise RuntimeError('USER SITE HIJACKED ORIN STAGE')\n", encoding="utf-8"
    )
    poisoned = dict(environment)
    poisoned.update(
        {
            "PYTHONPATH": str(fake_root),
            "PYTHONHOME": str(fake_root),
        }
    )
    completed = _run((str(launcher), "--version"), environment=poisoned, cwd=fake_root, check=False)
    _assert_success("-I injection proof", completed)
    if "FAKE ORIN STAGE" in completed.stdout + completed.stderr:
        raise RuntimeStageError("fake package was imported despite -I")


def _validate_package_data(python: Path, environment: dict[str, str], cwd: Path) -> None:
    code = (
        "import importlib.metadata as m, json\n"
        "from importlib.resources import files\n"
        "base = files('orin_stage').joinpath('catalog', 'data')\n"
        "paths = list(base.joinpath('targets').iterdir())\n"
        "hardware = list(base.joinpath('hardware').iterdir())\n"
        "license = any(str(item).endswith('licenses/LICENSE') for item in (m.files('orin-stage') or ()))\n"
        "print(json.dumps({'schema': base.joinpath('schema', 'target.schema.json').is_file(), "
        "'targets': len([p for p in paths if str(p).endswith('.yaml')]), "
        "'hardware': len([p for p in hardware if str(p).endswith('.yaml')]), 'license': license}))\n"
    )
    completed = _run((str(python), "-I", "-c", code), environment=environment, cwd=cwd)
    payload = json.loads(completed.stdout)
    if payload != {"schema": True, "targets": 7, "hardware": 2, "license": True}:
        raise RuntimeStageError(f"installed package data is incomplete: {payload}")


def _privileged_reexec_audit(python: Path, environment: dict[str, str], cwd: Path) -> None:
    code = r'''
import json
import subprocess
import sys
from pathlib import Path
from orin_stage.base.construction import BaseBuildResult
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
from orin_stage.privileged_base import ensure_jp623_base_with_sudo
from orin_stage.privileged_materialization import create_materialization_seed_with_sudo
from orin_stage.privileged_storage_delete import remove_base_storage_with_sudo
from orin_stage.privileged_storage_measure import measure_base_storage_with_sudo

root = Path.cwd() / 'privileged-data'
target = root / 'targets' / ('a' * 64)
target.mkdir(parents=True)
commands = []
def runner(command, **kwargs):
    commands.append({'command': list(command), 'kwargs': kwargs})
    module = command[5]
    if module == 'orin_stage.privileged_materialization':
        output = json.dumps({'archive_path': str(target / 'materialization' / 'seed.tar'), 'metadata_path': str(target / 'materialization' / 'seed.json'), 'seed_sha256': 'b' * 64})
    elif module == 'orin_stage.privileged_base':
        output = json.dumps({'target_directory': str(target), 'base_path': str(target / 'base'), 'lock_path': str(target / 'lock.json'), 'manifest_path': str(target / 'manifest.json'), 'receipt_path': str(target / 'receipt.json'), 'target_lock_digest': 'a' * 64, 'base_digest': 'b' * 64, 'cache_hit': False})
    elif module == 'orin_stage.privileged_storage_delete':
        output = json.dumps({'removed': True, 'target_lock_digest': 'a' * 64})
    else:
        output = json.dumps({'bytes_used': 0})
    return subprocess.CompletedProcess(command, 0, output, '')

create_materialization_seed_with_sudo(target, data_root=root, runner=runner, which=lambda _: '/usr/bin/sudo')
catalog = builtin_catalog_paths()
target_record = TargetResolver(catalog.targets_dir, catalog.schema_path).resolve('jetson-orin@jp6.2.3')
ensure_jp623_base_with_sudo(target_record, acquisition_receipt_path=root / 'receipt.json', data_root=root, qemu_binary=Path('/usr/bin/qemu-aarch64-static'), runner=runner, which=lambda _: '/usr/bin/sudo')
remove_base_storage_with_sudo(root, 'a' * 64, runner=runner)
measure_base_storage_with_sudo(root, 'a' * 64, runner=runner)
print(json.dumps({'executable': sys.executable, 'commands': commands}))
'''
    completed = _run((str(python), "-I", "-c", code), environment=environment, cwd=cwd)
    payload = json.loads(completed.stdout)
    if payload.get("executable") != str(python):
        raise RuntimeStageError("installed interpreter did not preserve its venv path")
    commands = payload.get("commands")
    if not isinstance(commands, list) or len(commands) != 4:
        raise RuntimeStageError("privileged adapter audit did not produce four commands")
    expected_modules = {
        "orin_stage.privileged_materialization",
        "orin_stage.privileged_base",
        "orin_stage.privileged_storage_delete",
        "orin_stage.privileged_storage_measure",
    }
    for record in commands:
        command = record.get("command", [])
        kwargs = record.get("kwargs", {})
        if (
            command[:3] != [command[0], "--", str(python)]
            or command[3:5] != ["-I", "-m"]
            or command[5] not in expected_modules
            or "-E" in command
            or "PYTHONPATH" in command
            or kwargs.get("shell") is not False
        ):
            raise RuntimeStageError(f"unsafe privileged re-exec command: {command}")


def stage_private_runtime(
    *,
    series: str,
    system_python: Path,
    wheelhouse: Path,
    output_root: Path,
) -> Path:
    if series not in {"jammy", "noble"}:
        raise RuntimeStageError("series must be jammy or noble")
    output_root = output_root.resolve()
    if output_root.exists():
        raise RuntimeStageError(f"staging root already exists: {output_root}")
    wheels = runtime_wheels(wheelhouse, series)
    if os.geteuid() != 0:
        raise RuntimeStageError("root-owned staging proof must run as uid 0")

    # A venv embeds absolute paths in its generated scripts and configuration.
    # Therefore it must be created at the final staging path, not moved there
    # from an atomic temporary directory afterwards.
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir()
    work = Path(tempfile.mkdtemp(prefix=f"orin-stage-{series}-private-runtime-"))
    try:
        runtime = output_root / RUNTIME_PREFIX.relative_to("/")
        launcher = output_root / LAUNCHER_PATH.relative_to("/")
        home = work / "home"
        outside_cwd = work / "outside-checkout"
        home.mkdir(parents=True)
        outside_cwd.mkdir()
        environment = _clean_environment(home)
        _run((str(system_python), "-m", "venv", str(runtime)), environment=environment, cwd=outside_cwd)
        python = runtime / "bin" / "python"
        install = _run(
            (
                str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                "--no-cache-dir", "--force-reinstall", *(str(wheel) for wheel in wheels),
            ),
            environment=environment,
            cwd=outside_cwd,
        )
        _assert_success("offline runtime install", install)
        launcher.parent.mkdir(parents=True)
        launcher.write_text(launcher_text(python), encoding="utf-8")
        launcher.chmod(0o755)

        validate_installed_distributions(_distribution_payload(python, environment, outside_cwd))
        _validate_package_data(python, environment, outside_cwd)
        _assert_not_hijacked(
            launcher, python, environment, outside_cwd, outside_cwd / "fake-cwd"
        )
        for arguments in (("--version",), ("--help",), ("target", "list")):
            completed = _run((str(launcher), *arguments), environment=environment, cwd=outside_cwd, check=False)
            _assert_success(f"ostg {' '.join(arguments)}", completed)
        _privileged_reexec_audit(python, environment, outside_cwd)
        if (home / ".local" / "share" / "orin-stage").exists():
            raise RuntimeStageError("runtime installation created a user data root")
        _harden_tree(output_root, launcher)
        validate_tree_permissions(output_root)
    except Exception:
        shutil.rmtree(output_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output_root


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", required=True, choices=("jammy", "noble"))
    parser.add_argument("--system-python", type=Path, default=Path("/usr/bin/python3"))
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--output-root", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    wheelhouse = args.wheelhouse or WHEELHOUSE_ROOT / args.series
    output_root = args.output_root or DEFAULT_OUTPUT_ROOT / args.series
    try:
        staged = stage_private_runtime(
            series=args.series,
            system_python=args.system_python,
            wheelhouse=wheelhouse,
            output_root=output_root,
        )
    except RuntimeStageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"COMPLETE series={args.series} staging_root={staged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
