"""Compatibility adapters from legacy processor rows into Phase 2 contracts."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting_contracts import DocumentFacts, EvidenceReference, GLCandidate, LineItemFacts, model_dict
from .accounting_decision_engine import AccountingDecisionEngine
from .gl_catalog import load_gl_catalog
from .semantic_classifier import classify_line


def v2_enabled() -> bool:
    return os.environ.get("ACCOUNTING_DECISION_ENGINE_V2", "1").strip().lower() not in {"0", "false", "off", "no"}


def capture_source_fields(row: dict[str, Any], *, document_id: str, line_item_id: str) -> None:
    meta = row.setdefault("_meta", {})
    if not isinstance(meta, dict):
        row["_meta"] = meta = {}
    meta.setdefault("source_text", {})
    source = meta["source_text"]
    if isinstance(source, dict):
        source.setdefault("raw_activity", meta.get("raw_activity") or meta.get("source_activity") or meta.get("ai_line_activity"))
        source.setdefault("raw_description", meta.get("source_line_description") or meta.get("ai_source_line_description") or row.get("Line Item Description"))
        source.setdefault("raw_invoice_description", meta.get("source_invoice_description") or row.get("Invoice Description"))
        source.setdefault("document_id", document_id)
        source.setdefault("line_item_id", line_item_id)


def decide_row(row: dict[str, Any], *, document_id: str, line_item_id: str, extraction_route: str) -> None:
    meta = row.setdefault("_meta", {})
    if not isinstance(meta, dict):
        row["_meta"] = meta = {}
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    raw_description = _text(source.get("raw_description"))
    raw_activity = _text(source.get("raw_activity"))
    normalized_description = _text(meta.get("normalized_source_description") or raw_description)
    generated_description = _text(row.get("Line Item Description"))
    evidence = [EvidenceReference(document_id=document_id, page=_int(meta.get("source_page")),
        text=raw_description or raw_activity or None, normalized_text=normalized_description or None,
        source_type="line_item", extraction_method=extraction_route)]
    facts_line = LineItemFacts(line_item_id=line_item_id, raw_activity=raw_activity or None,
        raw_description=raw_description or None, normalized_activity=raw_activity or None,
        normalized_description=normalized_description or None, generated_description=generated_description or None,
        quantity=_decimal(row.get("Quantity")), unit_price=_decimal(row.get("Unit Price")),
        amount=_decimal(row.get("Amount")), tax=_decimal(row.get("Tax")),
        detected_location=_text(row.get("Location")) or None, evidence=evidence)
    facts = DocumentFacts(document_id=document_id, invoice_id=_text(row.get("Invoice Number")) or line_item_id,
        vendor_candidate=_text(row.get("Vendor")) or None, invoice_number=_text(row.get("Invoice Number")) or None,
        invoice_date=_date(row.get("Invoice Date")), due_date=_date(row.get("Due Date")),
        property_candidate=_text(row.get("Property Abbreviation")) or None,
        total_amount=_decimal(row.get("Amount")), line_items=[facts_line], extraction_route=extraction_route,
        extraction_model=_text(meta.get("extraction_model")) or None, evidence=[])
    semantics = classify_line(facts_line, document_id=document_id,
        document_family=_document_family(row), document_context=_text(source.get("raw_invoice_description")))
    candidates = _adapt_candidates(row, normalized_description or raw_description or raw_activity)
    _, catalog = load_gl_catalog()
    decision = AccountingDecisionEngine().decide(facts, semantics, catalog, candidates, {})
    legacy_gl = _text(row.get("GL Account"))
    meta["document_facts"] = model_dict(facts)
    meta["semantic_classification"] = model_dict(semantics)
    meta["accounting_decision"] = model_dict(decision)
    meta["ai_gl_accounting_reasoning"] = model_dict(decision)  # temporary UI adapter
    meta["gl_shadow_comparison"] = {"legacy_selected_gl": legacy_gl or None,
        "v2_selected_gl": decision.selected_gl_code, "same": legacy_gl == (decision.selected_gl_code or ""),
        "difference_reason": None if legacy_gl == (decision.selected_gl_code or "") else decision.why_selected}
    if v2_enabled():
        row["GL Account"] = decision.selected_gl_code or ""
        reasons = list(meta.get("manual_review_reasons") or [])
        reasons = [reason for reason in reasons if reason not in {"gl_account_missing", "invalid_gl_account"}]
        if decision.review_blocking:
            reasons.append("accounting_decision_blocking")
        meta["manual_review_reasons"] = sorted(set(reasons))


def _adapt_candidates(row: dict[str, Any], evidence_text: str) -> list[GLCandidate]:
    _, catalog = load_gl_catalog()
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    out: list[GLCandidate] = []
    seen: set[tuple[str, str]] = set()
    legacy_source = "learned_correction" if meta.get("learned_corrections_applied") else str(
        meta.get("gl_suggestion_source") or meta.get("accounting_source") or "deterministic_parser"
    )
    sources = [
        (row.get("GL Account"), legacy_source, "legacy_row"),
        (meta.get("ai_source_gl_candidate"), "ai_candidate", "ai_source_gl_candidate"),
        (meta.get("vendor_default_gl"), "vendor_default", "vendor_profile"),
        (meta.get("historical_gl"), "historical_mapping", "historical_mapping"),
        (meta.get("learned_gl"), "learned_correction", "approved_correction"),
        (meta.get("canonical_gl"), "canonical_rule", "canonical_rule"),
    ]
    for item in meta.get("gl_candidate_inputs") or []:
        if isinstance(item, dict):
            sources.append((item.get("gl_code"), str(item.get("source") or "legacy_adapter"), item.get("source_id")))
    for raw, source, source_id in sources:
        code = _text(raw)
        if not code or code not in catalog or (code, source) in seen:
            continue
        seen.add((code, source))
        out.append(GLCandidate(gl_code=code, gl_name=catalog[code].gl_name, source=source,
            source_id=source_id, base_score=0.95 if source in {"deterministic_parser", "canonical_rule"} else 0.75,
            positive_evidence=[{"source": source_id, "value": code}], rule_version="legacy-adapter/1.0"))
    # Catalog candidates are proposals only. The engine still performs all selection.
    normalized_evidence = evidence_text.lower()
    for code, account in catalog.items():
        if account.trade_families and any(token in normalized_evidence for token in account.description_tokens):
            out.append(GLCandidate(gl_code=code, gl_name=account.gl_name, source="catalog_text_match",
                source_id="chart_metadata", base_score=0.55, rule_version="gl-catalog/1.0"))
    return out


def _document_family(row: dict[str, Any]) -> str:
    bill_type = _text(row.get("Bill or Credit")).lower()
    return "credit" if "credit" in bill_type else "invoice"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["capture_source_fields", "decide_row", "v2_enabled"]
