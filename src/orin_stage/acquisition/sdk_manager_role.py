from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SdkManagerComponentRole:
    """Versioned SDK Manager component-selection contract.

    A role is deliberately smaller than a JetPack release. It answers which
    SDK Manager sections Orin Stage intends to acquire for its developer
    environment and which default groups must be explicitly excluded. Changing
    the role changes its digest and therefore cannot be mistaken for the same
    acquisition contract.
    """

    role_id: str
    include_host: bool
    install_method: str | None
    select_groups: tuple[str, ...]
    deselect_groups: tuple[str, ...] = ()
    additional_sdks: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = {
            "role_id": self.role_id,
            "include_host": self.include_host,
            "install_method": self.install_method,
            "select_groups": list(self.select_groups),
            "deselect_groups": list(self.deselect_groups),
            "additional_sdks": list(self.additional_sdks),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


JP6_DEVELOPER_ROLE_V1 = SdkManagerComponentRole(
    role_id="jp6-developer-v1",
    include_host=False,
    install_method="direct_flash",
    select_groups=(
        "Jetson Linux",
        "Jetson Runtime Components",
        "Jetson SDK Components",
    ),
    deselect_groups=(
        "Developer Tools",
        "Jetson Platform Services",
    ),
)

# SDK Manager advertises ``direct_flash`` explicitly for JP6.2.3. Its query
# commands for earlier JP6 GA releases omit install-method, so those releases
# use the SDK Manager default rather than receiving an unsupported argument.
JP6_DEFAULT_INSTALL_METHOD_DEVELOPER_ROLE_V1 = SdkManagerComponentRole(
    role_id="jp6-default-install-method-developer-v1",
    include_host=False,
    install_method=None,
    select_groups=JP6_DEVELOPER_ROLE_V1.select_groups,
    deselect_groups=JP6_DEVELOPER_ROLE_V1.deselect_groups,
)


_COMPONENT_ROLES = {
    JP6_DEVELOPER_ROLE_V1.role_id: JP6_DEVELOPER_ROLE_V1,
    JP6_DEFAULT_INSTALL_METHOD_DEVELOPER_ROLE_V1.role_id: (
        JP6_DEFAULT_INSTALL_METHOD_DEVELOPER_ROLE_V1
    ),
}


def sdk_manager_component_role(role_id: str) -> SdkManagerComponentRole:
    """Resolve the exact component-selection contract named by a target record."""

    try:
        return _COMPONENT_ROLES[role_id]
    except KeyError as exc:
        raise ValueError(f"unknown SDK Manager component role: {role_id!r}") from exc
