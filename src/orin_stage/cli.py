from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import __version__
from .acquisition.sdk_manager import SdkManagerClient
from .base.lock import load_target_lock
from .base.receipt import base_directory_is_reusable, load_base_receipt
from .build_capsule import BuildCommandError
from .build_toolchain import BuildToolchainManager
from .catalog import CatalogError, TargetResolver, builtin_catalog_paths
from .catalog.resolver import ResolvedCatalogTarget
from .doctor import doctor_exit_code, format_report, run_doctor
from .materialization_seed import SEED_FORMAT, SEED_FORMAT_VERSION
from .planning.orchestration import (
    JP623_HARDWARE_PROFILE,
    JP623_QEMU_BINARY,
    JP623_SDK_MANAGER_TARGET,
    ReleaseEnsureResult,
    ensure_jp623_release,
)
from .planning.planner import BasePlanStatus
from .privileged_base import ensure_jp623_base_with_sudo
from .privileged_materialization import create_materialization_seed_with_sudo
from .runtime import resolve_data_root
from .storage import DeletionPlan, StorageManager
from .target_executor import TargetCommandError
from .workspace_manager import (
    WorkspaceListEntry,
    WorkspaceManager,
    WorkspaceRecord,
)


PROGRAM_NAME = "ostg"
PROGRAM_VERSION = __version__


def _semantic_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _format_target_list() -> str:
    paths = builtin_catalog_paths()
    resolver = TargetResolver(paths.targets_dir, paths.schema_path)
    targets = sorted(
        resolver.list_targets(),
        key=lambda target: _semantic_version_key(target.jetpack_version),
    )
    rows = [
        ("TARGET", "JETPACK", "L4T", "STATUS"),
        *(
            (
                target.primary_selector,
                target.jetpack_version,
                target.jetson_linux_version,
                target.support_status,
            )
            for target in targets
        ),
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    return "\n".join(
        "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )


def _run_target_list() -> int:
    try:
        output = _format_target_list()
    except CatalogError as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: cannot load built-in target catalog: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _validate_target_ensure_status(
    target: ResolvedCatalogTarget,
    *,
    allow_validation_pending: bool,
) -> None:
    if target.is_unavailable:
        raise RuntimeError(
            f"target {target.selector!r} is unavailable and cannot be ensured"
        )
    version = str(target.record["release"]["jetpack"]["version"])
    if version != "6.2.3":
        raise RuntimeError("target ensure is currently implemented only for JP6.2.3")
    if target.is_validation_pending and not allow_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} is validation-pending; "
            "pass --allow-validation-pending to continue explicitly"
        )
    if not target.is_supported and not target.is_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} has unsupported status "
            f"{target.support_status!r}"
        )


def _format_target_ensure_result(result: ReleaseEnsureResult) -> str:
    target = result.target
    status = target.support_status
    if target.is_validation_pending:
        status = f"{status} (explicitly allowed)"
    acquisition = result.acquisition_result
    acquisition_status = (
        "cache-hit"
        if acquisition is None or acquisition.cache_hit
        else "downloaded+verified"
    )

    if result.final_plan.base_status is BasePlanStatus.BASE_REUSE:
        base_status = "reused"
        base_digest = result.final_plan.base_digest
        reference = result.final_plan.base_reference
        base_path = Path(reference) / "base" if reference is not None else None
    else:
        if result.base_result is None:
            raise RuntimeError("base construction returned no result")
        base_status = (
            "reused" if result.base_result.cache_hit else "constructed+validated"
        )
        base_digest = result.base_result.base_digest
        base_path = result.base_result.base_path

    if base_digest is None or base_path is None:
        raise RuntimeError("release ensure returned incomplete base evidence")
    rows = (
        ("Target:", target.selector),
        ("Status:", status),
        ("Acquisition:", acquisition_status),
        ("Base:", base_status),
        ("Base digest:", base_digest),
        ("Base path:", str(base_path)),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _run_target_ensure(
    selector: str,
    *,
    allow_validation_pending: bool,
    data_root: Path,
) -> int:
    if os.geteuid() == 0:
        print("error: Run target ensure as your normal user.", file=sys.stderr)
        print(
            "Orin Stage requests sudo only when base construction is required.",
            file=sys.stderr,
        )
        return 1

    try:
        paths = builtin_catalog_paths()
        resolver = TargetResolver(paths.targets_dir, paths.schema_path)
        target = resolver.resolve(selector)
        _validate_target_ensure_status(
            target,
            allow_validation_pending=allow_validation_pending,
        )
        result = ensure_jp623_release(
            resolver,
            SdkManagerClient(),
            selector=selector,
            hardware_profile=JP623_HARDWARE_PROFILE,
            required_sdk_manager_target=JP623_SDK_MANAGER_TARGET,
            data_root=data_root,
            qemu_binary=JP623_QEMU_BINARY,
            base_builder=ensure_jp623_base_with_sudo,
        )
        output = _format_target_ensure_result(result)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _validate_workspace_create_status(
    target: ResolvedCatalogTarget,
    *,
    allow_validation_pending: bool,
) -> None:
    if target.is_unavailable:
        raise RuntimeError(
            f"target {target.selector!r} is unavailable and cannot be used"
        )
    version = str(target.record["release"]["jetpack"]["version"])
    if version != "6.2.3":
        raise RuntimeError(
            "workspace create is currently implemented only for JP6.2.3"
        )
    if target.is_validation_pending and not allow_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} is validation-pending; "
            "pass --allow-validation-pending to continue explicitly"
        )
    if not target.is_supported and not target.is_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} has unsupported status "
            f"{target.support_status!r}"
        )


