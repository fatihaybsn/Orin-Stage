from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orin_stage.target_executor import TargetCommandError, TargetExecutor


class TargetCorpusError(RuntimeError):
    """Raised when the minimum ARM64 userspace corpus does not match expectations."""


@dataclass(frozen=True, slots=True)
class TargetCorpusResult:
    """Captured evidence for one successful ARM64 corpus check."""

    name: str
    command: tuple[str, ...]
    stdout: str
    stderr: str


Validator = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class _CorpusCheck:
    name: str
    command: tuple[str, ...]
    validate: Validator
    expectation: str


_SHA256_ORIN_STAGE = "a010cbd21e9d9c398048a2758e24e61a1ba133e0a9adcbf4ab03f6d3aa8c2a51"


def _checks() -> tuple[_CorpusCheck, ...]:
    return (
        _CorpusCheck(
            name="loader",
            command=("/lib/ld-linux-aarch64.so.1", "--list", "/bin/bash"),
            validate=lambda output: (
                "/lib/ld-linux-aarch64.so.1" in output
                and "/lib/aarch64-linux-gnu/libc.so.6" in output
            ),
            expectation="ARM64 loader and libc must resolve from the target tree",
        ),
        _CorpusCheck(
            name="bash",
            command=("/bin/bash", "-lc", 'printf "BASH_OK\\n"'),
            validate=lambda output: output.strip() == "BASH_OK",
            expectation="target Bash must execute successfully",
        ),
        _CorpusCheck(
            name="python_cpu",
            command=(
                "/usr/bin/python3",
                "-c",
                'import hashlib; print(hashlib.sha256(b"orin-stage").hexdigest())',
            ),
            validate=lambda output: output.strip() == _SHA256_ORIN_STAGE,
            expectation="target Python must produce the deterministic CPU result",
        ),
        _CorpusCheck(
            name="dpkg",
            command=("/usr/bin/dpkg", "--print-architecture"),
            validate=lambda output: output.strip() == "arm64",
            expectation="target dpkg architecture must be arm64",
        ),
        _CorpusCheck(
            name="apt",
            command=("/usr/bin/apt-cache", "policy", "nvidia-jetpack"),
            validate=lambda output: (
                "nvidia-jetpack:" in output
                and "Installed:" in output
                and "Installed: (none)" not in output
            ),
            expectation="APT metadata for nvidia-jetpack must be readable",
        ),
    )


def run_arm64_userspace_corpus(
    workspace_root: Path,
    *,
    executor: TargetExecutor | None = None,
) -> tuple[TargetCorpusResult, ...]:
    """Run the minimum Gate-D-style ARM64 userspace corpus on one workspace."""
    target_executor = executor or TargetExecutor()
    results: list[TargetCorpusResult] = []

    for check in _checks():
        try:
            completed = target_executor.run(workspace_root, check.command)
        except TargetCommandError as exc:
            raise TargetCorpusError(
                f"ARM64 corpus check {check.name!r} could not run: {exc}"
            ) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if not check.validate(stdout):
            raise TargetCorpusError(
                f"ARM64 corpus check {check.name!r} failed: {check.expectation}; "
                f"stdout={stdout!r}"
            )

        results.append(
            TargetCorpusResult(
                name=check.name,
                command=check.command,
                stdout=stdout,
                stderr=stderr,
            )
        )

    return tuple(results)
