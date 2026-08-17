from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .sdk_manager_discovery import SdkManagerDiscovery


class SdkManagerDownloadPlanError(ValueError):
    """Raised when a download-only plan would be ambiguous or unsafe."""


@dataclass(frozen=True, slots=True)
class SdkManagerDownloadPlan:
    """A non-executing plan for one exact SDK Manager download-only session.

    The plan contains only identity and process arguments. Creating it does not
    invoke SDK Manager and does not download anything. Component selection and
    the versioned response file are intentionally handled in a later slice.
    """

    canonical_id: str
    sdk_manager_version: str
    jetpack_version: str
    sdk_manager_target: str
    query_source: str
    download_folder: Path
    include_host: bool
    command: tuple[str, ...]


def build_downloadonly_plan(
    discovery: SdkManagerDiscovery,
    *,
    download_folder: Path,
    include_host: bool,
    executable: str = "sdkmanager",
) -> SdkManagerDownloadPlan:
    """Build, but do not execute, the exact SDK Manager download command.

    ``include_host`` is deliberately mandatory. SDK Manager treats host-side
    components as an explicit selection (``--host``); Orin Stage must not guess
    whether the future developer role needs them before its component/response
    file contract is finalized.

    License acceptance is also not injected here. NVIDIA documents that when a
    license-accept option is omitted, SDK Manager asks the user to review and
    accept the applicable licenses. Orin Stage must not silently accept those
    terms on the user's behalf.
    """

    if not executable or not executable.strip():
        raise SdkManagerDownloadPlanError("SDK Manager executable must not be empty")

    folder = Path(download_folder)
    if not folder.is_absolute():
        raise SdkManagerDownloadPlanError(
            "SDK Manager shared download folder must be an absolute path"
        )

    target = discovery.target
    command = [
        executable,
        "--cli",
        "--action",
        "downloadonly",
        "--login-type",
        "devzone",
        "--product",
        "Jetson",
        "--version",
        target.jetpack_version,
        "--target-os",
        "Linux",
    ]

    if include_host:
        command.append("--host")

    command.extend(
        [
            "--target",
            target.sdk_manager_target,
            "--download-folder",
            str(folder),
            "--exit-on-finish",
        ]
    )

    return SdkManagerDownloadPlan(
        canonical_id=target.canonical_id,
        sdk_manager_version=discovery.sdk_manager_version,
        jetpack_version=target.jetpack_version,
        sdk_manager_target=target.sdk_manager_target,
        query_source=discovery.query_source,
        download_folder=folder,
        include_host=include_host,
        command=tuple(command),
    )
