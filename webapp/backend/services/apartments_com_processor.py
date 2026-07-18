"""Deterministic Apartments.com / CoStar invoice processor.

Ports the useful parsing rules from ``Old Scripts/Apartments.com.py`` into the
web console pipeline. This module does not call Dropbox and does not write the
ResMan workbook; the shared preview/export layers own those responsibilities.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

from utils.canonical_vendors import canonical_vendor_name
from utils.text_normalization import proper_case_preserve_acronyms

from .document_ingestion import ingest_document
from .utility_processor_common import money

_LOG = logging.getLogger(__name__)

CENT = Decimal("0.01")
VENDOR_KEY = "apartments_com"
VENDOR_DISPLAY_NAME = "Apartments.com"
DEFAULT_GL = "6335"
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


SITE_ID_TO_PROPERTY: dict[str, tuple[str, str]] = {
    "270312641": ("Adelade", "The Adelade"),
    "185990171": ("APA", "Admiral Place"),
    "175513091": ("BCA", "Blue Country Apartments"),
    "253691831": ("COS", "The Cosgrove"),
    "216181941": ("GGOG", "Griffin Gate Apartments"),
    "179330251": ("LLA", "Liberty Landing"),
    "256019981": ("MVA", "Magnolia Village"),
    "291880121": ("OC-CCA", "Canoe Creek Apartments"),
    "285978251": ("OG-PPA", "The Oakley at Pro Park"),
    "300708751": ("OTF", "Oak Tree Farm Apartments"),
    "293873391": ("RCC", "River Canyon"),
    "300709311": ("TFF", "The Firefly"),
    "279316491": ("TGAP", "The Glenwood at Pinson"),
    "291375061": ("TPAC", "The Park at Carson"),
    "298805361": ("TPW", "The Penn Warren"),
    "279295781": ("TRA-Rain", "The Raintree Apartments"),
    "257140731": ("TRG1", "The Rowe at Gate 1"),
    "300709201": ("TVUGDG", "The Villas UGDG"),
    "318273321": ("VILLASPV", "The Villas of Pine Valley"),
    "322978811": ("THSA", "Harmony Square Townhomes"),
    "183626491": ("AMA", "Aspen Meadow Apartments"),
    # Not present in the old script, but present in current CoStar invoices.
    "213757911": ("SWTG", "The Gables at Red River"),
}

PRODUCTS = (
    "Campus Network Diamond Plus",
    "Campus Network Platinum Plus",
    "Campus Network Gold Plus",
    "Apartments Network 3 Diamond",
    "Apartments Network 3 Platinum",
    "Apartments Network 3 Gold",
    "Apartments Network 3 Silver",
    "Apartments Network 3 Bronze",
    "Network 3 Diamond Plus",
    "Network 3 Platinum Plus",
    "Network 3 Gold Plus",
    "Network 3 Silver Plus",
    "Network 3 Bronze Plus",
    "Social and Reputation Suite",
    "Rental Advertising",
)
PRODUCT_PATTERN = "|".join(re.escape(product) for product in PRODUCTS)


@dataclass
class ApartmentsComLine:
    product: str
    site_id: str
    contract_number: str
    period_start: datetime | None
    period_end: datetime | None
    subtotal: Decimal
    tax: Decimal
    amount: Decimal
    property_abbreviation: str = ""
    property_display_name: str = ""
    manual_review_reasons: list[str] = field(default_factory=list)


@dataclass
class ApartmentsComInvoice:
    invoice_number: str = ""
    account_number: str = ""
    invoice_date: datetime | None = None
    due_date: datetime | None = None
    service_period_start: datetime | None = None
    service_period_end: datetime | None = None
    invoice_amount: Decimal = Decimal("0.00")
    source_file: str = ""
    line_items: list[ApartmentsComLine] = field(default_factory=list)
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


def process_apartments_com_batch(
    *,
    input_folder: Path | str | None = None,
    output_folder: Path | str | None = None,
    template_path: Path | str | None = None,
    config_path: Path | str | None = None,
    run_context: dict[str, Any] | None = None,
    progress_callback: Callable[..., None] | None = None,
    should_cancel_callback: Callable[[], bool] | None = None,
) -> ProcessBatchResult:
    inp = Path(input_folder or ".")
    out = Path(output_folder or inp)
    out.mkdir(parents=True, exist_ok=True)
    run_context = run_context or {}
    dry_run = bool(run_context.get("dry_run"))
    timestamp = str(run_context.get("timestamp") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    parsed: list[ApartmentsComInvoice] = []

    files = sorted([p for p in inp.iterdir() if p.is_file()], key=lambda p: p.name.lower())
    _progress(progress_callback, current_step="Reading Apartments.com invoice file(s)", files_total=len(files))

    for index, path in enumerate(files, start=1):
        if _cancelled(should_cancel_callback, run_context):
            break
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append({"filename": path.name, "reason": f"unsupported_extension:{path.suffix.lower()}"})
            continue
        try:
            candidate = ingest_document(path, vendor_hint=VENDOR_DISPLAY_NAME)
            inv = parse_apartments_com_invoice_text(candidate.document_text or "", source_file=path.name)
            _finalize_invoice(inv)
            parsed.append(inv)
            _progress(
                progress_callback,
                current_file=path.name,
                current_step=f"Parsed {path.name}",
                files_done=index,
                files_total=len(files),
                invoices_created=len(parsed),
                rows_created=sum(len(invoice.line_items) for invoice in parsed),
                percent=10 + (index / max(1, len(files))) * 80,
            )
        except Exception as exc:  # pragma: no cover - operator batch defense
            _LOG.exception("Apartments.com processor failed for %s", path)
            skipped.append({"filename": path.name, "reason": f"{type(exc).__name__}: {exc}"})
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    cancelled = _cancelled(should_cancel_callback, run_context)
    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.line_items and not cancelled]
    review_json = [_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons]
    row_count = sum(len(inv.get("rows") or []) for inv in invoices_json)
    workbook_path = out / f"{VENDOR_KEY}_resman_import_{timestamp}.xlsx"

    summary = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": VENDOR_KEY,
        "processing_mode": "deterministic",
        "dry_run": dry_run,
        "files_total": len(files),
        "files_processed": len(files) - len(skipped),
        "files_skipped_unsupported": len(skipped),
        "invoices_produced": len(invoices_json),
        "rows_total": row_count,
        "line_items": row_count,
        "manual_review_total": len(review_json),
        "invoices_flagged_for_review": len(review_json),
        "output_folder": str(out),
        "would_write_workbook_path": str(workbook_path),
        "dropbox_called": False,
        "cancelled": cancelled,
        "source_logic": "ported_from_old_apartments_com_script",
    }

    _progress(
        progress_callback,
        status="completed",
        percent=100.0,
        current_step="Done",
        files_done=len(files),
        files_total=len(files),
        invoices_created=len(invoices_json),
        rows_created=row_count,
        warnings_count=len(review_json),
    )

    return ProcessBatchResult(
        success=not errors and not cancelled,
        return_code=130 if cancelled else 0 if not errors else 1,
        summary=summary,
        invoices=invoices_json,
        manual_review_rows=review_json,
        resman_workbook_path=None,
        manual_review_workbook_path=None,
        debug_csv_path=None,
        log_path="",
        errors=(["cancelled_by_user"] if cancelled else errors)
        + [f"{s['filename']}: {s['reason']}" for s in skipped],
    )


def parse_apartments_com_invoice_text(text: str, *, source_file: str = "") -> ApartmentsComInvoice:
    normalized = _normalize_text(text)
    invoice_number = _first(r"Invoice\s+Number\s+(\d+)", normalized)
    invoice_date = _parse_date(_first(r"Invoice\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", normalized))
    due_date = _parse_date(_first(r"Due\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", normalized))
    account_number = (
        _first(r"Account\s+#/Location\s+ID\s+(\d{9,})", normalized)
        or _first(r"Account\s+#/Location\s+ID:\s*(\d{9,})", normalized)
    )
    service_period_start, service_period_end = _extract_header_service_period(normalized)
    invoice_amount = (
        money(_first(r"Invoice\s+Amount\s+USD\s+([\d,]+\.\d{2})", normalized))
        or money(_first(r"Current\s+Invoice\s+Total\s+USD\s+([\d,]+\.\d{2})", normalized))
    )
    lines = _extract_detail_lines(
        normalized,
        invoice_month=service_period_start.month if service_period_start else None,
        invoice_year=service_period_start.year if service_period_start else None,
    )
    return ApartmentsComInvoice(
        invoice_number=invoice_number,
        account_number=account_number,
        invoice_date=invoice_date,
        due_date=due_date,
        service_period_start=service_period_start,
        service_period_end=service_period_end,
        invoice_amount=invoice_amount.quantize(CENT, rounding=ROUND_HALF_UP),
        source_file=source_file,
        line_items=lines,
        debug_info={
            "source_file": source_file,
            "parser": "webapp_native_apartments_com",
            "header_account_location_id": account_number,
        },
    )


def _extract_detail_lines(
    text: str,
    *,
    invoice_month: int | None,
    invoice_year: int | None,
) -> list[ApartmentsComLine]:
    pattern = re.compile(
        rf"({PRODUCT_PATTERN})"
        r"\s+(\d{9,})"
        r"(?:\s+\S+)?"
        r"\s+(\d+)"
        r"\s+(\d{1,2}/\d{1,2}/\d{4})"
        r"\s+to\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})"
        r"\s+([\d,]+\.?\d*)"
        r"\s+([\d,]+\.?\d*)"
        r"\s+([\d,]+\.?\d*)",
        flags=re.I,
    )
    lines: list[ApartmentsComLine] = []
    for match in pattern.finditer(text):
        period_start = _parse_date(match.group(4))
        period_end = _parse_date(match.group(5))
        if (
            invoice_month
            and invoice_year
            and period_start
            and (period_start.month != invoice_month or period_start.year != invoice_year)
        ):
            continue
        site_id = match.group(2).strip()
        prop_abbr, prop_name = SITE_ID_TO_PROPERTY.get(site_id, ("", ""))
        reasons = [] if prop_abbr else [f"site_id_mapping_not_found:{site_id}"]
        lines.append(
            ApartmentsComLine(
                product=proper_case_preserve_acronyms(match.group(1).strip()),
                site_id=site_id,
                contract_number=match.group(3).strip(),
                period_start=period_start,
                period_end=period_end,
                subtotal=money(match.group(6)).quantize(CENT, rounding=ROUND_HALF_UP),
                tax=money(match.group(7)).quantize(CENT, rounding=ROUND_HALF_UP),
                amount=money(match.group(8)).quantize(CENT, rounding=ROUND_HALF_UP),
                property_abbreviation=prop_abbr,
                property_display_name=prop_name,
                manual_review_reasons=reasons,
            )
        )
    return lines


def _finalize_invoice(inv: ApartmentsComInvoice) -> None:
    reasons = inv.manual_review_reasons
    if not inv.invoice_number:
        reasons.append("invoice_number_missing")
    if not inv.invoice_date:
        reasons.append("invoice_date_missing")
    if not inv.due_date and inv.invoice_date:
        inv.due_date = inv.invoice_date + timedelta(days=30)
        reasons.append("due_date_missing_and_net30_fallback_used")
    elif not inv.due_date:
        reasons.append("due_date_missing")
    if not inv.account_number:
        reasons.append("account_number_missing")
    if not inv.service_period_start or not inv.service_period_end:
        reasons.append("service_period_missing")
    if not inv.line_items:
        reasons.append("line_items_missing_or_unreadable")
    for line in inv.line_items:
        reasons.extend(line.manual_review_reasons)
    line_total = sum((line.amount for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    if inv.invoice_amount and abs(line_total - inv.invoice_amount) > Decimal("0.02"):
        reasons.append("amount_mismatch")
    if inv.invoice_amount == Decimal("0.00"):
        inv.invoice_amount = line_total
    inv.manual_review_reasons = sorted(set(reason for reason in reasons if reason))
    inv.debug_info["line_items_total"] = str(line_total)
    inv.debug_info["source_total"] = str(inv.invoice_amount)


def _invoice_to_preview_dict(inv: ApartmentsComInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(inv)
    line_total = sum((line.amount for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    first_line = inv.line_items[0] if inv.line_items else None
    return {
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "billing_date": _fmt_iso(inv.invoice_date),
        "service_period": (
            f"{_fmt_iso(inv.service_period_start)} -> {_fmt_iso(inv.service_period_end)}"
            if inv.service_period_start and inv.service_period_end
            else ""
        ),
        "property_abbreviation": first_line.property_abbreviation if first_line else "",
        "location": "",
        "service_address": first_line.property_display_name if first_line else "",
        "total_amount": float(inv.invoice_amount or line_total),
        "line_items_total": float(line_total),
        "source_total_amount": float(inv.invoice_amount or line_total),
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": "local_webapp_link",
        "debug_info": inv.debug_info,
    }


def _rows_for_invoice(inv: ApartmentsComInvoice) -> list[dict[str, Any]]:
    vendor_name = canonical_vendor_name(
        vendor_key=VENDOR_KEY,
        aliases=[VENDOR_DISPLAY_NAME, "Apartments LLC", "CoStar"],
        fallback=VENDOR_DISPLAY_NAME,
    )
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(inv.line_items, start=1):
        invoice_desc = _invoice_description(line)
        line_desc = _line_item_description(line)
        rows.append(
            {
                "Invoice Number": inv.invoice_number,
                "Bill or Credit": "Credit" if line.amount < Decimal("0.00") else "Bill",
                "Invoice Date": _fmt_date(inv.invoice_date),
                "Accounting Date": _fmt_date(inv.invoice_date),
                "Vendor": vendor_name,
                "Invoice Description": invoice_desc,
                "Line Item Number": str(index),
                "Property Abbreviation": line.property_abbreviation,
                "Location": "",
                "GL Account": DEFAULT_GL,
                "Line Item Description": line_desc,
                "Amount": float(line.amount),
                "Expense Type": "General",
                "Is Replacement Reserve": False,
                "Payment Date": "",
                "Reference Number": line.contract_number,
                "Payment Method": "",
                "Department": "",
                "Due Date": _fmt_date(inv.due_date),
                "Quantity": 1,
                "Unit Price": float(line.amount),
                "Tax": False,
                "Received Date": _fmt_date(inv.invoice_date),
                "Document Url": "",
                "_meta": {
                    "manual_review_reasons": list(inv.manual_review_reasons),
                    "support_document_status": "local_webapp_link",
                    "source_file": inv.source_file,
                    "processor": "webapp_native_apartments_com",
                    "site_id": line.site_id,
                    "contract_number": line.contract_number,
                    "subtotal": str(line.subtotal),
                    "tax_amount": str(line.tax),
                },
            }
        )
    return rows


def _manual_review_to_dict(inv: ApartmentsComInvoice) -> dict[str, Any]:
    line_total = sum((line.amount for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    first_line = inv.line_items[0] if inv.line_items else None
    return {
        "source_file": inv.source_file,
        "vendor": VENDOR_DISPLAY_NAME,
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": first_line.property_abbreviation if first_line else "",
        "location": "",
        "total_amount": float(inv.invoice_amount),
        "line_items_total": float(line_total),
        "source_total_amount": float(inv.invoice_amount),
        "line_count": len(inv.line_items),
        "reasons": list(inv.manual_review_reasons),
    }


def _invoice_description(line: ApartmentsComLine) -> str:
    return _reference_description(line)


def _line_item_description(line: ApartmentsComLine) -> str:
    return _reference_description(line)


def _reference_description(line: ApartmentsComLine) -> str:
    period = _fmt_period(line.period_start, line.period_end)
    return " - ".join(part for part in (period, line.product) if part)


def _extract_header_service_period(text: str) -> tuple[datetime | None, datetime | None]:
    match = re.search(
        r"Service\s+Period\s+(\d{1,2}/\d{1,2}/\d{4})\s+to\s+(\d{1,2}/\d{1,2}/\d{4})",
        text,
        flags=re.I,
    )
    if not match:
        return None, None
    return _parse_date(match.group(1)), _parse_date(match.group(2))


def _progress(callback: Callable[..., None] | None, **fields: Any) -> None:
    if callback is None:
        return
    try:
        callback(**fields)
    except Exception:
        pass


def _cancelled(
    callback: Callable[[], bool] | None,
    run_context: dict[str, Any] | None = None,
) -> bool:
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


def _normalize_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\u00a0", " ")
    )


def _first(pattern: str, text: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text or "", flags)
    return match.group(1).strip() if match else ""


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


def _fmt_period(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f"{start.strftime('%m/%d/%y')}-{end.strftime('%m/%d/%y')}"
    return ""


__all__ = [
    "ApartmentsComInvoice",
    "ApartmentsComLine",
    "ProcessBatchResult",
    "parse_apartments_com_invoice_text",
    "process_apartments_com_batch",
]
