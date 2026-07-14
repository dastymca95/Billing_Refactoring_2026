"""Accounting-grade GL reasoning for service contractor invoices.

The AI extractor can surface words; this module classifies the purchased work
before GL selection so labor/service lines do not drift into supplies accounts
and broad contract accounts do not beat more specific same-family GLs.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import yaml

from .. import settings
from . import ai_mapping_review


GL_META: dict[str, dict[str, str]] = {
    "6500": {"name": "Contract Services", "family": "contract_services", "mode": "labor_service", "specificity": "broad"},
    "6505": {"name": "Appliance - Contract", "family": "appliance", "mode": "labor_service", "specificity": "specific"},
    "6512": {"name": "Countertop Refinishing / Paint", "family": "countertop", "mode": "labor_service", "specificity": "specific"},
    "6515": {"name": "Contract Grounds (trash removal common areas)", "family": "trash_removal", "mode": "labor_service", "specificity": "specific"},
    "6530": {"name": "Contract Maintenance-Temp", "family": "general_maintenance", "mode": "labor_service", "specificity": "specific"},
    "6540": {"name": "Electrical - Contract", "family": "electrical", "mode": "labor_service", "specificity": "specific"},
    "6555": {"name": "HVAC - Contract", "family": "hvac", "mode": "labor_service", "specificity": "specific"},
    "6560": {"name": "Pest Control - Contract", "family": "pest", "mode": "labor_service", "specificity": "specific"},
    "6565": {"name": "Plumbing - Contract", "family": "plumbing", "mode": "labor_service", "specificity": "specific"},
    "6570": {"name": "Plumbing Tub & Sink Refinish", "family": "tub_refinishing", "mode": "labor_service", "specificity": "specific"},
    "6595": {"name": "Other Contract Services", "family": "contract_services", "mode": "labor_service", "specificity": "broad"},
    "6606": {"name": "Appliance Parts & Supplies", "family": "appliance", "mode": "materials", "specificity": "specific"},
    "6615": {"name": "Building Maintenance & Repairs - Minor", "family": "general_maintenance", "mode": "labor_service", "specificity": "specific"},
    "6627": {"name": "Electrical Parts & Supplies", "family": "electrical", "mode": "materials", "specificity": "specific"},
    "6654": {"name": "HVAC Parts & Supplies", "family": "hvac", "mode": "materials", "specificity": "specific"},
    "6660": {"name": "Light Bulbs & Fixture Repairs", "family": "electrical", "mode": "materials", "specificity": "specific"},
    "6669": {"name": "Maint. Tools & Supplies", "family": "general_maintenance", "mode": "materials", "specificity": "broad"},
    "6672": {"name": "Pest Control - Supplies", "family": "pest", "mode": "materials", "specificity": "specific"},
    "6675": {"name": "Plumbing Supplies", "family": "plumbing", "mode": "materials", "specificity": "specific"},
    "6720": {"name": "Carpet & Vinyl Repairs & Dyes", "family": "flooring", "mode": "labor_service", "specificity": "specific"},
    "6730": {"name": "Cleaning / Janitorial Supplies", "family": "cleaning", "mode": "materials", "specificity": "specific"},
    "6740": {"name": "Contract Carpet Cleaning", "family": "cleaning", "mode": "labor_service", "specificity": "specific"},
    "6750": {"name": "Contract Cleaning", "family": "cleaning", "mode": "labor_service", "specificity": "specific"},
    "6760": {"name": "Contract Painting", "family": "painting", "mode": "labor_service", "specificity": "specific"},
    "6770": {"name": "Paint & Supplies", "family": "painting", "mode": "materials", "specificity": "specific"},
    "6775": {"name": "Maintenance Turn", "family": "unit_turn", "mode": "labor_service", "specificity": "specific"},
    "6780": {"name": "Sheetrock Repair", "family": "general_maintenance", "mode": "labor_service", "specificity": "specific"},
    "6785": {"name": "Trash/Furniture Removal for Turn", "family": "trash_removal", "mode": "labor_service", "specificity": "specific"},
    "6790": {"name": "Turn Supplies", "family": "unit_turn", "mode": "materials", "specificity": "specific"},
    "6800": {"name": "Grounds", "family": "landscaping", "mode": "labor_service", "specificity": "broad"},
    "6810": {"name": "Landscape - Contract", "family": "landscaping", "mode": "labor_service", "specificity": "specific"},
    "7510": {"name": "Cabinets & Contertop Replacement", "family": "cabinets", "mode": "labor_service", "specificity": "specific"},
    "7534": {"name": "Floor Covering - Carpet", "family": "flooring", "mode": "labor_service", "specificity": "specific"},
    "7536": {"name": "Floor Covering - Vinyl / Tile / Wood", "family": "flooring", "mode": "labor_service", "specificity": "specific"},
    "7595": {"name": "Remodel Of Units", "family": "remodel", "mode": "labor_service", "specificity": "specific"},
}

SUPPLIES_BY_FAMILY = {
    "painting": "6770",
    "cleaning": "6730",
    "plumbing": "6675",
    "electrical": "6627",
    "hvac": "6654",
    "appliance": "6606",
    "pest": "6672",
    "general_maintenance": "6669",
    "unit_turn": "6790",
}

SERVICE_VENDOR_TERMS = (
    "service",
    "services",
    "contractor",
    "contracting",
    "home services",
    "maintenance",
    "repair",
    "repairs",
    "handy",
    "handyman",
    "installation",
    "install",
    "remodel",
    "construction",
    "cleaning",
    "painting",
    "plumbing",
    "hvac",
    "electric",
    "flooring",
)

MATERIAL_VENDOR_TERMS = (
    "sherwin",
    "lowe",
    "home depot",
    "chadwell",
    "supply",
    "supplies",
    "parts",
    "hardware",
    "paint store",
)

MATERIAL_TERMS = (
    "materials",
    "material",
    "supplies",
    "supply",
    "parts",
    "part",
    "paint gallons",
    "gallons",
    "gallon",
    "primer",
    "brushes",
    "brush",
    "rollers",
    "roller",
    "tape",
    "caulk",
    "trays",
    "tray",
    "sprayers",
    "sprayer",
    "filters",
    "filter",
    "bulbs",
    "bulb",
    "breaker",
    "breakers",
    "fittings",
    "fitting",
    "valves",
    "valve",
    "locks",
    "lock",
    "hardware",
    "chemicals",
    "chemical",
)

SERVICE_TERMS = (
    "labor",
    "service",
    "services",
    "service call",
    "work",
    "work order",
    "repair",
    "repaired",
    "maintenance",
    "install",
    "installed",
    "installation",
    "remove",
    "removed",
    "replace",
    "replaced",
    "paint",
    "painting",
    "painted",
    "clean",
    "cleaning",
    "make ready",
    "turn",
    "unit turn",
    "refinish",
    "refinishing",
    "resurface",
    "resurfacing",
    "reglaze",
    "reglazing",
)

UNIT_TURN_TERMS = (
    "unit turn",
    "turn",
    "turnover",
    "make ready",
    "make-ready",
    "make ready",
    "move out",
    "move-out",
    "vacant",
    "remodel",
    "renovation",
    "renovate",
    "full unit",
)

TRADE_TERMS: dict[str, tuple[str, ...]] = {
    "painting": ("paint", "painting", "painted", "repaint", "paint labor", "paint unit", "paint apartment"),
    "cleaning": ("cleaning services", "cleaning service", "cleaning", "clean", "move out clean", "move-out clean", "janitorial"),
    "plumbing": ("plumb", "leak", "leaks", "visible leak", "visible leaks", "toilet", "sewer", "drain", "pipe", "faucet", "water heater", "no visible leak", "no visible leaks"),
    "electrical": ("electrical", "electrician", "outlet", "breaker", "wiring", "panel", "gfci", "power surge"),
    "hvac": ("hvac", "air conditioner", "a/c", "ac ", "heat pump", "furnace", "thermostat", "refrigerant", "condenser"),
    "appliance": ("appliance", "refrigerator", "fridge", "dishwasher", "stove", "range", "washer", "dryer", "microwave"),
    "flooring": ("flooring", "floor covering", "carpet", "vinyl", "plank", "tile", "subfloor"),
    "cabinets": ("cabinet", "cabinets", "countertop", "counter top", "countertops"),
    "landscaping": ("landscape", "landscaping", "lawn", "mow", "tree", "mulch", "grounds"),
    "pest": ("pest", "exterminat", "termite", "roach", "bed bug"),
    "legal": ("legal", "attorney", "law office", "court", "eviction"),
    "utility": ("electric bill", "water bill", "sewer bill", "gas bill", "utility"),
    "general_maintenance": ("maintenance", "repair", "repairs", "handyman", "misc services", "work order"),
    "remodel": ("remodel", "renovation", "renovate", "make ready", "unit turn"),
}

ASSET_TERMS: dict[str, tuple[str, ...]] = {
    "bathtub": ("bathtub", "bath tub", "tub", "shower tub"),
    "cabinet": ("cabinet", "cabinets"),
    "countertop": ("countertop", "counter top", "counters"),
    "appliance": ("appliance", "refrigerator", "fridge", "dishwasher", "stove", "washer", "dryer"),
    "flooring": ("flooring", "floor", "carpet", "vinyl", "tile", "plank"),
    "door": ("door",),
    "lock": ("lock", "lockset"),
    "toilet": ("toilet",),
    "water_heater": ("water heater", "wtr htr"),
}

UNRELATED_FAMILIES = {
    "electric",
    "water",
    "sewer",
    "trash",
    "admin",
    "office",
    "insurance",
    "marketing",
    "legal",
}


def classify_line_item_semantics(
    line_item: dict[str, Any],
    vendor_profile: dict[str, Any] | None = None,
    invoice_context: dict[str, Any] | None = None,
    gl_catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify the economic substance of a contractor/service invoice line."""
    del gl_catalog
    vendor_profile = vendor_profile or {}
    invoice_context = invoice_context or {}
    activity = _clean(
        line_item.get("activity")
        or line_item.get("line_activity")
        or line_item.get("service_type")
        or line_item.get("category")
    )
    description = _clean(
        line_item.get("source_line_description")
        or line_item.get("raw_description")
        or line_item.get("description")
    )
    if activity and description and _norm(activity) not in _norm(description):
        line_text = f"{activity} {description}"
    else:
        line_text = description or activity
    norm_line = _norm(line_text)
    invoice_text = _clean(invoice_context.get("invoice_text"))
    norm_invoice = _norm(invoice_text)

    material_indicators = _matched_terms(norm_line, MATERIAL_TERMS)
    service_indicators = _matched_terms(norm_line, SERVICE_TERMS)
    trade_family, trade_hits = _classify_trade(norm_line, norm_invoice)
    specific_assets = _matched_assets(norm_line)
    if "bathtub" in specific_assets and _line_has_any(norm_line, ("paint", "painting", "refinish", "resurface", "reglaze")):
        trade_family = "tub_refinishing"
        if "painting" not in trade_hits and _line_has_any(norm_line, ("paint", "painting")):
            trade_hits.append("painting")

    work_mode = _classify_work_mode(
        norm_line,
        material_indicators=material_indicators,
        service_indicators=service_indicators,
        vendor_profile=vendor_profile,
    )
    unit_match = extract_line_location(line_text)
    line_unit = unit_match.get("location") or ""
    unit_turn_context = _unit_turn_context(
        norm_line=norm_line,
        norm_invoice=norm_invoice,
        trade_family=trade_family,
        line_unit=line_unit,
        invoice_context=invoice_context,
        vendor_profile=vendor_profile,
    )
    capital_or_repair = _capital_or_repair_context(norm_line, trade_family, unit_turn_context)
    confidence_basis = []
    if trade_hits:
        confidence_basis.append("keyword")
    if vendor_profile.get("vendor_type") and vendor_profile.get("vendor_type") != "unknown":
        confidence_basis.append("vendor profile")
    if description:
        confidence_basis.append("line description")
    if unit_turn_context:
        confidence_basis.append("invoice context")
    if vendor_profile.get("historical_codes"):
        confidence_basis.append("historical mapping")

    return {
        "trade_family": trade_family,
        "work_mode": work_mode,
        "unit_turn_context": unit_turn_context,
        "capital_or_repair_context": capital_or_repair,
        "specific_asset": specific_assets[0] if specific_assets else "unknown",
        "specific_assets": specific_assets,
        "location_detected": line_unit,
        "unit_location": line_unit,
        "location_evidence": unit_match.get("text") or "",
        "material_indicators": material_indicators,
        "service_indicators": service_indicators,
        "trade_indicators": trade_hits,
        "is_material": work_mode == "materials",
        "is_labor_or_service": work_mode in {"labor_service", "recurring_service"},
        "property_context": _clean(invoice_context.get("property_abbreviation")),
        "confidence_basis": confidence_basis,
    }


