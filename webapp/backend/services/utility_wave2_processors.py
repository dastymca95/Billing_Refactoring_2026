"""Phase U2 deterministic processors for utility vendors with old-script references.

The old project had separate scripts for Alabama Power, EPB Fiber, City of
Henderson, CDE Lightband, and Nolin RECC.  This module keeps the useful
business rules but routes every vendor through the Phase U1 utility safety
helpers: no standalone tax rows, validated GLs, valid property/unit lookups,
dry-run safe output, and consistent web preview rows.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from utils.canonical_vendors import canonical_vendor_name
from utils.property_lookup import UnitMatch, match_by_address

from .. import settings
from .document_ingestion import ingest_document
from .utility_processor_common import (
    UtilityChargeLine,
    allocate_tax_proportionally,
    build_utility_invoice_number,
    classify_utility_line,
    classify_utility_line_detail,
    default_gl_for_line,
    filter_exportable_utility_lines,
    load_chart_of_accounts,
    load_vendor_config,
    money,
    validate_utility_template_rows,
)
from .description_builder import build_invoice_description, build_line_item_description


_LOG = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
UTILITY_GL_CODES = {
    "6139",
    "6178",
    "6905",
    "6910",
    "6915",
    "6920",
    "6925",
    "6930",
    "6935",
    "6955",
    "6956",
    "6960",
    "6995",
}


@dataclass(frozen=True)
class Wave2VendorSpec:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    default_gl: str = ""
    invoice_month_source: str = "service_period_end"
    community_billing: bool = False


@dataclass
class ParsedUtilityInvoice:
    vendor_key: str
    vendor_display_name: str
    account_number: str
    invoice_number: str
    invoice_date: datetime | None
    due_date: datetime | None
    service_period_start: datetime | None
    service_period_end: datetime | None
    service_address: str
    line_items: list[UtilityChargeLine]
    tax_total: Decimal = Decimal("0.00")
    source_file: str = ""
    explicit_invoice_number: str = ""
    property_abbreviation: str = ""
    location: str = ""
    support_document_url: str = ""
    support_document_status: str = "dry_run_no_dropbox"
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


SPECS: dict[str, Wave2VendorSpec] = {
    "alabama_power": Wave2VendorSpec(
        key="alabama_power",
        display_name="Alabama Power",
        aliases=("Alabama Power", "AlabamaPower.com"),
    ),
    "epb_fiber_optics": Wave2VendorSpec(
        key="epb_fiber_optics",
        display_name="EPB Fiber Optics",
        aliases=("EPB Fiber Optics", "EPB"),
        default_gl="6960",
        invoice_month_source="invoice_date",
    ),
    "the_city_of_henderson": Wave2VendorSpec(
        key="the_city_of_henderson",
        display_name="The City of Henderson",
        aliases=("The City of Henderson", "City of Henderson"),
    ),
    "cde_lightband": Wave2VendorSpec(
        key="cde_lightband",
        display_name="CDE Lightband",
        aliases=("CDE Lightband", "CDE"),
    ),
    "nolin_recc_smarthub": Wave2VendorSpec(
        key="nolin_recc_smarthub",
        display_name="Nolin RECC Smarthub",
        aliases=("Nolin RECC Smarthub", "Nolin Rural Electric Cooperative"),
        community_billing=True,
    ),
}


# ---------------------------------------------------------------------------
# Public processor entrypoints
# ---------------------------------------------------------------------------


def process_alabama_power_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave2_utility_batch("alabama_power", **kwargs)


def process_epb_fiber_optics_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave2_utility_batch("epb_fiber_optics", **kwargs)


def process_the_city_of_henderson_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave2_utility_batch("the_city_of_henderson", **kwargs)


def process_cde_lightband_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave2_utility_batch("cde_lightband", **kwargs)


def process_nolin_recc_smarthub_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave2_utility_batch("nolin_recc_smarthub", **kwargs)


def process_wave2_utility_batch(
    vendor_key: str,
    *,
    input_folder: Path | str | None = None,
    output_folder: Path | str | None = None,
    template_path: Path | str | None = None,
    config_path: Path | str | None = None,
    run_context: dict[str, Any] | None = None,
    progress_callback: Callable[..., None] | None = None,
    should_cancel_callback: Callable[[], bool] | None = None,
) -> ProcessBatchResult:
    spec = SPECS[vendor_key]
    inp = Path(input_folder or ".")
    out = Path(output_folder or inp)
    out.mkdir(parents=True, exist_ok=True)
    run_context = run_context or {}
    dry_run = bool(run_context.get("dry_run"))
    timestamp = str(run_context.get("timestamp") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    cfg = _load_config(config_path, vendor_key)
    errors: list[str] = []
    skipped: list[dict[str, Any]] = []
    parsed: list[ParsedUtilityInvoice] = []
    valid_gls = load_chart_of_accounts()

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

    files = sorted(
        [p for p in inp.iterdir() if p.is_file()],
        key=lambda p: p.name.lower(),
    )
    _progress(progress_callback, current_step=f"Reading {spec.display_name} file(s)", files_total=len(files))

    for index, path in enumerate(files, start=1):
        if cancelled():
            break
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append({"filename": path.name, "reason": f"unsupported_extension:{path.suffix.lower()}"})
            continue
        try:
            candidate = ingest_document(path, vendor_hint=spec.display_name)
            invoices = _parse_document(spec, candidate.document_text, path.name)
            for inv in invoices:
                inv.vendor_display_name = canonical_vendor_name(
                    vendor_key=vendor_key,
                    aliases=list(spec.aliases),
                    fallback=spec.display_name,
                )
                _finalize_invoice(inv, spec, cfg, valid_gls)
            parsed.extend(invoices)
            _progress(
                progress_callback,
                current_step=f"Parsed {path.name}",
                files_done=index,
                invoices_created=len(parsed),
                rows_created=sum(len(i.line_items) for i in parsed),
            )
        except Exception as exc:  # pragma: no cover - kept defensive for operator batches
            _LOG.exception("Wave 2 utility processor failed for %s", path)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.line_items]
    review_json = [_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons]
    row_count = sum(len(inv.get("rows") or []) for inv in invoices_json)
    workbook_path = out / f"{vendor_key}_resman_import_{timestamp}.xlsx"
    review_path = out / f"{vendor_key}_manual_review_{timestamp}.xlsx"

    summary = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": vendor_key,
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
        # U2 processors are web-preview first; export endpoint writes the workbook.
        # Dry-run and smoke tests must never get a path that implies a file exists.
        resman_workbook_path=None,
        manual_review_workbook_path=None,
        debug_csv_path=None,
        log_path="",
        errors=errors + [f"{s['filename']}: {s['reason']}" for s in skipped],
    )


# ---------------------------------------------------------------------------
# Vendor parsers
# ---------------------------------------------------------------------------


def _parse_document(spec: Wave2VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    parser: Callable[[Wave2VendorSpec, str, str], list[ParsedUtilityInvoice]] = {
        "alabama_power": _parse_alabama_power,
        "epb_fiber_optics": _parse_epb_fiber,
        "the_city_of_henderson": _parse_henderson,
        "cde_lightband": _parse_cde_lightband,
        "nolin_recc_smarthub": _parse_nolin_recc,
    }[spec.key]
    return parser(spec, text or "", source_file)


def _parse_alabama_power(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"\b(\d{5}-\d{5})\b", text)
    due = _parse_date(_first(r"Draft Date\s+([A-Za-z]+ \d{1,2}, \d{4})", text))
    match = re.search(
        r"(?P<addr>\d+\s+[A-Z0-9 .#-]+?)\s+"
        r"(?P<start>[A-Za-z]+ \d{1,2}, \d{4})\s*-\s*"
        r"(?P<end>[A-Za-z]+ \d{1,2}, \d{4})",
        text,
        re.IGNORECASE,
    )
    service_address = _clean(match.group("addr") if match else "")
    start = _parse_date(match.group("start") if match else "")
    end = _parse_date(match.group("end") if match else "")
    invoice_date = end or due

    current_service = money(_first(r"Current Service\s+\$?\s*([\d,]+\.\d{2})", text))
    tax_total = money(_first(r"Alabama Gross Receipts Tax\s+\$?\s*([\d,]+\.\d{2})", text))
    total = money(_first(r"Total Current Electric Service\s+\$?\s*([\d,]+\.\d{2})", text))
    if current_service == 0:
        current_service = money(_first(r"Current Electric Service\s+\+?\$?\s*([\d,]+\.\d{2})", text))
    line_amount = current_service if current_service > 0 else total
    lines = [UtilityChargeLine("Current Electric Service", line_amount, gl_account="", source_page=1)]

    return [
        ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            invoice_date=invoice_date,
            due_date=due or invoice_date,
            service_period_start=start,
            service_period_end=end,
            service_address=service_address,
            line_items=lines,
            tax_total=tax_total if current_service > 0 else Decimal("0.00"),
            source_file=source_file,
            debug_info={"source_total": str(total)},
        )
    ]


def _parse_epb_fiber(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"AccountNumber:\s*([A-Z0-9-]+)", text)
    explicit = _first(r"InvoiceNumber:\s*([A-Z0-9-]+)", text)
    invoice_date = _parse_date(_first(r"BillingDate:\s*([A-Za-z]+\s*\d{1,2},\s*\d{4})", text))
    due = _parse_date(_first(r"PaymentDueDate:\s*([A-Za-z]+\s*\d{1,2},\s*\d{4})", text))
    service_address = _service_address_from_text(
        text,
        [
            r"(\d{3,6}\s*N\s*Chamberlain\s*Ave)",
            r"(\d{3,6}\s+[A-Za-z0-9 .'-]+(?:Ave|Avenue|St|Street|Rd|Road|Dr|Drive|Ct|Court))",
        ],
    )
    if "1400" in service_address and "Chamberlain" not in service_address:
        service_address = "1400 N Chamberlain Ave"
    billing_range = re.search(r"BillingDates:\s*([A-Za-z]+)\s*(\d{1,2})\s*-\s*([A-Za-z]+)\s*(\d{1,2})", text)
    start = end = None
    if billing_range and invoice_date:
        start = _parse_date(f"{billing_range.group(1)} {billing_range.group(2)}, {invoice_date.year}")
        end_year = invoice_date.year + (1 if billing_range.group(3).lower().startswith("jan") and invoice_date.month == 12 else 0)
        end = _parse_date(f"{billing_range.group(3)} {billing_range.group(4)}, {end_year}")
    charges_block = _between(text, "Statement of New Charges", "TotalNewCharges") or text
    lines: list[UtilityChargeLine] = []
    for raw_label, amount in re.findall(
        r"(FiPhoneforBusiness|Fi-SpeedInternetforBusiness|MonthlyServiceCharges|LongDistanceCharges)\s+(-?\$?[\d,]+\.\d{2})",
        charges_block,
    ):
        label = {
            "FiPhoneforBusiness": "FiPhone for Business",
            "Fi-SpeedInternetforBusiness": "Fi-Speed Internet for Business",
            "MonthlyServiceCharges": "Monthly Service Charges",
            "LongDistanceCharges": "Long Distance Charges",
        }[raw_label]
        amt = money(amount)
        if amt != 0:
            lines.append(UtilityChargeLine(label, amt, gl_account=spec.default_gl, source_page=1))
    tax = money(_first(r"TaxandSurcharges\s+\$?([\d,]+\.\d{2})", charges_block))
    total = money(_first(r"TotalNewCharges\s+\$?([\d,]+\.\d{2})", text))
    if not lines and total > 0:
        lines = [UtilityChargeLine("Fi-Speed Internet for Business", total, gl_account=spec.default_gl, source_page=1)]
        tax = Decimal("0.00")
    return [
        ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            explicit_invoice_number=explicit,
            invoice_date=invoice_date,
            due_date=due or invoice_date,
            service_period_start=start or invoice_date,
            service_period_end=end or invoice_date,
            service_address=service_address,
            line_items=lines,
            tax_total=tax,
            source_file=source_file,
            debug_info={"source_total": str(total)},
        )
    ]


def _parse_henderson(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*([0-9-]+)", text, flags=re.DOTALL)
    if not account:
        account = _first(r"Account No\.\s+Service Address\s*\n\s*([0-9-]+)", text)
    due = _parse_date(_first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*[0-9-]+\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.DOTALL))
    total = money(_first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*[0-9-]+\s+\d{1,2}/\d{1,2}/\d{4}\s+([\d,]+\.\d{2})", text, flags=re.DOTALL))
    service_address = _first(r"Service Address\s*\n\s*([^\n]+)", text)
    sp = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})\s+([A-Za-z ]+)", text)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    service_family = _clean(sp.group(3) if sp else "Electric") or "Electric"
    invoice_date = end or due

    current_block = _between(text, "Current Billing", "Current Charges") or text
    raw_lines: list[UtilityChargeLine] = []
    tax_total = Decimal("0.00")
    for label, amount in re.findall(r"([A-Za-z][A-Za-z ]{2,40})\s+([\d,]+\.\d{2})", current_block):
        clean = _clean(label)
        clean = re.sub(r"^Charge Code Amount\s+", "", clean, flags=re.IGNORECASE)
        amt = money(amount)
        if amt == 0:
            continue
        bucket = classify_utility_line(clean)
        if (
            bucket == "tax"
            or clean.lower() in {"fee", "911 fee"}
            or "911 fee" in clean.lower()
            or "school" in clean.lower()
        ):
            tax_total += amt
        else:
            raw_lines.append(UtilityChargeLine(clean, amt, source_page=1))
    if not raw_lines and total > 0:
        raw_lines.append(UtilityChargeLine(service_family, total, source_page=1))
        tax_total = Decimal("0.00")
    return [
        ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            invoice_date=invoice_date,
            due_date=due or invoice_date,
            service_period_start=start,
            service_period_end=end,
            service_address=service_address,
            line_items=raw_lines,
            tax_total=tax_total,
            source_file=source_file,
            debug_info={"source_total": str(total), "service_family": service_family},
        )
    ]


def _parse_cde_lightband(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account #:\s*([0-9-]+)", text)
    statement = _parse_date(_first(r"Statement Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text))
    due = _parse_date(_first(r"Date Due:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text))
    service_address = _first(r"Service:\s*([^\n]+?)(?:\s+Paid by Autopay|\s+Total Due:|\n)", text)
    service_address = _clean(re.sub(r"\s+Paid by Autopay.*$", "", service_address, flags=re.I))
    sp = re.search(r"Service Period:\s*(\d{1,2}/\d{1,2}/\d{2})\s+to\s+(\d{1,2}/\d{1,2}/\d{2})", text, re.I)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    electric = money(_first(r"Electric Energy Charge\s+([\d,]+\.\d{2})", text))
    tax = money(_first(r"Sales Tax\s+([\d,]+\.\d{2})", text))
    subtotal = money(_first(r"<<ELECTRIC SUB-TOTAL>>\s+([\d,]+\.\d{2})", text))
    connect_fee = money(_first(r"(?:Connect Fee|Connection Fee|Service Connection)\s+([\d,]+\.\d{2})", text))
    lines: list[UtilityChargeLine] = []
    if electric > 0:
        lines.append(UtilityChargeLine("Electric Energy Charge", electric, source_page=1))
    elif subtotal > 0:
        lines.append(UtilityChargeLine("Electric Service", subtotal, source_page=1))
        tax = Decimal("0.00")
    if connect_fee > 0:
        lines.append(
            UtilityChargeLine(
                "Connection Fee",
                connect_fee,
                line_type="connection_fee",
                gl_account="6956",
                taxable=False,
                source_page=1,
            )
        )
    return [
        ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            invoice_date=statement or end,
            due_date=due or statement or end,
            service_period_start=start,
            service_period_end=end,
            service_address=service_address,
            line_items=lines,
            tax_total=tax,
            source_file=source_file,
            debug_info={"source_total": str(subtotal or electric)},
        )
    ]


def _parse_nolin_recc(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    billing_date = _parse_date(_first(r"Billing Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text))
    due = _parse_date(_first(r"PAYMENT WILL DRAFT\s+ON\s+(\d{1,2}/\d{1,2}/\d{4})", text))
    detail_dates: dict[str, tuple[datetime | None, datetime | None]] = {}
    for m in re.finditer(
        r"Reading Dates.*?From\s+To.*?\n\s*\d+\s+(\d{1,2}/\d{1,2}/\d{2})\s+(\d{1,2}/\d{1,2}/\d{2}).{0,700}?Account Number:\s*(\d+)",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        detail_dates[m.group(3)] = (_parse_date(m.group(1)), _parse_date(m.group(2)))

    invoices: list[ParsedUtilityInvoice] = []
    for line in _lines(text):
        m = re.match(
            r"(?P<account>\d{9})\s+(?P<bill_type>NEW ACCOUNT|FINAL|REGULAR)\s+"
            r"(?P<addr>\d{3,6}\s+[A-Z0-9 ]+?DR\s+APT\s+[A-Z0-9-]+)\s+"
            r"\$?\.?(?:\d[\d,]*\.\d{2}|00)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        account = m.group("account")
        start, end = detail_dates.get(account, (billing_date, billing_date))
        amount = money(m.group(4))
        suffix_final = " Final" if m.group("bill_type").upper() == "FINAL" else ""
        invoices.append(
            ParsedUtilityInvoice(
                vendor_key=spec.key,
                vendor_display_name=spec.display_name,
                account_number=account,
                invoice_number="",
                invoice_date=billing_date or end,
                due_date=due or billing_date or end,
                service_period_start=start,
                service_period_end=end,
                service_address=_clean(m.group("addr")),
                line_items=[UtilityChargeLine("Electric Service", amount, source_page=1)],
                tax_total=Decimal("0.00"),
                source_file=source_file,
                debug_info={"bill_type": m.group("bill_type"), "invoice_suffix": suffix_final},
            )
        )
    return invoices


# ---------------------------------------------------------------------------
# Row rendering + validation
# ---------------------------------------------------------------------------


def _finalize_invoice(
    inv: ParsedUtilityInvoice,
    spec: Wave2VendorSpec,
    cfg: dict[str, Any],
    valid_gls: dict[str, str],
) -> None:
    history = _history_hint(inv.vendor_display_name or spec.display_name, inv.account_number)
    service_match = _match_service_address(inv.service_address)
    if not inv.property_abbreviation:
        inv.property_abbreviation = str(history.get("property") or "") or (
            service_match.property_abbreviation if service_match else ""
        )
    if not inv.location and service_match and service_match.unit_number:
        inv.location = service_match.unit_number

    month_anchor = {
        "invoice_date": inv.invoice_date,
        "service_period_start": inv.service_period_start,
        "service_period_end": inv.service_period_end,
    }.get(spec.invoice_month_source) or inv.service_period_end or inv.invoice_date
    suffix = str(inv.debug_info.get("invoice_suffix") or "")
    inv.invoice_number = build_utility_invoice_number(
        account_number=inv.account_number,
        service_period_end=month_anchor,
        explicit_invoice_number=inv.explicit_invoice_number,
        rule="account_number_service_period",
    )
    if suffix and suffix.lower() not in inv.invoice_number.lower():
        inv.invoice_number = f"{inv.invoice_number}{suffix}"

    if not inv.account_number:
        inv.manual_review_reasons.append("account_number_missing")
    if not inv.invoice_number:
        inv.manual_review_reasons.append("invoice_number_missing")
    if not inv.invoice_date:
        inv.manual_review_reasons.append("invoice_date_missing")
    if not inv.due_date:
        inv.manual_review_reasons.append("due_date_missing")
        inv.due_date = inv.invoice_date
    if not inv.property_abbreviation:
        inv.manual_review_reasons.append("property_mapping_required")
    if not inv.service_address:
        inv.manual_review_reasons.append("service_address_missing_or_unresolved")

    adjusted_lines = filter_exportable_utility_lines(inv.line_items)
    if not adjusted_lines:
        inv.manual_review_reasons.append("line_items_missing_or_unreadable")
    allocation = allocate_tax_proportionally(adjusted_lines, inv.tax_total)
    finalized: list[UtilityChargeLine] = []
    for line in allocation.lines:
        classification = classify_utility_line_detail(line.description)
        gl = _best_gl(inv, spec, line, history, cfg, valid_gls)
        if not gl:
            if classification.classification == "fire_protection_service":
                inv.manual_review_reasons.append("gl_mapping_required_fire_service")
            else:
                inv.manual_review_reasons.append("gl_mapping_required")
        inv.manual_review_reasons.extend(classification.manual_review_flags if not gl else [])
        metadata = dict(line.metadata)
        metadata["line_classification"] = classification.classification
        metadata["line_classification_reason"] = classification.reason
        metadata["line_classification_keywords"] = list(classification.matched_keywords)
        if allocation.tax_total:
            metadata["tax_total"] = str(allocation.tax_total)
            if allocation.allocation_by_index:
                metadata["tax_allocation_by_index"] = {
                    str(k): str(v) for k, v in allocation.allocation_by_index.items()
                }
        finalized.append(
            UtilityChargeLine(
                    description=line.description,
                    amount=line.money,
                    line_type=(
                        classification.classification
                        if classification.classification not in {"service", "electric_service", "water_service", "sewer_service", "wastewater_service", "stormwater_service", "gas_service", "internet_fiber_service", "cable_service", "trash_service", "fire_protection_service"}
                        else classification.classification
                    ),
                    gl_account=gl,
                taxable=line.taxable,
                include_in_export=line.include_in_export,
                source_page=line.source_page,
                trace_id=line.trace_id,
                metadata=metadata,
            )
        )
    inv.line_items = finalized

    expected = money(inv.debug_info.get("source_total") or 0)
    actual = sum((line.money for line in inv.line_items), Decimal("0.00"))
    if expected > 0 and abs(actual - expected) > Decimal("0.02"):
        inv.manual_review_reasons.append("amount_reconciliation_failed")
        inv.debug_info["reconciliation"] = {"expected": str(expected), "actual": str(actual)}

    rows = _rows_for_invoice(inv)
    validation = validate_utility_template_rows(rows, valid_gl_accounts=valid_gls)
    inv.debug_info["validation"] = {
        "ok": validation.ok,
        "blocking_reasons": list(validation.blocking_reasons),
        "warnings": list(validation.warnings),
    }
    inv.manual_review_reasons.extend(validation.blocking_reasons)
    inv.manual_review_reasons = sorted(set(r for r in inv.manual_review_reasons if r))


def _rows_for_invoice(inv: ParsedUtilityInvoice) -> list[dict[str, Any]]:
    match = _match_service_address(inv.service_address)
    desc_context = {
        "category": "utilities",
        "service_period_start": inv.service_period_start,
        "service_period_end": inv.service_period_end,
        "service_address": inv.service_address,
        "unit_number": inv.location if match and match.unit_number else "",
        "property_name": match.property_name if match else "",
        "property_abbreviation": inv.property_abbreviation,
        "property_level_service": bool(inv.debug_info.get("property_level_service")),
    }
    invoice_desc_result = build_invoice_description(desc_context)
    invoice_desc = invoice_desc_result.description
    inv.manual_review_reasons.extend(invoice_desc_result.review_flags)
    inv.manual_review_reasons = sorted(set(r for r in inv.manual_review_reasons if r))
    desc_meta = {
        "service_address": inv.service_address,
        "matched_property_name": match.property_name if match else "",
        "description_components": invoice_desc_result.components_used,
        "description_fallback_used": invoice_desc_result.fallback_used,
        "description_review_flags": list(invoice_desc_result.review_flags),
    }
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(inv.line_items, start=1):
        line_desc_result = build_line_item_description(
            desc_context,
            {"description": line.description},
        )
        inv.manual_review_reasons.extend(line_desc_result.review_flags)
        inv.manual_review_reasons = sorted(set(r for r in inv.manual_review_reasons if r))
        line_desc = line_desc_result.description
        rows.append(
            {
                "Invoice Number": inv.invoice_number,
                "Bill or Credit": "Bill",
                "Invoice Date": _fmt_date(inv.invoice_date),
                "Accounting Date": _fmt_date(inv.invoice_date),
                "Vendor": inv.vendor_display_name,
                "Invoice Description": invoice_desc,
                "Line Item Number": str(idx),
                "Property Abbreviation": inv.property_abbreviation,
                "Location": inv.location,
                "GL Account": line.gl_account,
                "Line Item Description": line_desc,
                "Amount": float(line.money),
                "Expense Type": "General",
                "Is Replacement Reserve": False,
                "Due Date": _fmt_date(inv.due_date),
                "Reference Number": "",
                "Document Url": inv.support_document_url,
                "_meta": {
                    "manual_review_reasons": list(inv.manual_review_reasons),
                    "support_document_status": inv.support_document_status,
                    "source_file": inv.source_file,
                    **desc_meta,
                    "utility_wave": "U2",
                    "tax_allocated": line.metadata.get("tax_allocated", ""),
                    "line_type": line.line_type,
                    "source_page": line.source_page,
                },
            }
        )
    return rows


def _invoice_to_preview_dict(inv: ParsedUtilityInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(inv)
    return {
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "billing_date": _fmt_iso(inv.invoice_date),
        "service_period": (
            f"{_fmt_iso(inv.service_period_start)} -> {_fmt_iso(inv.service_period_end)}"
            if inv.service_period_start or inv.service_period_end
            else ""
        ),
        "property_abbreviation": inv.property_abbreviation,
        "location": inv.location,
        "service_address": inv.service_address,
        "total_amount": float(sum((line.money for line in inv.line_items), Decimal("0.00"))),
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": inv.support_document_status,
        "debug_info": inv.debug_info,
    }


def _manual_review_to_dict(inv: ParsedUtilityInvoice) -> dict[str, Any]:
    return {
        "source_file": inv.source_file,
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": inv.property_abbreviation,
        "location": inv.location,
        "total_amount": float(sum((line.money for line in inv.line_items), Decimal("0.00"))),
        "line_count": len(inv.line_items),
        "reasons": list(inv.manual_review_reasons),
    }


# ---------------------------------------------------------------------------
# Reference data helpers
# ---------------------------------------------------------------------------


def _best_gl(
    inv: ParsedUtilityInvoice,
    spec: Wave2VendorSpec,
    line: UtilityChargeLine,
    history: dict[str, Any],
    cfg: dict[str, Any],
    valid_gls: dict[str, str],
) -> str:
    classification = classify_utility_line(line.description)
    if classification == "connection_fee":
        return "6956"
    if classification == "fire_protection_service":
        fire_cfg = (
            (cfg.get("utility_processing") or {}).get("fire_service_rules")
            or cfg.get("fire_service_rules")
            or {}
        )
        fire_gl = str(fire_cfg.get("gl_account") or fire_cfg.get("gl_code") or "").strip()
        return fire_gl if fire_gl in valid_gls else ""
    if classification == "trash_service":
        trash_gl = default_gl_for_line(line.description, vendor_key=spec.key, vendor_config=cfg)
        return trash_gl if trash_gl in valid_gls else ""
    if classification == "late_fee":
        late_gl = default_gl_for_line(line.description, vendor_key=spec.key, vendor_config=cfg)
        return late_gl if late_gl in valid_gls and late_gl != "6956" else ""
    if line.gl_account and line.gl_account in valid_gls:
        if line.gl_account == "6956" and classification != "connection_fee":
            return ""
        return line.gl_account
    historical = str(history.get("gl_account") or "")
    if historical in valid_gls and historical in UTILITY_GL_CODES and historical != "6956":
        return historical
    candidate = default_gl_for_line(line.description, vendor_key=spec.key, vendor_config=cfg)
    if candidate in valid_gls and not (candidate == "6956" and classification != "connection_fee"):
        return candidate
    if spec.default_gl and spec.default_gl in valid_gls:
        return spec.default_gl
    return ""


def _history_hint(vendor_name: str, account_number: str) -> dict[str, str]:
    account_key = re.sub(r"[^A-Z0-9]", "", account_number.upper())
    if not account_key:
        return {}
    path = settings.PROJECT_ROOT / "Gl Codes" / "General Ledger Report.csv"
    if not path.is_file():
        return {}
    prop_counts: Counter[str] = Counter()
    gl_counts: Counter[str] = Counter()
    vendor_norm = _norm(vendor_name)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if _norm(row.get("Vendor") or "") != vendor_norm:
                    continue
                hay = re.sub(
                    r"[^A-Z0-9]",
                    "",
                    f"{row.get('Reference') or ''} {row.get('Description') or ''}".upper(),
                )
                if account_key not in hay:
                    continue
                prop = (row.get("Property") or "").strip()
                if prop:
                    prop_counts[prop] += 1
                code = (row.get("GL_Account") or "").split(" ", 1)[0].strip()
                if (
                    code
                    and (row.get("Gl Accounts.Type") or "").lower() == "expense"
                    and code in UTILITY_GL_CODES
                ):
                    gl_counts[code] += 1
    except Exception:
        return {}
    out: dict[str, str] = {}
    if prop_counts:
        out["property"] = prop_counts.most_common(1)[0][0]
    if gl_counts:
        out["gl_account"] = gl_counts.most_common(1)[0][0]
    return out


def _match_service_address(service_address: str) -> UnitMatch | None:
    address = _address_only(service_address)
    if not address:
        return None
    return match_by_address(address) or match_by_address(service_address)


def _address_only(value: str) -> str:
    text = _clean(value)
    text = re.sub(r"\b(?:paid by autopay|service period|rate class|total due).*$", "", text, flags=re.I).strip()
    text = re.sub(r"\b(?:house meter|meter|rec|common|bldg|building)\b.*$", "", text, flags=re.I).strip()
    return text or _clean(value)


def _service_address_from_text(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        hit = _first(pattern, text, flags=re.IGNORECASE)
        if hit:
            return _clean(hit)
    return ""


def _load_config(config_path: Path | str | None, vendor_key: str) -> dict[str, Any]:
    if config_path:
        path = Path(config_path)
        if path.is_file():
            try:
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                return {}
    return load_vendor_config(vendor_key)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _first(pattern: str, text: str, *, flags: int = 0) -> str:
    match = re.search(pattern, text or "", flags)
    if not match:
        return ""
    return _clean(match.group(1))


def _between(text: str, start: str, end: str) -> str:
    pattern = re.escape(start) + r"(.*?)" + re.escape(end)
    return _first(pattern, text, flags=re.DOTALL | re.IGNORECASE)


def _lines(text: str) -> list[str]:
    return [_clean(line) for line in (text or "").splitlines() if _clean(line)]


def _clean(value: str) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _parse_date(value: str) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    text = re.sub(r"([A-Za-z]+)(\d{1,2})", r"\1 \2", text)
    text = re.sub(r",\s*", ", ", text)
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%m/%d/%Y") if value else ""


def _fmt_iso(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _progress(callback: Callable[..., None] | None, **kwargs: Any) -> None:
    if callback is None:
        return
    try:
        callback(**kwargs)
    except Exception:
        pass


__all__ = [
    "ProcessBatchResult",
    "process_alabama_power_batch",
    "process_cde_lightband_batch",
    "process_epb_fiber_optics_batch",
    "process_nolin_recc_smarthub_batch",
    "process_the_city_of_henderson_batch",
    "process_wave2_utility_batch",
]
