from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .sdk_manager_discovery import SdkManagerDiscovery


class SdkManagerManifestError(RuntimeError):
    """Raised when SDK Manager reference metadata cannot be identified exactly."""


def _contains_exact_value(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_exact_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_value(item, expected) for item in value)
    return value == expected


def _contains_key_value(value: Any, key: str, expected: str) -> bool:
    if isinstance(value, dict):
        if value.get(key) == expected:
            return True
        return any(_contains_key_value(item, key, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key_value(item, key, expected) for item in value)
    return False


def _load_json(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def find_sdk_manager_reference_files(
    state_root: Path,
    discovery: SdkManagerDiscovery,
) -> tuple[Path, ...]:
    """Locate NVIDIA SDK Manager's exact L3 software and HW reference JSONs.

    NVIDIA documents ``~/.nvsdkm/dist`` as the software reference location and
    ``~/.nvsdkm/hwdata`` as the hardware reference location. We identify files
    by exact release/target values inside JSON, not by guessed filenames.
    """

    root = Path(state_root)
    software_root = root / "dist"
    hardware_root = root / "hwdata"
    if not software_root.is_dir() or not hardware_root.is_dir():
        raise SdkManagerManifestError(
            f"SDK Manager reference directories are missing under {root}"
        )

    version = discovery.target.jetpack_version
    target_id = discovery.target.sdk_manager_target

    software_matches: list[Path] = []
    for path in sorted(software_root.rglob("sdkml3_*.json")):
        data = _load_json(path)
        if data is None:
            continue
        has_version = _contains_key_value(data, "releaseVersion", version) or _contains_exact_value(
            data, version
        )
        if has_version and _contains_exact_value(data, target_id):
            software_matches.append(path)

    if len(software_matches) != 1:
        raise SdkManagerManifestError(
            f"expected exactly one SDK Manager L3 software manifest for "
            f"JetPack {version}/{target_id}, found {len(software_matches)}: "
            f"{tuple(str(path) for path in software_matches)!r}"
        )

    hardware_matches: list[Path] = []
    for path in sorted(hardware_root.rglob("*.json")):
        data = _load_json(path)
        if data is None:
            continue
        if _contains_key_value(data, "id", target_id):
            hardware_matches.append(path)

    if not hardware_matches:
        raise SdkManagerManifestError(
            f"SDK Manager hardware reference for {target_id} was not found"
        )

    return (software_matches[0], *hardware_matches)


def copy_sdk_manager_reference_files(
    state_root: Path,
    discovery: SdkManagerDiscovery,
    *,
    destination: Path,
) -> tuple[Path, ...]:
    sources = find_sdk_manager_reference_files(state_root, discovery)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for index, source in enumerate(sources):
        prefix = "software" if index == 0 else "hardware"
        target = destination / f"{prefix}--{source.name}"
        shutil.copy2(source, target)
        copied.append(target)
    return tuple(copied)