def build_gl_accounting_reasoning(
    normalized: dict[str, Any],
    line_item: dict[str, Any],
    category: str,
) -> dict[str, Any] | None:
    """Return selected GL plus explainable accounting reasoning for a line."""
    invoice_context = _invoice_context(normalized)
    vendor_profile = _vendor_profile(str(normalized.get("vendor_name") or normalized.get("raw_vendor_name") or ""))
    semantics = classify_line_item_semantics(line_item, vendor_profile, invoice_context)
    line_item["line_semantics"] = semantics

    if semantics.get("location_detected"):
        location = _validated_line_location(
            property_abbreviation=str(normalized.get("property_abbreviation") or ""),
            location=str(semantics.get("location_detected") or ""),
        )
        if location:
            line_item["location"] = location
            semantics["location"] = location
            semantics["location_validated"] = True
        else:
            semantics["location_validated"] = False

    if not _should_reason_about_line(category, normalized, semantics):
        return None
    # Phase 2: this legacy reasoner is now a candidate adapter. Selection is
    # delegated to AccountingDecisionEngine; callers keep the old response
    # fields temporarily while migrating to the typed AccountingDecision.
    from .accounting_contracts import DocumentFacts, GLCandidate, LineItemFacts, model_dict
    from .accounting_decision_engine import AccountingDecisionEngine
    from .gl_catalog import load_gl_catalog
    from .semantic_classifier import classify_line

    raw_activity = _clean(line_item.get("activity") or line_item.get("line_activity"))
    raw_description = _clean(line_item.get("source_line_description") or line_item.get("raw_description") or line_item.get("description"))
    line_id = str(line_item.get("line_item_id") or line_item.get("line_item_number") or "line-1")
    facts_line = LineItemFacts(line_item_id=line_id, raw_activity=raw_activity or None,
        raw_description=raw_description or None, normalized_activity=raw_activity or None,
        normalized_description=raw_description or None, amount=_decimal_or_none(line_item.get("amount")))
    facts = DocumentFacts(document_id=str(normalized.get("_source_file") or "legacy-service-invoice"),
        invoice_id=str(normalized.get("invoice_number") or "legacy-invoice"), line_items=[facts_line],
        extraction_route="service_reasoner_adapter")
    typed_semantics = classify_line(facts_line, document_id=facts.document_id,
        document_family="invoice", document_context=str(normalized.get("invoice_description") or ""),
        vendor_profile=vendor_profile)
    _, catalog = load_gl_catalog()
    raw_codes = _dedupe([line_item.get("source_gl_candidate"), line_item.get("gl_account_candidate")])
    preferred_codes = _dedupe([
        SUPPLIES_BY_FAMILY.get(str(semantics.get("trade_family") or "")),
        "6770" if str(semantics.get("trade_family")) == "tub_refinishing" else "",
        *_preferred_gl_codes(semantics, vendor_profile),
    ])
    candidates = [GLCandidate(gl_code=code, gl_name=catalog[code].gl_name,
        source="deterministic_parser", source_id="legacy_validated_candidate", base_score=1.0,
        rule_version="service-reasoner-adapter/1.0") for code in raw_codes if code in catalog]
    candidates.extend(GLCandidate(gl_code=code, gl_name=catalog[code].gl_name,
        source="service_reasoner", source_id="preferred_gl_codes", base_score=0.85,
        rule_version="service-reasoner-adapter/1.0") for code in preferred_codes if code in catalog)
    decision = AccountingDecisionEngine().decide(facts, typed_semantics, catalog, candidates, {})
    if not decision.selected_gl_code:
        return None
    payload = model_dict(decision)
    rejected = []
    alternatives = []
    for candidate in decision.candidates_ranked:
        if candidate.gl_code == decision.selected_gl_code:
            continue
        incompatible = any(not result.get("passed") for result in candidate.compatibility_results)
        reason = (
            "Rejected because this line describes labor/service and no itemized materials or supplies are visible."
            if incompatible and typed_semantics.line_family == "labor_service"
            else "Rejected because this line itemizes materials/supplies rather than performed labor or service."
            if incompatible and typed_semantics.line_family == "materials"
            else "Reasonable alternative, but less precise for this line's accounting classification."
        )
        item = {"gl_code": candidate.gl_code, "gl_name": candidate.gl_name, "reason": reason, "incompatible": incompatible}
        rejected.append(item)
        if not incompatible:
            alternatives.append(item)
    for item in decision.rejected_alternatives:
        if item.get("gl_code") != decision.selected_gl_code:
            rejected.append(item)
    vague = _vague_maintenance(semantics)
    review_reason = "Line is vague maintenance text with no specific trade detail." if vague else decision.review_reason
    review_level = "required" if decision.review_blocking else "non_blocking" if decision.review_required or vague else "none"
    payload.update({
        "classification": semantics,
        "alternatives": alternatives[:4],
        "rejected_alternatives": rejected[:6],
        "review": {"required": review_level == "required", "level": review_level, "reason": review_reason},
        "reason": decision.why_selected,
        "confidence_label": "High" if decision.confidence >= .8 else "Medium" if decision.confidence >= .6 else "Low",
        "decision_type": "AccountingDecisionEngine",
    })
    return payload


