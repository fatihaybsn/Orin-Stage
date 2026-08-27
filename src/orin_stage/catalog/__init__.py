"""Exact target catalog loading and resolution."""

from .paths import BuiltinCatalogPaths, builtin_catalog_paths
from .resolver import (
    CatalogError,
    CatalogSchemaError,
    CatalogTargetSummary,
    CatalogSemanticValidationError,
    CatalogTargetValidationError,
    DuplicateSelectorError,
    DuplicateTargetIdError,
    ResolvedCatalogTarget,
    TargetNotFoundError,
    TargetNotUsableError,
    TargetResolver,
)
from .semantic import SemanticIssue, validate_target_semantics

__all__ = [
    "BuiltinCatalogPaths",
    "CatalogError",
    "CatalogSchemaError",
    "CatalogTargetSummary",
    "CatalogSemanticValidationError",
    "CatalogTargetValidationError",
    "DuplicateSelectorError",
    "DuplicateTargetIdError",
    "ResolvedCatalogTarget",
    "TargetNotFoundError",
    "TargetNotUsableError",
    "TargetResolver",
    "builtin_catalog_paths",
    "SemanticIssue",
    "validate_target_semantics",
]
