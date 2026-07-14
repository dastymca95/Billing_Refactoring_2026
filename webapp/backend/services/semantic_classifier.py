"""Universal evidence-first line semantic classification."""

from __future__ import annotations

import re

from .accounting_contracts import EvidenceReference, LineItemFacts, SemanticClassification
from .ai_mapping_review import normalize_key
from .gl_catalog import load_decision_config
from .service_invoice_gl_reasoning import extract_line_location


DOCUMENT_FAMILIES = {"invoice", "utility_bill", "vendor_statement", "past_due_notice", "credit", "loan_statement", "insurance_document", "legal_document", "marketing", "unknown"}


def classify_line(facts: LineItemFacts, *, document_id: str, document_family: str = "invoice",
                  document_context: str = "", vendor_profile: dict | None = None) -> SemanticClassification:
    config = load_decision_config()
    indicators = config.get("semantic_indicators") or {}
    raw = " ".join(value for value in (facts.raw_activity, facts.raw_description) if value)
    normalized = " ".join(value for value in (facts.normalized_activity, facts.normalized_description) if value)
    text = normalize_key(normalized or raw)
    context = normalize_key(document_context)
    material_hits = _hits(text, indicators.get("material") or [])
    service_hits = _hits(text, indicators.get("service") or [])
    recurring_hits = _hits(text, indicators.get("recurring") or [])
    fee_hits = _hits(text, indicators.get("fee") or [])
    capital_hits = _hits(text, indicators.get("capital") or [])
    trade_scores: list[tuple[int, str, list[str]]] = []
    for family, terms in (indicators.get("trades") or {}).items():
        line_hits = _hits(text, terms)
        context_hits = _hits(context, terms)
        trade_scores.append((len(line_hits) * 3 + len(context_hits), family, line_hits))
    trade_scores.sort(reverse=True)
    trade_score, trade_family, trade_hits = trade_scores[0] if trade_scores and trade_scores[0][0] else (0, "unknown", [])

    contradictions: list[str] = []
    if material_hits and service_hits:
        work_mode = "labor_service" if len(service_hits) > len(material_hits) else "material_purchase"
        contradictions.append("mixed_material_and_service_indicators")
    elif service_hits:
        work_mode = "labor_service"
    elif material_hits:
        work_mode = "material_purchase"
    elif recurring_hits:
        work_mode = "renewal" if _hits(text, ["renewal", "membership", "subscription"]) else "recurring_service"
    elif fee_hits:
        work_mode = "one_time_fee"
    elif trade_family == "utility":
        work_mode = "recurring_service"
    else:
        work_mode = "unknown"
    if trade_family == "utility" and not fee_hits:
        work_mode = "recurring_service"

    if trade_family == "legal":
        line_family = "legal"
    elif trade_family == "insurance":
        line_family = "insurance"
    elif trade_family == "utility":
        line_family = "utility"
    elif fee_hits:
        line_family = "fee"
    elif work_mode == "material_purchase":
        line_family = "materials"
    elif work_mode in {"labor_service", "recurring_service"}:
        line_family = "labor_service"
    elif recurring_hits:
        line_family = "subscription_membership"
    else:
        line_family = "unknown"

    evidence = list(facts.evidence)
    if (raw or normalized) and not evidence:
        evidence.append(EvidenceReference(document_id=document_id, text=raw or None,
            normalized_text=normalized or None, source_type="line_item", extraction_method="legacy_adapter"))
    positive_terms = material_hits + service_hits + recurring_hits + fee_hits + capital_hits + trade_hits
    positive = [EvidenceReference(document_id=document_id, text=raw or None, normalized_text=term,
        source_type="line_item", extraction_method="semantic_classifier") for term in dict.fromkeys(positive_terms)]
    location = facts.detected_location or extract_line_location(raw or normalized).get("location") or None
    basis = min(1.0, 0.25 + (0.25 if trade_score else 0) + (0.25 if work_mode != "unknown" else 0) + min(0.2, len(positive_terms) * 0.04))
    return SemanticClassification(
        semantic_version=str(config.get("semantic_version") or "semantic-classification/1.0"),
        line_item_id=facts.line_item_id,
        document_family=document_family if document_family in DOCUMENT_FAMILIES else "unknown",
        line_family=line_family, trade_family=trade_family, work_mode=work_mode,
        recurrence="recurring" if recurring_hits or work_mode == "recurring_service" else "one_time",
        capital_context="capital" if capital_hits else "operating",
        specific_assets=_assets(text), location_detected=location,
        positive_evidence=positive or evidence[:1], negative_evidence=[], contradictions=contradictions,
        confidence=round(basis, 2),
    )


def _hits(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(normalize_key(term))}(?![a-z0-9])", text)]


def _assets(text: str) -> list[str]:
    assets = ("bathtub", "cabinet", "countertop", "refrigerator", "dishwasher", "flooring", "carpet", "door", "toilet", "water heater")
    return [asset.replace(" ", "_") for asset in assets if normalize_key(asset) in text]


__all__ = ["classify_line"]
