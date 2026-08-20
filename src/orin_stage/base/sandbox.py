from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .recipe import HOST_BUILDER_IMAGE


class ConstructionSandboxError(RuntimeError):
    """Raised when the isolated host-side NVIDIA construction step fails."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class HostConstructionSandbox:
    """Transient rootful Podman sandbox for NVIDIA's x86 host-side scripts.

    The official BSP tree is the only writable host bind. Package changes made by
    ``l4t_flash_prerequisites.sh`` therefore stay inside the disposable container
    instead of mutating the developer workstation.
    """

    image: str = HOST_BUILDER_IMAGE
    podman_binary: str = "podman"

    def run_official_l4t_scripts(
        self,
        l4t_root: Path,
        *,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        root = Path(l4t_root).resolve()
        if not root.is_absolute() or not root.is_dir():
            raise ConstructionSandboxError(
                f"Linux_for_Tegra must be an existing absolute directory: {root}"
            )

        prerequisite = root / "tools" / "l4t_flash_prerequisites.sh"
        apply_binaries = root / "apply_binaries.sh"
        for path in (prerequisite, apply_binaries):
            if not path.is_file():
                raise ConstructionSandboxError(f"official NVIDIA script is missing: {path}")

        command: Sequence[str] = (
            self.podman_binary,
            "run",
            "--rm",
            "--privileged",
            "--pull=missing",
            "--platform=linux/amd64",
            "--env",
            "DEBIAN_FRONTEND=noninteractive",
            "--volume",
            f"{root}:/work/Linux_for_Tegra:rw",
            "--workdir",
            "/work/Linux_for_Tegra",
            self.image,
            "/bin/bash",
            "-ceu",
            "./tools/l4t_flash_prerequisites.sh\n./apply_binaries.sh",
        )
        completed = runner(
            tuple(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise ConstructionSandboxError(
                "official NVIDIA BSP/apply sandbox failed "
                f"({completed.returncode}): {completed.stderr}"
            )
        return completed
