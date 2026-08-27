from __future__ import annotations

import pytest

from orin_stage.cli import PROGRAM_VERSION, build_parser, main


def test_cli_help_has_product_name_and_data_root(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Orin Stage" in output
    assert "--data-root PATH" in output
    assert "~/.local/share/orin-stage" in output


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
