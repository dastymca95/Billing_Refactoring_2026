"""Canonical, side-effect-free GL payable validation."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def is_payable_gl_account(gl_code: str | None, gl_catalog: Mapping[str, Any] | Sequence[Any]) -> bool:
    code = str(gl_code or "").strip()
    if not code:
        return False
    if isinstance(gl_catalog, Mapping):
        entry = gl_catalog.get(code)
    else:
        entry = next((item for item in gl_catalog if _value(item, "gl_code", "code", "Number") == code), None)
    if entry is None:
        return False
    payable = _value(entry, "payable")
    if payable is not None:
        return bool(payable)
    account_type = str(_value(entry, "gl_account_type", "account_type", "Type") or "").lower()
    return bool(account_type) and "expense" in account_type and "asset" not in account_type


def _value(item: Any, *names: str) -> Any:
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value is not None:
            return value
    return None


__all__ = ["is_payable_gl_account"]
