"""Deterministic processor for Lowe's Pro Supply order invoices.

ResMan must use ``Order #`` as the invoice number. The shorter
``Lowe's Invoice Number`` is a store transaction reference and is ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

from utils.canonical_vendors import canonical_vendor_name
from utils.property_lookup import match_by_property_name

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore


_LOG = logging.getLogger(__name__)
CENT = Decimal("0.01")
VENDOR_KEY = "lowes"
VENDOR_DISPLAY_NAME = "Lowes Pro Supply"
MAX_INVOICE_DESCRIPTION_LENGTH = 75
MONEY_TOKEN = r"(?:\([\d,]+\.\d{2}\)|-?[\d,]+\.\d{2})"

PROPERTY_MAP = {
    "the park at carson": ("TPAC", "The Park at Carson"),
    "park at carson": ("TPAC", "The Park at Carson"),
}

CATEGORY_GL_MAP = {
    "electrical": "6627",
    "hardware": "6651",
    "lumber": "6666",
    "building materials": "6666",
    "tools": "6669",
    "rough plumbing": "6675",
    "plumbing": "6675",
    "paint": "6770",
}

ITEM_GL_OVERRIDES = {
    "L-6807513": "6651",
    "L-5115541": "6669",
    "L-3626925": "6627",
    "L-866423": "6675",
}

ITEM_DESCRIPTION_OVERRIDES = {
    "L-6609494": "Glade Cashmere Woods Candle",
    "L-6758706": "OC Cleaner Eucalyptus, 1 Gal",
    "L-1597629": "Klean-Strip Green Paint Thinner, Quart",
    "L-493054": "6-Ft 13-Gauge Heavy-Duty U-Post",
    "L-66735": "8-In x 12-In No Trespassing Sign",
    "L-821031": "Klean-Strip Premium Stripper, Quart",
    "L-839714": "Norton 4-In Coarse Wire Wheel",
    "L-1329437": "2/0 x 15-Ft Green Powder-Coated Hardware",
    "L-147": "Round-Head Combo Machine Screws with Nuts, 1/4 x 1, 25-Count",
    "L-58124": "Flat Washers SAE 1/4-In, 16-Count",
    "L-6806103": "Goof Off Pro Exact Spray",
    "L-23524": "3/4-In PVC Male Threaded Plug",
    "L-23856": "3/4-In PVC Male Adapter",
    "L-26052": "3/4-In PVC Tee",
    "L-26055": "3/4-In PVC 90-Degree Elbow",
    "L-552328": "4 x 4 x 8 Treated #2 Grade Lumber",
    "L-787549": "1-Lb Construction Screws, 3-In",
    "L-112599": "1-Lb Coarse Drywall Screws, Phillips Head",
    "L-1644921": "121-Oz Moxie Outdoor Bleach",
    "L-193074": "1/2-In x 2-Ft x 2-Ft Patch Panel",
    "L-5025472": "Angel Soft Mega Toilet Paper, 16 Equals 64 Rolls",
    "L-2626699": "50-Ft NeverKink Hose",
    "L-5195459": "Eaton Tamper-Resistant GFCI, 15A 125V",
    "L-7392468": "Moxie Outdoor Cleaner, 1 Gal",
    "L-6807513": "3M Heavy-Duty White Duct Tape, 2-In",
    "L-111772": "1-Lb Fine Drywall Screws, 1-1/8-In",
    "L-111802": "1-Lb Fine Drywall Screws, 2-In",
    "L-503428": "Gatehouse 3-1/2-In Orbital Hinge",
    "L-4839359": "Reinforced Wax Ring with Bolts",
    "L-6039305": "Project Source Pro-Flush Elongated Toilet",
    "L-751638": "3/8-In x 12-In Stainless-Steel Faucet Connector",
    "L-261820": "Triple-Grip #10 Anchors and Screws, 70-Count",
    "L-1944103": "Masonry Bit, 3/16-In x 3-1/2-In",
    "L-2132075": "Titen Turbo Hex Concrete Screws, 1/4-In x 2-1/4-In",
    "L-5115541": "Kobalt 6-Cu-Ft Steel Yard Cart",
    "L-115970": "3/4-In x 1/2-In Reducer",
    "L-130898": "Hubbell 1-Gang Weatherproof Plastic Box",
    "L-130902": "Hubbell 2-Gang Weatherproof Plastic Box",
    "L-166783": "Duck Electrical Duct Tape",
    "L-2132130": "#10 x 3-In Tan Exterior Screws, 70-Count",
    "L-254897": "3/4-In PVC Coupling",
    "L-254899": "3/4-In PVC Male Adapter",
    "L-305805": "Sellars Shop Rags, 200-Count",
    "L-677594": "Weatherproof In-Use Cover",
    "L-690020": "Weatherproof In-Use Cover",
    "L-70008": "14/2 Copper NM-B Wire, 100-Ft",
    "L-72809": "3/4-In PVC Schedule 40 Conduit",
    "L-73210": "3/4-In x 6-Ft Liquid-Tight Kit",
    "L-75749": "3/4-In PVC Conduit Clamps",
    "L-76151": "1/2-Pint Low-VOC Solvent Cement",
    "L-79214": "Hubbell 2-Gang Plastic Blank Box",
    "L-875078": "Power Pro One Exterior Screws, 1-Lb",
    "L-3626925": "100-Ft 14/3 Rubber Cord",
    "L-866423": "PVC 1-In Kit, White",
}

ITEM_SUMMARY_LABELS = {
    "L-6609494": "Candles",
    "L-6758706": "Eucalyptus Cleaner",
    "L-1597629": "Paint Thinner",
    "L-493054": "U-Posts",
    "L-66735": "Warning Signs",
    "L-821031": "Paint Stripper",
    "L-839714": "Wire Wheel",
    "L-1329437": "Hardware",
    "L-147": "Machine Screws",
    "L-58124": "Flat Washers",
    "L-6806103": "Goof Off Spray",
    "L-23524": "PVC Fittings",
    "L-23856": "PVC Fittings",
    "L-26052": "PVC Fittings",
    "L-26055": "PVC Fittings",
    "L-552328": "Treated Lumber",
    "L-787549": "Construction Screws",
    "L-112599": "Drywall Screws",
    "L-1644921": "Bleach",
    "L-193074": "Patch Panel",
    "L-5025472": "Tissue",
    "L-2626699": "50-Ft NeverKink Hose",
    "L-5195459": "GFCI Outlet",
    "L-7392468": "Outdoor Cleaner",
    "L-6807513": "Duct Tape",
    "L-111772": "Drywall Screws",
    "L-111802": "Drywall Screws",
    "L-503428": "Orbital Hinge",
    "L-4839359": "Wax Ring",
    "L-6039305": "Toilet",
    "L-751638": "Faucet Connectors",
    "L-261820": "Anchors and Screws",
    "L-1944103": "Masonry Bit",
    "L-2132075": "Concrete Screws",
    "L-5115541": "Steel Yard Cart",
    "L-115970": "Reducer",
    "L-130898": "Weatherproof Boxes",
    "L-130902": "Weatherproof Boxes",
    "L-166783": "Electrical Tape",
    "L-2132130": "Exterior Screws",
    "L-254897": "PVC Fittings",
    "L-254899": "PVC Fittings",
    "L-305805": "Shop Rags",
    "L-677594": "Weatherproof Covers",
    "L-690020": "Weatherproof Covers",
    "L-70008": "Electrical Wire",
    "L-72809": "PVC Conduit",
    "L-73210": "Liquid-Tight Kit",
    "L-75749": "Conduit Clamps",
    "L-76151": "Solvent Cement",
    "L-79214": "Blank Box",
    "L-875078": "Exterior Screws",
    "L-3626925": "Rubber Cord",
    "L-866423": "PVC Kit",
}

CATEGORY_SUMMARY_LABELS = {
    "building materials": "Building Materials",
    "electrical": "Electrical Supplies",
    "hardware": "Hardware",
    "lawn and garden": "Lawn and Garden Supplies",
    "lumber": "Lumber",
    "nonstock": "Cleaning Supplies",
    "paint": "Paint Supplies",
    "rough plumbing": "Plumbing Supplies",
    "seasonal and outdoor": "Outdoor Supplies",
    "tools": "Tools",
}


@dataclass
class LowesLineItem:
    line_number: str
    sku: str
    description: str
    source_category: str
    base_amount: Decimal
    amount_with_tax: Decimal = Decimal("0.00")
    gl_account: str = ""
    review_reasons: list[str] = field(default_factory=list)


@dataclass
class LowesInvoice:
    order_number: str = ""
    bill_or_credit: str = "Bill"
    bill_to_number: str = ""
    invoice_date: datetime | None = None
    due_date: datetime | None = None
    po_number: str = ""
    property_abbreviation: str = ""
    property_display_name: str = ""
    service_address: str = ""
    location: str = ""
    subtotal: Decimal = Decimal("0.00")
    tax_amount: Decimal = Decimal("0.00")
    total_amount: Decimal = Decimal("0.00")
    line_items: list[LowesLineItem] = field(default_factory=list)
    source_file: str = ""
    page_count: int = 0
    manual_review_reasons: list[str] = field(default_factory=list)
    debug_info: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessBatchResult:
    success: bool
    return_code: int
    summary: dict
    invoices: list[dict] = field(default_factory=list)
    manual_review_rows: list[dict] = field(default_factory=list)
    resman_workbook_path: str | None = None
    manual_review_workbook_path: str | None = None
    debug_csv_path: str | None = None
    log_path: str = ""
    errors: list[str] = field(default_factory=list)


def process_lowes_pro_supply_batch(
    *,
    input_folder: Path | str | None = None,
    output_folder: Path | str | None = None,
    template_path: Path | str | None = None,
    config_path: Path | str | None = None,
    run_context: dict[str, Any] | None = None,
    progress_callback: Callable[..., None] | None = None,
    should_cancel_callback: Callable[[], bool] | None = None,
) -> ProcessBatchResult:
    del output_folder, template_path, config_path
    inp = Path(input_folder or ".")
    files = sorted(inp.glob("*.pdf"), key=lambda path: path.name.lower())
    parsed: list[LowesInvoice] = []
    errors: list[str] = []

    _progress(progress_callback, current_step="Reading Lowe's invoice file(s)", files_total=len(files))
    for index, path in enumerate(files, start=1):
        if _cancelled(should_cancel_callback, run_context):
            break
        try:
            text, page_count = _extract_pdf_text(path)
            invoice = parse_lowes_pro_supply_text(text, source_file=path.name, page_count=page_count)
            _finalize_invoice(invoice)
            parsed.append(invoice)
            _progress(
                progress_callback,
                current_file=path.name,
                current_step=f"Parsed {path.name}",
                files_done=index,
                files_total=len(files),
                invoices_created=len(parsed),
                rows_created=sum(len(item.line_items) for item in parsed),
                percent=10 + (index / max(1, len(files))) * 80,
            )
        except Exception as exc:  # pragma: no cover
            _LOG.exception("Lowe's processor failed for %s", path)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    cancelled = _cancelled(should_cancel_callback, run_context)
    invoices_json = [_invoice_to_preview_dict(invoice) for invoice in parsed if invoice.line_items]
    review_json = [_manual_review_to_dict(invoice) for invoice in parsed if invoice.manual_review_reasons]
    summary = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": VENDOR_KEY,
        "processing_mode": "deterministic",
        "files_total": len(files),
        "files_processed": len(parsed),
        "files_skipped_unparseable": len(errors),
        "invoices_produced": len(invoices_json),
        "rows_total": sum(len(invoice["rows"]) for invoice in invoices_json),
        "manual_review_total": len(review_json),
        "dropbox_called": False,
        "cancelled": cancelled,
        "amount_total": float(sum((invoice.total_amount for invoice in parsed), Decimal("0.00"))),
        "source_logic": "lowes_order_number_and_line_total_reconciliation",
    }
    _progress(
        progress_callback,
        status="completed",
        percent=100.0,
        current_step="Done",
        files_done=len(parsed),
        files_total=len(files),
        invoices_created=len(invoices_json),
        rows_created=summary["rows_total"],
        warnings_count=len(review_json),
    )
    return ProcessBatchResult(
        success=not errors and not cancelled,
        return_code=130 if cancelled else 0 if not errors else 1,
        summary=summary,
        invoices=invoices_json,
        manual_review_rows=review_json,
        errors=(["cancelled_by_user"] if cancelled else []) + errors,
    )


def parse_lowes_pro_supply_text(text: str, *, source_file: str = "", page_count: int = 0) -> LowesInvoice:
    normalized = _normalize_text(text)
    ship_to_name, service_address = _extract_ship_to(normalized)
    property_abbreviation, property_display_name = _resolve_property(ship_to_name, service_address)
    category_totals = _extract_category_totals(normalized)
    invoice = LowesInvoice(
        order_number=_first(r"\bOrder\s*#\s*([A-Z0-9-]+)", normalized),
        bill_or_credit="Credit" if re.search(r"(?im)^\s*CREDIT\s*$", normalized) else "Bill",
        bill_to_number=_first(r"\bBill\s+To\s*#\s*(\d+)", normalized),
        invoice_date=_parse_date(_first(r"\bInvoice\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", normalized)),
        due_date=_parse_date(_first(r"\bDue\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", normalized)),
        po_number=_first(r"\bPO\s*#\s*([^\n]+)", normalized),
        property_abbreviation=property_abbreviation,
        property_display_name=property_display_name,
        service_address=service_address,
        location=_extract_explicit_location(service_address),
        subtotal=_money(_first(rf"\bLines\s+Total\b[^\n]*\bTotal\s+({MONEY_TOKEN})", normalized)),
        tax_amount=_money(_first(rf"\bLAR\s+SalesTax\s+({MONEY_TOKEN})", normalized)),
        total_amount=_money(_first(rf"\bInvoice\s+Total\s+({MONEY_TOKEN})", normalized)),
        line_items=_extract_line_items(normalized),
        source_file=source_file,
        page_count=page_count,
    )
    _allocate_tax(invoice, category_totals)
    invoice.debug_info = {
        "source_file": source_file,
        "parser": "webapp_native_lowes_pro_supply",
        "identifier_basis": "order_number",
        "bill_or_credit": invoice.bill_or_credit,
        "subtotal": float(invoice.subtotal),
        "tax_amount": float(invoice.tax_amount),
        "invoice_total": float(invoice.total_amount),
        "ship_to_name": ship_to_name,
        "service_address": service_address,
        "po_number": invoice.po_number,
        "source_category_totals": {key: float(value) for key, value in category_totals.items()},
    }
    return invoice


def _extract_line_items(text: str) -> list[LowesLineItem]:
    lines = [line.strip() for line in text.splitlines()]
    quantity_token = r"(?:\(\d+(?:\.\d+)?\)|\d+(?:\.\d+)?)"
    item_pattern = re.compile(
        r"^(?P<line>\d+)\s+(?P<sku>L-[A-Z0-9-]+)\s+"
        rf"(?P<ordered>{quantity_token})\s+(?P<uom>[A-Z]+)\s+"
        rf"(?P<shipped>{quantity_token})\s+(?P<unit>{MONEY_TOKEN})"
        rf"(?:\s+(?P<amount>{MONEY_TOKEN}))?$",
        re.IGNORECASE,
    )
    items: list[LowesLineItem] = []
    index = 0
    while index < len(lines):
        match = item_pattern.match(lines[index])
        if not match:
            index += 1
            continue
        amount = _money(match.group("amount") or "0")
        description_parts: list[str] = []
        source_category = ""
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if item_pattern.match(candidate) or re.match(r"^\d+\s+Lines\s+Total\b", candidate, re.I):
                break
            if re.match(
                r"^(?:INVOICE|CREDIT|Bill\s+To\s+#|Order\s+#|Invoice\s+Date|"
                r"Customer\s+Copy|Lines\s+Total|LAR\s+SalesTax|Invoice\s+Total|"
                r"Description\s+Total\s+Merchandise)\b",
                candidate,
                re.I,
            ):
                break
            category_match = re.match(r"^GL\s+CODE:\s*(.*)$", candidate, re.I)
            if category_match:
                source_category = _clean_category(category_match.group(1))
                cursor += 1
                break
            if (
                candidate
                and len(candidate) > 2
                and not re.fullmatch(r"[-A-Z0-9]", candidate, re.I)
                and not candidate.lower().startswith(("customer copy", "invoice", "credit"))
            ):
                description_parts.append(candidate)
            cursor += 1
        sku = match.group("sku").upper()
        description = ITEM_DESCRIPTION_OVERRIDES.get(sku) or " ".join(description_parts).strip() or sku
        if amount != Decimal("0.00"):
            gl_account = ITEM_GL_OVERRIDES.get(sku) or _resolve_gl(source_category, description)
            reasons = [] if gl_account else [f"gl_mapping_not_found:{source_category or description}"]
            items.append(
                LowesLineItem(
                    line_number=match.group("line"),
                    sku=sku,
                    description=description,
                    source_category=source_category,
                    base_amount=amount,
                    gl_account=gl_account,
                    review_reasons=reasons,
                )
            )
        index = max(cursor, index + 1)
    return items


def _resolve_gl(source_category: str, description: str) -> str:
    category = _category_key(source_category)
    detail = _key(description)
    if any(token in detail for token in ("cleaner", "bleach", "candle", "angel soft", "tissue", "goof off")):
        return "6730"
    if any(token in detail for token in ("hose", "pvc", "faucet", "toilet", "wax ring", "solvent cement")):
        return "6675"
    if "yard cart" in detail:
        return "6669"
    for label, gl_account in CATEGORY_GL_MAP.items():
        if label in category:
            return gl_account
    if category in {"nonstock", "lawn and garden"}:
        return "6730"
    if "seasonal" in category or "outdoor" in category:
        return "6675"
    return ""


def _allocate_tax(invoice: LowesInvoice, category_totals: dict[str, Decimal]) -> None:
    if not invoice.line_items:
        return
    base_total = sum((item.base_amount for item in invoice.line_items), Decimal("0.00"))
    if invoice.subtotal == Decimal("0.00"):
        invoice.subtotal = base_total.quantize(CENT, rounding=ROUND_HALF_UP)
    if invoice.total_amount == Decimal("0.00"):
        invoice.total_amount = (invoice.subtotal + invoice.tax_amount).quantize(CENT, rounding=ROUND_HALF_UP)
    item_categories = {_category_key(item.source_category) for item in invoice.line_items}
    category_total = sum((category_totals.get(category, Decimal("0.00")) for category in item_categories), Decimal("0.00"))
    if item_categories and item_categories.issubset(category_totals) and abs(category_total - invoice.total_amount) <= CENT:
        for category in item_categories:
            category_items = [item for item in invoice.line_items if _category_key(item.source_category) == category]
            category_base = sum((item.base_amount for item in category_items), Decimal("0.00"))
            remaining_category = category_totals[category]
            for index, item in enumerate(category_items):
                if index == len(category_items) - 1:
                    item.amount_with_tax = remaining_category.quantize(CENT, rounding=ROUND_HALF_UP)
                else:
                    ratio = item.base_amount / category_base if category_base else Decimal("0")
                    item.amount_with_tax = (category_totals[category] * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
                    remaining_category -= item.amount_with_tax
        return

    remaining = invoice.total_amount
    for index, item in enumerate(invoice.line_items):
        if index == len(invoice.line_items) - 1:
            item.amount_with_tax = remaining.quantize(CENT, rounding=ROUND_HALF_UP)
        else:
            ratio = item.base_amount / base_total if base_total else Decimal("0")
            allocated_tax = (invoice.tax_amount * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
            item.amount_with_tax = (item.base_amount + allocated_tax).quantize(CENT, rounding=ROUND_HALF_UP)
            remaining -= item.amount_with_tax


def _extract_category_totals(text: str) -> dict[str, Decimal]:
    block = _first(
        r"\bDescription\s+Total\s+Merchandise\s*\n(.+?)(?=\nCustomer\s+Copy\b)",
        text,
        flags=re.I | re.S,
    )
    if not block:
        return {}
    totals: dict[str, Decimal] = {}
    special_patterns = {
        "building materials": rf"\bBUILDING\s+({MONEY_TOKEN})\s+MATERIALS\b",
        "seasonal and outdoor": rf"\bSEASONAL\s+and\s+({MONEY_TOKEN})\s+OUTDOOR\b",
    }
    for category, pattern in special_patterns.items():
        value = _first(pattern, block, flags=re.I)
        if value:
            totals[category] = _money(value)
    normal_categories = (
        "electrical",
        "hardware",
        "lumber",
        "nonstock",
        "paint",
        "rough plumbing",
        "tools",
        "lawn and garden",
        "plumbing",
    )
    for category in normal_categories:
        pattern = rf"\b{re.escape(category)}\s+({MONEY_TOKEN})(?=\s|$)"
        value = _first(pattern, block, flags=re.I)
        if value:
            totals[category] = _money(value)
    return totals


def _finalize_invoice(invoice: LowesInvoice) -> None:
    reasons = list(invoice.manual_review_reasons)
    if not invoice.order_number:
        reasons.append("order_number_missing")
    if not invoice.invoice_date:
        reasons.append("invoice_date_missing")
    if not invoice.due_date:
        reasons.append("due_date_missing")
    if not invoice.property_abbreviation:
        reasons.append("property_mapping_not_found")
    if not invoice.line_items:
        reasons.append("payable_line_items_missing")
    base_total = sum((item.base_amount for item in invoice.line_items), Decimal("0.00")).quantize(CENT)
    row_total = sum((item.amount_with_tax for item in invoice.line_items), Decimal("0.00")).quantize(CENT)
    if abs(base_total - invoice.subtotal) > CENT:
        reasons.append(f"subtotal_mismatch:lines={base_total}:printed={invoice.subtotal}")
    if abs((invoice.subtotal + invoice.tax_amount) - invoice.total_amount) > CENT:
        reasons.append(
            f"printed_total_mismatch:subtotal_plus_tax={invoice.subtotal + invoice.tax_amount}:printed={invoice.total_amount}"
        )
    if abs(row_total - invoice.total_amount) > CENT:
        reasons.append(f"row_total_mismatch:rows={row_total}:printed={invoice.total_amount}")
    for item in invoice.line_items:
        reasons.extend(item.review_reasons)
    invoice.manual_review_reasons = sorted(set(reason for reason in reasons if reason))


def _invoice_to_preview_dict(invoice: LowesInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(invoice)
    return {
        "account_number": invoice.bill_to_number,
        "invoice_number": invoice.order_number,
        "billing_date": _fmt_iso(invoice.invoice_date),
        "property_abbreviation": invoice.property_abbreviation,
        "location": invoice.location,
        "service_address": invoice.service_address,
        "total_amount": float(invoice.total_amount),
        "line_items_total": float(sum((item.amount_with_tax for item in invoice.line_items), Decimal("0.00"))),
        "source_total_amount": float(invoice.total_amount),
        "manual_review_reasons": list(invoice.manual_review_reasons),
        "rows": rows,
        "source_file": invoice.source_file,
        "support_document_status": "local_webapp_link",
        "debug_info": invoice.debug_info,
    }


def _rows_for_invoice(invoice: LowesInvoice) -> list[dict[str, Any]]:
    vendor_name = canonical_vendor_name(
        vendor_key=VENDOR_KEY,
        aliases=["Lowes Pro Supply", "Lowe's Pro Supply"],
        fallback=VENDOR_DISPLAY_NAME,
    )
    invoice_description = _invoice_description(invoice)
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(invoice.line_items, start=1):
        rows.append(
            {
                "Invoice Number": invoice.order_number,
                "Bill or Credit": invoice.bill_or_credit,
                "Invoice Date": _fmt_date(invoice.invoice_date),
                "Accounting Date": _fmt_date(invoice.invoice_date),
                "Vendor": vendor_name,
                "Invoice Description": invoice_description,
                "Line Item Number": str(index),
                "Property Abbreviation": invoice.property_abbreviation,
                "Location": invoice.location,
                "GL Account": item.gl_account,
                "Line Item Description": item.description,
                "Amount": float(item.amount_with_tax),
                "Expense Type": "General",
                "Is Replacement Reserve": False,
                "Payment Date": "",
                "Reference Number": "",
                "Payment Method": "",
                "Department": "",
                "Due Date": _fmt_date(invoice.due_date),
                "Quantity": "",
                "Unit Price": "",
                "Tax": "",
                "Received Date": "",
                "Document Url": "",
                "_meta": {
                    "manual_review_reasons": list(invoice.manual_review_reasons),
                    "support_document_status": "local_webapp_link",
                    "source_file": invoice.source_file,
                    "processor": "webapp_native_lowes_pro_supply",
                    "page_count": invoice.page_count,
                    "order_number": invoice.order_number,
                    "source_category": item.source_category,
                    "source_base_amount": float(item.base_amount),
                    "allocated_tax": float(item.amount_with_tax - item.base_amount),
                },
            }
        )
    return rows


def _manual_review_to_dict(invoice: LowesInvoice) -> dict[str, Any]:
    return {
        "source_file": invoice.source_file,
        "account_number": invoice.bill_to_number,
        "invoice_number": invoice.order_number,
        "invoice_date": _fmt_date(invoice.invoice_date),
        "property_abbreviation": invoice.property_abbreviation,
        "location": invoice.location,
        "total_amount": float(invoice.total_amount),
        "line_items_total": float(sum((item.amount_with_tax for item in invoice.line_items), Decimal("0.00"))),
        "source_total_amount": float(invoice.total_amount),
        "line_count": len(invoice.line_items),
        "reasons": list(invoice.manual_review_reasons),
    }


def _invoice_description(invoice: LowesInvoice) -> str:
    context = _invoice_context(invoice)
    item_summary = _invoice_item_summary(invoice, context=context)
    description = " - ".join(part for part in (context, item_summary) if part)
    if len(description) <= MAX_INVOICE_DESCRIPTION_LENGTH:
        return description
    return _fit_description(context, _category_summary_labels(invoice))


def _invoice_context(invoice: LowesInvoice) -> str:
    po = re.sub(r"\s+", " ", invoice.po_number).strip()
    if not po:
        return "Maintenance Supplies"
    key = _key(po)
    if re.fullmatch(r"\d+", po) or re.search(r"\b0{3,}\b", po):
        return "Maintenance Supplies"
    if re.fullmatch(r"\d{6,8}", po):
        return "Maintenance Supplies"
    if key in {"office", "office supplies"}:
        return "Office Supplies"
    if key in {"pool", "pool supplies"}:
        return "Pool Supplies"
    if "pressure wash" in key:
        return "Pressure Wash Supplies"
    if po.upper().startswith("BLG "):
        return f"Building {po[4:].strip()} Maintenance Supplies"
    if po.upper().startswith("BUILDING "):
        return f"{po} Maintenance Supplies"
    return "Maintenance Supplies"


def _invoice_item_summary(invoice: LowesInvoice, *, context: str) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for item in invoice.line_items:
        label = ITEM_SUMMARY_LABELS.get(item.sku) or _fallback_summary_label(item.description)
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return _fit_description(context, labels, summary_only=True)


def _fit_description(context: str, labels: list[str], *, summary_only: bool = False) -> str:
    unique = _unique_labels(labels)
    if not unique:
        return "" if summary_only else context
    summary = _natural_join(unique)
    combined = f"{context} - {summary}" if context else summary
    if len(combined) <= MAX_INVOICE_DESCRIPTION_LENGTH:
        return summary if summary_only else combined

    selected: list[str] = []
    for label in unique:
        candidate = _natural_join([*selected, label])
        candidate_combined = f"{context} - {candidate}" if context else candidate
        if len(candidate_combined) <= MAX_INVOICE_DESCRIPTION_LENGTH:
            selected.append(label)
    if selected:
        summary = _natural_join(selected)
        return summary if summary_only else f"{context} - {summary}"
    return "" if summary_only else context[:MAX_INVOICE_DESCRIPTION_LENGTH].rstrip()


def _category_summary_labels(invoice: LowesInvoice) -> list[str]:
    return _unique_labels(
        [
            CATEGORY_SUMMARY_LABELS.get(_category_key(item.source_category), "")
            for item in invoice.line_items
        ]
    )


def _fallback_summary_label(description: str) -> str:
    text = re.sub(r"\s+", " ", description or "").strip()
    replacements = (
        (r"\b\d+(?:-\w+)?\b", ""),
        (r"\b(?:count|quart|gallon|gal|pack|rolls?)\b", ""),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,-")


def _unique_labels(labels: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        clean = re.sub(r"\s+", " ", label or "").strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _natural_join(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _extract_ship_to(text: str) -> tuple[str, str]:
    block = _first(r"\bSHIP\s+TO:\s*\n(.+?)(?=\nShip\s+Point\b)", text, flags=re.I | re.S)
    if not block:
        return "", ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in block.splitlines() if line.strip()]
    property_name = lines[0] if lines else ""
    address_lines = [
        line
        for line in lines
        if re.search(r"\b\d{2,}\s+[A-Za-z]", line)
        or re.search(r"\b[A-Z][a-z]+,\s*[A-Z]{2}\s+\d{5}", line)
    ]
    deduped: list[str] = []
    for line in address_lines:
        if line not in deduped:
            deduped.append(line)
    return property_name, ", ".join(deduped)


def _resolve_property(name: str, address: str) -> tuple[str, str]:
    matched = match_by_property_name(name)
    if matched is not None:
        return matched.property_abbreviation, matched.property_name
    for value in (name, address):
        key = _key(value)
        for candidate, resolved in PROPERTY_MAP.items():
            if candidate in key:
                return resolved
    return "", name


def _extract_explicit_location(service_address: str) -> str:
    match = re.search(r"\b(?:Apt|Apartment|Unit|Suite)\s*#?\s*([A-Z0-9-]+)\b", service_address, re.I)
    return match.group(1).strip() if match else ""


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not available")
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages), len(pdf.pages)


def _cancelled(callback: Callable[[], bool] | None, run_context: dict[str, Any] | None) -> bool:
    if callback is not None:
        try:
            return bool(callback())
        except Exception:
            return False
    hook = (run_context or {}).get("should_cancel")
    if callable(hook):
        try:
            return bool(hook())
        except Exception:
            return False
    return False


def _progress(callback: Callable[..., None] | None, **fields: Any) -> None:
    if callback is None:
        return
    try:
        callback(**fields)
    except Exception:
        pass


def _normalize_text(value: str) -> str:
    return str(value or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")


def _clean_category(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _category_key(value: str) -> str:
    key = _key(value)
    if key.startswith("building materi"):
        return "building materials"
    if key.startswith("seasonal and ou"):
        return "seasonal and outdoor"
    return key


def _key(value: str) -> str:
    text = _normalize_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first(pattern: str, text: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text or "", flags)
    return match.group(1).strip() if match else ""


def _money(value: str | Decimal | None) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    raw = str(value or "").strip()
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9.-]", "", raw)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return Decimal("0.00")
    amount = Decimal(cleaned)
    if negative and amount > 0:
        amount = -amount
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%m/%d/%Y") if value else ""


def _fmt_iso(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


__all__ = [
    "LowesInvoice",
    "LowesLineItem",
    "ProcessBatchResult",
    "parse_lowes_pro_supply_text",
    "process_lowes_pro_supply_batch",
]
