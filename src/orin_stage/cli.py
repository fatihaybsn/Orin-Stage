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
from .base.lock import load_target_lock, target_lock_digest
from .base.receipt import base_directory_is_reusable, load_base_receipt
from .build_capsule import BuildCommandError
from .build_identity import JP6_BUILD_IDENTITY
from .build_toolchain import (
    BuildToolchainError,
    BuildToolchainManager,
    BuildToolchainNotFoundError,
)
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
from .storage import DeletionPlan, StorageManager, StorageStatus
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
        print("error: Run ostg target ensure as your normal user.", file=sys.stderr)
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
        print("error: Run ostg workspace create as your normal user.", file=sys.stderr)
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
        WorkspaceManager(data_root).open(selector)
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


def _format_inspect_section(
    title: str,
    rows: Sequence[tuple[str, str]],
) -> str:
    width = max(len(label) for label, _value in rows)
    details = "\n".join(
        f"  {label:<{width}}  {value}" for label, value in rows
    )
    return f"{title}\n{details}"


def _run_workspace_inspect(selector: str, *, data_root: Path) -> int:
    try:
        manager = WorkspaceManager(data_root)
        workspace = manager.open(selector)
        size_plan = StorageManager(data_root).plan_workspace_remove(
            workspace.workspace_id
        )
        _validate_workspace_plan_binding(workspace, size_plan)

        target_dir = data_root / "targets" / workspace.target_lock_digest
        if target_dir.is_symlink() or not target_dir.is_dir():
            raise RuntimeError(
                f"workspace target metadata not found: {workspace.target_lock_digest}"
            )
        lock = load_target_lock(target_dir / "lock.json")
        if (
            target_lock_digest(lock) != workspace.target_lock_digest
            or target_dir.name != workspace.target_lock_digest
        ):
            raise RuntimeError("workspace/target identity mismatch")

        lock_target = lock.get("target")
        if not isinstance(lock_target, Mapping):
            raise RuntimeError("target lock has invalid target metadata")
        canonical_id = lock_target.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id:
            raise RuntimeError("target lock has invalid canonical target identity")

        paths = builtin_catalog_paths()
        target = TargetResolver(paths.targets_dir, paths.schema_path).resolve(
            canonical_id
        )
        release = target.record["release"]
        if (
            lock_target.get("canonical_id") != target.canonical_id
            or lock_target.get("jetpack_version")
            != release["jetpack"]["version"]
            or lock_target.get("l4t_version") != release["l4t"]["version"]
            or lock_target.get("jetson_linux_release_revision")
            != release["jetson_linux"]["release_revision"]
        ):
            raise RuntimeError("workspace/target identity mismatch")

        receipt = load_base_receipt(target_dir / "receipt.json")
        base_path = target_dir / "base"
        if base_path.is_symlink() or not base_path.is_dir():
            raise RuntimeError("workspace/base identity mismatch")
        if (
            receipt.get("target_lock_digest") != workspace.target_lock_digest
            or receipt.get("base_digest") != workspace.base_digest
        ):
            raise RuntimeError("workspace/base identity mismatch")

        try:
            BuildToolchainManager(data_root).inspect()
            managed_toolchain = "ready"
        except BuildToolchainNotFoundError:
            managed_toolchain = "not acquired"
        except BuildToolchainError:
            managed_toolchain = "invalid"

        primary_selector = (
            target.aliases[0] if target.aliases else target.canonical_id
        )
        sections = (
            _format_inspect_section(
                "Workspace",
                (
                    ("Name:", workspace.workspace_name),
                    ("ID:", workspace.workspace_id),
                    ("Generation:", str(workspace.generation)),
                    ("Root:", str(workspace.root_path)),
                    ("Size:", _format_allocated_bytes(size_plan.bytes_used)),
                ),
            ),
            _format_inspect_section(
                "Target",
                (
                    ("Canonical ID:", primary_selector),
                    ("JetPack:", str(release["jetpack"]["version"])),
                    (
                        "Jetson Linux/L4T:",
                        str(release["jetson_linux"]["release_revision"]),
                    ),
                    ("Support status:", target.support_status),
                    ("Target lock digest:", workspace.target_lock_digest),
                ),
            ),
            _format_inspect_section(
                "Base",
                (
                    ("Digest:", workspace.base_digest),
                    ("Path:", str(base_path)),
                ),
            ),
            _format_inspect_section(
                "Build",
                (
                    ("GCC:", JP6_BUILD_IDENTITY.gcc_version),
                    ("Binutils:", JP6_BUILD_IDENTITY.binutils_version),
                    ("Toolchain identity:", JP6_BUILD_IDENTITY.digest()),
                    ("Managed toolchain:", managed_toolchain),
                ),
            ),
            _format_inspect_section(
                "Execution",
                (
                    ("ARM64 userspace:", "QEMU linux-user / CPU-only"),
                    (
                        "Hardware fidelity:",
                        "matching physical Orin required",
                    ),
                ),
            ),
        )
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print("\n\n".join(sections))
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


