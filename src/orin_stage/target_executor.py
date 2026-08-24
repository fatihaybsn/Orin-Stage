from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class TargetExecutorError(RuntimeError):
    """Base error for transient ARM64 target execution."""


class TargetExecutorNotFoundError(TargetExecutorError):
    """Raised when the configured Podman executable cannot be found."""


class TargetCommandError(TargetExecutorError):
    """Raised when an ARM64 target command exits unsuccessfully."""

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
            f"target command failed with exit code {returncode}: "
            f"{' '.join(command)}"
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class TargetExecutor:
    """Run the existing ARM64 workspace tree through transient rootless Podman.

    The workspace is supplied as Podman's external rootfs. Podman owns only the
    temporary process/container lifecycle; it does not own or copy workspace
    state. ARM64 instruction execution is provided by the host binfmt/QEMU setup.
    """

    podman_binary: str = "podman"

    def run(
        self,
        workspace_root: Path,
        command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Run a non-interactive target command and capture its output."""
        root = self._workspace_root(workspace_root)
        target_command = self._target_command(command)
        podman_command = self._podman_command(root, target_command)
        return self._execute(podman_command, runner=runner, capture_output=True)

    def shell(
        self,
        workspace_root: Path,
        *,
        shell: str = "/bin/bash",
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Open an interactive target shell with the terminal attached."""
        root = self._workspace_root(workspace_root)
        if not shell:
            raise TargetExecutorError("target shell path must not be empty")
        podman_command = self._podman_command(
            root,
            (shell,),
            interactive=True,
        )
        return self._execute(podman_command, runner=runner, capture_output=False)

    def _workspace_root(self, workspace_root: Path) -> Path:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise TargetExecutorError(
                f"workspace root must be an existing directory: {root}"
            )
        return root

    @staticmethod
    def _target_command(command: Sequence[str]) -> tuple[str, ...]:
        arguments = tuple(command)
        if not arguments or not arguments[0]:
            raise TargetExecutorError("target command must not be empty")
        if not all(isinstance(argument, str) for argument in arguments):
            raise TargetExecutorError("target command arguments must be strings")
        return arguments

    def _podman_command(
        self,
        root: Path,
        target_command: tuple[str, ...],
        *,
        interactive: bool = False,
    ) -> tuple[str, ...]:
        command = [self.podman_binary, "run", "--rm"]
        if interactive:
            command.extend(("--interactive", "--tty"))
        command.extend(("--rootfs", str(root)))
        command.extend(target_command)
        return tuple(command)

    @staticmethod
    def _execute(
        command: tuple[str, ...],
        *,
        runner: Runner,
        capture_output: bool,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, object] = {
            "check": False,
            "text": True,
        }
        if capture_output:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE

        try:
            completed = runner(command, **kwargs)
        except FileNotFoundError as exc:
            raise TargetExecutorNotFoundError(
                f"Podman executable not found: {command[0]!r}"
            ) from exc

        if completed.returncode != 0:
            raise TargetCommandError(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return completed
