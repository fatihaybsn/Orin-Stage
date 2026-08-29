from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orin_stage.acquisition.sdk_manager import (
    SdkManagerNotFoundError,
    SdkManagerTimeoutError,
)
from orin_stage.doctor import (
    CheckStatus,
    DoctorCheck,
    doctor_exit_code,
    format_report,
    run_doctor,
)
from orin_stage.build_toolchain import BuildToolchainError


class WorkingSdkManager:
    def version(self, timeout_seconds: float | None = None) -> str:
        assert timeout_seconds == 5.0
        return "SDK Manager 2.3.0"


class MissingSdkManager:
    def version(self, timeout_seconds: float | None = None) -> str:
        raise SdkManagerNotFoundError("SDK Manager executable not found")


class TimedOutSdkManager:
    def version(self, timeout_seconds: float | None = None) -> str:
        assert timeout_seconds == 5.0
        raise SdkManagerTimeoutError(
            ("sdkmanager", "--ver"),
            timeout_seconds,
        )


def _completed(
    command: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _healthy_environment(tmp_path: Path) -> dict[str, object]:
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n',
        encoding="utf-8",
    )
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("alice:100000:65536\n", encoding="utf-8")
    subgid.write_text("alice:100000:65536\n", encoding="utf-8")
    binfmt_root = tmp_path / "binfmt_misc"
    binfmt_root.mkdir()
    (binfmt_root / "qemu-aarch64").write_text(
        "enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: PF\n",
        encoding="utf-8",
    )
    qemu = tmp_path / "qemu-aarch64-static"
    qemu.write_text("fake executable", encoding="utf-8")

    executables = {
        "podman": "/usr/bin/podman",
        "newuidmap": "/usr/bin/newuidmap",
        "newgidmap": "/usr/bin/newgidmap",
        "sudo": "/usr/bin/sudo",
    }

    def which(name: str) -> str | None:
        return executables.get(name)

    def runner(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == ("/usr/bin/podman", "--version"):
            return _completed(command, stdout="podman version 5.4.2\n")
        if command == ("/usr/bin/podman", "unshare", "true"):
            return _completed(command)
        if command == (str(qemu), "--version"):
            return _completed(command, stdout="qemu-aarch64 version 8.2.2\n")
        raise AssertionError(f"unexpected command: {command}")

    return {
        "data_root": tmp_path / "data-root",
        "system": "Linux",
        "machine": "x86_64",
        "username": "alice",
        "os_release_path": os_release,
        "subuid_path": subuid,
        "subgid_path": subgid,
        "binfmt_root": binfmt_root,
        "qemu_static_path": qemu,
        "which": which,
        "runner": runner,
        "disk_usage": lambda path: SimpleNamespace(free=128 * 1024**3),
        "sdk_manager": WorkingSdkManager(),
    }


def _checks_by_name(checks: list[DoctorCheck]) -> dict[str, DoctorCheck]:
    return {check.name: check for check in checks}


def test_healthy_doctor_exits_zero_and_formats_deterministically(tmp_path: Path) -> None:
    checks = run_doctor(**_healthy_environment(tmp_path))  # type: ignore[arg-type]

    assert doctor_exit_code(checks) == 0
    assert not any(check.status is CheckStatus.FAIL for check in checks)
    report = format_report(checks)
    assert report.startswith("Orin Stage Doctor\n\nPASS  Host OS")
    assert "INFO  Host distribution" in report
    assert "INFO  Free disk" in report
    assert "128.0 GiB" in report
    assert "PASS  sudo" in report
    assert report.endswith("Summary: 11 PASS, 0 WARN, 0 FAIL")


def test_non_linux_host_fails_and_exits_one(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    environment["system"] = "Darwin"

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["Host OS"].status is CheckStatus.FAIL
    assert doctor_exit_code(checks) == 1


def test_non_x86_64_host_fails(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    environment["machine"] = "aarch64"

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["Host architecture"].status is CheckStatus.FAIL
    assert doctor_exit_code(checks) == 1


def test_missing_sdk_manager_is_only_a_warning(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    environment["sdk_manager"] = MissingSdkManager()

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["SDK Manager"].status is CheckStatus.WARN
    assert doctor_exit_code(checks) == 0


def test_missing_sudo_is_only_a_warning(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    healthy_which = environment["which"]

    def which(name: str) -> str | None:
        if name == "sudo":
            return None
        return healthy_which(name)  # type: ignore[operator]

    environment["which"] = which
    checks = run_doctor(**environment)  # type: ignore[arg-type]

    result = _checks_by_name(checks)["sudo"]
    assert result.status is CheckStatus.WARN
    assert doctor_exit_code(checks) == 0


def test_sdk_manager_timeout_is_only_a_warning(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    environment["sdk_manager"] = TimedOutSdkManager()

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    result = _checks_by_name(checks)["SDK Manager"]
    assert result.status is CheckStatus.WARN
    assert result.detail == "version probe timed out"
    assert doctor_exit_code(checks) == 0


def test_missing_qemu_static_is_a_warning(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    environment["qemu_static_path"] = tmp_path / "missing-qemu"

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["QEMU static"].status is CheckStatus.WARN
    assert doctor_exit_code(checks) == 0


def test_missing_binfmt_entry_fails(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    empty_binfmt_root = tmp_path / "empty-binfmt"
    empty_binfmt_root.mkdir()
    environment["binfmt_root"] = empty_binfmt_root

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["ARM64 binfmt"].status is CheckStatus.FAIL
    assert doctor_exit_code(checks) == 1


def test_binfmt_without_fix_binary_flag_fails(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    binfmt = Path(environment["binfmt_root"]) / "qemu-aarch64"
    binfmt.write_text("enabled\nflags: POC\n", encoding="utf-8")

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    result = _checks_by_name(checks)["ARM64 binfmt"]
    assert result.status is CheckStatus.FAIL
    assert "flags=POC" in result.detail


@pytest.mark.parametrize("missing", ["subuid_path", "subgid_path"])
def test_missing_subid_mapping_fails(tmp_path: Path, missing: str) -> None:
    environment = _healthy_environment(tmp_path)
    Path(environment[missing]).write_text(
        "someone-else:100000:65536\n",
        encoding="utf-8",
    )

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["subuid/subgid"].status is CheckStatus.FAIL
    assert doctor_exit_code(checks) == 1


def test_failed_podman_unshare_fails(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    healthy_runner = environment["runner"]

    def runner(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == ("/usr/bin/podman", "unshare", "true"):
            return _completed(command, returncode=125, stderr="cannot create namespace")
        return healthy_runner(command, **kwargs)  # type: ignore[operator]

    environment["runner"] = runner

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["podman unshare"].status is CheckStatus.FAIL
    assert doctor_exit_code(checks) == 1


def test_doctor_does_not_create_or_acquire_managed_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = _healthy_environment(tmp_path)
    data_root = Path(environment["data_root"])

    def forbidden_ensure(*args: object, **kwargs: object) -> object:
        raise AssertionError("doctor must not acquire the managed toolchain")

    monkeypatch.setattr(
        "orin_stage.build_toolchain.BuildToolchainManager.ensure",
        forbidden_ensure,
    )

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["Data root"].status is CheckStatus.PASS
    toolchain = _checks_by_name(checks)["Managed JP6 toolchain"]
    assert toolchain.status is CheckStatus.INFO
    assert toolchain.detail == "not acquired"
    assert not data_root.exists()


def test_doctor_passes_only_for_present_valid_managed_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = _healthy_environment(tmp_path)
    root = Path(environment["data_root"]) / "build" / "toolchains" / "digest" / "root"

    class PresentManager:
        def __init__(self, data_root: Path) -> None:
            pass

        def inspect(self) -> object:
            return SimpleNamespace(root_path=root)

    monkeypatch.setattr("orin_stage.doctor.BuildToolchainManager", PresentManager)

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    toolchain = _checks_by_name(checks)["Managed JP6 toolchain"]
    assert toolchain.status is CheckStatus.PASS
    assert toolchain.detail == str(root)


def test_doctor_warns_for_invalid_managed_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = _healthy_environment(tmp_path)

    class InvalidManager:
        def __init__(self, data_root: Path) -> None:
            pass

        def inspect(self) -> object:
            raise BuildToolchainError("receipt mismatch")

    monkeypatch.setattr("orin_stage.doctor.BuildToolchainManager", InvalidManager)

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    toolchain = _checks_by_name(checks)["Managed JP6 toolchain"]
    assert toolchain.status is CheckStatus.WARN
    assert toolchain.detail == "invalid: receipt mismatch"


def test_unusable_existing_data_root_fails(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    data_root = Path(environment["data_root"])
    data_root.write_text("not a directory", encoding="utf-8")

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    assert _checks_by_name(checks)["Data root"].status is CheckStatus.FAIL


def test_other_distribution_is_warning_without_support_claim(tmp_path: Path) -> None:
    environment = _healthy_environment(tmp_path)
    Path(environment["os_release_path"]).write_text(
        'ID=fedora\nVERSION_ID="42"\nPRETTY_NAME="Fedora Linux 42"\n',
        encoding="utf-8",
    )

    checks = run_doctor(**environment)  # type: ignore[arg-type]

    distribution = _checks_by_name(checks)["Host distribution"]
    assert distribution.status is CheckStatus.WARN
    assert "supported" not in distribution.detail.lower()
    assert "validated" not in distribution.detail.lower()