def _format_storage_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> str:
    if not rows:
        return "(none)"
    all_rows = (headers, *rows)
    widths = [
        max(len(row[index]) for row in all_rows)
        for index in range(len(headers))
    ]
    return "\n".join(
        "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(row)
        ).rstrip()
        for row in all_rows
    )


def _format_storage_status(status: StorageStatus) -> str:
    summary_rows = (
        ("SDK Manager cache", _format_allocated_bytes(status.sdkm_cache_bytes)),
        ("Bases", _format_allocated_bytes(status.base_bytes)),
        ("Workspaces", _format_allocated_bytes(status.workspace_bytes)),
        ("Build outputs", _format_allocated_bytes(status.build_output_bytes)),
        ("Tracked total", _format_allocated_bytes(status.tracked_bytes)),
    )
    summary_width = max(len(label) for label, _value in summary_rows)
    summary = "\n".join(
        f"{label:<{summary_width}}  {value}" for label, value in summary_rows
    )
    bases = _format_storage_table(
        ("TARGET", "ID", "SIZE"),
        tuple(
            (entry.label, entry.identifier, _format_allocated_bytes(entry.bytes_used))
            for entry in sorted(
                status.bases,
                key=lambda entry: (entry.label, entry.identifier),
            )
        ),
    )
    workspaces = _format_storage_table(
        ("NAME", "ID", "SIZE"),
        tuple(
            (entry.label, entry.identifier, _format_allocated_bytes(entry.bytes_used))
            for entry in sorted(
                status.workspaces,
                key=lambda entry: (entry.label, entry.identifier),
            )
        ),
    )
    return (
        "Orin Stage Storage\n\n"
        f"{summary}\n\n"
        f"Bases\n{bases}\n\n"
        f"Workspaces\n{workspaces}"
    )


def _run_storage_status(data_root: Path) -> int:
    try:
        status = StorageManager(data_root).status()
        output = _format_storage_status(status)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _format_storage_base_delete_plan(plan: DeletionPlan) -> str:
    rows = [
        ("Type:", "base"),
        ("Target ID:", plan.identifier),
        ("Path:", str(plan.path)),
        ("Current size:", _format_allocated_bytes(plan.bytes_used)),
        ("Status:", "BLOCKED" if plan.blocked_by else "ready"),
    ]
    if plan.blocked_by:
        dependencies = ", ".join(plan.blocked_by)
        rows.append(("Blocked by:", dependencies))
    else:
        rows.append(("Action:", "remove immutable target"))
    width = max(len(label) for label, _value in rows)
    details = "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
    if plan.blocked_by:
        return (
            f"{details}\n\n"
            f"Deletion blocked by workspace(s): {', '.join(plan.blocked_by)}"
        )
    command = (
        f"ostg storage delete base {plan.identifier} "
        f"--confirm {plan.identifier}"
    )
    return f"{details}\n\nTo continue:\n{command}"


