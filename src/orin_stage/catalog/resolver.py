from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .semantic import validate_target_semantics


class CatalogError(RuntimeError):
    """Base error for catalog loading and target resolution."""


class CatalogSchemaError(CatalogError):
    """Raised when target.schema.json itself is invalid."""


class CatalogTargetValidationError(CatalogError):
    """Raised when a checked-in target record violates target.schema.json."""

    def __init__(self, path: Path, errors: Iterable[str]) -> None:
        self.path = path
        self.errors = tuple(errors)
        details = "\n".join(f"- {error}" for error in self.errors)
        super().__init__(f"Invalid target catalog record: {path}\n{details}")


class CatalogSemanticValidationError(CatalogError):
    """Raised when a schema-valid record violates cross-field catalog invariants."""

    def __init__(self, path: Path, errors: Iterable[str]) -> None:
        self.path = path
        self.errors = tuple(errors)
        details = "\n".join(f"- {error}" for error in self.errors)
        super().__init__(f"Semantically invalid target catalog record: {path}\n{details}")


class DuplicateTargetIdError(CatalogError):
    """Raised when two catalog records declare the same canonical id."""


class DuplicateSelectorError(CatalogError):
    """Raised when an alias/canonical selector would resolve ambiguously."""


class TargetNotFoundError(CatalogError):
    """Raised when a selector is not present in the exact target catalog."""

    def __init__(self, selector: str) -> None:
        self.selector = selector
        super().__init__(f"Unknown exact target selector: {selector!r}")


class TargetNotUsableError(CatalogError):
    """Raised when a known target is not yet allowed for environment creation."""

    def __init__(self, selector: str, canonical_id: str, support_status: str) -> None:
        self.selector = selector
        self.canonical_id = canonical_id
        self.support_status = support_status
        super().__init__(
            f"Exact target {selector!r} resolves to {canonical_id!r} but is not usable; "
            f"support status is {support_status!r}"
        )


@dataclass(frozen=True, slots=True)
class CatalogTargetSummary:
    """Stable, immutable metadata for target-list/UI surfaces.

    The summary intentionally excludes the full catalog record and any future
    acquisition/lock data. It is suitable for `ostg target list`-style output.
    """

    canonical_id: str
    primary_selector: str
    aliases: tuple[str, ...]
    jetpack_version: str
    jetson_linux_version: str
    support_status: str
    lifecycle: str
    availability: str
    source_path: Path

    @property
    def is_supported(self) -> bool:
        return self.support_status == "supported"

    @property
    def is_validation_pending(self) -> bool:
        return self.support_status == "validation-pending"

    @property
    def is_unavailable(self) -> bool:
        return self.support_status == "unavailable"


@dataclass(frozen=True, slots=True)
class ResolvedCatalogTarget:
    """A selector resolved to one canonical catalog record.

    This is deliberately *not* a target lock. The catalog states what Orin Stage
    expects for a published target. Acquisition/runtime evidence (SDK Manager
    version, our SHA-256 values, resolved package closure, response-file digest,
    etc.) belongs to the later exact lock/receipt stages.
    """

    selector: str
    canonical_id: str
    aliases: tuple[str, ...]
    support_status: str
    source_path: Path
    record: Mapping[str, Any]

    @property
    def is_supported(self) -> bool:
        return self.support_status == "supported"

    @property
    def is_validation_pending(self) -> bool:
        return self.support_status == "validation-pending"

    @property
    def is_unavailable(self) -> bool:
        return self.support_status == "unavailable"


