"""Deterministic ResMan, LLC invoice processor.

This processor ports the useful accounting rules from the old ResMan script
into the current web console pipeline. It intentionally does not upload to
Dropbox or write a workbook; the webapp export layer owns those concerns.
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

from .document_ingestion import ingest_document
from .utility_processor_common import load_chart_of_accounts, money

_LOG = logging.getLogger(__name__)

CENT = Decimal("0.01")
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
VENDOR_KEY = "resman_llc"
VENDOR_DISPLAY_NAME = "ResMan, LLC"


CUSTOMER_PROPERTY_MAP: dict[str, str] = {
    "RSM-000023758-000070723": "Adelade",
    "RSM-000023758-000044528": "APA",
    "RSM-000023758-000040739": "BCA",
    "RSM-000023758-000063445": "COS",
    "RSM-000023758-000065160": "GGOG",
    "RSM-000023758-000033136": "LLA",
    "RSM-000023758-000059320": "MVA",
    "RSM-000023758-000058678": "OAKTREE",
    "RSM-000023758-000073139": "OC-CCA",
    "RSM-000023758-000073145": "OG-PPA",
    "RSM-000023758-000071374": "OTF",
    "RSM-000023758-000076233": "RCC",
    "RSM-000023758-000045033": "SAGE",
    "RSM-000023758-000072594": "SWTG",
    "RSM-000023758-000074218": "TFF",
    "RSM-000023758-000072513": "TGAP",
    "RSM-000023758-000074591": "TPAC",
    "RSM-000023758-000075249": "TPW",
    "RSM-000023758-000072514": "TRA-Rain",
    "RSM-000023758-000055642": "TRG1",
    "RSM-000023758-000050684": "TVUGDG",
    "RSM-000023758-000044523": "UGC",
    "RSM-000023758-000077520": "VILLASPV",
    "RSM-000023758-000074590": "VOA",
    "RSM-000023758-000077521": "THSA",
    "RSM-000023758-000042687": "AMA",
    "RSM-000023758-000079440": "PVT",
    "RSM-000023758-000079286": "TEC",
    "RSM-000023758-000079725": "TKA",
    "RSM-000023758-000079726": "TLA",
    "RSM-000023758-000079944": "FAL",
    "RSM-000023758-000000000": "NGM",
}

PROPERTY_TEXT_MAP: tuple[tuple[str, str], ...] = (
    ("the flats at lancaster", "FAL"),
    ("flats at lancaster", "FAL"),
    ("the kensington", "TKA"),
    ("trinity lofts", "TLA"),
    ("nex gen management", "NGM"),
)

GL_6315_ITEMS = {
    "Websites Monthly Subscription",
    "Website Package",
}
GL_6115_ITEMS = {
    "ResMan Qualifier",
    "Credit Builder",
    "Qualifier_FACTA",
}
GL_6115_KEYWORDS = (
    "credit",
    "screening",
    "transunion",
    "smartmove",
    "qualifier",
    "facta",
)
KNOWN_ITEM_NAMES = (
    "Websites Monthly Subscription",
    "Website Package",
    "ResMan Conventional Monthly Service",
    "ResMan Leasing Pro: BlueMoon 2.0",
    "ResMan Leasing Pro Flex - Monthly",
    "ResMan Qualifier",
    "Credit Builder",
    "Qualifier_FACTA",
)


@dataclass
class ResManLineItem:
    item_name: str
    qty: int
    rate: Decimal
    amount: Decimal
    taxable: bool
    period_start: datetime | None = None
    period_end: datetime | None = None
    gl_account: str = ""
    tax_allocated: Decimal = Decimal("0.00")

    @property
    def amount_with_tax(self) -> Decimal:
        return (self.amount + self.tax_allocated).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class ParsedResManInvoice:
    invoice_number: str
    invoice_date: datetime | None
    due_date: datetime | None
    customer_number: str
    property_abbreviation: str
    amount_due: Decimal
    tax_total: Decimal
    invoice_type: str
    source_file: str
    line_items: list[ResManLineItem] = field(default_factory=list)
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


def process_resman_llc_batch(
    *,
    input_folder: Path | str | None = None,
    output_folder: Path | str | None = None,
    template_path: Path | str | None = None,
    config_path: Path | str | None = None,
    run_context: dict[str, Any] | None = None,
    progress_callback: Callable[..., None] | None = None,
    should_cancel_callback: Callable[[], bool] | None = None,
) -> ProcessBatchResult:
    """Process one ResMan batch into canonical web preview invoices."""

    inp = Path(input_folder or ".")
    out = Path(output_folder or inp)
    out.mkdir(parents=True, exist_ok=True)
    run_context = run_context or {}
    dry_run = bool(run_context.get("dry_run"))
    timestamp = str(run_context.get("timestamp") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    valid_gls = load_chart_of_accounts()
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    parsed: list[ParsedResManInvoice] = []

    def cancelled() -> bool:
        if should_cancel_callback is not None:
            try:
                return bool(should_cancel_callback())
            except Exception:
                return False
        hook = run_context.get("should_cancel")
        if callable(hook):
            try:
                return bool(hook())
            except Exception:
                return False
        return False

    files = sorted([p for p in inp.iterdir() if p.is_file()], key=lambda p: p.name.lower())
    _progress(progress_callback, current_step="Reading ResMan invoice file(s)", files_total=len(files))

    for index, path in enumerate(files, start=1):
        if cancelled():
            break
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append({"filename": path.name, "reason": f"unsupported_extension:{path.suffix.lower()}"})
            continue
        try:
            candidate = ingest_document(path, vendor_hint=VENDOR_DISPLAY_NAME)
            inv = parse_resman_invoice_text(candidate.document_text or "", source_file=path.name)
            _finalize_invoice(inv, valid_gls=valid_gls)
            parsed.append(inv)
            _progress(
                progress_callback,
                current_step=f"Parsed {path.name}",
                files_done=index,
                invoices_created=len(parsed),
                rows_created=sum(len(i.line_items) for i in parsed),
            )
        except Exception as exc:  # pragma: no cover - defensive for operator uploads
            _LOG.exception("ResMan processor failed for %s", path)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.line_items]
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
        "source_logic": "ported_from_old_resman_script",
    }

    _progress(
        progress_callback,
        status="completed",
        percent=100.0,
        current_step="Done",
        files_done=len(files),
        invoices_created=len(invoices_json),
        rows_created=row_count,
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
        errors=errors + [f"{s['filename']}: {s['reason']}" for s in skipped],
    )


def parse_resman_invoice_text(text: str, *, source_file: str = "") -> ParsedResManInvoice:
    normalized = _normalize_text(text)
    invoice_type = _detect_invoice_type(normalized)
    invoice_number = _first(r"Invoice\s*#[:\s]+(RSM\d+)", normalized)
    if not invoice_number:
        invoice_number = _first(r"#\s*(RSM\d+)", normalized)
    invoice_date = _parse_date(
        _first(r"Invoice\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})", normalized)
        or _first(r"#\s*RSM\d+\s+(\d{1,2}/\d{1,2}/\d{4})", normalized)
        or _first(r"Order\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})", normalized)
    )
    due_date = _parse_date(
        _first(r"Net\s*\d+\s+(\d{1,2}/\d{1,2}/\d{4})", normalized)
        or _first(r"Due\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})", normalized)
    )
    customer_number = _first(r"(RSM-\d{9}-\d{9})", normalized)
    amount_due = money(_first(r"Invoice\s+Amount\s+Due\s+\$?([\d,]+\.\d{2})", normalized))
    if amount_due == Decimal("0.00"):
        amount_due = money(_first(r"Amount\s+Due\s+\$?([\d,]+\.\d{2})", normalized))
    tax_total = money(_first(r"Tax\s+Total\s+\$?([\d,]+\.\d{2})", normalized))
    line_items = _extract_line_items(normalized)

    return ParsedResManInvoice(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        customer_number=customer_number,
        property_abbreviation=(
            CUSTOMER_PROPERTY_MAP.get(customer_number, "")
            or _property_from_invoice_text(normalized)
        ),
        amount_due=amount_due,
        tax_total=tax_total,
        invoice_type=invoice_type,
        source_file=source_file,
        line_items=line_items,
        debug_info={
            "source_file": source_file,
            "customer_number": customer_number,
            "invoice_type": invoice_type,
            "source_total": str(amount_due),
            "tax_total": str(tax_total),
        },
    )


def _finalize_invoice(inv: ParsedResManInvoice, *, valid_gls: dict[str, str]) -> None:
    reasons = inv.manual_review_reasons
    if not inv.invoice_number:
        reasons.append("invoice_number_missing")
    if not inv.invoice_date:
        reasons.append("invoice_date_missing")
    if not inv.due_date:
        reasons.append("due_date_missing")
    if not inv.customer_number:
        reasons.append("customer_number_missing")
    if inv.customer_number and not inv.property_abbreviation:
        reasons.append(f"unknown_resman_customer_number:{inv.customer_number}")
    if inv.amount_due == Decimal("0.00"):
        reasons.append("invoice_amount_due_missing_or_zero")

    non_zero_lines: list[ResManLineItem] = []
    skipped_zero = 0
    for line in inv.line_items:
        line.gl_account = _gl_for_item(line.item_name)
        if line.amount == Decimal("0.00"):
            skipped_zero += 1
            continue
        if valid_gls and line.gl_account not in valid_gls:
            reasons.append(f"gl_code_not_found:{line.gl_account}")
        non_zero_lines.append(line)
    inv.line_items = non_zero_lines
    if skipped_zero:
        inv.debug_info["zero_amount_lines_skipped"] = skipped_zero
    if not inv.line_items:
        reasons.append("no_payable_line_items_found")

    _allocate_tax(inv)
    line_total = sum((line.amount_with_tax for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    delta = (line_total - inv.amount_due).copy_abs().quantize(CENT)
    inv.debug_info["line_items_total"] = str(line_total)
    inv.debug_info["reconciliation"] = {
        "source_total": str(inv.amount_due),
        "line_items_total": str(line_total),
        "delta": str(delta),
        "tolerance": "0.05",
    }
    if inv.amount_due and delta > Decimal("0.05"):
        reasons.append(f"amount_mismatch:lines_{line_total}_vs_invoice_{inv.amount_due}")
    inv.manual_review_reasons = sorted(set(r for r in reasons if r))


def _extract_line_items(text: str) -> list[ResManLineItem]:
    items: list[ResManLineItem] = []
    lines = [line.strip() for line in text.splitlines()]
    data_with_period = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})(\d+)"
        r"\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d{2})\s+(T|NT)\b",
        re.I,
    )
    item_line = re.compile(
        r"^(?P<name>.+?)\s+(?P<qty>\d+)\s+\$(?P<rate>[\d,]+\.\d+)"
        r"\s+\$(?P<amount>[\d,]+\.\d{2})\s+(?P<tax>T|NT)\b",
        re.I,
    )
    period_re = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})")

    for i, line in enumerate(lines):
        old_match = data_with_period.search(line)
        if old_match:
            name = _find_item_name_before(lines, i)
            if name:
                items.append(
                    ResManLineItem(
                        item_name=name,
                        qty=int(old_match.group(3)),
                        rate=money(old_match.group(4)),
                        amount=money(old_match.group(5)),
                        taxable=old_match.group(6).upper() == "T",
                        period_start=_parse_date(old_match.group(1)),
                        period_end=_parse_date(old_match.group(2)),
                    )
                )
            continue

        match = item_line.search(line)
        if not match:
            continue
        raw_name = _normalize_item_name(match.group("name"))
        if _looks_like_non_item_name(raw_name):
            continue
        period_start: datetime | None = None
        period_end: datetime | None = None
        for lookahead in lines[i + 1 : i + 5]:
            period_match = period_re.search(lookahead)
            if period_match:
                period_start = _parse_date(period_match.group(1))
                period_end = _parse_date(period_match.group(2))
                break
        items.append(
            ResManLineItem(
                item_name=raw_name,
                qty=int(match.group("qty")),
                rate=money(match.group("rate")),
                amount=money(match.group("amount")),
                taxable=match.group("tax").upper() == "T",
                period_start=period_start,
                period_end=period_end,
            )
        )
    return items


def _allocate_tax(inv: ParsedResManInvoice) -> None:
    tax_total = inv.tax_total.quantize(CENT, rounding=ROUND_HALF_UP)
    if not inv.line_items:
        return
    if tax_total == Decimal("0.00"):
        return
    taxable_lines = [line for line in inv.line_items if line.taxable]
    taxable_subtotal = sum((line.amount for line in taxable_lines), Decimal("0.00"))
    if taxable_subtotal == Decimal("0.00"):
        inv.manual_review_reasons.append("tax_total_present_but_no_taxable_lines")
        return
    allocated = Decimal("0.00")
    for line in taxable_lines[:-1]:
        share = (tax_total * line.amount / taxable_subtotal).quantize(CENT, rounding=ROUND_HALF_UP)
        line.tax_allocated = share
        allocated += share
    taxable_lines[-1].tax_allocated = (tax_total - allocated).quantize(CENT, rounding=ROUND_HALF_UP)


def _invoice_to_preview_dict(inv: ParsedResManInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(inv)
    line_total = sum((line.amount_with_tax for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    return {
        "account_number": inv.customer_number,
        "invoice_number": inv.invoice_number,
        "billing_date": _fmt_iso(inv.invoice_date),
        "service_period": _invoice_service_period(inv),
        "property_abbreviation": inv.property_abbreviation,
        "location": "",
        "service_address": "",
        "total_amount": float(inv.amount_due or line_total),
        "line_items_total": float(line_total),
        "source_total_amount": float(inv.amount_due) if inv.amount_due else None,
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": "local_webapp_link",
        "debug_info": inv.debug_info,
    }


def _rows_for_invoice(inv: ParsedResManInvoice) -> list[dict[str, Any]]:
    vendor_name = canonical_vendor_name(
        vendor_key=VENDOR_KEY,
        aliases=["ResMan", "Resman LLC", "ResMan LLC"],
        fallback=VENDOR_DISPLAY_NAME,
    )
    invoice_desc = _invoice_description(inv)
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(inv.line_items, start=1):
        line_desc = _line_description(line)
        rows.append(
            {
                "Invoice Number": inv.invoice_number,
                "Bill or Credit": "Bill",
                "Invoice Date": _fmt_date(inv.invoice_date),
                "Accounting Date": _fmt_date(inv.invoice_date),
                "Vendor": vendor_name,
                "Invoice Description": invoice_desc,
                "Line Item Number": str(idx),
                "Property Abbreviation": inv.property_abbreviation,
                "Location": "",
                "GL Account": line.gl_account,
                "Line Item Description": line_desc,
                "Amount": float(line.amount_with_tax),
                "Expense Type": "General",
                "Is Replacement Reserve": False,
                "Payment Date": "",
                "Reference Number": "",
                "Payment Method": "",
                "Department": "",
                "Due Date": _fmt_date(inv.due_date),
                "Quantity": "",
                "Unit Price": "",
                "Tax": "",
                "Received Date": "",
                "Document Url": "",
                "_meta": {
                    "manual_review_reasons": list(inv.manual_review_reasons),
                    "support_document_status": "local_webapp_link",
                    "source_file": inv.source_file,
                    "resman_processor": "deterministic",
                    "invoice_type": inv.invoice_type,
                    "customer_number": inv.customer_number,
                    "tax_allocated": str(line.tax_allocated),
                    "base_amount": str(line.amount),
                },
            }
        )
    return rows


def _manual_review_to_dict(inv: ParsedResManInvoice) -> dict[str, Any]:
    line_total = sum((line.amount_with_tax for line in inv.line_items), Decimal("0.00")).quantize(CENT)
    return {
        "source_file": inv.source_file,
        "account_number": inv.customer_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": inv.property_abbreviation,
        "location": "",
        "total_amount": float(inv.amount_due or line_total),
        "line_items_total": float(line_total),
        "source_total_amount": float(inv.amount_due) if inv.amount_due else None,
        "line_count": len(inv.line_items),
        "reasons": list(inv.manual_review_reasons),
        "reconciliation": inv.debug_info.get("reconciliation"),
    }


def _detect_invoice_type(text: str) -> str:
    hay = text.lower()
    if any(item.lower() in hay for item in GL_6115_ITEMS):
        return "B"
    return "A"


def _gl_for_item(item_name: str) -> str:
    normalized = _normalize_item_name(item_name)
    if normalized in GL_6115_ITEMS:
        return "6115"
    if any(keyword in normalized.lower() for keyword in GL_6115_KEYWORDS):
        return "6115"
    if normalized in GL_6315_ITEMS:
        return "6315"
    return "6136"


def _invoice_description(inv: ParsedResManInvoice) -> str:
    lines_with_period = [line for line in inv.line_items if line.period_start and line.period_end]
    if not lines_with_period:
        return (
            "Monthly ResMan Software & Compliance Services"
            if inv.invoice_type == "A"
            else "Monthly ResMan Credit & Screening Services"
        )
    line = (
        lines_with_period[0]
        if inv.invoice_type == "A"
        else max(lines_with_period, key=lambda item: item.period_start or datetime.min)
    )
    prefix = _fmt_period(line.period_start, line.period_end)
    suffix = (
        "Monthly ResMan Software & Compliance Services"
        if inv.invoice_type == "A"
        else "Monthly ResMan Credit & Screening Services"
    )
    return f"{prefix} - {suffix}"


def _line_description(line: ResManLineItem) -> str:
    if line.period_start and line.period_end:
        return f"{_fmt_period(line.period_start, line.period_end)} - {line.item_name}"
    return line.item_name


def _invoice_service_period(inv: ParsedResManInvoice) -> str:
    starts = [line.period_start for line in inv.line_items if line.period_start]
    ends = [line.period_end for line in inv.line_items if line.period_end]
    if not starts and not ends:
        return ""
    return f"{_fmt_iso(min(starts))} -> {_fmt_iso(max(ends))}"


def _property_from_invoice_text(text: str) -> str:
    normalized = " ".join(str(text or "").lower().split())
    for property_name, abbreviation in PROPERTY_TEXT_MAP:
        if property_name in normalized:
            return abbreviation
    return ""


def _find_item_name_before(lines: list[str], index: int) -> str:
    fallback: list[str] = []
    for candidate in lines[max(0, index - 5) : index][::-1]:
        normalized = _normalize_item_name(candidate)
        if not normalized:
            continue
        for known in KNOWN_ITEM_NAMES:
            if normalized == known or known.lower() in normalized.lower():
                return known
        if not re.match(r"^\d{1,2}/\d{1,2}/\d{4}", normalized) and not _looks_like_non_item_name(normalized):
            fallback.append(normalized)
    return fallback[0] if fallback else ""


def _normalize_item_name(value: str) -> str:
    text = _normalize_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    for known in KNOWN_ITEM_NAMES:
        if text.lower() == known.lower() or known.lower() in text.lower():
            return known
    return text


def _looks_like_non_item_name(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return True
    if text in {"subtotal", "tax total", "invoice total", "item/description"}:
        return True
    return any(
        needle in text
        for needle in (
            "invoice amount due",
            "less payments",
            "customer number",
            "sales rep",
            "click here",
            "amount due",
            "ship to",
            "bill to",
        )
    )


def _normalize_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\u00a0", " ")
    )


def _first(pattern: str, text: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text or "", flags)
    return match.group(1).strip() if match else ""


def _parse_date(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%m/%d/%Y") if value else ""


def _fmt_iso(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _fmt_short(value: datetime | None) -> str:
    return value.strftime("%m/%d/%y") if value else ""


def _fmt_period(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f"{_fmt_short(start)}-{_fmt_short(end)}"
    if start:
        return _fmt_short(start)
    if end:
        return _fmt_short(end)
    return ""


def _progress(callback: Callable[..., None] | None, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(**payload)
    except TypeError:
        try:
            callback(payload)
        except Exception:
            return
    except Exception:
        return
