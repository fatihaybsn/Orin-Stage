from __future__ import annotations

from pathlib import Path

from orin_stage.runtime import resolve_data_root


def test_default_data_root_uses_user_local_share(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_data_root() == (tmp_path / ".local" / "share" / "orin-stage").resolve()


def test_explicit_data_root_is_expanded_and_resolved(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_data_root("~/custom-orin-stage") == (tmp_path / "custom-orin-stage").resolve()


def test_resolving_data_root_does_not_create_it(tmp_path: Path) -> None:
    candidate = tmp_path / "not-created"

    resolved = resolve_data_root(candidate)

    assert resolved == candidate.resolve()
    assert not candidate.exists()
