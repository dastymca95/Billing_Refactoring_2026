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


def build_invoice_description(
    normalized_invoice: dict[str, Any],
    canonical_rules: dict[str, Any] | None = None,
) -> DescriptionResult:
    """Build the invoice-level ResMan description from validated evidence."""

    category = _category(normalized_invoice)
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
    escaped = re.escape(unit_text)
    patterns = (
        rf"\b(?:APT|APARTMENT|UNIT|STE|SUITE|#)\s*{escaped}\b",
        rf"\b{escaped}\s+(?:APT|APARTMENT|UNIT|STE|SUITE)\b",
    )
    return any(re.search(pattern, address) for pattern in patterns)


def _summary_invoice_description(normalized_invoice: dict[str, Any]) -> DescriptionResult:
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


def _summary_line_item_description(
    normalized_invoice: dict[str, Any],
    line_item: dict[str, Any],
    category: str,
) -> DescriptionResult:
    source = normalize_source_line_description(
        line_item.get("source_line_description")
        or line_item.get("description")
        or line_item.get("line_item_description")
        or "",
    )
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


def _concise(value: str) -> str:
    clean = " ".join(str(value or "").strip().split())
    if not clean:
        return ""
    words = clean.split()
    if len(words) > 8:
        clean = " ".join(words[:8])
    return proper_case_preserve_acronyms(clean[:72])


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
    return str(normalized_invoice.get("category") or normalized_invoice.get("invoice_category") or "utilities").strip().lower()


def _period(normalized_invoice: dict[str, Any]) -> str:
    explicit = str(normalized_invoice.get("service_period_range") or "").strip()
    if explicit:
        return explicit
    start = _short_date(normalized_invoice.get("service_period_start"))
    end = _short_date(normalized_invoice.get("service_period_end"))
    if start and end:
        return f"{start}-{end}"
    return start or end


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
    "build_invoice_description",
    "build_line_item_description",
    "normalize_service_address_for_row",
    "proper_case_preserve_acronyms_for_row",
]
