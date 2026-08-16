from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


_JETPACK_HEADER = re.compile(r"^JetPack\s+(?P<label>.+?)\s*$")


class SdkManagerQueryParseError(ValueError):
    """Raised when SDK Manager query output is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class SdkManagerJetsonRelease:
    """A normalized JetPack release advertised by SDK Manager.

    ``display_label`` preserves NVIDIA's human-facing text, including labels
    such as ``(rev. 1)``. ``version`` is the exact value SDK Manager places in
    its generated ``--version`` argument. ``targets`` are the exact SDK
    Manager target identifiers advertised for that release.
    """

    display_label: str
    version: str
    targets: tuple[str, ...]


def parse_jetson_query_output(output: str) -> tuple[SdkManagerJetsonRelease, ...]:
    """Parse the useful JetPack/target facts from SDK Manager query output.

    SDK Manager emits banners, login progress and other presentation text in
    addition to the actual options. We intentionally ignore that surrounding
    text and only interpret JetPack headers plus the generated ``sdkmanager``
    command lines beneath them.

    The raw output may contain authentication/user-facing information, so this
    parser returns only the normalized facts needed by Orin Stage. Callers
    should not persist the raw query output as acquisition evidence.
    """

    releases: list[SdkManagerJetsonRelease] = []
    current_label: str | None = None
    current_version: str | None = None
    current_targets: list[str] = []

    def publish_current() -> None:
        nonlocal current_label, current_version, current_targets

        if current_label is None:
            return
        if current_version is None or not current_targets:
            raise SdkManagerQueryParseError(
                f"JetPack entry has no usable SDK Manager command: {current_label!r}"
            )

        releases.append(
            SdkManagerJetsonRelease(
                display_label=current_label,
                version=current_version,
                targets=tuple(current_targets),
            )
        )
        current_label = None
        current_version = None
        current_targets = []

    for raw_line in output.splitlines():
        line = raw_line.strip()

        header = _JETPACK_HEADER.fullmatch(line)
        if header is not None:
            publish_current()
            current_label = f"JetPack {header.group('label')}"
            continue

        if not line.startswith("sdkmanager "):
            continue

        if current_label is None:
            raise SdkManagerQueryParseError(
                "SDK Manager option command appeared before a JetPack header"
            )

        try:
            command = tuple(shlex.split(line))
        except ValueError as exc:
            raise SdkManagerQueryParseError(
                f"Could not parse SDK Manager option command: {line!r}"
            ) from exc

        if _argument_value(command, "--product") != "Jetson":
            raise SdkManagerQueryParseError(
                f"Unexpected non-Jetson SDK Manager option under {current_label!r}"
            )

        version = _argument_value(command, "--version")
        target = _argument_value(command, "--target")

        if current_version is None:
            current_version = version
        elif current_version != version:
            raise SdkManagerQueryParseError(
                f"Conflicting --version values under {current_label!r}: "
                f"{current_version!r} and {version!r}"
            )

        if target not in current_targets:
            current_targets.append(target)

    publish_current()
    return tuple(releases)


def find_jetpack_release(
    releases: tuple[SdkManagerJetsonRelease, ...],
    version: str,
) -> SdkManagerJetsonRelease:
    """Return one exact SDK Manager JetPack version without fuzzy matching."""

    matches = [release for release in releases if release.version == version]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"SDK Manager does not advertise JetPack {version}")
    raise SdkManagerQueryParseError(
        f"SDK Manager advertised JetPack {version} more than once"
    )


def _argument_value(command: tuple[str, ...], option: str) -> str:
    try:
        index = command.index(option)
    except ValueError as exc:
        raise SdkManagerQueryParseError(
            f"SDK Manager option command is missing {option}"
        ) from exc

    value_index = index + 1
    if value_index >= len(command) or command[value_index].startswith("--"):
        raise SdkManagerQueryParseError(
            f"SDK Manager option command has no value for {option}"
        )
    return command[value_index]