def _format_storage_base_delete_result(plan: DeletionPlan) -> str:
    rows = (
        ("Type:", "base"),
        ("Target ID:", plan.identifier),
        ("Removed size:", _format_allocated_bytes(plan.bytes_used)),
        ("Action:", "removed"),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _validate_storage_base_plan(plan: DeletionPlan, target_digest: str) -> None:
    if plan.kind != "base" or plan.identifier != target_digest:
        raise RuntimeError("base deletion returned inconsistent identity evidence")


def _run_storage_base_delete(
    target_digest: str,
    *,
    confirmation: str | None,
    data_root: Path,
) -> int:
    if confirmation is not None and confirmation != target_digest:
        print(
            "error: base deletion confirmation must exactly match "
            f"target ID {target_digest}",
            file=sys.stderr,
        )
        return 1
    try:
        storage = StorageManager(data_root)
        if confirmation is None:
            plan = storage.plan_base_remove(target_digest)
            _validate_storage_base_plan(plan, target_digest)
            output = _format_storage_base_delete_plan(plan)
        else:
            plan = storage.remove_base(
                target_digest,
                confirmation=confirmation,
            )
            _validate_storage_base_plan(plan, target_digest)
            output = _format_storage_base_delete_result(plan)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _format_storage_sdkm_cache_delete_plan(plan: DeletionPlan) -> str:
    rows = (
        ("Type:", "sdkm-cache"),
        ("Confirmation:", plan.identifier),
        ("Path:", str(plan.path)),
        ("Current size:", _format_allocated_bytes(plan.bytes_used)),
        ("Status:", "ready"),
        ("Action:", "clear SDK Manager download cache"),
    )
    width = max(len(label) for label, _value in rows)
    details = "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
    command = f"ostg storage delete sdkm-cache --confirm {plan.identifier}"
    return f"{details}\n\nTo continue:\n{command}"


def _format_storage_sdkm_cache_delete_result(plan: DeletionPlan) -> str:
    rows = (
        ("Type:", "sdkm-cache"),
        ("Confirmation:", plan.identifier),
        ("Removed size:", _format_allocated_bytes(plan.bytes_used)),
        ("Action:", "removed"),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _validate_storage_sdkm_cache_plan(plan: DeletionPlan) -> None:
    if plan.kind != "sdkm-cache" or plan.identifier != "sdkm-downloads":
        raise RuntimeError(
            "SDK Manager cache deletion returned inconsistent identity evidence"
        )


def _run_storage_sdkm_cache_delete(
    *,
    confirmation: str | None,
    data_root: Path,
) -> int:
    if confirmation is not None and confirmation != "sdkm-downloads":
        print(
            "error: SDK Manager cache deletion confirmation must exactly match "
            "sdkm-downloads",
            file=sys.stderr,
        )
        return 1
    try:
        storage = StorageManager(data_root)
        if confirmation is None:
            plan = storage.plan_sdkm_cache_remove()
            _validate_storage_sdkm_cache_plan(plan)
            output = _format_storage_sdkm_cache_delete_plan(plan)
        else:
            plan = storage.remove_sdkm_cache(confirmation=confirmation)
            _validate_storage_sdkm_cache_plan(plan)
            output = _format_storage_sdkm_cache_delete_result(plan)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


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
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="inspect one workspace and its exact identities",
    )
    inspect_parser.add_argument(
        "--workspace",
        required=True,
        metavar="WORKSPACE",
    )
    storage_parser = subparsers.add_parser(
        "storage",
        help="show tracked Orin Stage storage usage",
    )
    storage_subparsers = storage_parser.add_subparsers(
        dest="storage_command",
        required=True,
    )
    storage_subparsers.add_parser(
        "status",
        help="show tracked storage totals and entries",
    )
    storage_delete_parser = storage_subparsers.add_parser(
        "delete",
        help="plan or confirm explicit storage deletion",
    )
    storage_delete_subparsers = storage_delete_parser.add_subparsers(
        dest="storage_delete_kind",
        required=True,
    )
    storage_delete_base_parser = storage_delete_subparsers.add_parser(
        "base",
        help="plan or confirm immutable target removal",
    )
    storage_delete_base_parser.add_argument(
        "target_digest",
        metavar="TARGET_DIGEST",
    )
    storage_delete_base_parser.add_argument(
        "--confirm",
        metavar="TARGET_DIGEST",
    )
    storage_delete_sdkm_parser = storage_delete_subparsers.add_parser(
        "sdkm-cache",
        help="plan or confirm SDK Manager download cache cleanup",
    )
    storage_delete_sdkm_parser.add_argument(
        "--confirm",
        metavar="sdkm-downloads",
    )
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
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

    if args.command == "inspect":
        return _run_workspace_inspect(args.workspace, data_root=data_root)

    if args.command == "storage" and args.storage_command == "status":
        return _run_storage_status(data_root)

    if (
        args.command == "storage"
        and args.storage_command == "delete"
        and args.storage_delete_kind == "base"
    ):
        return _run_storage_base_delete(
            args.target_digest,
            confirmation=args.confirm,
            data_root=data_root,
        )

    if (
        args.command == "storage"
        and args.storage_command == "delete"
        and args.storage_delete_kind == "sdkm-cache"
    ):
        return _run_storage_sdkm_cache_delete(
            confirmation=args.confirm,
            data_root=data_root,
        )

    parser.print_help()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
