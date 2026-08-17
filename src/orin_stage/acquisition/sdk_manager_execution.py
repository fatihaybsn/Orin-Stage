from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sdk_manager import SdkManagerCommandError, SdkManagerNotFoundError
from .sdk_manager_response import SdkManagerResponseFile


@dataclass(frozen=True, slots=True)
class SdkManagerExecutionPlan:
    command: tuple[str, ...]
    metadata_directory: Path
    logs_directory: Path


def build_response_file_execution_plan(
    response_file: SdkManagerResponseFile,
    *,
    metadata_directory: Path,
    logs_directory: Path,
    executable: str = "sdkmanager",
) -> SdkManagerExecutionPlan:
    metadata = Path(metadata_directory)
    logs = Path(logs_directory)
    if not metadata.is_absolute() or not logs.is_absolute():
        raise ValueError("SDK Manager metadata/log directories must be absolute")
    if not response_file.path.is_absolute():
        raise ValueError("SDK Manager response-file path must be absolute")

    command = (
        executable,
        "--cli",
        "--action",
        "downloadonly",
        "--response-file",
        str(response_file.path),
        "--export-response-file",
        str(metadata),
        "--export-logs",
        str(logs),
        "--exit-on-finish",
    )
    return SdkManagerExecutionPlan(command, metadata, logs)


def execute_downloadonly(
    plan: SdkManagerExecutionPlan,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Execute SDK Manager without capturing its login/progress text.

    Output is intentionally inherited by the terminal. Query/login output can
    contain user-facing authentication material and should not be copied into an
    Orin Stage receipt. SDK Manager's own exported logs are kept separately.
    """

    plan.metadata_directory.mkdir(parents=True, exist_ok=True)
    plan.logs_directory.mkdir(parents=True, exist_ok=True)

    try:
        completed = runner(plan.command, check=False, text=True)
    except FileNotFoundError as exc:
        raise SdkManagerNotFoundError(
            f"SDK Manager executable not found: {plan.command[0]!r}"
        ) from exc

    if completed.returncode != 0:
        raise SdkManagerCommandError(
            command=plan.command,
            returncode=completed.returncode,
            stdout="",
            stderr="",
        )
