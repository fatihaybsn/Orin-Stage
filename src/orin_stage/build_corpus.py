from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from orin_stage.build_capsule import BuildCapsuleRunner, BuildCommandError
from orin_stage.build_identity import (
    JP6_BOOTLIN_BINUTILS_VERSION,
    JP6_BOOTLIN_GCC_VERSION,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256,
)
from orin_stage.target_executor import TargetCommandError, TargetExecutor


class BuildCorpusError(RuntimeError):
    """Raised when the minimum same-tree build corpus does not match expectations."""


@dataclass(frozen=True, slots=True)
class BuildCorpusResult:
    """Captured evidence for one successful Gate E same-tree build check."""

    stdout: str
    stderr: str


_MARKER_DIR = "/tmp/orin-stage-gate-e"
_MARKER_PATH = f"{_MARKER_DIR}/marker.h"
_MARKER_TEXT = "#define ORIN_STAGE_GATE_E 623"

_SOURCE = """\
#include \"marker.h\"
#include <stdio.h>

#if ORIN_STAGE_GATE_E != 623
#error wrong target marker
#endif

int main(void) {
    puts("ORIN_STAGE_GATE_E_OK");
    return 0;
}
"""

_BUILD_SCRIPT_TEMPLATE = r"""
set -eu

/opt/toolchain/bin/aarch64-buildroot-linux-gnu-gcc --version | head -n 1 | \
    grep -F 'Buildroot 2022.08) ${EXPECTED_GCC_VERSION}' >/dev/null
/opt/toolchain/bin/aarch64-buildroot-linux-gnu-ld --version | head -n 1 | \
    grep -F 'GNU ld (GNU Binutils) ${EXPECTED_BINUTILS_VERSION}' >/dev/null
printf 'TOOLCHAIN_IDENTITY_OK\n'

test "$(cat /target/tmp/orin-stage-gate-e/marker.h)" = "#define ORIN_STAGE_GATE_E 623"
printf 'SAME_TREE_OK\n'

if touch /target/tmp/orin-stage-gate-e/build-write-test 2>/dev/null; then
    printf 'TARGET_WRITABLE\n' >&2
    exit 42
fi
printf 'TARGET_READ_ONLY_OK\n'

/opt/toolchain/bin/aarch64-buildroot-linux-gnu-gcc \
    --sysroot=/target \
    -isystem /target/usr/include/aarch64-linux-gnu \
    -I/target/tmp/orin-stage-gate-e \
    -B/target/usr/lib/aarch64-linux-gnu/ \
    -L/target/usr/lib/aarch64-linux-gnu \
    -L/target/lib/aarch64-linux-gnu \
    -H \
    -Wl,-t \
    hello.c \
    -o hello-arm64

/opt/toolchain/bin/aarch64-buildroot-linux-gnu-readelf -h hello-arm64
""".strip()


def _build_script() -> str:
    return (
        _BUILD_SCRIPT_TEMPLATE
        .replace("${EXPECTED_GCC_VERSION}", JP6_BOOTLIN_GCC_VERSION)
        .replace("${EXPECTED_BINUTILS_VERSION}", JP6_BOOTLIN_BINUTILS_VERSION)
    )


def verify_toolchain_archive(path: Path) -> str:
    """Verify the exact Bootlin archive bytes proven for the JP6 MVP slice."""
    archive = Path(path).expanduser().resolve()
    if not archive.is_file():
        raise BuildCorpusError(f"toolchain archive must be an existing file: {archive}")

    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256:
        raise BuildCorpusError(
            "toolchain archive SHA-256 mismatch: "
            f"expected {JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256}, got {actual}"
        )
    return actual


def _contains_host_contamination(output: str) -> bool:
    """Reject host-native include/library evidence from the build trace."""
    if "x86_64" in output:
        return True

    for raw_line in output.splitlines():
        line = raw_line.lstrip(". ")
        if line.startswith(("/usr/include/", "/usr/lib/", "/lib/x86_64-linux-gnu/")):
            return True
    return False


def run_same_tree_build_corpus(
    workspace_root: Path,
    toolchain_root: Path,
    *,
    build_runner: BuildCapsuleRunner | None = None,
    target_executor: TargetExecutor | None = None,
) -> BuildCorpusResult:
    """Run the minimum Gate E corpus against one existing JP6 workspace.

    The ARM64 target executor first writes a marker into the mutable workspace.
    The x86 build capsule must see that exact marker through /target, must not be
    able to write to /target, and must produce an AArch64 executable while its
    compiler/linker trace remains free of host-native x86 include/library paths.
    """
    builder = build_runner or BuildCapsuleRunner()
    executor = target_executor or TargetExecutor()

    marker_command = (
        "/bin/bash",
        "-lc",
        f"mkdir -p {_MARKER_DIR} && "
        f"printf '{_MARKER_TEXT}\\n' > {_MARKER_PATH}",
    )
    try:
        executor.run(workspace_root, marker_command)
    except TargetCommandError as exc:
        raise BuildCorpusError(f"could not create same-tree marker: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="orin-stage-gate-e-") as scratch:
        repository_root = Path(scratch)
        (repository_root / "hello.c").write_text(_SOURCE, encoding="utf-8")

        try:
            completed = builder.run(
                workspace_root,
                repository_root,
                toolchain_root,
                ("/bin/bash", "-lc", _build_script()),
            )
        except BuildCommandError as exc:
            raise BuildCorpusError(f"same-tree build could not run: {exc}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    audit = f"{stdout}\n{stderr}"

    if "TOOLCHAIN_IDENTITY_OK" not in stdout:
        raise BuildCorpusError("build toolchain identity was not proven")
    if "SAME_TREE_OK" not in stdout:
        raise BuildCorpusError("build did not observe the ARM64 shell mutation")
    if "TARGET_READ_ONLY_OK" not in stdout or "TARGET_WRITABLE" in audit:
        raise BuildCorpusError("build target tree was not proven read-only")
    if "Machine:" not in stdout or "AArch64" not in stdout:
        raise BuildCorpusError("build artifact was not proven to be AArch64")
    if _contains_host_contamination(audit):
        raise BuildCorpusError("host x86 include/library contamination detected")

    return BuildCorpusResult(stdout=stdout, stderr=stderr)
