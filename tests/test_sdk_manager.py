from __future__ import annotations

import subprocess

import pytest

from orin_stage.acquisition.sdk_manager import (
    SdkManagerClient,
    SdkManagerCommandError,
    SdkManagerNotFoundError,
    SdkManagerTimeoutError,
)


def test_version_runs_official_sdkmanager_version_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        seen.append(tuple(command))
        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
        }
        return subprocess.CompletedProcess(command, 0, stdout="2.4.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = SdkManagerClient()

    assert client.version() == "2.4.1"
    assert seen == [("sdkmanager", "--ver")]


def test_version_reports_missing_sdkmanager(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SdkManagerNotFoundError, match="executable not found"):
        SdkManagerClient().version()


def test_version_forwards_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, **kwargs):
        assert kwargs["timeout"] == 5.0
        return subprocess.CompletedProcess(command, 0, stdout="2.4.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert SdkManagerClient().version(timeout_seconds=5.0) == "2.4.1"


def test_version_converts_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SdkManagerTimeoutError, match="timed out after 2.5 seconds") as caught:
        SdkManagerClient().version(timeout_seconds=2.5)

    assert caught.value.command == ("sdkmanager", "--ver")
    assert caught.value.timeout_seconds == 2.5


def test_version_preserves_failed_command_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            7,
            stdout="partial output",
            stderr="sdkmanager failure",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SdkManagerCommandError) as caught:
        SdkManagerClient().version()

    error = caught.value
    assert error.command == ("sdkmanager", "--ver")
    assert error.returncode == 7
    assert error.stdout == "partial output"
    assert error.stderr == "sdkmanager failure"


def test_query_jetson_returns_unparsed_current_query_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []
    raw_output = "Jetson query output\nwith SDK Manager formatting\n"

    def fake_run(command, **kwargs):
        seen.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout=raw_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = SdkManagerClient().query_jetson()

    assert output == raw_output
    assert seen == [
        (
            "sdkmanager",
            "--query",
            "non-interactive",
            "--login-type",
            "devzone",
            "--product",
            "Jetson",
            "--show-all-versions",
        )
    ]


def test_query_jetson_can_request_archived_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        seen.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="archived\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert SdkManagerClient().query_jetson(archived=True) == "archived\n"
    assert seen == [
        (
            "sdkmanager",
            "--query",
            "non-interactive",
            "--login-type",
            "devzone",
            "--product",
            "Jetson",
            "--archived-versions",
        )
    ]
