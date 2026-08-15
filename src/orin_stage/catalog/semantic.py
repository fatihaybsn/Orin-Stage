from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SemanticIssue:
    """One catalog invariant violation that JSON Schema cannot express cleanly."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _jetpack_token(version: str) -> str:
    """Convert the catalog JetPack display literal to its canonical selector token."""
    if version.endswith(" DP"):
        return f"{version[:-3]}-dp"
    return version


def _release_channel(revision: str) -> str:
    """Return the NVIDIA repository release channel, e.g. 36.5.2 -> 36.5."""
    parts = revision.split(".")
    return ".".join(parts[:2])


def _dotted_path_exists(record: Mapping[str, Any], dotted_path: str) -> bool:
    current: Any = record
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def validate_target_semantics(record: Mapping[str, Any]) -> tuple[SemanticIssue, ...]:
    """Validate cross-field invariants for one already schema-valid target record.

    This layer deliberately stays narrower than acquisition/lock validation. It
    validates relationships between values that are already present in the
    static catalog; it does not dereference hardware profiles, contact NVIDIA,
    infer SDK Manager availability, or require runtime/acquisition evidence.
    """

    issues: list[SemanticIssue] = []

    release = record["release"]
    jetpack = release["jetpack"]
    jetson_linux = release["jetson_linux"]
    l4t = release["l4t"]

    jp_token = _jetpack_token(jetpack["version"])
    release_revision = jetson_linux["release_revision"]
    artifact_revision = jetson_linux["artifact_revision"]
    display_version = jetson_linux["display_version"]

    expected_id = f"nvidia.jetpack-{jp_token}.jetson-linux-{release_revision}"
    if record["id"] != expected_id:
        issues.append(
            SemanticIssue(
                "id",
                f"must match release identity {expected_id!r}; got {record['id']!r}",
            )
        )

    expected_alias = f"jetson-orin@jp{jp_token}"
    aliases = record["aliases"]
    if aliases[0] != expected_alias:
        issues.append(
            SemanticIssue(
                "aliases[0]",
                f"must be the primary exact selector {expected_alias!r}; got {aliases[0]!r}",
            )
        )

    if l4t["version"] != release_revision:
        issues.append(
            SemanticIssue(
                "release.l4t.version",
                "must equal release.jetson_linux.release_revision because L4T is "
                "modeled as the same release-line alias",
            )
        )

    expected_release_tag = f"jetson_{display_version}"
    if jetson_linux["release_tag"] != expected_release_tag:
        issues.append(
            SemanticIssue(
                "release.jetson_linux.release_tag",
                f"must match display_version as {expected_release_tag!r}",
            )
        )

    construction_inputs = record["construction_inputs"]
    for artifact_name in ("bsp", "sample_rootfs"):
        actual_release = construction_inputs[artifact_name]["release"]
        if actual_release != artifact_revision:
            issues.append(
                SemanticIssue(
                    f"construction_inputs.{artifact_name}.release",
                    "must equal release.jetson_linux.artifact_revision "
                    f"({artifact_revision!r}); got {actual_release!r}",
                )
            )

    repository = record["packages"]["repository"]
    repo_platform = record["target"]["soc"]["repository_platform"]
    channel = _release_channel(release_revision)
    expected_suites = {
        f"common r{channel} main",
        f"{repo_platform} r{channel} main",
    }
    actual_suites = set(repository["suites"])
    missing_suites = sorted(expected_suites - actual_suites)
    if missing_suites:
        issues.append(
            SemanticIssue(
                "packages.repository.suites",
                f"missing release-channel suite(s): {', '.join(missing_suites)}",
            )
        )

    target_arch = record["userspace"]["architecture"]["debian_architecture"]
    package_arch = record["packages"]["meta_package"]["architecture"]
    if package_arch != target_arch:
        issues.append(
            SemanticIssue(
                "packages.meta_package.architecture",
                "must equal userspace.architecture.debian_architecture "
                f"({target_arch!r}); got {package_arch!r}",
            )
        )

    official_checksums = record["checksums"]["official"]
    checksum_artifacts = official_checksums["artifacts"]
    for artifact_name in ("bsp", "sample_rootfs"):
        construction_filename = construction_inputs[artifact_name]["filename"]
        checksum_filename = checksum_artifacts[artifact_name]["filename"]
        # NVIDIA sources in this catalog sometimes disagree only in filename
        # letter case (for example r36... vs R36...). Preserve both literals,
        # but reject a checksum that points at a substantively different file.
        if construction_filename.casefold() != checksum_filename.casefold():
            issues.append(
                SemanticIssue(
                    f"checksums.official.artifacts.{artifact_name}.filename",
                    "must identify the same artifact as construction_inputs "
                    "(letter-case differences are allowed)",
                )
            )

    checksum_source = official_checksums.get("source")
    reference_source = record["sources"]["references"]["release_checksums"]
    if checksum_source != reference_source:
        issues.append(
            SemanticIssue(
                "sources.references.release_checksums",
                "must equal checksums.official.source (including the shared null case)",
            )
        )

    for index, evidence in enumerate(record["sources"]["evidence"]):
        field = evidence["field"]
        if not _dotted_path_exists(record, field):
            issues.append(
                SemanticIssue(
                    f"sources.evidence[{index}].field",
                    f"references unknown catalog field {field!r}",
                )
            )

    return tuple(issues)
