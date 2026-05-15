"""Universal reasoning pipeline for non-deterministic invoices.

Dedicated vendor processors still own structured vendors. This service is the
shared path for unknown, semi-structured, screenshot, and variable supplier
documents: ingest candidates, classify category, apply Canonical Rules, validate
references, and then build ResMan rows from the normalized invoice model.
"""

from __future__ import annotations

from typing import Any

from . import ai_invoice_processor
from . import canonical_rules
from . import document_ingestion


def reason_invoice_candidates(
    payload: dict[str, Any],
    *,
    references: dict[str, list[dict[str, Any]]] | None = None,
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical normalized invoice model from extraction candidates."""
    normalized = ai_invoice_processor.validate_ai_extraction(
        payload,
        references=references,
        rules_override=rules_override,
    )
    return canonical_rules.canonicalize_normalized_invoice(
        normalized,
        references=references or ai_invoice_processor.load_references(),
        rules_override=rules_override,
    )


def reason_document_candidate(
    document: document_ingestion.DocumentCandidate | dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    references: dict[str, list[dict[str, Any]]] | None = None,
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize an extraction payload with a DocumentCandidate attached.

    The candidate is intentionally metadata/text only at this layer. Business
    rules still come from Canonical Rules and validation references.
    """
    if isinstance(document, dict):
        document = document_ingestion.document_candidate_from_dict(document)
    merged = dict(payload or {})
    merged.setdefault("_document_text", document.document_text)
    merged.setdefault("_source_file", document.source_file)
    merged.setdefault("_source_type", document.source_type)
    merged.setdefault("_document_candidate", document.to_dict())
    if document.vendor_hint:
        merged.setdefault("vendor_hint", document.vendor_hint)
    warnings = list(merged.get("warnings") or [])
    for warning in document.warnings:
        if warning not in warnings:
            warnings.append(warning)
    if warnings:
        merged["warnings"] = warnings
    return reason_invoice_candidates(
        merged,
        references=references,
        rules_override=rules_override,
    )


def build_resman_invoice(
    payload: dict[str, Any],
    *,
    batch_id: str,
    source_file: str,
    vendor_key: str = "unknown",
    support_document_url: str = "",
    support_document_status: str = "",
    support_document_dropbox_path: str = "",
    references: dict[str, list[dict[str, Any]]] | None = None,
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize extraction candidates and build the ResMan invoice payload."""
    normalized = reason_invoice_candidates(
        payload,
        references=references,
        rules_override=rules_override,
    )
    return ai_invoice_processor.ai_result_to_invoice(
        normalized,
        batch_id=batch_id,
        source_file=source_file,
        vendor_key=vendor_key,
        support_document_url=support_document_url,
        support_document_status=support_document_status,
        support_document_dropbox_path=support_document_dropbox_path,
    )


def classify_payload(payload: dict[str, Any]) -> str:
    """Expose the category classifier for smoke tests and diagnostics."""
    document_candidate = payload.get("_document_candidate")
    document_text = payload.get("_document_text")
    if isinstance(document_candidate, dict):
        document_text = document_text or document_candidate.get("document_text")
    normalized = {
        "vendor_name": payload.get("vendor_name"),
        "raw_vendor_name": payload.get("vendor_name"),
        "invoice_description": payload.get("invoice_description"),
        "service_address": payload.get("service_address") or document_text,
        "line_items": payload.get("line_items") or [],
    }
    return canonical_rules.classify_invoice_category(normalized)


__all__ = [
    "build_resman_invoice",
    "classify_payload",
    "reason_document_candidate",
    "reason_invoice_candidates",
]
