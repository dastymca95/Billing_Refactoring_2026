"""Deterministic Granite Telecommunications invoice processor.

Granite sends one multi-page statement per property/account. The first page
contains the account-level balance, while later pages identify the location,
service period, and charge detail. Historical ResMan evidence posts Granite
to GL 6178 (Telephone) as one net line per account.
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
from utils.text_normalization import proper_case_preserve_acronyms

from .utility_processor_common import money

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore


_LOG = logging.getLogger(__name__)

CENT = Decimal("0.01")
VENDOR_KEY = "granite_telecommunications_llc"
VENDOR_DISPLAY_NAME = "Granite Telecommunications, LLC"
DEFAULT_GL = "6178"

# Account number is Granite's stable property identifier and is more reliable
# than the occasionally abbreviated location label printed on the statement.
ACCOUNT_PROPERTY_MAP: dict[str, tuple[str, str]] = {
    "05798620": ("APA", "Admiral Place Apartments"),
    "05798621": ("AMA", "Aspen Meadows Apartments"),
    "05798622": ("BCA", "Blue Country Apartments"),
    "05798623": ("OC-CCA", "Canoe Creek"),
    "05798624": ("GGOG", "Griffin Gate Apartments"),
    "05798625": ("THSA", "Harmony Square Townhomes"),
    "05798626": ("LLA", "Liberty Landing"),
    "05798627": ("MVA", "Magnolia Village Apartments"),
    "05798628": ("OAKTREE", "Oak Tree Apartments"),
    "05798630": ("RCC", "River Canyon"),
    "05798631": ("SAGE", "Sage Flats"),
    "05798633": ("Adelade", "The Adelade"),
    "05798635": ("TFF", "The Firefly"),
    "05798636": ("TGAP", "The Glenwood at Pinson"),
    "05798637": ("OG-PPA", "The Oakley at Pro Park"),
    "05798638": ("TPAC", "The Park at Carson"),
    "05798639": ("TPW", "The Penn Warren"),
    "05798640": ("TRA-Rain", "The Raintree Apartments"),
    "05798641": ("TRG1", "The Rowe at Gate 1"),
    "05798642": ("VILLASPV", "The Villas of Pine Valley"),
    "05798643": ("TVUGDG", "The Villas UGDG"),
    "05798644": ("VOA", "Villages of Autumnwood"),
    "05798645": ("TEC", "Next-Gen Office"),
    "05811929": ("TEC", "2833 Cobalt Dr"),
    "05827752": ("OTF", "Oak Tree Farms"),
    "05870897": ("TKA", "The Kensington"),
    "05870916": ("TLA", "Trinity Lofts"),
    "05914590": ("FAL", "Flats at Lancaster"),
}

LOCATION_PROPERTY_MAP: dict[str, tuple[str, str]] = {
    "admiral place apartments": ("APA", "Admiral Place Apartments"),
    "aspen meadows apartments": ("AMA", "Aspen Meadows Apartments"),
    "blue country apartments": ("BCA", "Blue Country Apartments"),
    "canoe creek": ("OC-CCA", "Canoe Creek"),
    "griffin gate apartments og": ("GGOG", "Griffin Gate Apartments"),
    "harmony square townhomes": ("THSA", "Harmony Square Townhomes"),
    "liberty landings": ("LLA", "Liberty Landing"),
    "magnolia village apartments": ("MVA", "Magnolia Village Apartments"),
    "oak tree apartments": ("OAKTREE", "Oak Tree Apartments"),
    "river canyon": ("RCC", "River Canyon"),
    "sage flats": ("SAGE", "Sage Flats"),
    "the adelade": ("Adelade", "The Adelade"),
    "the fire fly": ("TFF", "The Firefly"),
    "the firefly": ("TFF", "The Firefly"),
    "the glenwood at pinson": ("TGAP", "The Glenwood at Pinson"),
    "the oakly at pro park": ("OG-PPA", "The Oakley at Pro Park"),
    "the oakley at pro park": ("OG-PPA", "The Oakley at Pro Park"),
    "the park at carson": ("TPAC", "The Park at Carson"),
    "the penn warren": ("TPW", "The Penn Warren"),
    "the raintree apartments": ("TRA-Rain", "The Raintree Apartments"),
    "the rowe at gate 1 jack miller": ("TRG1", "The Rowe at Gate 1"),
    "the villas of pine valley": ("VILLASPV", "The Villas of Pine Valley"),
    "the villas ugdg": ("TVUGDG", "The Villas UGDG"),
    "villages of autumnwood": ("VOA", "Villages of Autumnwood"),
    "next gen office": ("TEC", "Next-Gen Office"),
    "2833 cobalt dr": ("TEC", "2833 Cobalt Dr"),
    "oak tree farms": ("OTF", "Oak Tree Farms"),
    "the kensington": ("TKA", "The Kensington"),
    "trinity lofts": ("TLA", "Trinity Lofts"),
    "375 s lancaster rd": ("FAL", "Flats at Lancaster"),
    "flats at lancaster": ("FAL", "Flats at Lancaster"),
}


@dataclass
class GraniteInvoice:
    invoice_number: str = ""
    account_number: str = ""
    invoice_date: datetime | None = None
    due_date: datetime | None = None
    service_period_start: datetime | None = None
    service_period_end: datetime | None = None
    location_raw: str = ""
    service_address: str = ""
    property_abbreviation: str = ""
    property_display_name: str = ""
    current_charges: Decimal = Decimal("0.00")
    adjustments: Decimal = Decimal("0.00")
    total_amount_due: Decimal = Decimal("0.00")
    amount_to_post: Decimal = Decimal("0.00")
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


def process_granite_telecommunications_batch(
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
    files = sorted(inp.glob("*.pdf"), key=lambda path: path.name.lower())
    errors: list[str] = []
    parsed: list[GraniteInvoice] = []

    _progress(progress_callback, current_step="Reading Granite invoice file(s)", files_total=len(files))
    for index, path in enumerate(files, start=1):
        if _cancelled(should_cancel_callback, run_context):
            break
        try:
            text, page_count = _extract_pdf_text(path)
            invoice = parse_granite_invoice_text(text, source_file=path.name, page_count=page_count)
            _finalize_invoice(invoice)
            parsed.append(invoice)
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
        except Exception as exc:  # pragma: no cover - operator upload defense
            _LOG.exception("Granite processor failed for %s", path)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    cancelled = _cancelled(should_cancel_callback, run_context)
    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.amount_to_post != Decimal("0.00")]
    review_json = [_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons]
    summary = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": VENDOR_KEY,
        "processing_mode": "deterministic",
        "files_total": len(files),
        "files_processed": len(parsed),
        "files_skipped_unparseable": len(errors),
        "invoices_produced": len(invoices_json),
        "rows_total": len(invoices_json),
        "manual_review_total": len(review_json),
        "dropbox_called": False,
        "cancelled": cancelled,
        "amount_total": float(sum((inv.amount_to_post for inv in parsed), Decimal("0.00"))),
        "source_logic": "granite_account_property_map_and_net_current_charges",
    }
    _progress(
        progress_callback,
        status="completed",
        percent=100.0,
        current_step="Done",
        files_done=len(parsed),
        files_total=len(files),
        invoices_created=len(invoices_json),
        rows_created=len(invoices_json),
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


def parse_granite_invoice_text(
    text: str,
    *,
    source_file: str = "",
    page_count: int = 0,
) -> GraniteInvoice:
    normalized = _normalize_text(text)
    account_number = _first(r"ACCOUNT\s+NUMBER:\s*(\d{8})", normalized)
    invoice_number = (
        _first(r"INVOICE\s+NUMBER:\s*(\d{6,})", normalized)
        or _first(r"\bInvoice:\s*(\d{6,})\b", normalized)
    )
    invoice_date = _parse_date(_first(r"INVOICE\s+DATE:\s*(\d{1,2}/\d{1,2}/\d{2,4})", normalized))
    location_raw = _first(r"Location\s*:\s*([^\n]+)", normalized)
    service_address = _first(r"Location\s*:\s*[^\n]+\n([^\n]+)", normalized)
    current_charges = _extract_money_after_label(
        normalized,
        r"CURRENT\s+CHARGES,\s*TAXES,\s*SURCHARGES:",
    )
    adjustments = _extract_money_after_label(normalized, r"ADJUSTMENTS:")
    total_amount_due = _extract_money_after_label(normalized, r"TOTAL\s+AMOUNT\s+DUE:")
    period_start, period_end = _extract_service_period(normalized)
    property_abbreviation, property_display_name = _resolve_property(
        account_number=account_number,
        location=location_raw,
        service_address=service_address,
    )
    amount_to_post = (current_charges + adjustments).quantize(CENT, rounding=ROUND_HALF_UP)

    return GraniteInvoice(
        invoice_number=invoice_number,
        account_number=account_number,
        invoice_date=invoice_date,
        due_date=invoice_date,
        service_period_start=period_start,
        service_period_end=period_end,
        location_raw=location_raw,
        service_address=service_address.replace(" | ", ", "),
        property_abbreviation=property_abbreviation,
        property_display_name=property_display_name,
        current_charges=current_charges,
        adjustments=adjustments,
        total_amount_due=total_amount_due,
        amount_to_post=amount_to_post,
        source_file=source_file,
        page_count=page_count,
        debug_info={
            "source_file": source_file,
            "parser": "webapp_native_granite_telecommunications",
            "location_raw": location_raw,
            "current_charges": float(current_charges),
            "adjustments": float(adjustments),
            "printed_total_amount_due": float(total_amount_due),
            "amount_basis": "current_charges_plus_adjustments_excluding_previous_balance_and_payments",
        },
    )


def _finalize_invoice(inv: GraniteInvoice) -> None:
    reasons = inv.manual_review_reasons
    if not inv.invoice_number:
        reasons.append("invoice_number_missing")
    if not inv.account_number:
        reasons.append("account_number_missing")
    if not inv.invoice_date:
        reasons.append("invoice_date_missing")
    if not inv.property_abbreviation:
        reasons.append(f"property_mapping_not_found:{inv.location_raw or inv.service_address}")
    if not inv.service_period_start or not inv.service_period_end:
        reasons.append("service_period_missing")
    if inv.current_charges == Decimal("0.00"):
        reasons.append("current_charges_missing_or_zero")
    if inv.amount_to_post == Decimal("0.00"):
        reasons.append("amount_to_post_missing_or_zero")
    if (
        inv.total_amount_due != Decimal("0.00")
        and abs(inv.total_amount_due - inv.amount_to_post) > Decimal("0.02")
    ):
        reasons.append(
            f"net_current_charges_mismatch:post={inv.amount_to_post}:printed_due={inv.total_amount_due}"
        )
    inv.manual_review_reasons = sorted(set(reason for reason in reasons if reason))


def _invoice_to_preview_dict(inv: GraniteInvoice) -> dict[str, Any]:
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
        "service_address": inv.service_address,
        "total_amount": float(inv.amount_to_post),
        "line_items_total": float(inv.amount_to_post),
        "source_total_amount": float(inv.amount_to_post),
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": "local_webapp_link",
        "debug_info": inv.debug_info,
    }


def _rows_for_invoice(inv: GraniteInvoice) -> list[dict[str, Any]]:
    vendor_name = canonical_vendor_name(
        vendor_key=VENDOR_KEY,
        aliases=["Granite Telecommunications", "Granite Telecommunications LLC", "grtel"],
        fallback=VENDOR_DISPLAY_NAME,
    )
    description = _description(inv)
    return [
        {
            "Invoice Number": inv.invoice_number,
            "Bill or Credit": "Credit" if inv.amount_to_post < Decimal("0.00") else "Bill",
            "Invoice Date": _fmt_date(inv.invoice_date),
            "Accounting Date": _fmt_date(inv.invoice_date),
            "Vendor": vendor_name,
            "Invoice Description": description,
            "Line Item Number": "1",
            "Property Abbreviation": inv.property_abbreviation,
            "Location": "",
            "GL Account": DEFAULT_GL,
            "Line Item Description": description,
            "Amount": float(inv.amount_to_post),
            "Expense Type": "General",
            "Is Replacement Reserve": False,
            "Payment Date": "",
            "Reference Number": inv.account_number,
            "Payment Method": "",
            "Department": "",
            "Due Date": _fmt_date(inv.due_date),
            "Quantity": 1,
            "Unit Price": float(inv.amount_to_post),
            "Tax": False,
            "Received Date": _fmt_date(inv.invoice_date),
            "Document Url": "",
            "_meta": {
                "manual_review_reasons": list(inv.manual_review_reasons),
                "support_document_status": "local_webapp_link",
                "source_file": inv.source_file,
                "processor": "webapp_native_granite_telecommunications",
                "page_count": inv.page_count,
                "account_number": inv.account_number,
            },
        }
    ]


def _manual_review_to_dict(inv: GraniteInvoice) -> dict[str, Any]:
    return {
        "source_file": inv.source_file,
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": inv.property_abbreviation,
        "location": "",
        "total_amount": float(inv.amount_to_post),
        "line_items_total": float(inv.amount_to_post),
        "source_total_amount": float(inv.amount_to_post),
        "line_count": 1 if inv.amount_to_post else 0,
        "reasons": list(inv.manual_review_reasons),
    }


def _description(inv: GraniteInvoice) -> str:
    period = _fmt_period(inv.service_period_start, inv.service_period_end)
    detail = f"Phone Service - Account {inv.account_number}".strip()
    return " - ".join(part for part in (period, detail) if part)


def _resolve_property(
    *,
    account_number: str,
    location: str,
    service_address: str,
) -> tuple[str, str]:
    if account_number in ACCOUNT_PROPERTY_MAP:
        return ACCOUNT_PROPERTY_MAP[account_number]
    for value in (location, service_address.split("|", 1)[0]):
        key = _property_key(value)
        if key in LOCATION_PROPERTY_MAP:
            return LOCATION_PROPERTY_MAP[key]
    return "", proper_case_preserve_acronyms(location)


def _extract_service_period(text: str) -> tuple[datetime | None, datetime | None]:
    matches = re.findall(
        r"\b(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}/\d{1,2}/\d{2,4})\b",
        text,
    )
    for start_raw, end_raw in matches:
        start = _parse_date(start_raw)
        end = _parse_date(end_raw)
        if start and end and end >= start and (end - start).days <= 62:
            return start, end
    return None, None


def _extract_money_after_label(text: str, label_pattern: str) -> Decimal:
    raw = _first(
        rf"{label_pattern}\s*(?P<amount>-?\s*\$?\s*[\d,]+\.\d{{2}})",
        text,
    )
    return money(re.sub(r"\s+", "", raw)).quantize(CENT, rounding=ROUND_HALF_UP)


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not available")
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages), len(pdf.pages)


def _cancelled(
    callback: Callable[[], bool] | None,
    run_context: dict[str, Any] | None,
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


def _progress(callback: Callable[..., None] | None, **fields: Any) -> None:
    if callback is None:
        return
    try:
        callback(**fields)
    except Exception:
        pass


def _property_key(value: str) -> str:
    text = _normalize_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00a0", " ")
    )


def _first(pattern: str, text: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text or "", flags)
    if not match:
        return ""
    if "amount" in match.groupdict():
        return str(match.group("amount") or "").strip()
    return match.group(1).strip()


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
    "ACCOUNT_PROPERTY_MAP",
    "GraniteInvoice",
    "LOCATION_PROPERTY_MAP",
    "ProcessBatchResult",
    "parse_granite_invoice_text",
    "process_granite_telecommunications_batch",
]
