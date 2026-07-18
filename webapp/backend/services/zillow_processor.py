"""Deterministic Zillow Rentals invoice processor.

Zillow changed the PDF text layout from the older training processor:
current invoices flatten the header as "Bill to: Sold to: INV..." and
split the service period/package across several short lines. This webapp
processor parses that layout directly and intentionally does not upload to
Dropbox or write workbooks; preview/export layers own those concerns.
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

from .utility_processor_common import money

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore

_LOG = logging.getLogger(__name__)

CENT = Decimal("0.01")
VENDOR_KEY = "zillow_rentals"
VENDOR_DISPLAY_NAME = "Zillow Rentals"
DEFAULT_GL = "6335"

PROPERTY_BY_NAME: dict[str, tuple[str, str]] = {
    "admiral place": ("APA", "Admiral Place"),
    "aspen meadow": ("AMA", "Aspen Meadow"),
    "blue country apartments": ("BCA", "Blue Country Apartments"),
    "canoe creek": ("OC-CCA", "Canoe Creek"),
    "flats at lancaster": ("FAL", "Flats at Lancaster"),
    "flats at landcaster": ("FAL", "Flats at Lancaster"),
    "the flats at lancaster": ("FAL", "The Flats at Lancaster"),
    "the flats at landcaster": ("FAL", "The Flats at Lancaster"),
    "griffin gate apartments": ("GGOG", "Griffin Gate Apartments"),
    "liberty landing": ("LLA", "Liberty Landing"),
    "magnolia village": ("MVA", "Magnolia Village"),
    "oak tree apartments": ("OAKTREE", "Oak Tree Apartments"),
    "oak tree farm apartments": ("OTF", "Oak Tree Farm Apartments"),
    "park at carson": ("TPAC", "Park at Carson"),
    "pleasant view townhomes": ("PVT", "Pleasantview Village Townhomes"),
    "pleasantview townhomes": ("PVT", "Pleasantview Village Townhomes"),
    "pleasant view village townhomes": ("PVT", "Pleasantview Village Townhomes"),
    "pleasantview village townhomes": ("PVT", "Pleasantview Village Townhomes"),
    "raintree": ("TRA-Rain", "Rain Tree Apartments"),
    "rain tree": ("TRA-Rain", "Rain Tree Apartments"),
    "rain tree apartments": ("TRA-Rain", "Rain Tree Apartments"),
    "sage flats at martin": ("SAGE", "Sage Flats at Martin"),
    "the gables at red river": ("SWTG", "The Gables at Red River"),
    "the glenwood at pinson": ("TGAP", "The Glenwood at Pinson"),
    "the kensington": ("TKA", "The Kensington"),
    "the rowe at gate 1": ("TRG1", "The Rowe at Gate 1"),
    "trinity lofts": ("TLA", "Trinity Lofts"),
    "villages of autumnwood": ("VOA", "Villages of Autumnwood"),
}

SIGNATURE_PROPERTIES = {"AMA", "OC-CCA"}
PACKAGE_ORDER = ("Signature Package", "Premium Package", "Enhanced Package", "Base Package")


@dataclass
class ZillowInvoice:
    invoice_number: str = ""
    account_number: str = ""
    invoice_date: datetime | None = None
    due_date: datetime | None = None
    property_name_raw: str = ""
    property_abbreviation: str = ""
    property_display_name: str = ""
    service_period_start: datetime | None = None
    service_period_end: datetime | None = None
    package: str = ""
    amount_due: Decimal = Decimal("0.00")
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


def process_zillow_rentals_batch(
    *,
    input_folder: Path | str,
    output_folder: Path | str | None = None,
    template_path: Path | str | None = None,
    config_path: Path | str | None = None,
    run_context: dict[str, Any] | None = None,
    progress_callback: Callable[..., None] | None = None,
    should_cancel_callback: Callable[[], bool] | None = None,
) -> ProcessBatchResult:
    """Process every Zillow PDF into one ResMan row per invoice."""

    inp = Path(input_folder)
    out = Path(output_folder or inp)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    parsed: list[ZillowInvoice] = []
    skipped: list[dict[str, str]] = []
    files = sorted(
        [p for p in inp.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: p.name.lower(),
    )

    _progress(progress_callback, current_step="Reading Zillow invoice file(s)", files_total=len(files))

    for index, path in enumerate(files, start=1):
        if _cancelled(should_cancel_callback):
            break
        try:
            text, page_count = _extract_pdf_text(path)
            inv = parse_zillow_invoice_text(text, source_file=path.name, page_count=page_count)
            _finalize_invoice(inv)
            parsed.append(inv)
            _progress(
                progress_callback,
                current_file=path.name,
                current_step=f"Parsed {path.name}",
                files_done=index,
                files_total=len(files),
                invoices_created=len(parsed),
                rows_created=len(parsed),
                percent=10 + (index / max(1, len(files))) * 80,
            )
        except Exception as exc:  # pragma: no cover - defensive for operator uploads
            _LOG.exception("Zillow processor failed for %s", path)
            skipped.append({"filename": path.name, "reason": f"{type(exc).__name__}: {exc}"})
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    if _cancelled(should_cancel_callback):
        summary = _summary(files, [], skipped, cancelled=True)
        return ProcessBatchResult(
            success=False,
            return_code=130,
            summary=summary,
            invoices=[],
            manual_review_rows=[],
            errors=["cancelled_by_user"],
        )

    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.amount_due != Decimal("0.00")]
    review_json = [_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons]
    summary = _summary(files, invoices_json, skipped, manual_review_total=len(review_json))

    _progress(
        progress_callback,
        status="completed",
        percent=100.0,
        current_step="Done",
        files_done=len(files),
        files_total=len(files),
        invoices_created=len(invoices_json),
        rows_created=sum(len(inv.get("rows") or []) for inv in invoices_json),
        warnings_count=len(review_json),
    )

    return ProcessBatchResult(
        success=not errors,
        return_code=0 if not errors else 1,
        summary=summary,
        invoices=invoices_json,
        manual_review_rows=review_json,
        resman_workbook_path=None,
        manual_review_workbook_path=None,
        debug_csv_path=None,
        log_path="",
        errors=errors,
    )


def parse_zillow_invoice_text(text: str, *, source_file: str = "", page_count: int = 0) -> ZillowInvoice:
    raw = _normalize_text(text)
    recurring = _section(raw, r"Recurring\s+monthly\s+charges", r"\nTotal\b")
    invoice_date = _parse_date(_first(r"Invoice\s*date\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", raw))
    due_date = _parse_date(_first(r"Due\s*Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", raw))
    invoice_number = _first(r"\b(INV\d{6,12})\b", raw)
    if not invoice_number and re.match(r"^INV\d{6,12}$", Path(source_file).stem, re.I):
        invoice_number = Path(source_file).stem.upper()
    account_number = _first(r"Account\s*#\s*:?\s*(ZRN-[A-Z0-9-]+(?:-ZRN-[A-Z0-9-]+)?)", raw)
    amount_due = (
        money(_first(r"Amount\s+due\s+\$?(-?[\d,]+\.\d{2})", raw))
        or money(_first(r"Current\s+Invoice\s+Balance\s+\$?(-?[\d,]+\.\d{2})", raw))
        or money(_first(r"Invoice\s+amount\s*:\s*\$?(-?[\d,]+\.\d{2})", raw))
    )
    property_name = _extract_property_name(raw)
    prop_abbr, prop_display = _resolve_property(property_name)
    period_start, period_end = _extract_service_period(recurring or raw, invoice_date)
    package = _extract_package(recurring or raw)

    return ZillowInvoice(
        invoice_number=invoice_number,
        account_number=account_number,
        invoice_date=invoice_date,
        due_date=due_date,
        property_name_raw=property_name,
        property_abbreviation=prop_abbr,
        property_display_name=prop_display,
        service_period_start=period_start,
        service_period_end=period_end,
        package=package,
        amount_due=amount_due.quantize(CENT, rounding=ROUND_HALF_UP),
        source_file=source_file,
        page_count=page_count,
        debug_info={
            "source_file": source_file,
            "property_name_raw": property_name,
            "package_detected": package,
            "parser": "webapp_native_zillow",
        },
    )


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not available")
    with pdfplumber.open(path) as pdf:
        parts = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(parts), len(pdf.pages)


def _finalize_invoice(inv: ZillowInvoice) -> None:
    reasons = inv.manual_review_reasons
    if not inv.invoice_number:
        reasons.append("invoice_number_missing")
    if not inv.invoice_date:
        reasons.append("invoice_date_missing")
    if not inv.due_date and inv.invoice_date:
        inv.due_date = inv.invoice_date + timedelta(days=30)
        reasons.append("due_date_missing_and_fallback_used")
    elif not inv.due_date:
        reasons.append("due_date_missing")
    if not inv.account_number:
        reasons.append("account_number_missing")
    if not inv.property_name_raw:
        reasons.append("property_name_missing")
    if inv.property_name_raw and not inv.property_abbreviation:
        reasons.append(f"property_mapping_not_found:{inv.property_name_raw}")
    if not inv.service_period_start or not inv.service_period_end:
        _infer_service_period(inv)
        reasons.append("service_period_inferred")
    if inv.amount_due == Decimal("0.00"):
        reasons.append("amount_due_missing_or_zero")
    if not inv.package:
        inv.package = "Enhanced Package"
        reasons.append("package_inferred")

    inv.manual_review_reasons = sorted(set(reason for reason in reasons if reason))


def _infer_service_period(inv: ZillowInvoice) -> None:
    base = inv.invoice_date or datetime.now()
    start = base.replace(day=1)
    if start.month == 12:
        next_first = start.replace(year=start.year + 1, month=1, day=1)
    else:
        next_first = start.replace(month=start.month + 1, day=1)
    inv.service_period_start = start
    inv.service_period_end = next_first - timedelta(days=1)


def _invoice_to_preview_dict(inv: ZillowInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(inv)
    return {
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "billing_date": _fmt_iso(inv.invoice_date),
        "service_period": (
            f"{_fmt_iso(inv.service_period_start)} -> {_fmt_iso(inv.service_period_end)}"
            if inv.service_period_start and inv.service_period_end
            else ""
        ),
        "property_abbreviation": inv.property_abbreviation,
        "location": "",
        "service_address": inv.property_display_name or inv.property_name_raw,
        "total_amount": float(inv.amount_due),
        "line_items_total": float(inv.amount_due),
        "source_total_amount": float(inv.amount_due),
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": "local_webapp_link",
        "debug_info": inv.debug_info,
    }


def _rows_for_invoice(inv: ZillowInvoice) -> list[dict[str, Any]]:
    vendor_name = canonical_vendor_name(
        vendor_key=VENDOR_KEY,
        aliases=["Zillow", "Zillow Inc", "Zillow, Inc."],
        fallback=VENDOR_DISPLAY_NAME,
    )
    invoice_desc = _description(inv)
    return [
        {
            "Invoice Number": inv.invoice_number,
            "Bill or Credit": "Credit" if inv.amount_due < Decimal("0.00") else "Bill",
            "Invoice Date": _fmt_date(inv.invoice_date),
            "Accounting Date": _fmt_date(inv.invoice_date),
            "Vendor": vendor_name,
            "Invoice Description": invoice_desc,
            "Line Item Number": "1",
            "Property Abbreviation": inv.property_abbreviation,
            "Location": "",
            "GL Account": DEFAULT_GL,
            "Line Item Description": invoice_desc,
            "Amount": float(inv.amount_due),
            "Expense Type": "General",
            "Is Replacement Reserve": False,
            "Payment Date": "",
            "Reference Number": "",
            "Payment Method": "",
            "Department": "",
            "Due Date": _fmt_date(inv.due_date),
            "Quantity": 1,
            "Unit Price": float(inv.amount_due),
            "Tax": False,
            "Received Date": _fmt_date(inv.invoice_date),
            "Document Url": "",
            "_meta": {
                "manual_review_reasons": list(inv.manual_review_reasons),
                "support_document_status": "local_webapp_link",
                "source_file": inv.source_file,
                "processor": "webapp_native_zillow",
                "page_count": inv.page_count,
                "account_number": inv.account_number,
            },
        }
    ]


def _manual_review_to_dict(inv: ZillowInvoice) -> dict[str, Any]:
    return {
        "source_file": inv.source_file,
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": inv.property_abbreviation,
        "location": "",
        "total_amount": float(inv.amount_due),
        "line_items_total": float(inv.amount_due),
        "source_total_amount": float(inv.amount_due),
        "line_count": 1 if inv.amount_due else 0,
        "reasons": list(inv.manual_review_reasons),
    }


def _description(inv: ZillowInvoice) -> str:
    period = _fmt_period(inv.service_period_start, inv.service_period_end)
    prop = inv.property_display_name or proper_case_preserve_acronyms(inv.property_name_raw) or "Zillow Rentals"
    package = inv.package or "Enhanced Package"
    if package == "Signature Package" or inv.property_abbreviation in SIGNATURE_PROPERTIES:
        suffix = "Zillow Rentals - Signature Package"
    else:
        suffix = f"Zillow Rentals - Zillow Rent Connect: {package}"
    return " - ".join(part for part in (period, f"{prop}: {suffix}") if part)


def _extract_property_name(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if not re.search(r"\bBill\s+to\s*:", line, re.I):
            continue
        inline = re.sub(r"^.*?\bBill\s+to\s*:", "", line, flags=re.I).strip()
        inline = re.sub(r"\bSold\s+to\s*:.*$", "", inline, flags=re.I).strip()
        inline = re.sub(r"\bINV\d{6,12}\b.*$", "", inline, flags=re.I).strip()
        inline = _clean_property_header_line(inline)
        if inline and not _looks_like_header_noise(inline):
            return inline
        for candidate in lines[idx + 1 : idx + 6]:
            if re.search(
                r"^(?:Sold\s+to:|Invoice\s+date:|Account\s+#:|Recurring\s+monthly\s+charges|Product\s+Service\s+period)\b",
                candidate,
                re.I,
            ):
                break
            cleaned = _clean_property_header_line(candidate)
            if cleaned and not _looks_like_header_noise(cleaned):
                return cleaned
    match = re.search(
        r"Bill\s+to\s*:\s*Sold\s+to\s*:\s*INV\d+\s*\n(?P<line>[^\n]+)",
        text,
        flags=re.I,
    )
    if match:
        return _clean_property_header_line(match.group("line"))
    return ""


def _looks_like_header_noise(value: str) -> bool:
    text = _normalize_text(value).strip().lower()
    if not text:
        return True
    return bool(
        re.search(
            r"^(?:sold to|invoice|account|recurring monthly charges|product service period|zillow inc|email|phone)\b",
            text,
            re.I,
        )
    )


def _clean_property_header_line(line: str) -> str:
    text = _normalize_text(line)
    text = re.sub(r"\s+Invoice\s+date\s*:.*$", "", text, flags=re.I).strip()
    text = re.sub(
        r"\s+\d{2,6}[A-Za-z]?\s+[A-Za-z0-9 .'-]+(?:Street|St|Road|Rd|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Court|Ct)\b.*$",
        "",
        text,
        flags=re.I,
    ).strip()
    return proper_case_preserve_acronyms(text)


def _resolve_property(name: str) -> tuple[str, str]:
    key = _property_key(name)
    if key in PROPERTY_BY_NAME:
        return PROPERTY_BY_NAME[key]
    compact = re.sub(r"\b(apartments?|apts?)\b", "", key).strip()
    if compact in PROPERTY_BY_NAME:
        return PROPERTY_BY_NAME[compact]
    return "", proper_case_preserve_acronyms(name)


def _property_key(value: str) -> str:
    text = _normalize_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_service_period(text: str, invoice_date: datetime | None) -> tuple[datetime | None, datetime | None]:
    dates = [_parse_date(value) for value in re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)]
    dates = [d for d in dates if d is not None]
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1 and invoice_date:
        return dates[0], invoice_date
    return None, None


def _extract_package(text: str) -> str:
    clean = _normalize_text(text)
    for package in PACKAGE_ORDER:
        if re.search(re.escape(package), clean, flags=re.I):
            return package
    if re.search(r"\bSignature\b", clean, flags=re.I) and re.search(r"\bPackage\b", clean, flags=re.I):
        return "Signature Package"
    if re.search(r"\bPremium\b", clean, flags=re.I):
        return "Premium Package"
    if re.search(r"\bEnhanced\b", clean, flags=re.I):
        return "Enhanced Package"
    if re.search(r"\bBase\b", clean, flags=re.I):
        return "Base Package"
    return ""


def _section(text: str, start_pattern: str, end_pattern: str) -> str:
    match = re.search(f"({start_pattern})(?P<body>.*?){end_pattern}", text, flags=re.I | re.S)
    return match.group("body") if match else ""


def _summary(
    files: list[Path],
    invoices: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    *,
    manual_review_total: int = 0,
    cancelled: bool = False,
) -> dict[str, Any]:
    rows_total = sum(len(inv.get("rows") or []) for inv in invoices)
    return {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": VENDOR_KEY,
        "processing_mode": "deterministic",
        "files_total": len(files),
        "files_processed": len(invoices),
        "files_skipped_unparseable": len(skipped),
        "invoices_produced": len(invoices),
        "rows_total": rows_total,
        "manual_review_total": manual_review_total,
        "dropbox_called": False,
        "cancelled": cancelled,
    }


def _progress(callback: Callable[..., None] | None, **fields: Any) -> None:
    if callback is None:
        return
    try:
        callback(**fields)
    except Exception:
        pass


def _cancelled(callback: Callable[[], bool] | None) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
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
    "ProcessBatchResult",
    "ZillowInvoice",
    "parse_zillow_invoice_text",
    "process_zillow_rentals_batch",
]
