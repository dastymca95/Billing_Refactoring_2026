"""Shared deterministic helpers for utility-style vendor processors.

This module is intentionally business-rule oriented but vendor agnostic.  It
does not parse a specific bill layout.  Vendor processors feed it candidate
service lines, tax/fee totals, property/location evidence, and YAML-derived
settings; this module applies the common utility rules consistently before the
rows reach the ResMan template preview/export layer.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import yaml

from .. import settings
from .description_builder import (
    build_invoice_description,
    build_line_item_description,
    normalize_service_address_for_row,
)
from utils.text_normalization import (
    looks_like_city_state_zip,
    proper_case_preserve_acronyms,
)


CENT = Decimal("0.01")

UTILITY_REQUIRED_COLUMNS: tuple[str, ...] = (
    "Invoice Number",
    "Bill or Credit",
    "Invoice Date",
    "Accounting Date",
    "Vendor",
    "Invoice Description",
    "Line Item Number",
    "Property Abbreviation",
    "GL Account",
    "Line Item Description",
    "Amount",
    "Expense Type",
    "Is Replacement Reserve",
    "Due Date",
)

PAYMENT_KEYWORDS = (
    "payments received",
    "payment received",
    "autopay",
    "automatic payment",
    "amount paid",
    "amount enclosed",
)
PAYMENT_CREDIT_KEYWORDS = (
    "credit memo",
    "refund",
    "reversed payment",
    "payment received",
)
PREVIOUS_BALANCE_KEYWORDS = (
    "previous balance",
    "balance forward",
    "prior balance",
    "past due balance",
)
TAX_KEYWORDS = (
    "tax",
    "sales tax",
    "state tax",
    "school tax",
    "county tax",
    "franchise tax",
)
CONNECTION_FEE_KEYWORDS = (
    "account establishment charge",
    "connect fee",
    "connection fee",
    "reconnection fee",
    "reconnect charge",
    "reconnect fee",
    "recon chg",
    "reconnection charge",
    "service connection",
    "new service fee",
    "connection charge",
    "turn on fee",
    "turn-on fee",
    "activation fee",
    "utility transfer fee",
)
LATE_FEE_KEYWORDS = (
    "late fee",
    "late charge",
    "past due fee",
    "late payment charge",
    "penalty",
)
FIRE_SERVICE_KEYWORDS = (
    "fire detection",
    "fire protection",
    "fire service",
    "private fire",
    "fire line",
    "sprinkler",
    "sprinkler service",
    "detector check",
    "fire meter",
    "fire hydrant",
    "fire standby",
    "fire capacity",
)
TRASH_SERVICE_KEYWORDS = (
    "trash",
    "garbage",
    "refuse",
    "solid waste",
    "waste collection",
    "trash collection",
    "sanitation",
    "dumpster",
    "recycling",
)
INTERNET_FIBER_KEYWORDS = (
    "fiber",
    "internet",
    "broadband",
    "wi-fi",
    "wifi",
)
CABLE_KEYWORDS = (
    "cable",
    "television",
)
WASTEWATER_KEYWORDS = (
    "wastewater",
    "sewer",
)
STORMWATER_KEYWORDS = (
    "stormwater",
    "storm water",
)
WATER_KEYWORDS = (
    "water",
)
GAS_KEYWORDS = (
    "gas",
    "therm",
    "natural gas",
)
ELECTRIC_KEYWORDS = (
    "electric",
    "electricity",
    "kwh",
    "power",
    "demand",
)
ELECTRIC_COMMON_KEYWORDS = (
    "area light",
    "common light",
    "led light",
    "rental light",
    "rental lights",
    "outdoor light",
    "outdoor lights",
    "parking lot light",
    "pole charge",
    "security light",
    "site light",
    "street light",
    "yard light",
)

DEFAULT_UTILITY_GL: dict[str, str] = {
    "electric": "6915",
    "electric_common": "6915",
    "electric_vacant": "6920",
    "gas": "6930",
    "gas_vacant": "6935",
    "water": "6955",
    "sewer": "6955",
    "wastewater": "6955",
    "stormwater": "6995",
    "internet": "6960",
    "fiber": "6960",
    "cable": "6905",
    "trash": "6940",
    "sanitation": "6940",
    "fire_protection": "6860",
    "connection_fee": "6956",
}


@dataclass(frozen=True)
class UtilityChargeLine:
    """One candidate service/fee line before final ResMan row rendering."""

    description: str
    amount: Decimal | str | float | int
    line_type: str = "service"
    gl_account: str = ""
    taxable: bool = True
    include_in_export: bool = True
    source_page: int | None = None
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def money(self) -> Decimal:
        return money(self.amount)


@dataclass(frozen=True)
class UtilityTaxAllocation:
    """Result of proportional tax allocation."""

    lines: list[UtilityChargeLine]
    tax_total: Decimal
    taxable_base_total: Decimal
    allocation_by_index: dict[int, Decimal]
    rounding_adjustment_index: int | None = None


@dataclass(frozen=True)
class UtilityValidationResult:
    ok: bool
    blocking_reasons: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class UtilityLineClassification:
    classification: str
    confidence: float
    matched_keywords: tuple[str, ...] = ()
    reason: str = ""
    recommended_gl_strategy: str = "keyword_or_vendor_default"
    should_be_separate_line: bool = False
    should_allocate_tax: bool = True
    manual_review_flags: tuple[str, ...] = ()


def money(value: Any) -> Decimal:
    """Return a cents-rounded Decimal from a UI/PDF/CSV value."""

    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal("0.00")
    text = text.replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def text_has_any(text: str, keywords: Iterable[str]) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return any(k.lower() in normalized for k in keywords)


def _matched_keywords(text: str, keywords: Iterable[str]) -> tuple[str, ...]:
    normalized = " ".join(str(text or "").lower().split())
    return tuple(k for k in keywords if k.lower() in normalized)


def classify_utility_line_detail(description: str) -> UtilityLineClassification:
    """Classify a source utility line with priority and GL guidance.

    The order is intentionally conservative. Setup fees and fire-service
    lines must be identified before the generic water/electric buckets can
    absorb them.
    """

    text = description or ""
    checks: list[tuple[str, Iterable[str], str, str, bool, bool, tuple[str, ...]]] = [
        (
            "payment",
            PAYMENT_KEYWORDS + PAYMENT_CREDIT_KEYWORDS,
            "Matched payment/credit exclusion keyword.",
            "exclude_non_expense",
            False,
            False,
            (),
        ),
        (
            "previous_balance",
            PREVIOUS_BALANCE_KEYWORDS,
            "Matched previous/balance-forward exclusion keyword.",
            "exclude_unless_vendor_allows",
            False,
            False,
            (),
        ),
        (
            "connection_fee",
            CONNECTION_FEE_KEYWORDS,
            "Matched utility connection/reconnection fee keyword.",
            "force_gl_6956",
            True,
            False,
            (),
        ),
        (
            "late_fee",
            LATE_FEE_KEYWORDS,
            "Matched late fee keyword.",
            "vendor_late_fee_or_default_never_6956",
            True,
            False,
            (),
        ),
        (
            "tax",
            TAX_KEYWORDS,
            "Matched tax keyword.",
            "allocate_proportionally_no_standalone_row",
            False,
            False,
            (),
        ),
        (
            "fire_protection_service",
            FIRE_SERVICE_KEYWORDS,
            "Matched fire protection / sprinkler utility service keyword.",
            "fire_service_rule_or_manual_review",
            True,
            True,
            ("gl_mapping_required_fire_service",),
        ),
        (
            "electric_common_service",
            ELECTRIC_COMMON_KEYWORDS,
            "Matched outdoor/common-area electric lighting keyword.",
            "electric_common_gl_6915",
            True,
            True,
            (),
        ),
        (
            "trash_service",
            TRASH_SERVICE_KEYWORDS,
            "Matched trash/sanitation service keyword.",
            "trash_gl_6940",
            True,
            True,
            (),
        ),
        (
            "internet_fiber_service",
            INTERNET_FIBER_KEYWORDS,
            "Matched internet/fiber service keyword.",
            "internet_fiber_gl",
            True,
            True,
            (),
        ),
        (
            "cable_service",
            CABLE_KEYWORDS,
            "Matched cable/television service keyword.",
            "cable_gl",
            True,
            True,
            (),
        ),
        (
            "stormwater_service",
            STORMWATER_KEYWORDS,
            "Matched stormwater service keyword.",
            "stormwater_gl",
            True,
            True,
            (),
        ),
        (
            "wastewater_service",
            WASTEWATER_KEYWORDS,
            "Matched sewer/wastewater service keyword.",
            "water_sewer_gl",
            True,
            True,
            (),
        ),
        (
            "water_service",
            WATER_KEYWORDS,
            "Matched water service keyword.",
            "water_sewer_gl",
            True,
            True,
            (),
        ),
        (
            "gas_service",
            GAS_KEYWORDS,
            "Matched gas service keyword.",
            "gas_gl",
            True,
            True,
            (),
        ),
        (
            "electric_service",
            ELECTRIC_KEYWORDS,
            "Matched electric service keyword.",
            "electric_gl",
            True,
            True,
            (),
        ),
    ]
    for classification, keywords, reason, strategy, separate, allocate_tax, flags in checks:
        matched = _matched_keywords(text, keywords)
        if matched:
            return UtilityLineClassification(
                classification=classification,
                confidence=0.95 if classification in {"connection_fee", "late_fee", "fire_protection_service"} else 0.88,
                matched_keywords=matched,
                reason=reason,
                recommended_gl_strategy=strategy,
                should_be_separate_line=separate,
                should_allocate_tax=allocate_tax,
                manual_review_flags=flags,
            )
    return UtilityLineClassification(
        classification="service",
        confidence=0.45,
        reason="No specific utility keyword matched; fall back to vendor/default GL.",
        recommended_gl_strategy="vendor_default_or_manual_review",
        should_be_separate_line=True,
        should_allocate_tax=True,
    )


def classify_utility_line(description: str) -> str:
    """Classify a source line into utility-processing buckets."""

    return classify_utility_line_detail(description).classification


def is_non_expense_line(description: str) -> bool:
    return classify_utility_line(description) in {"payment", "previous_balance"}


def service_family(description: str, *, vendor_key: str = "") -> str:
    """Infer the utility family from a line/vendor hint."""

    hay = f"{vendor_key} {description}".lower()
    classification = classify_utility_line(description)
    if classification == "connection_fee":
        return "connection_fee"
    if classification == "electric_common_service":
        return "electric_common"
    if classification == "fire_protection_service":
        return "fire_protection"
    if classification == "trash_service":
        return "trash"
    if any(k in hay for k in INTERNET_FIBER_KEYWORDS):
        return "internet"
    if any(k in hay for k in CABLE_KEYWORDS):
        return "cable"
    if any(k in hay for k in TRASH_SERVICE_KEYWORDS):
        return "trash"
    if any(k in hay for k in WASTEWATER_KEYWORDS):
        return "sewer"
    if any(k in hay for k in STORMWATER_KEYWORDS):
        return "stormwater"
    if any(k in hay for k in WATER_KEYWORDS):
        return "water"
    if any(k in hay for k in GAS_KEYWORDS):
        return "gas"
    if any(k in hay for k in ELECTRIC_KEYWORDS):
        return "electric"
    return "utility"


def default_gl_for_line(
    description: str,
    *,
    vendor_key: str = "",
    vendor_config: dict[str, Any] | None = None,
) -> str:
    """Return the best deterministic GL candidate from config + keywords.

    Vendor YAML remains the first source of truth. Keyword defaults are a
    conservative fallback and always return numeric Chart-of-Accounts codes.
    """

    cfg = vendor_config or {}
    line_type = classify_utility_line(description)
    if line_type == "connection_fee":
        return "6956"
    if line_type == "electric_common_service":
        return "6915"
    if line_type == "fire_protection_service":
        fire_cfg = (
            (cfg.get("utility_processing") or {}).get("fire_service_rules")
            or cfg.get("fire_service_rules")
            or {}
        )
        return str(fire_cfg.get("gl_account") or fire_cfg.get("gl_code") or "").strip()
    if line_type == "trash_service":
        trash_cfg = (
            (cfg.get("utility_processing") or {}).get("trash_service_rules")
            or cfg.get("trash_service_rules")
            or {}
        )
        return str(trash_cfg.get("gl_account") or trash_cfg.get("gl_code") or "").strip() or "6940"
    if line_type == "late_fee":
        late_cfg = (cfg.get("special_charges") or {}).get("late_fee") or {}
        late_gl = str(late_cfg.get("gl_account") or late_cfg.get("gl_code") or "").strip()
        return late_gl or str(((cfg.get("accounting_mapping") or {}).get("default_gl_code") or "")).strip()
    if line_type in {"tax", "payment", "previous_balance"}:
        return ""

    default_gl = str(((cfg.get("accounting_mapping") or {}).get("default_gl_code") or "")).strip()
    family = service_family(description, vendor_key=vendor_key)
    if family == "utility" and default_gl:
        return default_gl
    return DEFAULT_UTILITY_GL.get(family) or default_gl


def allocate_tax_proportionally(
    lines: Iterable[UtilityChargeLine],
    tax_total: Decimal | str | float | int,
) -> UtilityTaxAllocation:
    """Allocate tax across taxable/exportable service lines and reconcile.

    Tax-only lines are not emitted. The rounding remainder is applied to the
    largest taxable base line so the final cents sum exactly.
    """

    original = list(lines)
    tax = money(tax_total)
    if tax == 0:
        return UtilityTaxAllocation(original, tax, Decimal("0.00"), {}, None)

    taxable_indexes = [
        i
        for i, line in enumerate(original)
        if line.include_in_export
        and line.taxable
        and line.money > 0
        and classify_utility_line(line.description) not in {"tax", "payment", "previous_balance"}
    ]
    base = sum((original[i].money for i in taxable_indexes), Decimal("0.00"))
    if base <= 0:
        return UtilityTaxAllocation(original, tax, base, {}, None)

    allocations: dict[int, Decimal] = {}
    for i in taxable_indexes:
        share = (original[i].money / base) * tax
        allocations[i] = share.quantize(CENT, rounding=ROUND_HALF_UP)

    allocated = sum(allocations.values(), Decimal("0.00"))
    remainder = (tax - allocated).quantize(CENT, rounding=ROUND_HALF_UP)
    adjust_index: int | None = None
    if remainder:
        adjust_index = max(taxable_indexes, key=lambda idx: original[idx].money)
        allocations[adjust_index] = (allocations[adjust_index] + remainder).quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )

    adjusted: list[UtilityChargeLine] = []
    for i, line in enumerate(original):
        extra = allocations.get(i, Decimal("0.00"))
        if extra:
            metadata = dict(line.metadata)
            metadata["tax_allocated"] = str(extra)
            adjusted.append(
                UtilityChargeLine(
                    description=line.description,
                    amount=(line.money + extra).quantize(CENT, rounding=ROUND_HALF_UP),
                    line_type=line.line_type,
                    gl_account=line.gl_account,
                    taxable=line.taxable,
                    include_in_export=line.include_in_export,
                    source_page=line.source_page,
                    trace_id=line.trace_id,
                    metadata=metadata,
                )
            )
        else:
            adjusted.append(line)

    return UtilityTaxAllocation(adjusted, tax, base, allocations, adjust_index)


def filter_exportable_utility_lines(lines: Iterable[UtilityChargeLine]) -> list[UtilityChargeLine]:
    """Remove payments, previous balances, tax-only rows, and zero rows."""

    out: list[UtilityChargeLine] = []
    for line in lines:
        bucket = classify_utility_line(line.description)
        if bucket in {"payment", "previous_balance", "tax"}:
            continue
        if line.money == 0:
            continue
        if not line.include_in_export:
            continue
        out.append(line)
    return out


def service_period_range(start: Any, end: Any) -> str:
    s = _format_short_date(start)
    e = _format_short_date(end)
    if s and e:
        return f"{s}-{e}"
    return s or e


def build_utility_invoice_number(
    *,
    account_number: str = "",
    service_period_end: Any = None,
    explicit_invoice_number: str = "",
    rule: str = "account_number_service_period",
) -> str:
    """Build utility invoice numbers from canonical/vendor rule names."""

    explicit = " ".join(str(explicit_invoice_number or "").split())
    account = " ".join(str(account_number or "").split())
    if rule in {"explicit_invoice_number", "bill_invoice_number"} and explicit:
        return explicit
    end = _coerce_date(service_period_end)
    if rule in {"account_number_service_period", "account_month_year"}:
        if account and end:
            return f"{account} {end.strftime('%b')} {end.strftime('%y')}"
        if account:
            return account
    return explicit or account


def compose_invoice_description(
    *,
    service_period_start: Any = None,
    service_period_end: Any = None,
    service_address_or_property: str = "",
    vendor_name: str = "",
    property_name: str = "",
    category: str = "utilities",
    property_level_service: bool = False,
) -> str:
    result = build_invoice_description(
        {
            "category": category,
            "service_period_start": service_period_start,
            "service_period_end": service_period_end,
            "service_address": service_address_or_property,
            "property_name": property_name or vendor_name,
            "property_level_service": property_level_service,
        }
    )
    return result.description


def compose_line_item_description(
    *,
    service_period_start: Any = None,
    service_period_end: Any = None,
    service_address_or_property: str = "",
    source_line_description: str = "",
    property_name: str = "",
    category: str = "utilities",
    property_level_service: bool = False,
) -> str:
    result = build_line_item_description(
        {
            "category": category,
            "service_period_start": service_period_start,
            "service_period_end": service_period_end,
            "service_address": service_address_or_property,
            "property_name": property_name,
            "property_level_service": property_level_service,
        },
        {"description": source_line_description},
    )
    return result.description


def title_clean(value: str) -> str:
    return proper_case_preserve_acronyms(value)


def looks_like_raw_address(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        re.search(r"\b\d{2,6}\s+[A-Za-z0-9 .'-]+\s+(St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Pkwy|Way)\b", text, re.I)
    )


def load_chart_of_accounts(path: Path | None = None) -> dict[str, str]:
    coa = path or (settings.PROJECT_ROOT / "Gl Codes" / "Chart Of Accounts.csv")
    out: dict[str, str] = {}
    if not coa.is_file():
        return out
    with coa.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("Number") or row.get("GL Code") or row.get("Code") or "").strip()
            if not code:
                continue
            out[code] = (row.get("Name") or row.get("Description") or "").strip()
    return out


def load_vendor_config(vendor_key: str) -> dict[str, Any]:
    path = settings.PROJECT_ROOT / "config" / "vendors" / f"{vendor_key}.yaml"
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def validate_utility_template_rows(
    rows: Iterable[dict[str, Any]],
    *,
    valid_gl_accounts: dict[str, str] | None = None,
    require_document_url: bool = False,
) -> UtilityValidationResult:
    """Validate common utility export safety rules."""

    valid_gl_accounts = valid_gl_accounts if valid_gl_accounts is not None else load_chart_of_accounts()
    blocking: list[str] = []
    warnings: list[str] = []
    for idx, row in enumerate(rows, start=1):
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        for column in UTILITY_REQUIRED_COLUMNS:
            if column == "Document Url" and not require_document_url:
                continue
            if not str(row.get(column, "")).strip():
                blocking.append(f"row_{idx}:{_flag_column(column)}_missing")
        gl = str(row.get("GL Account", "")).strip()
        if gl and (not gl.isdigit() or (valid_gl_accounts and gl not in valid_gl_accounts)):
            blocking.append(f"row_{idx}:invalid_gl_account")
        location = str(row.get("Location", "")).strip()
        if looks_like_raw_address(location):
            blocking.append(f"row_{idx}:raw_address_in_location")
        invoice_description = str(row.get("Invoice Description", "") or "")
        line_item_description = str(row.get("Line Item Description", "") or "")
        desc = " ".join((invoice_description, line_item_description))
        if is_non_expense_line(desc):
            blocking.append(f"row_{idx}:payment_or_previous_balance_expense_line")
        if classify_utility_line(desc) == "tax":
            blocking.append(f"row_{idx}:standalone_tax_line")
        classification = classify_utility_line(line_item_description or invoice_description)
        source_classification = str(meta.get("line_classification") or meta.get("line_type") or "")
        if classification == "connection_fee" and gl != "6956":
            blocking.append(f"row_{idx}:connection_fee_wrong_gl")
        if classification == "late_fee" and gl == "6956":
            blocking.append(f"row_{idx}:late_fee_wrong_connect_gl")
        if classification == "fire_protection_service" and gl == DEFAULT_UTILITY_GL.get("water"):
            blocking.append(f"row_{idx}:fire_service_mapped_as_water")
        if source_classification == "fire_protection_service" and gl == DEFAULT_UTILITY_GL.get("water"):
            blocking.append(f"row_{idx}:fire_service_mapped_as_water")
        if classification == "trash_service" and gl != DEFAULT_UTILITY_GL.get("trash"):
            blocking.append(f"row_{idx}:trash_service_wrong_gl")
        if classification == "stormwater_service" and gl != DEFAULT_UTILITY_GL.get("stormwater"):
            blocking.append(f"row_{idx}:stormwater_service_wrong_gl")
        if source_classification == "stormwater_service" and gl != DEFAULT_UTILITY_GL.get("stormwater"):
            blocking.append(f"row_{idx}:stormwater_service_wrong_gl")
        service_address = normalize_service_address_for_row(
            meta.get("service_address") or meta.get("ai_service_address") or ""
        )
        property_name = str(meta.get("property_name") or meta.get("matched_property_name") or "").strip()
        if service_address and service_address not in invoice_description:
            blocking.append(f"row_{idx}:invoice_description_missing_service_address")
        if service_address and property_name and property_name in invoice_description and service_address not in invoice_description:
            blocking.append(f"row_{idx}:invoice_description_uses_property_instead_of_service_address")
        if looks_like_city_state_zip(invoice_description):
            blocking.append(f"row_{idx}:invoice_description_contains_city_state_zip")
        expected_invoice_description = proper_case_preserve_acronyms(invoice_description)
        expected_line_description = proper_case_preserve_acronyms(line_item_description)
        if invoice_description and invoice_description != expected_invoice_description:
            blocking.append(f"row_{idx}:invoice_description_not_proper_case")
        if line_item_description and line_item_description != expected_line_description:
            blocking.append(f"row_{idx}:line_item_description_not_proper_case")
        if str(row.get("Expense Type", "")).strip() not in {"General", "general"}:
            warnings.append(f"row_{idx}:expense_type_not_general")
    return UtilityValidationResult(not blocking, sorted(set(blocking)), sorted(set(warnings)))


def _flag_column(column: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_")


def _format_short_date(value: Any) -> str:
    dt = _coerce_date(value)
    return dt.strftime("%m/%d/%y") if dt else ""


def _coerce_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


__all__ = [
    "CENT",
    "DEFAULT_UTILITY_GL",
    "UTILITY_REQUIRED_COLUMNS",
    "UtilityChargeLine",
    "UtilityLineClassification",
    "UtilityTaxAllocation",
    "UtilityValidationResult",
    "allocate_tax_proportionally",
    "build_utility_invoice_number",
    "classify_utility_line",
    "classify_utility_line_detail",
    "compose_invoice_description",
    "compose_line_item_description",
    "default_gl_for_line",
    "filter_exportable_utility_lines",
    "is_non_expense_line",
    "load_chart_of_accounts",
    "load_vendor_config",
    "looks_like_raw_address",
    "money",
    "service_family",
    "service_period_range",
    "validate_utility_template_rows",
]
