from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from orin_stage.build_identity import JP6_BUILD_IMAGE


class BuildCapsuleError(RuntimeError):
    """Base error for transient x86 build capsule execution."""


class BuildCapsuleNotFoundError(BuildCapsuleError):
    """Raised when the configured Podman executable cannot be found."""


class BuildCommandError(BuildCapsuleError):
    """Raised when a build command exits unsuccessfully."""

    def __init__(
        self,
        command: tuple[str, ...],
        returncode: int,
        stdout: str | None,
        stderr: str | None,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"build command failed with exit code {returncode}: {' '.join(command)}"
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class BuildCapsuleRunner:
    """Run a transient x86 build container against one existing target tree.

    The target workspace is mounted read-only at /target. The source repository
    is mounted read-write at /workspace. The Bootlin cross-toolchain is mounted
    read-only at /opt/toolchain. Podman owns only the transient process/container
    lifecycle; target state remains owned by Orin Stage.
    """

    image: str = JP6_BUILD_IMAGE
    podman_binary: str = "podman"

    def run(
        self,
        workspace_root: Path,
        repository_root: Path,
        toolchain_root: Path,
        command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        target = self._directory(workspace_root, "workspace root")
        repository = self._directory(repository_root, "repository root")
        toolchain = self._directory(toolchain_root, "toolchain root")
        build_command = self._build_command(command)

        podman_command = (
            self.podman_binary,
            "run",
            "--rm",
            "--volume",
            f"{target}:/target:ro",
            "--volume",
            f"{toolchain}:/opt/toolchain:ro",
            "--volume",
            f"{repository}:/workspace:rw",
            "--workdir",
            "/workspace",
            self.image,
            *build_command,
        )
        return self._execute(podman_command, runner=runner)

    @staticmethod
    def _directory(path: Path, label: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise BuildCapsuleError(f"{label} must be an existing directory: {resolved}")
        return resolved

    @staticmethod
    def _build_command(command: Sequence[str]) -> tuple[str, ...]:
        arguments = tuple(command)
        if not arguments or not arguments[0]:
            raise BuildCapsuleError("build command must not be empty")
        if not all(isinstance(argument, str) for argument in arguments):
            raise BuildCapsuleError("build command arguments must be strings")
        return arguments

    @staticmethod
    def _execute(
        command: tuple[str, ...],
        *,
        runner: Runner,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = runner(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BuildCapsuleNotFoundError(
                f"Podman executable not found: {command[0]!r}"
            ) from exc

        if completed.returncode != 0:
            raise BuildCommandError(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return completed
