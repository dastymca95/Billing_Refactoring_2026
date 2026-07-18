"""Typed inventory of deterministic vendor processing coverage.

The processor registry is authoritative. YAML files enrich registered
processors with human-readable identity and safely editable declarative
patterns; a YAML file alone never makes a vendor deterministic.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..settings import PROJECT_ROOT
from .billing_v2 import deterministic_processor_audit


CONTRACT_VERSION = "deterministic-coverage/1.0"
VENDORS_DIR = PROJECT_ROOT / "config" / "vendors"


class DeterministicPatternField(BaseModel):
    path: str
    label: str
    values: list[str] = Field(default_factory=list)
    editable: bool = True


class DeterministicCoverage(BaseModel):
    contract_version: str = CONTRACT_VERSION
    vendor_key: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive", "registered_unavailable"]
    implementation_kind: Literal["hybrid", "code_managed"]
    processor_module: str
    processor_entrypoint: str
    processor_available: bool
    config_present: bool
    config_name: str | None = None
    editable: bool = False
    pattern_count: int = 0
    patterns: list[DeterministicPatternField] = Field(default_factory=list)
    failure_code: str | None = None


def inventory() -> list[DeterministicCoverage]:
    """Return every registered processor with safe declarative metadata."""
    return list(_inventory_cached())


@lru_cache(maxsize=1)
def _inventory_cached() -> tuple[DeterministicCoverage, ...]:
    rows = deterministic_processor_audit().get("processors") or []
    return tuple(_coverage_from_audit(row) for row in rows)


def invalidate_inventory_cache() -> None:
    _inventory_cached.cache_clear()


def registered_vendor_keys() -> set[str]:
    return {item.vendor_key for item in inventory()}


def resolve_vendor(vendor_name: str | None, abbreviation: str | None = None) -> DeterministicCoverage | None:
    """Resolve only exact normalized identities; never infer by fuzzy text."""
    candidates = {_norm(vendor_name), _norm(abbreviation)} - {""}
    if not candidates:
        return None
    matches: list[DeterministicCoverage] = []
    for item in inventory():
        identities = {_norm(item.display_name), _norm(item.vendor_key), *(_norm(alias) for alias in item.aliases)} - {""}
        if identities & candidates:
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def coverage_for_key(vendor_key: str) -> DeterministicCoverage | None:
    return next((item for item in inventory() if item.vendor_key == vendor_key), None)


def config_path_for_key(vendor_key: str, module_name: str = "") -> Path | None:
    """Find the registry-keyed YAML; never infer an unproven association."""
    direct = VENDORS_DIR / f"{vendor_key}.yaml"
    if direct.is_file():
        return direct
    return None


def _coverage_from_audit(row: dict[str, Any]) -> DeterministicCoverage:
    vendor_key = str(row.get("vendor_key") or "")
    module = str(row.get("module") or "")
    available = bool(row.get("available"))
    path = config_path_for_key(vendor_key, module)
    rules: dict[str, Any] = {}
    if path:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            rules = loaded if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError):
            rules = {}
    identity = rules.get("vendor_identity") if isinstance(rules.get("vendor_identity"), dict) else {}
    display_name = str(identity.get("vendor_name") or vendor_key.replace("_", " ").title())
    aliases = [str(value) for value in (identity.get("aliases") or []) if str(value).strip()]
    aliases.extend(str(value) for value in (identity.get("detection_keywords") or []) if str(value).strip())
    patterns = _pattern_fields(rules)
    active = bool(identity.get("active", True))
    status = "registered_unavailable" if not available else "active" if active else "inactive"
    failure_code = None
    if not available:
        raw = str(row.get("error") or "processor_unavailable")
        failure_code = raw.split(":", 1)[0].strip() or "processor_unavailable"
    return DeterministicCoverage(
        vendor_key=vendor_key,
        display_name=display_name,
        aliases=sorted(set(aliases), key=str.casefold),
        status=status,
        implementation_kind="hybrid" if path else "code_managed",
        processor_module=module,
        processor_entrypoint=str(row.get("entrypoint") or ""),
        processor_available=available,
        config_present=bool(path),
        config_name=path.name if path else None,
        editable=bool(path and rules),
        pattern_count=sum(len(item.values) for item in patterns),
        patterns=patterns,
        failure_code=failure_code,
    )


def _pattern_fields(rules: dict[str, Any]) -> list[DeterministicPatternField]:
    fields: list[DeterministicPatternField] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path)
            return
        leaf = path.rsplit(".", 1)[-1].casefold()
        is_pattern = any(token in leaf for token in ("pattern", "keyword", "alias", "contains"))
        if is_pattern and isinstance(value, list) and all(isinstance(item, str) for item in value):
            fields.append(DeterministicPatternField(
                path=path,
                label=leaf.replace("_", " ").title(),
                values=list(value),
            ))

    walk(rules)
    return fields


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


__all__ = [
    "CONTRACT_VERSION", "DeterministicCoverage", "DeterministicPatternField",
    "config_path_for_key", "coverage_for_key", "inventory", "registered_vendor_keys", "resolve_vendor",
    "invalidate_inventory_cache",
]
