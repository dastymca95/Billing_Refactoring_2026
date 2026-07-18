"""Universal evidence-first line semantic classification."""

from __future__ import annotations

import re

from .accounting_contracts import EvidenceReference, LineItemFacts, SemanticClassification
from .ai_mapping_review import normalize_key
from .canonical_semantics import resolve_canonical_concept
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
    text = _expand_pos_abbreviations(text, indicators.get("retail_abbreviations") or {})
    context = normalize_key(document_context)
    scoped_context_refs = [ref for ref in facts.evidence if ref.source_type in {
        "line_section_header", "line_product_context",
    }]
    scoped_context = normalize_key(" ".join(
        str(ref.text or ref.normalized_text or "") for ref in scoped_context_refs
    ))
    material_hits = list(dict.fromkeys(
        _hits(text, indicators.get("material") or []) + _physical_goods_evidence(facts, text)
    ))
    if _has_explicit_material_subject(text):
        material_hits.append("physical_goods_subject")
    service_hits = _hits(text, indicators.get("service") or [])
    recurring_hits = _hits(text, indicators.get("recurring") or [])
    fee_hits = _hits(text, indicators.get("fee") or [])
    utility_service_type = detect_utility_service_type(text, raw or normalized)
    subscription_subject_hits = _hits(
        text, indicators.get("subscription_membership_subject") or []
    )
    capital_hits = _hits(text, indicators.get("capital") or [])
    trade_scores: list[tuple[int, str, list[str], list[str]]] = []
    for family, terms in (indicators.get("trades") or {}).items():
        line_hits = _hits(text, terms)
        scoped_hits = _hits(scoped_context, terms)
        # Invoice-level context may describe a different line in a mixed
        # receipt.  It remains available as document evidence, but it cannot
        # assign a trade family without a line-level hit.
        trade_scores.append((len(line_hits) * 3 + len(scoped_hits) * 2,
                             family, line_hits, scoped_hits))
    trade_scores.sort(reverse=True)
    trade_score, trade_family, trade_hits, scoped_trade_hits = (
        trade_scores[0] if trade_scores and trade_scores[0][0]
        else (0, "unknown", [], [])
    )
    if utility_service_type and not fee_hits:
        trade_family = "utility"
        trade_hits = [f"utility_service:{utility_service_type}"]
        scoped_trade_hits = []
        trade_score = max(trade_score, 3)
    # Context may disambiguate the trade only when this line independently
    # proves it is a physical-good purchase. This remains safe for mixed bills.
    if trade_family == "unknown" and material_hits:
        contextual_scores = []
        for family, terms in (indicators.get("trades") or {}).items():
            hits = _hits(context, terms)
            contextual_scores.append((len(hits), family, hits))
        contextual_scores.sort(reverse=True)
        if contextual_scores and contextual_scores[0][0]:
            _, trade_family, context_hits = contextual_scores[0]
            trade_hits = [f"document_context:{term}" for term in context_hits]
            scoped_trade_hits = []
            trade_score = len(context_hits)

    contradictions: list[str] = []
    if material_hits and service_hits:
        # Words such as "repair" and "maintenance" often describe the use of
        # tangible goods (repair kit, maintenance supplies), not performed
        # labor.  Counting tokens made a generated summary with two service
        # words override explicit merchandise evidence. Require an affirmative
        # performance marker before treating this mixed phrase as labor.
        material_subject = _has_explicit_material_subject(text)
        service_performance = _has_explicit_service_performance(text)
        work_mode = (
            "material_purchase"
            if material_subject and not service_performance
            else "labor_service" if len(service_hits) > len(material_hits) else "material_purchase"
        )
        contradictions.append("mixed_material_and_service_indicators")
    elif service_hits:
        work_mode = "labor_service"
    elif material_hits:
        work_mode = "material_purchase"
    elif recurring_hits:
        work_mode = "renewal" if _hits(text, ["renewal", "membership", "subscription"]) else "recurring_service"
    elif subscription_subject_hits:
        work_mode = "renewal" if _hits(text, ["renewal", "dues"]) else "recurring_service"
    elif fee_hits:
        work_mode = "one_time_fee"
    elif trade_family in {"tub_refinishing", "countertop"}:
        # Refinishing is intrinsically performed work. Matrix headers often
        # name only the surface (Bath Tub, Wall Tile, Tub Mat) because the
        # surrounding form supplies the operation.
        work_mode = "labor_service"
    elif trade_family == "utility":
        work_mode = "recurring_service"
    elif trade_family in {"insurance", "renters_insurance"}:
        work_mode = "recurring_service"
    else:
        work_mode = "unknown"
    if trade_family == "utility" and not fee_hits:
        work_mode = "recurring_service"
    # Policy/coverage identifiers can look like retail SKUs, and allocation
    # rows also contain quantity/price fields. Once current-line text proves
    # the insurance trade, those structural tokens are not evidence of a
    # tangible-material purchase.
    if trade_family in {"insurance", "renters_insurance"} and not fee_hits:
        work_mode = "recurring_service"
    if trade_family == "processing_fee":
        work_mode = "one_time_fee"

    if trade_family == "legal":
        line_family = "legal"
    elif trade_family in {"insurance", "renters_insurance"}:
        line_family = "insurance"
    elif trade_family == "utility":
        line_family = "utility"
    elif trade_family == "processing_fee":
        line_family = "fee"
    elif subscription_subject_hits:
        # "Fee" is commonly a billing-form modifier (membership fee,
        # license fee, renewal fee). It describes the expense family only
        # when no stronger current-line subject is present.
        line_family = "subscription_membership"
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
    positive_terms = (material_hits + service_hits + recurring_hits + fee_hits
                      + subscription_subject_hits + capital_hits + trade_hits)
    positive = [EvidenceReference(document_id=document_id, text=raw or None, normalized_text=term,
        source_type="line_item", extraction_method="semantic_classifier") for term in dict.fromkeys(positive_terms)]
    positive.extend(EvidenceReference(
        document_id=document_id,
        page=scoped_context_refs[0].page if scoped_context_refs else None,
        text=scoped_context_refs[0].text if scoped_context_refs else None,
        normalized_text=term, source_type="line_section_header",
        extraction_method="semantic_classifier",
    ) for term in dict.fromkeys(scoped_trade_hits))
    location = facts.detected_location or extract_line_location(raw or normalized).get("location") or None
    basis = min(1.0, 0.25 + (0.25 if trade_score else 0) + (0.25 if work_mode != "unknown" else 0) + min(0.2, len(positive_terms) * 0.04))
    canonical = resolve_canonical_concept(
        raw or normalized, line_family=line_family, trade_family=trade_family,
        work_mode=work_mode,
    )
    if canonical.matched_phrase:
        line_family = canonical.line_family
        trade_family = canonical.trade_family
        work_mode = canonical.work_mode
        positive.append(EvidenceReference(
            document_id=document_id, text=raw or None,
            normalized_text=canonical.concept_id,
            source_type="canonical_semantic_concept",
            extraction_method=canonical.version,
        ))
        basis = max(basis, 0.75)
    return SemanticClassification(
        semantic_version=str(config.get("semantic_version") or "semantic-classification/1.0"),
        line_item_id=facts.line_item_id,
        document_family=document_family if document_family in DOCUMENT_FAMILIES else "unknown",
        line_family=line_family, trade_family=trade_family, work_mode=work_mode,
        recurrence="recurring" if recurring_hits or subscription_subject_hits
        or work_mode == "recurring_service" else "one_time",
        capital_context="capital" if capital_hits else "operating",
        specific_assets=_assets(text), location_detected=location,
        positive_evidence=positive or evidence[:1], negative_evidence=[], contradictions=contradictions,
        confidence=round(basis, 2),
    )


