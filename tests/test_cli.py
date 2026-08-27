from __future__ import annotations

import pytest

from orin_stage.cli import PROGRAM_VERSION, build_parser, main
from orin_stage.doctor import CheckStatus, DoctorCheck


def test_cli_help_has_product_name_and_data_root(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Orin Stage" in output
    assert "--data-root PATH" in output
    assert "~/.local/share/orin-stage" in output
    assert "doctor" in output


def test_cli_version(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"ostg {PROGRAM_VERSION}"


def test_cli_without_subcommand_is_non_destructive_and_shows_help(capsys, tmp_path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root)]) == 0
    assert not data_root.exists()
    assert "usage: ostg" in capsys.readouterr().out


def test_cli_doctor_returns_zero_for_healthy_checks(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "orin_stage.cli.run_doctor",
        lambda data_root: [DoctorCheck(CheckStatus.PASS, "Host OS", "Linux")],
    )

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Orin Stage Doctor" in output
    assert "PASS" in output
    assert "Summary: 1 PASS, 0 WARN, 0 FAIL" in output


def test_cli_doctor_returns_one_when_a_check_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "orin_stage.cli.run_doctor",
        lambda data_root: [
            DoctorCheck(CheckStatus.FAIL, "Host architecture", "aarch64")
        ],
    )

    assert main(["doctor"]) == 1
    assert "Summary: 0 PASS, 0 WARN, 1 FAIL" in capsys.readouterr().out
