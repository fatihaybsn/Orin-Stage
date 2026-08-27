from __future__ import annotations

import subprocess
from dataclasses import dataclass


class SdkManagerError(RuntimeError):
    """Base error for NVIDIA SDK Manager process interaction."""


class SdkManagerNotFoundError(SdkManagerError):
    """Raised when the sdkmanager executable cannot be found."""


class SdkManagerTimeoutError(SdkManagerError):
    """Raised when SDK Manager exceeds a caller-provided timeout."""

    def __init__(
        self,
        command: tuple[str, ...],
        timeout_seconds: float,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"SDK Manager command timed out after {timeout_seconds:g} seconds: "
            f"{' '.join(command)}"
        )


class SdkManagerCommandError(SdkManagerError):
    """Raised when SDK Manager exits unsuccessfully."""

    def __init__(
        self,
        command: tuple[str, ...],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"SDK Manager command failed with exit code {returncode}: "
            f"{' '.join(command)}"
        )


@dataclass(frozen=True, slots=True)
class SdkManagerClient:
    """Minimal process adapter for the NVIDIA SDK Manager CLI.

    This layer owns process invocation only. It can inspect the installed
    client version and ask NVIDIA SDK Manager which Jetson SDK options are
    available. Query parsing, downloads and receipts stay outside this slice.
    """

    executable: str = "sdkmanager"

    def version(self, timeout_seconds: float | None = None) -> str:
        """Return the installed SDK Manager client version."""
        completed = self._run("--ver", timeout_seconds=timeout_seconds)
        return completed.stdout.strip()

    def query_jetson(self, *, archived: bool = False) -> str:
        """Return SDK Manager's unparsed Jetson query output.

        Standard discovery asks for all currently available Jetson versions.
        Archived discovery is a separate SDK Manager mode and is requested
        explicitly when older releases need to be inspected.
        """
        arguments = [
            "--query",
            "non-interactive",
            "--login-type",
            "devzone",
            "--product",
            "Jetson",
        ]

        if archived:
            arguments.append("--archived-versions")
        else:
            arguments.append("--show-all-versions")

        completed = self._run(*arguments)
        return completed.stdout

    def _run(
        self,
        *arguments: str,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = (self.executable, *arguments)
        run_options: dict[str, object] = {
            "check": False,
            "capture_output": True,
            "text": True,
        }
        if timeout_seconds is not None:
            run_options["timeout"] = timeout_seconds

        try:
            completed = subprocess.run(command, **run_options)
        except FileNotFoundError as exc:
            raise SdkManagerNotFoundError(
                f"SDK Manager executable not found: {self.executable!r}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            timeout = timeout_seconds if timeout_seconds is not None else exc.timeout
            raise SdkManagerTimeoutError(command, timeout) from exc

        if completed.returncode != 0:
            raise SdkManagerCommandError(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        return completed
