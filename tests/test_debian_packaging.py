from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _module(filename: str, name: str):
    script = REPO_ROOT / "tools" / "release" / filename
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_control_declares_exact_two_series_contract_and_runtime_dependencies() -> None:
    control = (REPO_ROOT / "debian" / "control").read_text(encoding="utf-8")

    for dependency in (
        "debhelper-compat (= 13)",
        "python3-dev",
        "python3-venv",
        "python3-wheel",
        "build-essential",
        "libyaml-dev",
        "cython3",
        "rustc-1.85",
        "cargo-1.85",
        "podman",
        "uidmap",
        "qemu-user-static",
        "binfmt-support",
        "sudo",
    ):
        assert dependency in control
    assert "Architecture: amd64" in control
    assert "${orin:PythonDepends}" in control
    assert "sdkmanager" not in control.lower()


def test_rules_resolves_series_from_os_release_and_builds_offline() -> None:
    rules = (REPO_ROOT / "debian" / "rules").read_text(encoding="utf-8")

    assert "SERIES := $(shell . /etc/os-release 2>/dev/null && echo $$VERSION_CODENAME)" in rules
    assert "ifeq ($(filter $(SERIES),jammy noble),)" in rules
    assert "--series $(SERIES)" in rules
    assert "--wheelhouse debian/.wheelhouse/$(SERIES)" in rules
    assert "--runtime-sources deps/runtime-sdists" in rules
    assert "--build-sources deps/build-sdists" in rules
    assert "--vendor deps/cargo-vendor" in rules
    assert "--without-pip" in rules
    assert "--launcher-runtime-python /usr/lib/orin-stage/venv/bin/python" in rules
    assert "cargo-1.85" in rules
    assert "rustc-1.85" in rules


def test_source_format_and_no_maintainer_scripts() -> None:
    assert (REPO_ROOT / "debian" / "source" / "format").read_text(encoding="utf-8") == (
        "3.0 (quilt)\n"
    )
    assert not any(
        (REPO_ROOT / "debian" / name).exists()
        for name in ("postinst", "preinst", "prerm", "postrm")
    )


def test_python_substvars_generates_correct_range_for_jammy_and_noble(
    tmp_path: Path, monkeypatch
) -> None:
    substvars = _module("write_debian_substvars.py", "debian_substvars_test")

    # Jammy (Python 3.10)
    output_jammy = tmp_path / "debian" / "orin-stage.substvars.jammy"
    output_jammy.parent.mkdir(parents=True, exist_ok=True)
    output_jammy.write_text("shlibs:Depends=libyaml-0-2\nmisc:Depends=\n", encoding="utf-8")
    monkeypatch.setattr(substvars.sys, "version_info", (3, 10, 12, "final", 0))
    assert substvars.main(("--output", str(output_jammy))) == 0
    values_jammy = dict(
        line.split("=", 1) for line in output_jammy.read_text(encoding="utf-8").splitlines()
    )
    assert values_jammy == {
        "misc:Depends": "",
        "orin:PythonDepends": "python3 (>= 3.10~), python3 (<< 3.11)",
        "shlibs:Depends": "libyaml-0-2",
    }

    # Noble (Python 3.12)
    output_noble = tmp_path / "debian" / "orin-stage.substvars.noble"
    output_noble.write_text("shlibs:Depends=libc6 (>= 2.38), libyaml-0-2\nmisc:Depends=\n", encoding="utf-8")
    monkeypatch.setattr(substvars.sys, "version_info", (3, 12, 3, "final", 0))
    assert substvars.main(("--output", str(output_noble))) == 0
    values_noble = dict(
        line.split("=", 1) for line in output_noble.read_text(encoding="utf-8").splitlines()
    )
    assert values_noble == {
        "misc:Depends": "",
        "orin:PythonDepends": "python3 (>= 3.12~), python3 (<< 3.13)",
        "shlibs:Depends": "libc6 (>= 2.38), libyaml-0-2",
    }


def test_source_materializer_keeps_the_only_untracked_release_helpers_explicit() -> None:
    source = _module("prepare_debian_source.py", "debian_source_test")

    assert source.RELEASE_HELPERS == (
        Path("tools/release/prepare_debian_source.py"),
        Path("tools/release/write_debian_substvars.py"),
    )