class TargetResolver:
    """Load, validate and resolve exact JP6 target catalog records.

    Responsibilities at this stage:
    - validate every target YAML against target.schema.json;
    - enforce a unique canonical-id/selector namespace across the catalog;
    - resolve only explicit canonical ids or declared aliases;
    - preserve support status and source provenance without inventing a lock.

    It intentionally does not download artifacts, mutate workspaces, infer
    undocumented aliases, or fabricate acquisition/runtime evidence.
    """

    def __init__(self, targets_dir: Path | str, schema_path: Path | str) -> None:
        self.targets_dir = Path(targets_dir)
        self.schema_path = Path(schema_path)
        self._records_by_id: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._selector_to_id: dict[str, str] = {}
        self._validator = self._load_validator()
        self.reload()

    def _load_validator(self) -> Draft202012Validator:
        try:
            with self.schema_path.open("r", encoding="utf-8") as handle:
                schema = json.load(handle)
            Draft202012Validator.check_schema(schema)
        except (OSError, json.JSONDecodeError, SchemaError) as exc:
            raise CatalogSchemaError(
                f"Cannot load a valid Draft 2020-12 target schema from {self.schema_path}: {exc}"
            ) from exc

        return Draft202012Validator(schema, format_checker=FormatChecker())

    @staticmethod
    def _load_yaml_mapping(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise CatalogTargetValidationError(path, [f"YAML could not be loaded: {exc}"]) from exc

        if not isinstance(data, dict):
            raise CatalogTargetValidationError(path, ["document root must be a YAML mapping"])
        return data

    def _validate_record(self, path: Path, record: dict[str, Any]) -> None:
        errors = sorted(
            self._validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return

        formatted: list[str] = []
        for error in errors:
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            formatted.append(f"{location}: {error.message}")
        raise CatalogTargetValidationError(path, formatted)

    def _validate_semantics(self, path: Path, record: dict[str, Any]) -> None:
        issues = validate_target_semantics(record)
        if issues:
            raise CatalogSemanticValidationError(path, [str(issue) for issue in issues])

    def reload(self) -> None:
        """Atomically rebuild the in-memory catalog indexes from disk."""
        if not self.targets_dir.is_dir():
            raise CatalogError(f"Target catalog directory does not exist: {self.targets_dir}")

        records_by_id: dict[str, tuple[Path, dict[str, Any]]] = {}
        selector_to_id: dict[str, str] = {}

        paths = sorted(self.targets_dir.glob("*.yaml"))
        if not paths:
            raise CatalogError(f"No target YAML records found in: {self.targets_dir}")

        # First pass: structural/schema validity and canonical id uniqueness.
        for path in paths:
            record = self._load_yaml_mapping(path)
            self._validate_record(path, record)
            self._validate_semantics(path, record)

            canonical_id = record["id"]
            if canonical_id in records_by_id:
                previous_path = records_by_id[canonical_id][0]
                raise DuplicateTargetIdError(
                    f"Duplicate canonical target id {canonical_id!r}: "
                    f"{previous_path} and {path}"
                )
            records_by_id[canonical_id] = (path, record)

        # Second pass: one global exact-selector namespace. Canonical ids and
        # declared aliases may never point at two different records.
        for canonical_id, (path, record) in records_by_id.items():
            selectors = (canonical_id, *record["aliases"])
            for selector in selectors:
                owner = selector_to_id.get(selector)
                if owner is not None and owner != canonical_id:
                    owner_path = records_by_id[owner][0]
                    raise DuplicateSelectorError(
                        f"Ambiguous target selector {selector!r}: "
                        f"{owner!r} ({owner_path}) and {canonical_id!r} ({path})"
                    )
                selector_to_id[selector] = canonical_id

        # Publish only after the whole catalog has passed.
        self._records_by_id = records_by_id
        self._selector_to_id = selector_to_id

    def resolve(self, selector: str) -> ResolvedCatalogTarget:
        """Resolve an exact canonical id or explicitly declared alias.

        No case-folding, version guessing, prefix matching or "closest" release
        logic is performed. Exact identity is a catalog property.
        """
        if not isinstance(selector, str) or not selector:
            raise TargetNotFoundError(str(selector))

        canonical_id = self._selector_to_id.get(selector)
        if canonical_id is None:
            raise TargetNotFoundError(selector)

        path, internal_record = self._records_by_id[canonical_id]
        record = copy.deepcopy(internal_record)
        return ResolvedCatalogTarget(
            selector=selector,
            canonical_id=canonical_id,
            aliases=tuple(record["aliases"]),
            support_status=record["support"]["status"],
            source_path=path,
            record=record,
        )

    def resolve_for_use(self, selector: str) -> ResolvedCatalogTarget:
        """Resolve a target and require an explicit `supported` catalog state.

        `resolve()` answers identity. This method answers whether that identity is
        currently eligible for environment creation. Validation-pending and
        unavailable records remain resolvable for inspection, but are not usable.
        """
        resolved = self.resolve(selector)
        if not resolved.is_supported:
            raise TargetNotUsableError(
                selector=selector,
                canonical_id=resolved.canonical_id,
                support_status=resolved.support_status,
            )
        return resolved

    def list_targets(self) -> tuple[CatalogTargetSummary, ...]:
        """Return deterministic summaries for product target records.

        The user-facing target catalog contains only GA/production JP6 releases.
        Reference records such as the JetPack 6.0 Developer Preview may remain
        loadable/resolvable for provenance and tests, but they are deliberately
        excluded from `ostg target list` semantics.

        Support status is never rewritten here: a GA release may still be
        `validation-pending`, `supported`, or `unavailable` and is listed with
        that exact state.
        """
        summaries: list[CatalogTargetSummary] = []
        for canonical_id in sorted(self._records_by_id):
            path, record = self._records_by_id[canonical_id]
            jetpack = record["release"]["jetpack"]
            if jetpack["availability"] != "ga" or jetpack["lifecycle"] != "production":
                continue

            aliases = tuple(record["aliases"])
            summaries.append(
                CatalogTargetSummary(
                    canonical_id=canonical_id,
                    primary_selector=aliases[0],
                    aliases=aliases,
                    jetpack_version=jetpack["version"],
                    jetson_linux_version=record["release"]["jetson_linux"][
                        "release_revision"
                    ],
                    support_status=record["support"]["status"],
                    lifecycle=jetpack["lifecycle"],
                    availability=jetpack["availability"],
                    source_path=path,
                )
            )
        return tuple(summaries)

    def selectors(self) -> tuple[str, ...]:
        """Return the exact selector namespace in deterministic order."""
        return tuple(sorted(self._selector_to_id))

    def canonical_ids(self) -> tuple[str, ...]:
        """Return all canonical ids in deterministic order."""
        return tuple(sorted(self._records_by_id))
