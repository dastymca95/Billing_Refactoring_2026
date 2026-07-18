"""Dependency-aware reuse of authoritative per-row accounting decisions.

This is not an extraction cache.  It only prevents legacy/global normalizers
from executing the same central pipeline twice over unchanged facts.  Any
manual accounting edit or policy/catalog dependency change invalidates reuse.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import settings


ARTIFACT_VERSION = "accounting-pipeline-artifact/1.0"
_DEPENDENCY_FILES = (
    "config/accounting_decision_v2.yaml",
    "config/canonical_rules.yaml",
    "config/tenant_document_policies.yaml",
)


def _file_hash(relative: str) -> str:
    path = settings.PROJECT_ROOT / relative
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def dependency_versions() -> dict[str, str]:
    from .gl_catalog import load_gl_catalog

    catalog_version, _ = load_gl_catalog()
    values = {"gl_catalog": str(catalog_version)}
    values.update({relative: _file_hash(relative) for relative in _DEPENDENCY_FILES})
    operator_rules = settings.WEBAPP_DATA_ROOT / "operator_accounting_rules" / "rules.json"
    if operator_rules.is_file():
        try:
            values["operator_accounting_rules"] = hashlib.sha256(
                operator_rules.read_bytes()
            ).hexdigest()
        except OSError:
            values["operator_accounting_rules"] = "unreadable"
    else:
        values["operator_accounting_rules"] = "none"
    return values


def row_fingerprint(row: dict[str, Any], dependencies: dict[str, str] | None = None) -> str:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    source_text = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    payload = {
        "version": ARTIFACT_VERSION,
        "dependencies": dependencies or dependency_versions(),
        "accounting_inputs": {
            "vendor": row.get("Vendor"),
            "property": row.get("Property"),
            "location": row.get("Location"),
            "gl_account": row.get("GL Account"),
            "amount": row.get("Total Amount"),
            "unit_price": row.get("Unit Price"),
            "quantity": row.get("Quantity"),
            "tax": row.get("Tax"),
            "bill_or_credit": row.get("Bill or Credit"),
            "source_text": source_text,
            "document_facts": meta.get("document_facts"),
            "semantic_classification": meta.get("semantic_classification"),
            "semantic_reasoning_result": meta.get("semantic_reasoning_result"),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def request_fingerprint(
    row: dict[str, Any], dependencies: dict[str, str] | None = None
) -> str:
    """Fingerprint inputs before the engine writes its selected GL."""
    payload = json.loads(json.dumps(row, default=str))
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    for generated in (
        "accounting_decision", "accounting_pipeline_artifact", "phase3_accounting_route",
        "gl_shadow_comparison", "semantic_reasoning_trace",
    ):
        meta.pop(generated, None)
    # DocumentFacts are retained with the cached artifact for provenance, but
    # their generated identifiers/evidence envelopes are not accounting
    # inputs.  Row values, immutable source_text, semantic facts, catalogs and
    # policies remain in the fingerprint and control invalidation.
    meta.pop("document_facts", None)
    # AI extraction is candidate-only. The authoritative selected value is not
    # an input on a fresh cached-facts reconstruction.
    payload["GL Account"] = str(payload.get("GL Account") or "").strip()
    basis = {
        "version": ARTIFACT_VERSION,
        "dependencies": dependencies or dependency_versions(),
        "row": payload,
    }
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _persistent_path(key: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "cache" / "accounting_pipeline" / f"{key}.json"


def hydrate(row: dict[str, Any], dependencies: dict[str, str] | None = None) -> bool:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    if not meta.get("ai_generated"):
        return False
    deps = dependencies or dependency_versions()
    key = request_fingerprint(row, deps)
    try:
        payload = json.loads(_persistent_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if payload.get("version") != ARTIFACT_VERSION or payload.get("dependencies") != deps:
        return False
    selected = str(payload.get("selected_gl_code") or "").strip()
    row["GL Account"] = selected
    for field, value in (payload.get("meta") or {}).items():
        meta[field] = value
    # A reusable accounting decision is incomplete without the immutable
    # facts it decided over.  Older artifacts that did not retain this
    # contract deliberately miss and are recomputed once.
    if not isinstance(meta.get("source_text"), dict) or not isinstance(
        meta.get("document_facts"), dict
    ):
        return False
    decision = meta.get("accounting_decision")
    if not isinstance(decision, dict) or str(decision.get("selected_gl_code") or "").strip() != selected:
        return False
    meta["accounting_pipeline_artifact"] = {
        "version": ARTIFACT_VERSION,
        "fingerprint": row_fingerprint(row, deps),
        "request_fingerprint": key,
        "dependencies": deps,
        "decision_id": decision.get("decision_id"),
        "selected_gl_code": selected,
        "persistent_cache_hit": True,
    }
    return True


def is_reusable(row: dict[str, Any], dependencies: dict[str, str] | None = None) -> bool:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    artifact = meta.get("accounting_pipeline_artifact")
    decision = meta.get("accounting_decision")
    if not isinstance(artifact, dict) or not isinstance(decision, dict):
        return False
    if artifact.get("version") != ARTIFACT_VERSION:
        return False
    if str(decision.get("decision_id") or "") != str(artifact.get("decision_id") or ""):
        return False
    selected = str(decision.get("selected_gl_code") or "").strip()
    if selected != str(row.get("GL Account") or "").strip():
        return False
    return artifact.get("fingerprint") == row_fingerprint(row, dependencies)


def mark(
    row: dict[str, Any],
    dependencies: dict[str, str] | None = None,
    *,
    request_key: str = "",
) -> None:
    meta = row.setdefault("_meta", {})
    decision = meta.get("accounting_decision") if isinstance(meta, dict) else None
    if not isinstance(decision, dict):
        return
    deps = dependencies or dependency_versions()
    meta["accounting_pipeline_artifact"] = {
        "version": ARTIFACT_VERSION,
        "fingerprint": row_fingerprint(row, deps),
        "dependencies": deps,
        "decision_id": decision.get("decision_id"),
        "selected_gl_code": decision.get("selected_gl_code"),
    }
    if request_key and meta.get("ai_generated"):
        retained_meta = {
            key: meta.get(key) for key in (
                "source_text", "document_facts",
                "semantic_classification", "semantic_reasoning_trace",
                "operator_accounting_rule_trace", "tenant_accounting_policy_trace",
                "accounting_decision", "phase3_accounting_route", "gl_shadow_comparison",
            ) if key in meta
        }
        payload = {
            "version": ARTIFACT_VERSION,
            "dependencies": deps,
            "request_fingerprint": request_key,
            "selected_gl_code": decision.get("selected_gl_code"),
            "meta": retained_meta,
        }
        path = _persistent_path(request_key)
        tmp = path.with_suffix(".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(path)


__all__ = [
    "ARTIFACT_VERSION",
    "dependency_versions",
    "hydrate",
    "is_reusable",
    "mark",
    "request_fingerprint",
    "row_fingerprint",
]
