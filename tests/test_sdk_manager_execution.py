from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager import SdkManagerCommandError
from orin_stage.acquisition.sdk_manager_execution import (
    build_response_file_execution_plan,
    execute_downloadonly,
)
from orin_stage.acquisition.sdk_manager_response import SdkManagerResponseFile


def _response(tmp_path: Path) -> SdkManagerResponseFile:
    return SdkManagerResponseFile(
        path=(tmp_path / "response.ini").resolve(),
        sha256="a" * 64,
        role_id="jp6-developer-v1",
        role_digest="b" * 64,
    )


def test_execution_plan_uses_response_file_and_exports_evidence(tmp_path: Path) -> None:
    plan = build_response_file_execution_plan(
        _response(tmp_path),
        metadata_directory=(tmp_path / "metadata").resolve(),
        logs_directory=(tmp_path / "logs").resolve(),
    )

    assert plan.command == (
        "sdkmanager",
        "--cli",
        "--action",
        "downloadonly",
        "--response-file",
        str((tmp_path / "response.ini").resolve()),
        "--export-response-file",
        str((tmp_path / "metadata").resolve()),
        "--export-logs",
        str((tmp_path / "logs").resolve()),
        "--exit-on-finish",
    )
    assert "--licenses" not in plan.command
    assert "--license" not in plan.command
    assert "--flash" not in plan.command


def test_execution_does_not_capture_auth_output(tmp_path: Path) -> None:
    plan = build_response_file_execution_plan(
        _response(tmp_path),
        metadata_directory=(tmp_path / "metadata").resolve(),
        logs_directory=(tmp_path / "logs").resolve(),
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    execute_downloadonly(plan, runner=runner)

    assert calls[0][1] == {"check": False, "text": True}


def test_execution_failure_is_explicit(tmp_path: Path) -> None:
    plan = build_response_file_execution_plan(
        _response(tmp_path),
        metadata_directory=(tmp_path / "metadata").resolve(),
        logs_directory=(tmp_path / "logs").resolve(),
    )

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7)

    with pytest.raises(SdkManagerCommandError) as caught:
        execute_downloadonly(plan, runner=runner)
    assert caught.value.returncode == 7
