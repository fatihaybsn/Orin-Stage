from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SdkManagerComponentRole:
    """Versioned SDK Manager component-selection contract.

    A role is deliberately smaller than a JetPack release. It answers which
    SDK Manager sections Orin Stage intends to acquire for its developer
    environment. Changing the role changes its digest and therefore cannot be
    mistaken for the same acquisition contract.
    """

    role_id: str
    include_host: bool
    select_groups: tuple[str, ...]
    additional_sdks: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = {
            "role_id": self.role_id,
            "include_host": self.include_host,
            "select_groups": list(self.select_groups),
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
    include_host=True,
    select_groups=(
        "Jetson Linux",
        "Jetson Runtime Components",
        "Jetson SDK Components",
    ),
)
