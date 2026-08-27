from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from orin_stage.acquisition.acquisition_receipt import load_receipt, receipt_is_cache_hit
from orin_stage.acquisition.artifact_verification import verify_receipt_artifact
from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import write_json_atomic
from .chroot import Arm64ConstructionChroot, read_qemu_version
from .cleanup import write_construction_lease
from .identity import (
    build_base_digest,
    build_base_target_projection_digest,
)
from .lock import (
    acquisition_artifacts_from_receipt,
    build_canonical_target_lock,
    target_lock_digest,
    write_target_lock,
)
from .packages import (
    PackageRemovalPolicy,
    clean_package_archives,
    install_locked_package_set,
    resolve_construction_package_set,
    use_canonical_nvidia_construction_sources,
)
from .receipt import (
    base_directory_is_reusable,
    make_base_receipt,
    write_base_receipt,
)
from .recipe import (
    JP623_ALLOWED_REMOVAL_SET,
    JP623_REMOVAL_POLICY_VERSION,
    construction_recipe_digest_v1,
)
from .sandbox import HostConstructionSandbox
from .validation import build_final_manifest, validate_runtime_state


class BaseConstructionError(RuntimeError):
    """Raised when the JP6.2.3 official base cannot be constructed."""


_BASE_METADATA_MODE = 0o644


@dataclass(frozen=True, slots=True)
class BaseBuildResult:
    target_directory: Path
    base_path: Path
    lock_path: Path
    manifest_path: Path
    receipt_path: Path
    target_lock_digest: str
    base_digest: str
    cache_hit: bool