def _hits(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(normalize_key(term))}(?![a-z0-9])", text)]


def _has_explicit_material_subject(text: str) -> bool:
    return bool(re.search(
        r"(?<![a-z0-9])(?:supplies|materials?|parts?|hardware|merchandise|products?|items?|kits?)(?![a-z0-9])",
        text,
    ))


def _has_explicit_service_performance(text: str) -> bool:
    return bool(re.search(
        r"(?<![a-z0-9])(?:labor|labour|technician|service call|installation|installed|performed|hours?|trip charge)(?![a-z0-9])",
        text,
    ))


def detect_utility_service_type(normalized_text: str, source_text: str = "") -> str | None:
    """Detect billed utility service from line-bound, vendor-neutral evidence.

    A bare word such as ``water`` is not enough because it may describe a
    retail product or repair. A billing-period/account/usage marker must occur
    with the service term, keeping the rule applicable across vendors without
    turning merchandise into utility expense.
    """
    text = normalize_key(normalized_text)
    source = source_text or ""
    billing_context = bool(
        re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s*[-–—]\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", source)
        or re.search(r"(?<![a-z0-9])(?:billing period|service period|meter|usage|consumption|account)(?![a-z0-9])", text)
    )
    if not billing_context:
        return None
    ordered = (
        ("stormwater", ("stormwater", "storm water")),
        ("trash", ("sanitation", "trash", "garbage", "refuse", "solid waste", "waste collection")),
        ("water_sewer", ("wastewater", "waste water", "sewer", "water")),
        ("internet_fiber", ("internet", "fiber", "broadband")),
        ("electric", ("electric", "electricity", "kwh", "power")),
        ("natural_gas", ("natural gas", "therm")),
    )
    for service_type, terms in ordered:
        if _hits(text, list(terms)):
            return service_type
    return None


def _expand_pos_abbreviations(text: str, abbreviations: dict[str, str]) -> str:
    """Expand configured, vendor-neutral POS tokens in normalized text only."""
    expanded = text
    for token, meaning in abbreviations.items():
        expanded = re.sub(
            rf"(?<![a-z0-9]){re.escape(normalize_key(token))}(?![a-z0-9])",
            normalize_key(meaning),
            expanded,
        )
    return expanded


def _physical_goods_evidence(facts: LineItemFacts, text: str) -> list[str]:
    """Vendor-neutral evidence that a line represents tangible merchandise."""
    evidence: list[str] = []
    units = re.findall(
        r"(?<![a-z0-9])(?:\d+(?:\.\d+)?\s*)?(gal|gallon|qt|quart|pt|pint|oz|ounce|lb|pound|kg|liter|litre|ft|foot|box|case|pack|roll|sheet|each|ea)(?![a-z0-9])",
        text,
    )
    if units:
        evidence.extend(f"physical_unit:{unit}" for unit in dict.fromkeys(units))
    sku_like = bool(re.search(
        r"(?<![a-z0-9])(?=[a-z0-9-]{6,}(?![a-z0-9]))(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+",
        text,
    ))
    commercial_values = sum(value is not None for value in (facts.quantity, facts.unit_price, facts.amount))
    if sku_like and commercial_values >= 2:
        evidence.append("sku_with_quantity_price")
    if re.search(r"(?<![a-z0-9])(color|colour)(?![a-z0-9])", text) and (units or sku_like):
        evidence.append("product_color_attribute")
    return evidence


def _assets(text: str) -> list[str]:
    assets = ("bathtub", "cabinet", "countertop", "refrigerator", "dishwasher", "flooring", "carpet", "door", "toilet", "water heater")
    return [asset.replace(" ", "_") for asset in assets if normalize_key(asset) in text]


__all__ = ["classify_line", "detect_utility_service_type"]