def get_semantically_relevant_gl_alternatives(
    line_semantics: dict[str, Any],
    gl_catalog: list[dict[str, Any]] | None,
    selected_gl: str,
) -> list[dict[str, str]]:
    """Public helper for tests and future review endpoints."""
    del gl_catalog
    out = []
    for code in _preferred_gl_codes(line_semantics, {}):
        if code == selected_gl:
            continue
        account = _valid_account(code)
        if account and _is_semantically_relevant(code, line_semantics):
            out.append({"gl_code": account["gl_code"], "gl_name": account["gl_name"]})
    return out


def extract_line_location(text: str) -> dict[str, str]:
    source = _clean(text)
    if not source:
        return {"location": "", "text": ""}
    patterns = (
        r"\b(?:unit|apt|apartment|suite|ste)\s*#?\s*([A-Za-z]?-?\d{1,5}[A-Za-z]?)\b",
        r"\b#\s*([A-Za-z]?-?\d{1,5}[A-Za-z]?)\b",
        r"^\s*([A-Za-z]-?\d{1,5}[A-Za-z]?)\b(?=\s+(?:clean|cleaning|paint|painting|repair|maintenance|turn)\b)",
        r"\b(\d{1,5}[A-Za-z])\b(?=\s+(?:clean|cleaning|paint|painting|repair|maintenance|turn)\b)",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if not match:
            continue
        location = _normalize_location(match.group(1))
        if location:
            return {"location": location, "text": match.group(0).strip()}
    return {"location": "", "text": ""}


def _should_reason_about_line(category: str, normalized: dict[str, Any], semantics: dict[str, Any]) -> bool:
    if category in {"utilities", "trash_collection_services", "marketing", "subscriptions"}:
        return False
    if str(normalized.get("invoice_nature") or "") == "one_time":
        return True
    return bool(
        semantics.get("trade_family") not in {"unknown", "utility", "legal"}
        or semantics.get("work_mode") in {"materials", "labor_service"}
    )


def _preferred_gl_codes(semantics: dict[str, Any], vendor_profile: dict[str, Any]) -> list[str]:
    family = str(semantics.get("trade_family") or "unknown")
    mode = str(semantics.get("work_mode") or "unknown")
    assets = set(semantics.get("specific_assets") or [])
    unit_turn = bool(semantics.get("unit_turn_context"))
    supports_unit_turn = bool(vendor_profile.get("supports_unit_turn"))
    if mode == "materials":
        if family in SUPPLIES_BY_FAMILY:
            return _dedupe([SUPPLIES_BY_FAMILY[family], "6790" if unit_turn else "", "6669"])
        if family == "tub_refinishing":
            return _dedupe(["6770", "6669"])
        return _dedupe(["6669"])

    if family == "tub_refinishing":
        return _dedupe(["6570", "6760", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family == "painting":
        return _dedupe(["6760", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family == "cleaning":
        return _dedupe(["6740" if "flooring" in assets or _has_asset_text(semantics, "carpet") else "", "6750", "6775" if unit_turn else "", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family == "plumbing":
        return _dedupe(["6565", "6530", "6615", "6500"])
    if family == "electrical":
        return _dedupe(["6540", "6530", "6615", "6500"])
    if family == "hvac":
        return _dedupe(["6555", "6530", "6615", "6500"])
    if family == "appliance":
        return _dedupe(["6505", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family in {"cabinets", "countertop"}:
        return _dedupe(["6512" if "countertop" in assets else "", "7510", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family == "flooring":
        return _dedupe(["7536", "7534", "6720", "7595" if unit_turn or supports_unit_turn else "", "6500"])
    if family == "landscaping":
        return _dedupe(["6810", "6800", "6500"])
    if family == "pest":
        return _dedupe(["6560", "6500"])
    if family == "trash_removal":
        return _dedupe(["6785" if unit_turn else "", "6515", "6500"])
    if family == "remodel":
        return _dedupe(["7595", "6775", "6500"])
    if family == "general_maintenance":
        return _dedupe(["6530", "6615", "6775" if unit_turn else "", "7595" if unit_turn and supports_unit_turn else "", "6500"])
    return _dedupe(["6530", "6615", "6500"] if mode == "labor_service" else [])


def _first_valid_compatible(codes: list[str], semantics: dict[str, Any]) -> dict[str, str] | None:
    for code in codes:
        account = _valid_account(code)
        if account and _gl_compatible(code, semantics):
            return account
    return None


def _valid_account(code: str) -> dict[str, str] | None:
    account = ai_mapping_review.validate_gl_account(code)
    if account and ai_mapping_review.is_payable_gl_account(account):
        return account
    return None


def _gl_compatible(code: str, semantics: dict[str, Any]) -> bool:
    meta = GL_META.get(code, {})
    gl_mode = meta.get("mode")
    family = str(semantics.get("trade_family") or "unknown")
    work_mode = str(semantics.get("work_mode") or "unknown")
    if gl_mode == "materials" and work_mode == "labor_service":
        return False
    if gl_mode == "labor_service" and work_mode == "materials":
        return False
    return _is_semantically_relevant(code, semantics)


def _is_semantically_relevant(code: str, semantics: dict[str, Any]) -> bool:
    meta = GL_META.get(code, {})
    code_family = meta.get("family", "")
    family = str(semantics.get("trade_family") or "unknown")
    if not code_family:
        name = _norm((ai_mapping_review.validate_gl_account(code) or {}).get("gl_name", ""))
        return not any(term in name for term in UNRELATED_FAMILIES)
    if code_family in {"contract_services", "general_maintenance"}:
        return family not in {"utility", "legal"}
    if code_family in {"remodel", "unit_turn"}:
        return bool(semantics.get("unit_turn_context")) or family in {"remodel", "cabinets", "countertop", "flooring", "appliance", "painting", "cleaning", "general_maintenance", "tub_refinishing"}
    if family == "tub_refinishing":
        return code_family in {"tub_refinishing", "painting", "remodel", "unit_turn", "contract_services"}
    if family == "countertop":
        return code_family in {"countertop", "cabinets", "remodel", "unit_turn", "contract_services"}
    return code_family == family


def _rejected_alternatives(
    *,
    selected_code: str,
    preferred_codes: list[str],
    semantics: dict[str, Any],
    raw_code: str,
) -> list[dict[str, Any]]:
    codes = _dedupe([
        raw_code,
        SUPPLIES_BY_FAMILY.get(str(semantics.get("trade_family") or "")),
        "6770" if str(semantics.get("trade_family")) == "tub_refinishing" else "",
        *preferred_codes,
        "6500",
    ])
    out: list[dict[str, Any]] = []
    for code in codes:
        account = _valid_account(code)
        if not account or account["gl_code"] == selected_code:
            continue
        incompatible = not _gl_compatible(account["gl_code"], semantics)
        out.append({
            "gl_code": account["gl_code"],
            "gl_name": account["gl_name"],
            "reason": _reject_reason(account["gl_code"], selected_code, semantics, incompatible),
            "incompatible": incompatible,
        })
    return out


def _reject_reason(code: str, selected_code: str, semantics: dict[str, Any], incompatible: bool) -> str:
    del selected_code
    meta = GL_META.get(code, {})
    family = str(semantics.get("trade_family") or "unknown")
    if meta.get("mode") == "materials" and semantics.get("work_mode") == "labor_service":
        return "Rejected because this line describes labor/service and no itemized materials or supplies are visible."
    if meta.get("mode") == "labor_service" and semantics.get("work_mode") == "materials":
        return "Rejected because this line itemizes materials/supplies rather than performed labor or service."
    if code in {"6500", "6595"}:
        return "Possible fallback, but not selected because a more specific compatible GL is available."
    if code == "7595":
        return "Possible if client policy groups this unit-turn work under remodel, but the line has a more specific trade/service GL."
    if incompatible:
        return f"Rejected because its GL family does not match the line's {family} accounting classification."
    return "Reasonable alternative, but less precise for this line's accounting classification."


def _evidence(
    normalized: dict[str, Any],
    line_item: dict[str, Any],
    semantics: dict[str, Any],
    vendor_profile: dict[str, Any],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    activity = _clean(line_item.get("activity") or line_item.get("line_activity"))
    description = _clean(line_item.get("source_line_description") or line_item.get("description"))
    if activity:
        evidence.append({"source": "line_activity", "text": activity, "meaning": _trade_meaning(semantics)})
    if description:
        evidence.append({"source": "line_description", "text": description, "meaning": _description_meaning(semantics)})
    elif not activity:
        evidence.append({"source": "line_description", "text": _clean(line_item.get("description")), "meaning": _description_meaning(semantics)})
    if semantics.get("location_evidence"):
        evidence.append({"source": "line_description", "text": str(semantics.get("location_evidence")), "meaning": "visible unit/location for this line"})
    vendor_name = _clean(normalized.get("vendor_name") or normalized.get("raw_vendor_name"))
    if vendor_name:
        vendor_type = str(vendor_profile.get("vendor_type") or "unknown")
        meaning = "service contractor, not a material-only supplier" if vendor_type == "service_contractor" else "vendor profile used as supporting context"
        evidence.append({"source": "vendor_profile", "text": vendor_name, "meaning": meaning})
    if semantics.get("unit_turn_context"):
        context_text = _unit_turn_context_text(normalized)
        if context_text:
            evidence.append({"source": "invoice_context", "text": context_text, "meaning": "supports unit-turn or make-ready context"})
    return _dedupe_evidence(evidence)[:5]


def _confidence(
    *,
    selected_code: str,
    semantics: dict[str, Any],
    vendor_profile: dict[str, Any],
    normalized: dict[str, Any],
    line_item: dict[str, Any],
) -> tuple[float, str]:
    score = 0.50
    if semantics.get("trade_indicators"):
        score += 0.18
    if semantics.get("work_mode") in {"materials", "labor_service", "recurring_service"}:
        score += 0.10
    if _vendor_mode_matches(vendor_profile, semantics):
        score += 0.08
    if GL_META.get(selected_code, {}).get("specificity") == "specific":
        score += 0.08
    if semantics.get("location_detected") and semantics.get("location_validated"):
        score += 0.04
    if _total_reconciles(normalized):
        score += 0.04
    if selected_code in set(vendor_profile.get("historical_codes") or []):
        score += 0.06

    if _vague_maintenance(semantics):
        score -= 0.22
    if selected_code in {"6500", "6595"}:
        score -= 0.12
    if semantics.get("location_detected") and semantics.get("location_validated") is False:
        score -= 0.10
    if semantics.get("work_mode") == "unknown":
        score -= 0.10
    if line_item.get("source_gl_candidate") and not _gl_compatible(str(line_item.get("source_gl_candidate")), semantics):
        score -= 0.06

    if _vague_maintenance(semantics) and selected_code == "7595" and selected_code not in set(vendor_profile.get("historical_codes") or []):
        score = min(score, 0.62)
    score = round(max(0.20, min(0.96, score)), 2)
    label = "High" if score >= 0.85 else "Medium" if score >= 0.65 else "Low"
    return score, label


def _review_state(
    *,
    selected_code: str,
    semantics: dict[str, Any],
    confidence: float,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    required = False
    if GL_META.get(selected_code, {}).get("mode") == "materials" and semantics.get("work_mode") == "labor_service":
        required = True
        reasons.append("Labor/service line was mapped to a supplies GL.")
    if not _is_semantically_relevant(selected_code, semantics):
        required = True
        reasons.append("Selected GL family conflicts with line semantic family.")
    if semantics.get("location_detected") and semantics.get("location_validated") is False:
        reasons.append("Location is visible in the line text but was not validated against the property reference.")
    if _vague_maintenance(semantics):
        reasons.append("Line is vague maintenance text with no specific trade detail.")
    if selected_code == "7595" and _vague_maintenance(semantics):
        reasons.append("Vague maintenance line is mapped to remodel/unit-turn context.")
    if not _total_reconciles(normalized):
        reasons.append("Invoice total does not reconcile to line totals.")
    if confidence < 0.55:
        required = True
    if not reasons:
        return {"required": False, "level": "none", "reason": None}
    return {
        "required": required,
        "level": "required" if required else "non_blocking",
        "reason": "; ".join(_dedupe(reasons)),
    }


def _selection_reason(account: dict[str, str], semantics: dict[str, Any], vendor_profile: dict[str, Any]) -> str:
    name = account.get("gl_name") or GL_META.get(account.get("gl_code", ""), {}).get("name") or "Selected GL"
    family = str(semantics.get("trade_family") or "unknown").replace("_", " ")
    mode = str(semantics.get("work_mode") or "unknown").replace("_", "/")
    if semantics.get("work_mode") == "materials":
        return f"{name} was selected because the line itemizes {family} materials/supplies rather than performed service."
    if _vague_maintenance(semantics):
        return f"{name} was selected as the closest maintenance service GL, but the line itself is vague and should not receive high confidence."
    if vendor_profile.get("vendor_type") == "service_contractor":
        return f"{name} was selected because the line describes {family} {mode} from a service contractor, and no incompatible supplies purchase is itemized."
    return f"{name} was selected because the line describes {family} {mode} and this is the most specific compatible GL available."


def _invoice_context(normalized: dict[str, Any]) -> dict[str, Any]:
    line_texts = [
        _clean(
            " ".join(
                str(item.get(key) or "")
                for key in ("activity", "line_activity", "description", "source_line_description")
            )
        )
        for item in normalized.get("line_items") or []
        if isinstance(item, dict)
    ]
    invoice_text = " ".join(
        [
            _clean(normalized.get("invoice_description")),
            _clean(normalized.get("service_address")),
            " ".join(line_texts),
        ]
    )
    return {
        "invoice_text": invoice_text,
        "line_texts": line_texts,
        "property_abbreviation": _clean(normalized.get("property_abbreviation")),
        "line_trade_count": len({fam for fam in (_classify_trade(_norm(text), _norm(invoice_text))[0] for text in line_texts) if fam != "unknown"}),
        "line_unit_count": len([text for text in line_texts if extract_line_location(text).get("location")]),
    }


def _vendor_profile(vendor_name: str) -> dict[str, Any]:
    vendor_name = _clean(vendor_name)
    norm = _norm(vendor_name)
    vendor_type = "unknown"
    if any(term in norm for term in MATERIAL_VENDOR_TERMS):
        vendor_type = "material_supplier"
    if any(term in norm for term in SERVICE_VENDOR_TERMS):
        vendor_type = "service_contractor"
    yaml_profile = _load_vendor_yaml_profile(vendor_name)
    category = _norm(str(yaml_profile.get("category") or ""))
    default_gl = str(yaml_profile.get("default_gl_code") or "").strip()
    historical_codes = [str(code) for code in yaml_profile.get("historical_codes") or [] if code]
    if "construction" in category or "remodel" in category or "maintenance" in category:
        vendor_type = "service_contractor"
    supports_unit_turn = default_gl in {"7595", "6775"} or any(code in {"7595", "6775"} for code in historical_codes[:5])
    return {
        "vendor_name": vendor_name,
        "vendor_type": vendor_type,
        "default_gl_code": default_gl,
        "historical_codes": historical_codes,
        "supports_unit_turn": supports_unit_turn,
        "category": yaml_profile.get("category") or "",
    }


@lru_cache(maxsize=1024)
def _load_vendor_yaml_profile(vendor_name: str) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "_", ai_mapping_review.normalize_key(vendor_name)).strip("_")
    if not slug:
        return {}
    path = settings.RUNTIME_ASSET_ROOT / "config" / "vendors" / f"{slug}.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    mapping = data.get("accounting_mapping") or {}
    source = data.get("accounting_source") or {}
    hist = mapping.get("historical_gl_codes_observed") or source.get("source_gl_codes_observed") or []
    historical_codes = []
    for item in hist:
        if isinstance(item, dict) and item.get("gl_code"):
            historical_codes.append(str(item.get("gl_code")))
    identity = data.get("vendor_identity") or {}
    return {
        "category": identity.get("category") or "",
        "default_gl_code": mapping.get("default_gl_code") or source.get("most_common_gl_code") or "",
        "historical_codes": historical_codes,
    }


def _classify_trade(norm_line: str, norm_invoice: str) -> tuple[str, list[str]]:
    del norm_invoice
    assets = _matched_assets(norm_line)
    if "bathtub" in assets and _line_has_any(norm_line, ("refinish", "resurface", "reglaze", "painting", "paint tub", "tub painting")):
        return "tub_refinishing", _matched_terms(norm_line, ("bathtub", "tub", "refinish", "resurface", "reglaze", "painting"))
    if "bathtub" in assets and _line_has_any(norm_line, ("leak", "plumb", "drain", "faucet")):
        return "plumbing", _matched_terms(norm_line, TRADE_TERMS["plumbing"])
    if "countertop" in assets:
        return "countertop", _matched_terms(norm_line, ("countertop", "counter top", "counter", "refinish", "resurface"))
    for family in ("cabinets", "appliance", "plumbing", "electrical", "hvac", "flooring", "painting", "cleaning", "landscaping", "pest", "legal", "utility", "remodel", "general_maintenance"):
        hits = _matched_terms(norm_line, TRADE_TERMS[family])
        if hits:
            return family, hits
    return "unknown", []


def _classify_work_mode(
    norm_line: str,
    *,
    material_indicators: list[str],
    service_indicators: list[str],
    vendor_profile: dict[str, Any],
) -> str:
    if _line_has_any(norm_line, ("sales tax", "tax")) and not service_indicators:
        return "tax"
    if _line_has_any(norm_line, ("credit", "refund")):
        return "credit"
    if _line_has_any(norm_line, ("fee", "late charge", "service charge")) and not service_indicators:
        return "fee"
    if _line_has_any(norm_line, ("monthly", "recurring", "scheduled service", "contract period")):
        return "recurring_service"
    if _line_has_any(norm_line, ("leak", "leaks", "no visible leak", "no visible leaks", "diagnosis", "diagnostic")):
        return "labor_service"
    material_strength = len(material_indicators)
    service_strength = len(service_indicators)
    weak_service_only = set(service_indicators) <= {"paint", "clean"}
    if material_strength and weak_service_only:
        return "materials"
    if service_strength and not _only_material_context(norm_line, vendor_profile, material_strength):
        return "labor_service"
    if material_strength:
        return "materials"
    if vendor_profile.get("vendor_type") == "service_contractor" and _line_has_any(norm_line, ("maintenance", "repair", "work", "service")):
        return "labor_service"
    return "unknown"


def _only_material_context(norm_line: str, vendor_profile: dict[str, Any], material_strength: int) -> bool:
    if _line_has_any(norm_line, ("install", "installation", "labor", "service", "repair", "painting", "cleaning", "maintenance", "refinish", "resurface")):
        return False
    return material_strength > 0 and vendor_profile.get("vendor_type") == "material_supplier"


def _unit_turn_context(
    *,
    norm_line: str,
    norm_invoice: str,
    trade_family: str,
    line_unit: str,
    invoice_context: dict[str, Any],
    vendor_profile: dict[str, Any],
) -> bool:
    if _line_has_any(norm_line, UNIT_TURN_TERMS) or _line_has_any(norm_invoice, UNIT_TURN_TERMS):
        return True
    if vendor_profile.get("supports_unit_turn") and line_unit:
        return True
    if line_unit and int(invoice_context.get("line_trade_count") or 0) >= 2:
        return True
    if int(invoice_context.get("line_unit_count") or 0) >= 2 and trade_family in {"painting", "cleaning", "general_maintenance", "tub_refinishing"}:
        return True
    return False


def _capital_or_repair_context(norm_line: str, trade_family: str, unit_turn_context: bool) -> str:
    if _line_has_any(norm_line, ("capital", "full replacement", "complete replacement", "renovation", "remodel")):
        return "capital"
    if trade_family in {"cabinets", "countertop", "flooring", "appliance"} or unit_turn_context:
        return "ambiguous"
    if trade_family in {"painting", "cleaning", "plumbing", "electrical", "hvac", "general_maintenance", "tub_refinishing"}:
        return "operating"
    return "ambiguous"


def _validated_line_location(property_abbreviation: str, location: str) -> str:
    if not location:
        return ""
    if not property_abbreviation:
        return location
    try:
        prop = ai_mapping_review.validate_property_location(
            property_abbreviation=property_abbreviation,
            location=location,
        )
    except Exception:
        prop = None
    return str(prop.get("location") or location).strip() if prop else ""


def _vague_maintenance(semantics: dict[str, Any]) -> bool:
    family = semantics.get("trade_family")
    if family != "general_maintenance":
        return False
    indicators = set(semantics.get("trade_indicators") or [])
    services = set(semantics.get("service_indicators") or [])
    return indicators <= {"maintenance", "repair", "repairs", "work order"} and services <= {"maintenance", "service", "services", "work", "work order"}


def _vendor_mode_matches(vendor_profile: dict[str, Any], semantics: dict[str, Any]) -> bool:
    vendor_type = vendor_profile.get("vendor_type")
    if vendor_type == "service_contractor" and semantics.get("work_mode") in {"labor_service", "recurring_service"}:
        return True
    if vendor_type == "material_supplier" and semantics.get("work_mode") == "materials":
        return True
    return False


def _total_reconciles(normalized: dict[str, Any]) -> bool:
    summary = normalized.get("validation_summary") or {}
    if "total_reconciliation_passed" in summary:
        return bool(summary.get("total_reconciliation_passed"))
    total = _money(normalized.get("total_amount"))
    items_total = _money(sum(_money(item.get("amount")) for item in normalized.get("line_items") or [] if isinstance(item, dict)))
    return bool(total) and abs(total - items_total) <= 0.01


def _trade_meaning(semantics: dict[str, Any]) -> str:
    family = str(semantics.get("trade_family") or "unknown").replace("_", " ")
    mode = str(semantics.get("work_mode") or "unknown").replace("_", "/")
    return f"indicates {family} {mode}"


def _description_meaning(semantics: dict[str, Any]) -> str:
    if semantics.get("location_detected"):
        return "line-level work description with visible unit/location"
    return "line-level work description"


def _unit_turn_context_text(normalized: dict[str, Any]) -> str:
    bits = []
    for item in normalized.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        text = _clean(" ".join(str(item.get(k) or "") for k in ("activity", "description", "source_line_description")))
        if text:
            bits.append(text)
    return "; ".join(bits[:4])


def _has_asset_text(semantics: dict[str, Any], needle: str) -> bool:
    return needle in " ".join(semantics.get("specific_assets") or [])


def _matched_assets(norm_line: str) -> list[str]:
    assets = []
    for asset, terms in ASSET_TERMS.items():
        if _line_has_any(norm_line, terms):
            assets.append(asset)
    return assets


def _matched_terms(norm_text: str, terms: tuple[str, ...]) -> list[str]:
    out = []
    for term in terms:
        needle = _norm(term)
        if needle and _contains_term(norm_text, needle):
            out.append(term)
    return _dedupe(out)


def _line_has_any(norm_text: str, terms: tuple[str, ...]) -> bool:
    return any(_contains_term(norm_text, _norm(term)) for term in terms)


def _contains_term(norm_text: str, norm_term: str) -> bool:
    if not norm_text or not norm_term:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(norm_term)}(?![a-z0-9])", norm_text) is not None


def _normalize_location(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper()).lstrip("#")


def _dedupe(values: list[Any] | tuple[Any, ...]) -> list[Any]:
    out = []
    seen = set()
    for value in values:
        if value in ("", None):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _dedupe_evidence(values: list[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    seen = set()
    for value in values:
        key = (value.get("source"), value.get("text"), value.get("meaning"))
        if not value.get("text") or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    return ai_mapping_review.normalize_key(str(value or ""))


__all__ = [
    "build_gl_accounting_reasoning",
    "classify_line_item_semantics",
    "extract_line_location",
    "get_semantically_relevant_gl_alternatives",
]
