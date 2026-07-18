"""Portable path-safety helpers for serialized private artifact metadata.

Inputs can originate on a different operating system than the backend. Host
``Path`` semantics are therefore insufficient for deciding whether a value is
absolute or for extracting an upload's display filename.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath


_URI_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def portable_filename(value: str | None, *, fallback: str = "") -> str:
    """Return only the final filename component for Windows or POSIX input."""

    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        return fallback
    name = PurePosixPath(raw.replace("\\", "/")).name
    if name in {"", ".", ".."}:
        return fallback
    return name


def is_safe_relative_artifact_reference(value: str | None) -> bool:
    """Validate the portable, repository-style relative artifact contract."""

    raw = str(value or "")
    if not raw or raw != raw.strip() or "\x00" in raw:
        return False
    if _URI_REFERENCE.match(raw):
        return False

    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw.replace("\\", "/"))
    if (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or posix_path.is_absolute()
    ):
        return False

    # Serialized references use forward slashes on every host. This avoids a
    # safe-looking Windows reference becoming one literal POSIX filename.
    if "\\" in raw:
        return False
    components = raw.split("/")
    if any(component in {"", ".", ".."} for component in components):
        return False
    if any(":" in component for component in components):
        return False
    return True


__all__ = ["is_safe_relative_artifact_reference", "portable_filename"]
