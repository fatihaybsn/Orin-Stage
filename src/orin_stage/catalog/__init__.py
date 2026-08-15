"""Exact target catalog loading and resolution."""

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
    "SemanticIssue",
    "validate_target_semantics",
]
