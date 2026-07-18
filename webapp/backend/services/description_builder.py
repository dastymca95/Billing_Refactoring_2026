"""Canonical description construction for ResMan output rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any

from utils.text_normalization import (
    normalize_service_address_for_description,
    normalize_source_line_description,
    proper_case_preserve_acronyms,
)

@dataclass(frozen=True)
class DescriptionResult:
    description: str
    components_used: dict[str, str] = field(default_factory=dict)
    fallback_used: bool = False
    review_flags: tuple[str, ...] = ()


PROPERTY_LEVEL_CATEGORIES = {
    "trash_collection_services",
    "pest_control",
    "landscaping",
    "marketing",
    "subscriptions",
}

SERVICE_ADDRESS_DESCRIPTION_CATEGORIES = {
    "utilities",
    "pest_control",
    "landscaping",
    "trash_collection_services",
}

SUMMARY_DESCRIPTION_CATEGORIES = {
    "marketing",
    "subscriptions",
    "other_infrequent",
    "unknown",
}

ALWAYS_MONTHLY_DESCRIPTION_CATEGORIES = {
    "pest_control",
    "landscaping",
}

ONE_OFF_DESCRIPTION_CATEGORIES = {
    "other_infrequent",
    "unknown",
}

_GENERIC_CONTENT_WORDS = {
    "a", "an", "and", "at", "for", "from", "in", "inc", "invoice",
    "item", "llc", "of", "on", "or", "the", "to", "total", "with",
}

_GENERIC_SUMMARY_WORDS = {
    "door", "equipment", "goods", "hardware", "invoice", "item", "items",
    "maintenance", "material", "materials", "miscellaneous", "part", "parts",
    "product", "products", "purchase", "repair", "service", "services",
    "supplies", "supply", "work",
}

_GENERIC_LINE_LABEL_RE = re.compile(
    r"^(?:labor|labour|parts?(?:\s*#?\s*\d+)?|materials?(?:\s*#?\s*\d+)?|"
    r"items?(?:\s*#?\s*\d+)?|service|misc(?:ellaneous)?|sh|shipping(?:\s+and\s+handling)?|"
    r"shipping(?:\s+and|\s+&)\s+handeling|shipping\s+&\s+handling)$",
    re.I,
)
_OPAQUE_SERVICE_CODE_RE = re.compile(
    r"\b[A-Z]{1,5}[-_][A-Z]{1,5}(?:[-_][A-Z0-9]{1,8})+\b",
    re.I,
)
_SERVICE_FEE_TEXT_RE = re.compile(
    r"\b(?:service\s+fee|service\s+call|trip\s+charge)\b",
    re.I,
)


def build_invoice_description(
    normalized_invoice: dict[str, Any],
    canonical_rules: dict[str, Any] | None = None,
) -> DescriptionResult:
    """Build the invoice-level ResMan description from validated evidence."""

    category = _category(normalized_invoice)
    if _uses_monthly_description(normalized_invoice, category):
        return _monthly_invoice_description(normalized_invoice)
    if not _uses_service_address_description(category, normalized_invoice, canonical_rules):
        return _summary_invoice_description(normalized_invoice)

    target = _description_target(normalized_invoice, canonical_rules)
    period = _period(normalized_invoice)
    pieces = [part for part in (period, target.description) if part]
    description = " - ".join(pieces)
    flags = list(target.review_flags)
    if not description:
        flags.append("invoice_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            **target.components_used,
            "service_period": period,
        },
        fallback_used=target.fallback_used,
        review_flags=tuple(dict.fromkeys(flags)),
    )


def build_line_item_description(
    normalized_invoice: dict[str, Any],
    line_item: dict[str, Any],
    canonical_rules: dict[str, Any] | None = None,
) -> DescriptionResult:
    """Build the line-item ResMan description from invoice context + source line."""

    category = _category(normalized_invoice)
    if _uses_monthly_description(normalized_invoice, category):
        return _monthly_line_item_description(normalized_invoice, line_item)
    if not _uses_service_address_description(category, normalized_invoice, canonical_rules):
        return _summary_line_item_description(normalized_invoice, line_item, category)

    base = build_invoice_description(normalized_invoice, canonical_rules)
    source = normalize_source_line_description(
        line_item.get("source_line_description")
        or line_item.get("description")
        or line_item.get("line_item_description")
        or "",
    )
    pieces = [part for part in (base.description, source) if part]
    description = " - ".join(pieces)
    flags = list(base.review_flags)
    if not source:
        flags.append("line_item_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            **base.components_used,
            "source_line_description": source,
        },
        fallback_used=base.fallback_used,
        review_flags=tuple(dict.fromkeys(flags)),
    )


def normalize_service_address_for_row(value: Any) -> str:
    return normalize_service_address_for_description(value)


def proper_case_preserve_acronyms_for_row(value: Any) -> str:
    return proper_case_preserve_acronyms(value)


def _description_target(
    normalized_invoice: dict[str, Any],
    canonical_rules: dict[str, Any] | None,
) -> DescriptionResult:
    category = _category(normalized_invoice)
    service_address = normalize_service_address_for_description(
        normalized_invoice.get("service_address")
        or normalized_invoice.get("service_address_for_description")
        or "",
    )
    unit = proper_case_preserve_acronyms(
        normalized_invoice.get("unit_number")
        or normalized_invoice.get("location")
        or "",
    )
    if service_address:
        target = service_address
        if unit and not _address_already_includes_unit(service_address, unit):
            target = f"{unit} {service_address}".strip()
        return DescriptionResult(
            description=target,
            components_used={
                "target_source": "service_address",
                "service_address": service_address,
                "unit_number": unit,
            },
            fallback_used=False,
            review_flags=(),
        )

    property_name = proper_case_preserve_acronyms(
        normalized_invoice.get("property_name")
        or normalized_invoice.get("property_candidate")
        or normalized_invoice.get("property_abbreviation")
        or "",
    )
    allow_property_fallback = bool(normalized_invoice.get("property_level_service"))
    allow_property_fallback = allow_property_fallback or category in PROPERTY_LEVEL_CATEGORIES
    if _rules_allow_property_fallback(canonical_rules, category):
        allow_property_fallback = True
    if property_name and allow_property_fallback:
        return DescriptionResult(
            description=property_name,
            components_used={"target_source": "property_name", "property_name": property_name},
            fallback_used=True,
            review_flags=(),
        )
    if property_name:
        return DescriptionResult(
            description=property_name,
            components_used={"target_source": "property_name", "property_name": property_name},
            fallback_used=True,
            review_flags=("service_address_missing_or_unresolved",),
        )
    return DescriptionResult(
        description="",
        components_used={"target_source": "missing"},
        fallback_used=True,
        review_flags=("service_address_missing_or_unresolved",),
    )


def _address_already_includes_unit(service_address: str, unit: str) -> bool:
    address = f" {str(service_address or '').upper()} "
    unit_text = str(unit or "").strip().upper()
    if not unit_text:
        return False
    street_number = re.match(r"^\s*(\d{1,6})\b", str(service_address or ""))
    if street_number and street_number.group(1) == unit_text:
        return True
    composite = re.match(r"^(\d+)-([A-Z0-9]+)$", unit_text)
    if composite:
        street_number, unit_suffix = composite.groups()
        if re.search(rf"\b{re.escape(street_number)}\b", address) and re.search(
            rf"\b{re.escape(unit_suffix)}\b", address,
        ):
            return True
    building_unit = re.fullmatch(r"([A-Z])-(\d{3,4})", unit_text)
    if building_unit and re.search(
        rf"\b{re.escape(building_unit.group(1))}\s*-?\s*{re.escape(building_unit.group(2))}\b",
        address,
    ):
        return True
    compact = re.match(r"^(\d{2,6})([A-Z][A-Z0-9-]*)$", unit_text)
    if compact:
        street_number, unit_suffix = compact.groups()
        if re.search(rf"\b{re.escape(street_number)}\b", address) and re.search(
            rf"\b(?:APT|APARTMENT|UNIT|STE|SUITE|#)?\s*{re.escape(unit_suffix)}\b",
            address,
        ):
            return True
    escaped = re.escape(unit_text)
    patterns = (
        rf"\b(?:APT|APARTMENT|UNIT|STE|SUITE|#)\s*{escaped}\b",
        rf"\b{escaped}\s+(?:APT|APARTMENT|UNIT|STE|SUITE)\b",
    )
    return any(re.search(pattern, address) for pattern in patterns)


def _summary_invoice_description(normalized_invoice: dict[str, Any]) -> DescriptionResult:
    category = _category(normalized_invoice)
    if category in ONE_OFF_DESCRIPTION_CATEGORIES and not _uses_monthly_description(
        normalized_invoice,
        category,
    ):
        summary = build_one_off_content_summary(normalized_invoice)
        return DescriptionResult(
            description=summary,
            components_used={
                "description_style": "one_off_content_summary",
                "content_summary": summary,
            },
            fallback_used=False,
            review_flags=() if summary else ("invoice_description_missing",),
        )
    date_text = _short_date(normalized_invoice.get("invoice_date"))
    vendor = proper_case_preserve_acronyms(
        normalized_invoice.get("vendor_name")
        or normalized_invoice.get("vendor")
        or normalized_invoice.get("raw_vendor_name")
        or "",
    )
    item = _main_item_description(normalized_invoice)
    pieces = [part for part in (date_text, vendor, item) if part]
    description = " - ".join(pieces) or proper_case_preserve_acronyms(
        normalized_invoice.get("invoice_description") or item or "Invoice"
    )
    flags: list[str] = []
    if not description:
        flags.append("invoice_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            "description_style": "summary",
            "invoice_date": date_text,
            "vendor": vendor,
            "main_item": item,
        },
        fallback_used=False,
        review_flags=tuple(flags),
    )


def _monthly_invoice_description(normalized_invoice: dict[str, Any]) -> DescriptionResult:
    period = _month_period(normalized_invoice)
    item = _strip_trailing_source_date(_main_item_description(normalized_invoice))
    description = " - ".join(part for part in (period, item) if part) or proper_case_preserve_acronyms(
        normalized_invoice.get("invoice_description") or item or "Invoice"
    )
    flags: list[str] = []
    if not period:
        flags.append("service_period_missing")
    if not item:
        flags.append("invoice_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            "description_style": "monthly_service",
            "service_period": period,
            "main_item": item,
        },
        fallback_used=False,
        review_flags=tuple(flags),
    )


def _monthly_line_item_description(
    normalized_invoice: dict[str, Any],
    line_item: dict[str, Any],
) -> DescriptionResult:
    period = _month_period(normalized_invoice)
    source = normalize_source_line_description(
        line_item.get("source_line_description")
        or line_item.get("description")
        or line_item.get("line_item_description")
        or "",
    )
    source = _strip_trailing_source_date(source)
    description = " - ".join(part for part in (period, source) if part)
    flags: list[str] = []
    if not period:
        flags.append("service_period_missing")
    if not source:
        flags.append("line_item_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            "description_style": "monthly_service",
            "service_period": period,
            "source_line_description": source,
        },
        fallback_used=False,
        review_flags=tuple(flags),
    )


def _summary_line_item_description(
    normalized_invoice: dict[str, Any],
    line_item: dict[str, Any],
    category: str,
) -> DescriptionResult:
    source = build_contextual_one_off_line_description(normalized_invoice, line_item)
    vendor = proper_case_preserve_acronyms(
        normalized_invoice.get("vendor_name")
        or normalized_invoice.get("vendor")
        or normalized_invoice.get("raw_vendor_name")
        or "",
    )
    if category == "subscriptions" and vendor and source:
        description = f"{vendor} - {source}"
    elif category in {"marketing", "unknown"} and vendor and source:
        description = f"{vendor} - {source}"
    else:
        description = source
    flags: list[str] = []
    if not source:
        flags.append("line_item_description_missing")
    return DescriptionResult(
        description=proper_case_preserve_acronyms(description),
        components_used={
            "description_style": "summary",
            "vendor": vendor,
            "source_line_description": source,
        },
        fallback_used=False,
        review_flags=tuple(flags),
    )


def _main_item_description(normalized_invoice: dict[str, Any]) -> str:
    for item in normalized_invoice.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        amount = item.get("amount")
        try:
            if amount is not None and abs(float(amount)) <= 0.0001:
                continue
        except (TypeError, ValueError):
            pass
        desc = normalize_source_line_description(
            item.get("source_line_description")
            or item.get("description")
            or item.get("line_item_description")
            or "",
        )
        if desc:
            return _concise(desc)
    return _concise(str(normalized_invoice.get("invoice_description") or ""))


def build_one_off_content_summary(normalized_invoice: dict[str, Any]) -> str:
    """Summarize what a one-off invoice contains, never where it was sent."""
    source_summary = normalize_source_line_description(
        normalized_invoice.get("invoice_description") or "",
    )
    item_descriptions = [
        normalize_source_line_description(
            item.get("source_line_description")
            or item.get("description")
            or item.get("line_item_description")
            or "",
        )
        for item in normalized_invoice.get("line_items") or []
        if isinstance(item, dict) and abs(_numeric_amount(item.get("amount"))) > 0.0001
    ]
    item_descriptions = [description for description in item_descriptions if description]
    service_note_summary = _service_note_content_summary(
        normalized_invoice,
        item_descriptions,
    )
    if service_note_summary:
        return _fit_content_summary(service_note_summary)

    source_is_valid = bool(source_summary) and _summary_reflects_invoice_content(
        source_summary,
        item_descriptions,
        normalized_invoice,
    )
    document_summary = _document_work_summary(normalized_invoice)
    if source_is_valid and not _summary_is_generic(source_summary):
        if document_summary and _document_summary_is_more_specific(
            document_summary,
            source_summary,
        ):
            return _fit_content_summary(document_summary)
        return _fit_content_summary(source_summary)

    if document_summary:
        return _fit_content_summary(document_summary)

    meaningful_items = [
        description for description in item_descriptions
        if not _is_generic_line_label(description)
    ]
    semantic = _semantic_product_summary(meaningful_items)
    if semantic:
        return _fit_content_summary(semantic)
    if source_is_valid:
        return _fit_content_summary(source_summary)

    distinct: list[str] = []
    for description in meaningful_items:
        concise = _concise(description)
        if concise and concise.lower() not in {item.lower() for item in distinct}:
            distinct.append(concise)
        if len(distinct) >= 2:
            break
    return _fit_content_summary("; ".join(distinct)) or "Work Description Requires Review"


def build_contextual_one_off_line_description(
    normalized_invoice: dict[str, Any],
    line_item: dict[str, Any],
) -> str:
    """Enrich accounting-only line labels with the work actually purchased."""
    source = normalize_source_line_description(
        line_item.get("source_line_description")
        or line_item.get("description")
        or line_item.get("line_item_description")
        or "",
    )
    # A standalone fee/call/trip line is still line-level source evidence.
    # Replacing it with an invoice-wide summary collapses two semantic layers
    # and can make a second line appear to describe the first line's work.
    if _is_opaque_service_fee_label(source):
        return source
    if not source or not _is_generic_line_label(source):
        return source
    summary = build_one_off_content_summary(normalized_invoice)
    if not summary or summary == "Work Description Requires Review":
        return source
    key = _semantic_key(source)
    if key in {"labor", "labour"}:
        return f"Labor - {summary}"
    if key in {
        "sh", "shipping", "shipping and handling", "shipping handling",
        "shipping and handeling", "shipping handeling",
    }:
        return f"Shipping & Handling - {summary}"
    return f"{summary} - {proper_case_preserve_acronyms(source)}"


def polish_accounting_description_pair(
    invoice_description: Any,
    line_item_description: Any,
    *,
    gl_account: Any = "",
    vendor_name: Any = "",
    document_text: Any = "",
) -> tuple[str, str]:
    """Convert raw service-note text into concise ResMan-facing descriptions.

    This is intentionally conservative. It only rewrites descriptions when the
    invoice contains human-readable work evidence and the current output is
    clearly a raw note or an opaque service-fee/code label.
    """
    invoice_text = normalize_source_line_description(invoice_description or "")
    line_text = normalize_source_line_description(line_item_description or "")
    normalized = {
        "category": "other_infrequent",
        "vendor_name": vendor_name or "",
        "invoice_description": invoice_text,
        "_document_text": document_text or "",
        "line_items": [
            {
                "description": line_text,
                "amount": 1,
                "gl_account_candidate": str(gl_account or ""),
            }
        ],
    }
    summary = _service_note_content_summary(normalized, [line_text])
    if not summary:
        return invoice_text, line_text

    new_invoice = invoice_text
    if _is_raw_service_note(invoice_text) or _is_opaque_service_fee_label(invoice_text):
        new_invoice = summary

    new_line = line_text
    if _is_raw_service_note(line_text) or _is_opaque_service_fee_label(line_text):
        new_line = summary
    return new_invoice, new_line


def _summary_reflects_invoice_content(
    summary: str,
    item_descriptions: list[str],
    normalized_invoice: dict[str, Any],
) -> bool:
    summary_key = _semantic_key(summary)
    if not summary_key:
        return False
    context_values = (
        normalized_invoice.get("service_address"),
        normalized_invoice.get("property_candidate"),
        normalized_invoice.get("property_name"),
        normalized_invoice.get("property_abbreviation"),
        normalized_invoice.get("vendor_name"),
        normalized_invoice.get("raw_vendor_name"),
    )
    context_tokens = {
        token
        for value in context_values
        for token in _semantic_tokens(str(value or ""))
    }
    item_tokens = {
        token
        for description in item_descriptions
        for token in _semantic_tokens(description)
    }
    document_tokens = set(
        _semantic_tokens(str(normalized_invoice.get("_document_text") or ""))
    )
    summary_tokens = set(_semantic_tokens(summary))
    content_overlap = summary_tokens & (item_tokens | document_tokens)
    context_overlap = summary_tokens & context_tokens
    looks_like_address = bool(
        re.search(r"\b\d{1,6}\b", summary_key)
        and re.search(
            r"\b(?:st|street|rd|road|dr|drive|ave|avenue|blvd|boulevard|ln|lane|ct|court)\b",
            summary_key,
        )
    )
    if looks_like_address:
        return False
    if context_overlap and not content_overlap:
        return False
    return bool(content_overlap)


def _document_work_summary(normalized_invoice: dict[str, Any]) -> str:
    text = str(normalized_invoice.get("_document_text") or "")
    if not text.strip():
        return ""
    normalized = text.replace("\u00ad", "-")
    problem = re.search(
        r"(?im)^\s*([A-Z][A-Z0-9 /&-]{1,30}?)\s+FOUND\s+(?:A\s+)?BAD\s+"
        r"([A-Z0-9 /&-]{3,90}?)[.]?\s*$",
        normalized,
    )
    if problem:
        asset = proper_case_preserve_acronyms(problem.group(1).strip())
        components = proper_case_preserve_acronyms(problem.group(2).strip())
        components = re.sub(r"\s+And\s+", " & ", components, flags=re.I)
        return f"{asset} {components} Replacement"

    section = re.search(
        r"(?is)\b(?:services? performed|work performed|description of work|scope of work|"
        r"technician notes?|service notes?)\s*:?(.*?)(?:\bterms(?: and conditions)?\b|\bsubtotal\b|$)",
        normalized,
    )
    if not section:
        return ""
    candidates: list[str] = []
    for raw_line in section.group(1).splitlines():
        line = re.sub(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*:\s*", "", raw_line).strip(" -")
        if not line or re.fullmatch(r"[\d$.,=\s]+", line):
            continue
        key = _semantic_key(line)
        if any(marker in key for marker in ("order parts", "checked okay", "tax =", "additional labor")):
            continue
        if len(_semantic_tokens(line)) >= 3:
            candidates.append(line)
        if len(candidates) >= 2:
            break
    return "; ".join(candidates)


def _document_summary_is_more_specific(document_summary: str, source_summary: str) -> bool:
    source_tokens = {
        token for token in _semantic_tokens(source_summary)
        if token not in _GENERIC_SUMMARY_WORDS
    }
    document_tokens = {
        token for token in _semantic_tokens(document_summary)
        if token not in _GENERIC_SUMMARY_WORDS
    }
    return (
        len(document_tokens) >= len(source_tokens) + 2
        and len(document_tokens - source_tokens) >= 2
    )


def _is_generic_line_label(value: str) -> bool:
    return bool(_GENERIC_LINE_LABEL_RE.fullmatch(" ".join(str(value or "").split())))


def _is_opaque_service_fee_label(value: str) -> bool:
    text = " ".join(str(value or "").split())
    if not text:
        return False
    key = _semantic_key(text)
    has_fee = bool(_SERVICE_FEE_TEXT_RE.search(text))
    has_code = bool(_OPAQUE_SERVICE_CODE_RE.search(text))
    if has_code and has_fee:
        return True
    without_code = _OPAQUE_SERVICE_CODE_RE.sub("", text)
    tokens = [
        token for token in _semantic_tokens(without_code)
        if token not in {"fee", "call", "trip"}
    ]
    return key in {"service fee", "service call", "trip charge"} or (has_fee and not tokens)


def _is_raw_service_note(value: str) -> bool:
    key = _semantic_key(value)
    if not key:
        return False
    return any(
        marker in key
        for marker in (
            "no visible leak",
            "no access",
            "no keys",
            "could not access",
            "unable to access",
        )
    )


def _service_note_content_summary(
    normalized_invoice: dict[str, Any],
    item_descriptions: list[str],
) -> str:
    haystack = " ".join(
        part
        for part in (
            str(normalized_invoice.get("invoice_description") or ""),
            " ".join(item_descriptions),
            str(normalized_invoice.get("_document_text") or "")[:5000],
        )
        if part
    )
    if not haystack:
        return ""
    key = _semantic_key(haystack)
    if not (
        _SERVICE_FEE_TEXT_RE.search(haystack)
        or _is_raw_service_note(haystack)
        or _OPAQUE_SERVICE_CODE_RE.search(haystack)
    ):
        return ""

    segments = _service_note_segments(haystack)
    if not segments:
        return ""
    category = _service_note_category(normalized_invoice, haystack)
    return _fit_content_summary(f"{category} - {'; '.join(segments)}")


def _service_note_segments(text: str) -> list[str]:
    segments: list[str] = []
    seen: set[str] = set()
    for clause in re.split(r"[;\n]+", str(text or "")):
        clause = clause.strip(" .:-")
        if not clause:
            continue
        key = _semantic_key(clause)
        unit = _unit_label(clause)
        label = ""
        if "no visible leak" in key:
            label = "Leak Check"
        elif "leak" in key and any(token in key for token in ("check", "inspect", "inspection")):
            label = "Leak Check"
        elif "no access" in key or "no keys" in key or "unable to access" in key or "could not access" in key:
            label = "No Access/Keys"
        if not label:
            continue
        if unit:
            label = f"{label} {unit}"
        normalized = label.lower()
        if normalized not in seen:
            segments.append(label)
            seen.add(normalized)
        if len(segments) >= 3:
            break
    return segments


def _unit_label(value: str) -> str:
    match = re.search(
        r"\b(?:unit|apt|apartment|suite|ste|#)\s*([A-Z0-9-]+)\b",
        str(value or ""),
        flags=re.I,
    )
    if not match:
        return ""
    token = match.group(1).strip(" .,#")
    return f"Unit {token}" if token else ""


def _service_note_category(normalized_invoice: dict[str, Any], text: str) -> str:
    gls = {
        str(item.get("gl_account_candidate") or "").strip()
        for item in normalized_invoice.get("line_items") or []
        if isinstance(item, dict)
    }
    key = _semantic_key(
        " ".join(
            [
                text,
                str(normalized_invoice.get("vendor_name") or ""),
                str(normalized_invoice.get("raw_vendor_name") or ""),
            ]
        )
    )
    if "6565" in gls or any(token in key for token in ("plumb", "leak", "sewer", "drain")):
        return "Plumbing Service Fee"
    if "6555" in gls or any(token in key for token in ("hvac", "air conditioner", "furnace", "heat pump")):
        return "HVAC Service Fee"
    if any(token in key for token in ("electric", "breaker", "outlet", "wire")):
        return "Electrical Service Fee"
    if any(token in key for token in ("pest", "termite", "bug", "insect")):
        return "Pest Control Service Fee"
    return "Service Fee"


def _semantic_product_summary(descriptions: list[str]) -> str:
    haystack = _semantic_key(" ".join(descriptions))
    if not haystack:
        return ""
    if (
        re.search(r"\b(?:sewer|drain)\s+(?:line|pipe)\b", haystack)
        and any(marker in haystack for marker in ("dug up", "excavat", "cut out", "replac", "installed"))
    ):
        diameter = re.search(r"\b(\d{1,2})\s*(?:in|inch)\b", haystack)
        size = f"{diameter.group(1)}-In " if diameter else ""
        material = " With Schedule 40 PVC" if "schedule 40 pvc" in haystack else ""
        return f"Excavate & Replace Damaged {size}Sewer Line{material}"
    if any(
        marker in haystack
        for marker in (
            "labor", "trip charge", "service call", "repair", "installation",
            "installed", "adjusted", "restoration", "removal",
        )
    ):
        service_parts: list[str] = []
        for description in descriptions:
            concise = _concise(description)
            if concise and concise.lower() not in {part.lower() for part in service_parts}:
                service_parts.append(concise)
            if len(service_parts) >= 2:
                break
        return "; ".join(service_parts)
    concepts: list[str] = []

    has_entry_lock = bool(re.search(r"\bentry\b.*\block\b|\block\b.*\bentry\b", haystack))
    has_dummy_lock = bool(re.search(r"\bdummy\b.*\block\b|\block\b.*\bdummy\b", haystack))
    has_deadbolt = "deadbolt" in haystack
    if has_entry_lock and has_dummy_lock:
        concepts.append("Entry/Dummy Lever Locks")
    elif has_entry_lock:
        concepts.append("Entry Lever Lock")
    elif has_dummy_lock:
        concepts.append("Dummy Lever Lock")
    elif re.search(r"\b(?:door\s+)?(?:lock|deadbolt|latch|door knob)\b", haystack):
        concepts.append("Door Lock Hardware")
    if has_deadbolt:
        concepts.append("SmartKey Deadbolt" if "smartkey" in haystack else "Deadbolt")

    if "slab door" in haystack or "hollow core" in haystack:
        dimension = re.search(r"\b(\d{2})\s*(?:in|inch|\")?\b", haystack)
        panels = re.search(r"\b(\d+)\s*panel\b", haystack)
        door_parts = []
        if dimension:
            door_parts.append(f"{dimension.group(1)}-In")
        if panels:
            door_parts.append(f"{panels.group(1)}-Panel")
        door_parts.append("Slab Door")
        concepts.append(" ".join(door_parts))

    clusters = (
        (("toilet", "faucet", "valve", "pvc", "drain", "plumbing"), "Plumbing Parts & Supplies"),
        (("breaker", "outlet", "switch", "wire", "electrical"), "Electrical Parts & Supplies"),
        (("paint", "primer", "thinner", "brush", "roller"), "Paint & Finishing Supplies"),
        (("filter", "thermostat", "condenser", "compressor", "hvac"), "HVAC Parts & Supplies"),
        (("cleaner", "bleach", "detergent", "sanitizer"), "Cleaning Supplies"),
        (("pool", "chlorine", "algaecide"), "Pool Supplies"),
        (("roof", "shingle", "flashing"), "Roofing Materials"),
        (("floor", "tile", "carpet", "vinyl plank"), "Flooring Materials"),
        (("appliance", "washer", "dryer", "refrigerator", "range"), "Appliance Parts & Supplies"),
        (("drill", "saw", "wrench", "screwdriver", "tool"), "Tools & Hardware"),
        (("paper", "folder", "binder", "office"), "Office Supplies"),
    )
    for needles, label in clusters:
        if any(needle in haystack for needle in needles) and label not in concepts:
            concepts.append(label)
    return "; ".join(concepts[:3])


def _summary_is_generic(value: str) -> bool:
    meaningful = {
        token
        for token in _semantic_tokens(value)
        if token not in _GENERIC_SUMMARY_WORDS
    }
    return not meaningful


def _semantic_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\"&/ -]+", " ", str(value or "").lower())).strip()


def _semantic_tokens(value: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-z0-9]+", _semantic_key(value)):
        if token in _GENERIC_CONTENT_WORDS or len(token) <= 2:
            continue
        if token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.append(token)
    return tokens


def _fit_content_summary(value: str, limit: int = 75) -> str:
    clean = proper_case_preserve_acronyms(" ".join(str(value or "").split()))
    clean = re.sub(r"\bEntry/dummy\b", "Entry/Dummy", clean, flags=re.I)
    clean = re.sub(r"\bSmartkey\b", "SmartKey", clean, flags=re.I)
    clean = re.sub(r"\bHvac\b", "HVAC", clean)
    if len(clean) <= limit:
        return clean
    clipped = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,;/&-")
    return clipped or clean[:limit].rstrip()


def _numeric_amount(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _concise(value: str) -> str:
    clean = " ".join(str(value or "").strip().split())
    if not clean:
        return ""
    words = clean.split()
    if len(words) > 8:
        clean = " ".join(words[:8])
    return proper_case_preserve_acronyms(clean[:72])


def _strip_trailing_source_date(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    text = re.sub(
        r"\s*(?:[-–—]\s*)?\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b\s*$",
        "",
        text,
    )
    return " ".join(text.strip().split())


def _uses_monthly_description(normalized_invoice: dict[str, Any], category: str) -> bool:
    if category in ALWAYS_MONTHLY_DESCRIPTION_CATEGORIES:
        return True
    if category != "other_infrequent":
        return False
    haystack = " ".join(
        [
            str(normalized_invoice.get("vendor_name") or ""),
            str(normalized_invoice.get("vendor") or ""),
            str(normalized_invoice.get("raw_vendor_name") or ""),
            str(normalized_invoice.get("invoice_description") or ""),
            " ".join(
                str(item.get("description") or "")
                for item in normalized_invoice.get("line_items") or []
                if isinstance(item, dict)
            ),
        ]
    ).lower()
    return any(
        token in haystack
        for token in (
            "monthly",
            "recurring",
            "subscription",
            "scheduled service",
            "routine service",
            "service period",
            "contract period",
        )
    )


def _uses_service_address_description(
    category: str,
    normalized_invoice: dict[str, Any],
    canonical_rules: dict[str, Any] | None,
) -> bool:
    if bool(normalized_invoice.get("force_service_address_description")):
        return True
    if bool(normalized_invoice.get("summary_description_only")):
        return False
    if category in SERVICE_ADDRESS_DESCRIPTION_CATEGORIES:
        return True
    if category in SUMMARY_DESCRIPTION_CATEGORIES:
        return False
    if _rules_allow_property_fallback(canonical_rules, category):
        return True
    return category not in {"other_infrequent", "unknown"}


def _category(normalized_invoice: dict[str, Any]) -> str:
    return str(normalized_invoice.get("category") or normalized_invoice.get("invoice_category") or "unknown").strip().lower()


def _period(normalized_invoice: dict[str, Any]) -> str:
    if _uses_monthly_description(normalized_invoice, _category(normalized_invoice)):
        monthly = _month_period(normalized_invoice)
        if monthly:
            return monthly
    explicit = str(normalized_invoice.get("service_period_range") or "").strip()
    if explicit:
        return explicit
    start = _short_date(normalized_invoice.get("service_period_start"))
    end = _short_date(normalized_invoice.get("service_period_end"))
    if start and end:
        if start == end:
            return start
        return f"{start}-{end}"
    return start or end


def _month_period(normalized_invoice: dict[str, Any]) -> str:
    parsed = (
        _parse_date(normalized_invoice.get("invoice_date"))
        or _parse_date(normalized_invoice.get("service_period_end"))
        or _parse_date(normalized_invoice.get("service_period_start"))
    )
    return parsed.strftime("%b-%y") if parsed else ""


def _short_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%y")
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).strftime("%m/%d/%y")
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%y")
        except ValueError:
            continue
    return text


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _rules_allow_property_fallback(
    canonical_rules: dict[str, Any] | None,
    category: str,
) -> bool:
    if not canonical_rules:
        return False
    categories = canonical_rules.get("categories") or {}
    cfg = categories.get(category) or {}
    policy = str(cfg.get("location_policy") or "").lower()
    return "property_level" in policy or "blank_location_allowed" in policy


__all__ = [
    "DescriptionResult",
    "build_one_off_content_summary",
    "build_contextual_one_off_line_description",
    "build_invoice_description",
    "build_line_item_description",
    "normalize_service_address_for_row",
    "polish_accounting_description_pair",
    "proper_case_preserve_acronyms_for_row",
]