def _run_host(
    command: tuple[str, ...],
    *,
    cwd: Path | None = None,
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        command,
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise BaseConstructionError(
            f"base construction command failed ({completed.returncode}): "
            f"{' '.join(command)}\n{completed.stderr}"
        )
    return completed


def _require_jp623(target: ResolvedCatalogTarget) -> None:
    jetpack = str(target.record["release"]["jetpack"]["version"])
    l4t = str(target.record["release"]["l4t"]["version"])
    if jetpack != "6.2.3" or l4t != "36.5.2":
        raise BaseConstructionError(
            "Step 3 production builder currently supports only JetPack 6.2.3 / L4T 36.5.2"
        )


def _verified_artifact_paths(
    receipt: Mapping[str, object],
) -> dict[str, Path]:
    download_root = Path(str(receipt.get("download_root", ""))).resolve()
    if not download_root.is_absolute() or not download_root.is_dir():
        raise BaseConstructionError("acquisition receipt download root is unavailable")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise BaseConstructionError("acquisition receipt has no artifact list")

    result: dict[str, Path] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise BaseConstructionError("malformed acquisition artifact entry")
        try:
            kind = str(item["kind"])
            relative_path = str(item["relative_path"])
            size = int(item["size"])
            sha256 = str(item["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BaseConstructionError("malformed acquisition artifact entry") from exc
        if not verify_receipt_artifact(
            download_root,
            relative_path=relative_path,
            expected_size=size,
            expected_sha256=sha256,
        ):
            raise BaseConstructionError(f"acquisition artifact is no longer valid: {kind}")
        path = (download_root / relative_path).resolve()
        result[kind] = path

    if frozenset(result) != frozenset({"bsp", "sample_rootfs"}):
        raise BaseConstructionError("exact BSP + Sample RootFS acquisition set is required")
    return result


def _current_artifact_sha256(receipt: Mapping[str, object]) -> dict[str, str]:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        return {}
    result: dict[str, str] = {}
    for item in artifacts:
        if isinstance(item, Mapping) and isinstance(item.get("kind"), str):
            result[str(item["kind"])] = str(item.get("sha256", ""))
    return result


def _find_reusable_candidate(
    data_root: Path,
    target: ResolvedCatalogTarget,
    *,
    recipe_digest: str,
    acquisition_receipt: Mapping[str, object],
) -> BaseBuildResult | None:
    targets_root = Path(data_root) / "targets"
    if not targets_root.is_dir():
        return None
    current_artifacts = _current_artifact_sha256(acquisition_receipt)

    for directory in sorted(targets_root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        lock_path = directory / "lock.json"
        if not lock_path.is_file():
            continue
        try:
            with lock_path.open("r", encoding="utf-8") as handle:
                lock = json.load(handle)
            target_section = lock["target"]
            construction = lock["construction"]
            artifacts = lock["artifacts"]
            if target_section["canonical_id"] != target.canonical_id:
                continue
            if construction["recipe_digest"] != recipe_digest:
                continue
            if any(
                artifacts[kind]["sha256"] != current_artifacts.get(kind)
                for kind in ("bsp", "sample_rootfs")
            ):
                continue
            if not base_directory_is_reusable(directory):
                continue
            receipt_path = directory / "receipt.json"
            with receipt_path.open("r", encoding="utf-8") as handle:
                base_receipt = json.load(handle)
            return BaseBuildResult(
                target_directory=directory,
                base_path=directory / "base",
                lock_path=lock_path,
                manifest_path=directory / "manifest.json",
                receipt_path=receipt_path,
                target_lock_digest=directory.name,
                base_digest=str(base_receipt["base_digest"]),
                cache_hit=True,
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            continue
    return None


def _extract_official_rootfs(
    staging_work: Path,
    *,
    bsp: Path,
    sample_rootfs: Path,
    host_sandbox: HostConstructionSandbox,
    runner=subprocess.run,
) -> tuple[Path, Path]:
    _run_host(("tar", "-xf", str(bsp), "-C", str(staging_work)), runner=runner)
    l4t_root = staging_work / "Linux_for_Tegra"
    rootfs = l4t_root / "rootfs"
    if not l4t_root.is_dir():
        raise BaseConstructionError("BSP extraction did not produce Linux_for_Tegra")
    rootfs.mkdir(parents=True, exist_ok=True)
    _run_host(
        (
            "tar",
            "--numeric-owner",
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
            "-xpf",
            str(sample_rootfs),
            "-C",
            str(rootfs),
        ),
        runner=runner,
    )
    host_sandbox.run_official_l4t_scripts(l4t_root, runner=runner)
    return l4t_root, rootfs


def ensure_jp623_base(
    target: ResolvedCatalogTarget,
    *,
    acquisition_receipt_path: Path,
    data_root: Path,
    qemu_binary: Path = Path("/usr/bin/qemu-aarch64-static"),
    runner=subprocess.run,
) -> BaseBuildResult:
    """Build or reuse the first official JP6.2.3 immutable ARM64 base."""

    _require_jp623(target)
    root = Path(data_root).expanduser().resolve()
    if not root.is_absolute():
        raise BaseConstructionError("Orin Stage data root must be absolute")
    acquisition_path = Path(acquisition_receipt_path).expanduser().resolve()
    expected_receipts_root = (root / "sdkm" / "receipts").resolve()
    try:
        acquisition_path.relative_to(expected_receipts_root)
    except ValueError as exc:
        raise BaseConstructionError(
            "acquisition receipt must be inside the Orin Stage SDK Manager receipt root"
        ) from exc

    acquisition_receipt = load_receipt(acquisition_path)
    receipt_download_root = Path(str(acquisition_receipt.get("download_root", ""))).expanduser().resolve()
    expected_download_root = (root / "sdkm" / "downloads").resolve()
    if receipt_download_root != expected_download_root:
        raise BaseConstructionError(
            "acquisition receipt download root does not match the Orin Stage SDK Manager download root"
        )
    acquisition_digest = acquisition_receipt.get("acquisition_digest")
    if not isinstance(acquisition_digest, str) or not receipt_is_cache_hit(
        acquisition_path,
        expected_digest=acquisition_digest,
        download_root=Path(str(acquisition_receipt["download_root"])),
    ):
        raise BaseConstructionError(
            "acquisition receipt is not a valid published Step 2 acquisition"
        )
    if acquisition_receipt.get("canonical_id") != target.canonical_id:
        raise BaseConstructionError("acquisition receipt does not belong to requested target")
    artifact_paths = _verified_artifact_paths(acquisition_receipt)
    artifacts = acquisition_artifacts_from_receipt(acquisition_receipt)
    recipe_digest = construction_recipe_digest_v1()
    removal_policy = PackageRemovalPolicy(
        version=JP623_REMOVAL_POLICY_VERSION,
        jetpack_version="6.2.3",
        l4t_version="36.5.2",
        allowed_removal_set=JP623_ALLOWED_REMOVAL_SET,
    )
    sandbox = HostConstructionSandbox()

    reused = _find_reusable_candidate(
        root,
        target,
        recipe_digest=recipe_digest,
        acquisition_receipt=acquisition_receipt,
    )
    if reused is not None:
        return reused

    if os.geteuid() != 0:
        raise BaseConstructionError("official base construction requires root privileges")

    staging_root = root / "staging"
    targets_root = root / "targets"
    staging_root.mkdir(parents=True, exist_ok=True)
    targets_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f".base-jp623-{uuid.uuid4().hex}"
    staging.mkdir(exist_ok=False)

    try:
        write_construction_lease(staging)
        work = staging / "work"
        work.mkdir(exist_ok=False)
        _l4t_root, rootfs = _extract_official_rootfs(
            work,
            bsp=artifact_paths["bsp"],
            sample_rootfs=artifact_paths["sample_rootfs"],
            host_sandbox=sandbox,
            runner=runner,
        )
        qemu_version = read_qemu_version(qemu_binary, runner=runner)

        with use_canonical_nvidia_construction_sources(rootfs, target):
            with Arm64ConstructionChroot(
                rootfs,
                qemu_binary=qemu_binary,
                runner=runner,
            ) as chroot:
                package_set = resolve_construction_package_set(
                    chroot,
                    target,
                    removal_policy=removal_policy,
                    runner=runner,
                )
                lock = build_canonical_target_lock(
                    target,
                    acquisition_receipt=acquisition_receipt,
                    acquisition_receipt_path=acquisition_path,
                    data_root=root,
                    package_set=package_set,
                    construction_recipe_digest=recipe_digest,
                    qemu_version=qemu_version,
                )
                projection_digest = build_base_target_projection_digest(lock)
                base_digest = build_base_digest(
                    base_target_projection_digest=projection_digest,
                    construction_recipe_digest=recipe_digest,
                    artifacts=artifacts,
                )

                # Exact package closure is known only after official rootfs assembly.
                # Re-check now in case an already-published equivalent base exists.
                reused = _find_reusable_candidate(
                    root,
                    target,
                    recipe_digest=recipe_digest,
                    acquisition_receipt=acquisition_receipt,
                )
                if reused is not None and reused.base_digest == base_digest:
                    return reused

                package_transaction = install_locked_package_set(
                    chroot,
                    package_set,
                    runner=runner,
                )
                runtime_snapshot = validate_runtime_state(chroot, package_set)
                clean_package_archives(chroot)
        manifest = build_final_manifest(
            rootfs,
            base_digest=base_digest,
            package_set=package_set,
            runtime=runtime_snapshot,
        )
        lock_digest = target_lock_digest(lock)
        final_dir = targets_root / lock_digest
        if final_dir.exists():
            if base_directory_is_reusable(final_dir):
                with (final_dir / "receipt.json").open("r", encoding="utf-8") as handle:
                    existing = json.load(handle)
                if existing.get("base_digest") == base_digest:
                    return BaseBuildResult(
                        target_directory=final_dir,
                        base_path=final_dir / "base",
                        lock_path=final_dir / "lock.json",
                        manifest_path=final_dir / "manifest.json",
                        receipt_path=final_dir / "receipt.json",
                        target_lock_digest=lock_digest,
                        base_digest=base_digest,
                        cache_hit=True,
                    )
            raise BaseConstructionError(
                f"target lock directory already exists but is not reusable: {final_dir}"
            )

        publish = staging / "publish"
        publish.mkdir()
        base_path = publish / "base"
        os.replace(rootfs, base_path)
        lock_path = publish / "lock.json"
        manifest_path = publish / "manifest.json"
        receipt_path = publish / "receipt.json"
        write_target_lock(lock_path, lock, mode=_BASE_METADATA_MODE)
        write_json_atomic(manifest_path, manifest, mode=_BASE_METADATA_MODE)
        base_receipt = make_base_receipt(
            base_digest=base_digest,
            target_lock_digest_value=lock_digest,
            base_target_projection_digest=projection_digest,
            construction_recipe_digest=recipe_digest,
            construction_package_set_digest=package_set.digest(),
            package_transaction=package_transaction,
            artifacts=artifacts,
            manifest_path=manifest_path,
        )
        write_base_receipt(
            receipt_path,
            base_receipt,
            mode=_BASE_METADATA_MODE,
        )

        os.replace(publish, final_dir)
        return BaseBuildResult(
            target_directory=final_dir,
            base_path=final_dir / "base",
            lock_path=final_dir / "lock.json",
            manifest_path=final_dir / "manifest.json",
            receipt_path=final_dir / "receipt.json",
            target_lock_digest=lock_digest,
            base_digest=base_digest,
            cache_hit=False,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
