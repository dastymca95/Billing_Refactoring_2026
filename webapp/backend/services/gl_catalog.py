"""Versioned GL metadata catalog generated from approved repository sources."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .. import settings
from .accounting_contracts import GLAccountMetadata
from .ai_mapping_review import normalize_key
from .gl_payability import is_payable_gl_account


CONFIG_PATH = settings.PROJECT_ROOT / "config" / "accounting_decision_v2.yaml"
CHART_PATH = settings.RUNTIME_ASSET_ROOT / "Gl Codes" / "Chart Of Accounts.csv"


@lru_cache(maxsize=1)
def load_decision_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_gl_catalog() -> tuple[str, dict[str, GLAccountMetadata]]:
    """Load the catalog for the current tenant/runtime snapshot.

    The public no-argument contract remains backward compatible. Its cache key
    includes the runtime root, tenant and published chart fingerprint so test
    runtimes and tenants can never reuse another context's accounting catalog.
    """
    tenant_id = ""
    fingerprint = ""
    try:
        from . import resman_context_data as context_data
        from .tenant_accounting_policies import default_tenant_id

        tenant_id = default_tenant_id()
        fingerprint = context_data.current_snapshot_fingerprint(
            tenant_id, context_data.DatasetKind.GL_ACCOUNTS,
        ) or ""
    except Exception:
        pass
    return _load_gl_catalog_cached(
        str(CHART_PATH), str(settings.WEBAPP_DATA_ROOT), tenant_id, fingerprint,
    )


@lru_cache(maxsize=16)
def _load_gl_catalog_cached(
    chart_path: str,
    _runtime_root: str,
    tenant_id: str,
    fingerprint: str,
) -> tuple[str, dict[str, GLAccountMetadata]]:
    config = load_decision_config()
    overrides = config.get("gl_overrides") or {}
    catalog: dict[str, GLAccountMetadata] = {}
    with Path(chart_path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("Number") or "").strip()
            name = str(row.get("Name") or "").strip()
            if not code or not name:
                continue
            override = overrides.get(code) or {}
            inferred = _infer_metadata(name, str(row.get("Description") or ""))
            merged = {**inferred, **override}
            account = {"gl_account_type": str(row.get("Type") or "")}
            catalog[code] = GLAccountMetadata(
                gl_code=code,
                gl_name=name,
                gl_family=str(merged.get("gl_family") or "unknown"),
                trade_families=list(merged.get("trade_families") or []),
                compatible_work_modes=list(merged.get("compatible_work_modes") or []),
                incompatible_work_modes=list(merged.get("incompatible_work_modes") or []),
                capital_context=str(merged.get("capital_context") or "operating"),
                specificity=str(merged.get("specificity") or "broad"),
                payable=is_payable_gl_account(code, {code: account}),
                description_tokens=_tokens(f"{name} {row.get('Description') or ''}"),
                scope_qualifiers=_scope_qualifiers(name),
                metadata_source="chart+approved_config" if override else "chart_inference",
                metadata_confidence=0.98 if override else 0.65,
            )
    catalog_version = str(config.get("catalog_version") or "gl-catalog/1.0")
    # File-first ResMan adapter. Published tenant chart rows may add or update
    # catalog identity/payability, while approved semantic overrides remain
    # the only source of semantic specificity. No GL is selected here.
    try:
        from . import resman_context_data as context_data

        imported = (
            context_data.list_all_effective_records(
                tenant_id, context_data.DatasetKind.GL_ACCOUNTS,
            )
            if tenant_id else []
        )
        for row in imported:
            code = str(row.get("gl_code") or "").strip()
            name = str(row.get("gl_name") or "").strip()
            if not code or not name or not bool(row.get("active", True)):
                continue
            override = overrides.get(code) or {}
            inferred = _infer_metadata(name, str(row.get("description") or ""))
            merged = {**inferred, **override}
            catalog[code] = GLAccountMetadata(
                gl_code=code,
                gl_name=name,
                gl_family=str(merged.get("gl_family") or "unknown"),
                trade_families=list(merged.get("trade_families") or []),
                compatible_work_modes=list(merged.get("compatible_work_modes") or []),
                incompatible_work_modes=list(merged.get("incompatible_work_modes") or []),
                capital_context=str(merged.get("capital_context") or "operating"),
                specificity=str(merged.get("specificity") or "broad"),
                payable=bool(row.get("payable", False)),
                description_tokens=_tokens(f"{name} {row.get('description') or ''}"),
                scope_qualifiers=_scope_qualifiers(name),
                metadata_source=(
                    "resman_snapshot+approved_config" if override else "resman_snapshot+chart_inference"
                ),
                metadata_confidence=0.98 if override else 0.65,
            )
        if fingerprint:
            catalog_version = f"{catalog_version}+resman-{fingerprint[:12]}"
    except Exception:
        # The committed/runtime chart remains the backward-compatible source
        # when the optional tenant Data Hub has not been initialized.
        pass
    return catalog_version, catalog


# Compatibility for existing callers that explicitly invalidate the catalog.
load_gl_catalog.cache_clear = _load_gl_catalog_cached.cache_clear  # type: ignore[attr-defined]


def _infer_metadata(name: str, description: str) -> dict:
    text = normalize_key(f"{name} {description}")
    mode: list[str] = []
    incompatible: list[str] = []
    family = "unknown"
    if "license" in text and ("click" in text or "clicks" in text):
        family = "license_click_service"
        mode = ["renewal", "recurring_service"]
    if any(term in text for term in ("supplies", "parts", "materials", "tools")):
        mode = ["material_purchase"]
        incompatible = ["labor_service", "recurring_service"]
    elif any(term in text for term in ("contract", "service", "maintenance", "repair")):
        mode = ["labor_service", "recurring_service"]
        incompatible = ["material_purchase"]
    for candidate in ("plumbing", "electrical", "hvac", "painting", "cleaning", "landscaping", "pest", "appliance", "flooring", "legal", "insurance"):
        if candidate.replace("ing", "") in text:
            family = candidate
            break
    return {"gl_family": family, "trade_families": [] if family == "unknown" else [family],
            "compatible_work_modes": mode, "incompatible_work_modes": incompatible,
            "specificity": "specific" if family != "unknown" else "broad"}


def _tokens(value: str) -> list[str]:
    ignored = {"and", "the", "of", "other", "expense", "contract"}
    return sorted({token for token in re.findall(r"[a-z0-9]+", normalize_key(value)) if len(token) > 2 and token not in ignored})


def _scope_qualifiers(name: str) -> list[str]:
    """Preserve proper-noun/acronym scope that generic tokenization loses."""
    ignored = {"GL", "AP", "AR", "R&M"}
    return sorted({token.casefold() for token in re.findall(r"\b[A-Z][A-Z0-9&]{1,}\b", name)
                   if token not in ignored})


__all__ = ["load_decision_config", "load_gl_catalog"]