def _find_realized_target(
    data_root: Path,
    target: ResolvedCatalogTarget,
) -> Path | None:
    targets_dir = data_root / "targets"
    if not targets_dir.exists():
        return None
    if targets_dir.is_symlink() or not targets_dir.is_dir():
        raise RuntimeError(f"targets path is not a real directory: {targets_dir}")

    for candidate in sorted(targets_dir.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if not base_directory_is_reusable(candidate):
            continue
        lock = load_target_lock(candidate / "lock.json")
        target_metadata = lock.get("target")
        if (
            isinstance(target_metadata, Mapping)
            and target_metadata.get("canonical_id") == target.canonical_id
        ):
            return candidate
    return None


def _materialization_seed_is_complete(target_dir: Path) -> bool:
    seed_dir = target_dir / "materialization"
    if seed_dir.is_symlink() or (seed_dir.exists() and not seed_dir.is_dir()):
        raise RuntimeError(
            f"materialization path is not a real directory: {seed_dir}"
        )
    archive = seed_dir / "seed.tar"
    metadata = seed_dir / "seed.json"
    archive_exists = os.path.lexists(archive)
    metadata_exists = os.path.lexists(metadata)
    if archive_exists != metadata_exists:
        raise RuntimeError(
            f"materialization seed is incomplete under {seed_dir}; "
            "refusing to overwrite it"
        )
    if not archive_exists:
        return False
    if (
        archive.is_symlink()
        or not archive.is_file()
        or metadata.is_symlink()
        or not metadata.is_file()
    ):
        raise RuntimeError(f"materialization seed is invalid under {seed_dir}")
    return True


def _validate_materialization_seed_identity(target_dir: Path) -> None:
    seed_path = target_dir / "materialization" / "seed.json"
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read materialization seed metadata: {exc}") from exc
    if not isinstance(seed, dict):
        raise RuntimeError("materialization seed metadata is not a JSON object")
    if seed.get("format") != SEED_FORMAT:
        raise RuntimeError("materialization seed has an unsupported format")
    if seed.get("format_version") != SEED_FORMAT_VERSION:
        raise RuntimeError("materialization seed has an unsupported format version")
    if seed.get("archive") != "seed.tar":
        raise RuntimeError("materialization seed has an invalid archive name")
    seed_sha256 = seed.get("seed_sha256")
    if (
        not isinstance(seed_sha256, str)
        or len(seed_sha256) != 64
        or any(character not in "0123456789abcdef" for character in seed_sha256)
    ):
        raise RuntimeError("materialization seed has an invalid SHA-256")

    receipt = load_base_receipt(target_dir / "receipt.json")
    target_lock_digest = seed.get("target_lock_digest")
    base_digest = seed.get("base_digest")
    if (
        target_lock_digest != target_dir.name
        or target_lock_digest != receipt.get("target_lock_digest")
    ):
        raise RuntimeError("materialization seed target lock does not match the base")
    if base_digest != receipt.get("base_digest"):
        raise RuntimeError("materialization seed base digest does not match the base")


def _format_workspace_list(entries: Sequence[WorkspaceListEntry]) -> str:
    rows = [
        ("NAME", "ID", "JETPACK", "GENERATION"),
        *(
            (
                entry.workspace_name,
                entry.workspace_id,
                entry.jetpack_version,
                str(entry.generation),
            )
            for entry in entries
        ),
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    return "\n".join(
        "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )


def _run_workspace_list(data_root: Path) -> int:
    try:
        entries = (
            WorkspaceManager(data_root).list_workspaces()
            if data_root.exists()
            else ()
        )
        output = _format_workspace_list(entries)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _format_workspace_create_result(
    record: WorkspaceRecord,
    *,
    selector: str,
    materialization: str,
) -> str:
    rows = (
        ("Workspace:", record.workspace_name),
        ("Workspace ID:", record.workspace_id),
        ("Target:", selector),
        ("Generation:", str(record.generation)),
        ("Root:", str(record.root_path)),
        ("Materialization:", materialization),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _run_workspace_create(
    selector: str,
    workspace_name: str,
    *,
    allow_validation_pending: bool,
    data_root: Path,
) -> int:
    if os.geteuid() == 0:
        print("error: Run workspace create as your normal user.", file=sys.stderr)
        print(
            "Orin Stage requests sudo only when materialization seed creation "
            "is required.",
            file=sys.stderr,
        )
        return 1

    try:
        paths = builtin_catalog_paths()
        resolver = TargetResolver(paths.targets_dir, paths.schema_path)
        target = resolver.resolve(selector)
        _validate_workspace_create_status(
            target,
            allow_validation_pending=allow_validation_pending,
        )
        target_dir = _find_realized_target(data_root, target)
        if target_dir is None:
            command = f"ostg target ensure {selector}"
            if target.is_validation_pending:
                command = f"{command} --allow-validation-pending"
            print("error: Target is not ensured. Run:", file=sys.stderr)
            print(command, file=sys.stderr)
            return 1

        if _materialization_seed_is_complete(target_dir):
            materialization = "reused"
        else:
            create_materialization_seed_with_sudo(
                target_dir,
                data_root=data_root,
            )
            materialization = "created"

        if not _materialization_seed_is_complete(target_dir):
            raise RuntimeError("materialization seed creation returned no complete seed")
        _validate_materialization_seed_identity(target_dir)

        record = WorkspaceManager(data_root).create(target_dir, workspace_name)
        output = _format_workspace_create_result(
            record,
            selector=target.selector,
            materialization=materialization,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _target_exit_code(returncode: int) -> int:
    if 1 <= returncode <= 255:
        return returncode
    if -127 <= returncode < 0:
        return 128 - returncode
    return 1


def _forward_target_output(error: TargetCommandError) -> None:
    if error.stdout:
        sys.stdout.write(error.stdout)
    if error.stderr:
        sys.stderr.write(error.stderr)


def _run_workspace_shell(selector: str, *, data_root: Path) -> int:
    if os.geteuid() == 0:
        print("error: Run ostg shell as your normal user.", file=sys.stderr)
        return 1

    try:
        WorkspaceManager(data_root).shell(selector)
    except TargetCommandError as exc:
        _forward_target_output(exc)
        return _target_exit_code(exc.returncode)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    return 0


def _run_workspace_command(
    selector: str,
    command: Sequence[str],
    *,
    data_root: Path,
) -> int:
    if os.geteuid() == 0:
        print("error: Run ostg run as your normal user.", file=sys.stderr)
        return 1

    try:
        completed = WorkspaceManager(data_root).run(selector, command)
    except TargetCommandError as exc:
        _forward_target_output(exc)
        return _target_exit_code(exc.returncode)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return 0


def _run_workspace_build(
    selector: str,
    command: Sequence[str],
    *,
    data_root: Path,
) -> int:
    if os.geteuid() == 0:
        print("error: Run ostg build as your normal user.", file=sys.stderr)
        return 1

    try:
        repository_root = Path.cwd()
        toolchain = BuildToolchainManager(data_root).ensure()
        completed = WorkspaceManager(data_root).build(
            selector,
            repository_root,
            toolchain.root_path,
            command,
        )
    except BuildCommandError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        return _target_exit_code(exc.returncode)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return 0


def _format_allocated_bytes(bytes_used: int) -> str:
    amount = float(bytes_used)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable storage unit")


def _validate_workspace_plan_binding(
    record: WorkspaceRecord,
    plan: DeletionPlan,
) -> None:
    if (
        plan.kind != "workspace"
        or plan.identifier != record.workspace_id
        or plan.path != record.workspace_path
    ):
        raise RuntimeError("workspace storage plan does not match workspace metadata")


def _format_workspace_reset_plan(
    record: WorkspaceRecord,
    plan: DeletionPlan,
) -> str:
    rows = (
        ("Workspace:", record.workspace_name),
        ("Workspace ID:", record.workspace_id),
        ("Generation:", f"{record.generation} -> {record.generation + 1}"),
        ("Target lock:", record.target_lock_digest),
        ("Base digest:", record.base_digest),
        ("Current size:", _format_allocated_bytes(plan.bytes_used)),
        ("Action:", "reset to the same immutable base"),
    )
    width = max(len(label) for label, _value in rows)
    details = "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
    command = (
        f"ostg workspace reset {shlex.quote(record.workspace_name)} "
        f"--confirm {record.workspace_id}"
    )
    return f"{details}\n\nTo continue:\n{command}"


def _format_workspace_reset_result(
    before: WorkspaceRecord,
    after: WorkspaceRecord,
) -> str:
    rows = (
        ("Workspace:", after.workspace_name),
        ("Workspace ID:", after.workspace_id),
        ("Generation:", f"{before.generation} -> {after.generation}"),
        ("Target lock:", after.target_lock_digest),
        ("Base digest:", after.base_digest),
        ("Action:", "reset completed"),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _run_workspace_reset(
    selector: str,
    *,
    confirmation: str | None,
    data_root: Path,
) -> int:
    try:
        manager = WorkspaceManager(data_root)
        record = manager.open(selector)
        if confirmation is None:
            plan = StorageManager(data_root).plan_workspace_remove(
                record.workspace_id
            )
            _validate_workspace_plan_binding(record, plan)
            print(_format_workspace_reset_plan(record, plan))
            return 0
        if confirmation != record.workspace_id:
            print(
                "error: workspace reset confirmation must exactly match "
                f"workspace ID {record.workspace_id}",
                file=sys.stderr,
            )
            return 1

        updated = manager.reset(record.workspace_id)
        if (
            updated.workspace_id != record.workspace_id
            or updated.generation != record.generation + 1
            or updated.target_lock_digest != record.target_lock_digest
            or updated.base_digest != record.base_digest
        ):
            raise RuntimeError("workspace reset returned inconsistent identity evidence")
        output = _format_workspace_reset_result(record, updated)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _format_workspace_remove_plan(
    record: WorkspaceRecord,
    plan: DeletionPlan,
) -> str:
    rows = (
        ("Workspace:", record.workspace_name),
        ("Workspace ID:", plan.identifier),
        ("Path:", str(plan.path)),
        ("Current size:", _format_allocated_bytes(plan.bytes_used)),
        ("Action:", "remove workspace"),
    )
    width = max(len(label) for label, _value in rows)
    details = "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
    command = (
        f"ostg workspace remove {shlex.quote(record.workspace_name)} "
        f"--confirm {plan.identifier}"
    )
    return f"{details}\n\nTo continue:\n{command}"


def _format_workspace_remove_result(
    record: WorkspaceRecord,
    plan: DeletionPlan,
) -> str:
    rows = (
        ("Workspace:", record.workspace_name),
        ("Workspace ID:", plan.identifier),
        ("Path:", str(plan.path)),
        ("Removed size:", _format_allocated_bytes(plan.bytes_used)),
        ("Action:", "removed"),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _run_workspace_remove(
    selector: str,
    *,
    confirmation: str | None,
    data_root: Path,
) -> int:
    try:
        manager = WorkspaceManager(data_root)
        record = manager.open(selector)
        storage = StorageManager(data_root)
        plan = storage.plan_workspace_remove(record.workspace_id)
        _validate_workspace_plan_binding(record, plan)
        if confirmation is None:
            print(_format_workspace_remove_plan(record, plan))
            return 0
        if confirmation != record.workspace_id:
            print(
                "error: workspace remove confirmation must exactly match "
                f"workspace ID {record.workspace_id}",
                file=sys.stderr,
            )
            return 1

        removed = storage.remove_workspace(
            record.workspace_id,
            confirmation=confirmation,
        )
        _validate_workspace_plan_binding(record, removed)
        output = _format_workspace_remove_result(record, removed)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Orin Stage — JetPack 6 target software workspace engine",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        metavar="PATH",
        help="override the persistent data root (default: ~/.local/share/orin-stage)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check host prerequisites without changing the host",
    )
    target_parser = subparsers.add_parser(
        "target",
        help="inspect target software releases",
    )
    target_subparsers = target_parser.add_subparsers(
        dest="target_command",
        required=True,
    )
    target_subparsers.add_parser(
        "list",
        help="list built-in GA target releases",
    )
    ensure_parser = target_subparsers.add_parser(
        "ensure",
        help="ensure the implemented target acquisition and immutable base",
    )
    ensure_parser.add_argument("selector", metavar="SELECTOR")
    ensure_parser.add_argument(
        "--allow-validation-pending",
        action="store_true",
        help="explicitly allow a validation-pending target",
    )
    workspace_parser = subparsers.add_parser(
        "workspace",
        help="manage mutable target workspaces",
    )
    workspace_subparsers = workspace_parser.add_subparsers(
        dest="workspace_command",
        required=True,
    )
    workspace_subparsers.add_parser(
        "list",
        help="list published workspaces",
    )
    workspace_create_parser = workspace_subparsers.add_parser(
        "create",
        help="create a workspace from an ensured target",
    )
    workspace_create_parser.add_argument(
        "--target",
        required=True,
        metavar="SELECTOR",
    )
    workspace_create_parser.add_argument("--name", required=True)
    workspace_create_parser.add_argument(
        "--allow-validation-pending",
        action="store_true",
        help="explicitly allow a validation-pending target",
    )
    workspace_reset_parser = workspace_subparsers.add_parser(
        "reset",
        help="plan or confirm reset to the same immutable base",
    )
    workspace_reset_parser.add_argument("selector", metavar="WORKSPACE")
    workspace_reset_parser.add_argument("--confirm", metavar="WORKSPACE_ID")
    workspace_remove_parser = workspace_subparsers.add_parser(
        "remove",
        help="plan or confirm workspace removal",
    )
    workspace_remove_parser.add_argument("selector", metavar="WORKSPACE")
    workspace_remove_parser.add_argument("--confirm", metavar="WORKSPACE_ID")
    shell_parser = subparsers.add_parser(
        "shell",
        help="open an interactive shell in a workspace",
    )
    shell_parser.add_argument("--workspace", required=True, metavar="WORKSPACE")
    run_parser = subparsers.add_parser(
        "run",
        help="run a command in a workspace",
    )
    run_parser.add_argument("--workspace", required=True, metavar="WORKSPACE")
    run_parser.add_argument("target_argv", nargs=argparse.REMAINDER, metavar="COMMAND")
    build_command_parser = subparsers.add_parser(
        "build",
        help="run a host-native cross-build against a workspace",
    )
    build_command_parser.add_argument(
        "--workspace",
        required=True,
        metavar="WORKSPACE",
    )
    build_command_parser.add_argument(
        "build_argv",
        nargs=argparse.REMAINDER,
        metavar="COMMAND",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve once so every subcommand receives one canonical absolute path.
    # Resolution deliberately does not create directories.
    data_root = resolve_data_root(args.data_root)

    if args.command == "doctor":
        checks = run_doctor(data_root)
        print(format_report(checks))
        return doctor_exit_code(checks)

    if args.command == "target" and args.target_command == "list":
        return _run_target_list()

    if args.command == "target" and args.target_command == "ensure":
        return _run_target_ensure(
            args.selector,
            allow_validation_pending=args.allow_validation_pending,
            data_root=data_root,
        )

    if args.command == "workspace" and args.workspace_command == "list":
        return _run_workspace_list(data_root)

    if args.command == "workspace" and args.workspace_command == "create":
        return _run_workspace_create(
            args.target,
            args.name,
            allow_validation_pending=args.allow_validation_pending,
            data_root=data_root,
        )

    if args.command == "workspace" and args.workspace_command == "reset":
        return _run_workspace_reset(
            args.selector,
            confirmation=args.confirm,
            data_root=data_root,
        )

    if args.command == "workspace" and args.workspace_command == "remove":
        return _run_workspace_remove(
            args.selector,
            confirmation=args.confirm,
            data_root=data_root,
        )

    if args.command == "shell":
        return _run_workspace_shell(args.workspace, data_root=data_root)

    if args.command == "run":
        target_argv = tuple(args.target_argv)
        if target_argv[:1] == ("--",):
            target_argv = target_argv[1:]
        if not target_argv:
            parser.error("ostg run requires a command after '--'")
        return _run_workspace_command(
            args.workspace,
            target_argv,
            data_root=data_root,
        )

    if args.command == "build":
        build_argv = tuple(args.build_argv)
        if build_argv[:1] == ("--",):
            build_argv = build_argv[1:]
        if not build_argv:
            parser.error("ostg build requires a command after '--'")
        return _run_workspace_build(
            args.workspace,
            build_argv,
            data_root=data_root,
        )

    parser.print_help()
    return 0
