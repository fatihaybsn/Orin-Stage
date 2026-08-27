from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BuiltinCatalogPaths:
    targets_dir: Path
    schema_path: Path
    hardware_dir: Path


def builtin_catalog_paths() -> BuiltinCatalogPaths:
    """Return filesystem paths for the catalog shipped inside the package."""
    data_dir = Path(__file__).resolve().parent / "data"
    return BuiltinCatalogPaths(
        targets_dir=data_dir / "targets",
        schema_path=data_dir / "schema" / "target.schema.json",
        hardware_dir=data_dir / "hardware",
    )
