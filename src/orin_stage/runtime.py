from __future__ import annotations

from pathlib import Path


DEFAULT_DATA_ROOT = Path("~/.local/share/orin-stage")


def resolve_data_root(explicit: Path | str | None = None) -> Path:
    """Resolve the Orin Stage persistent data root without creating it.

    The product default follows the layout already used by the MVP vertical
    slice. An explicit path is intended for controlled development/test use and
    future CLI plumbing; directory creation remains the responsibility of the
    operation that actually needs a directory.
    """

    candidate = DEFAULT_DATA_ROOT if explicit is None else Path(explicit)
    return candidate.expanduser().resolve()
