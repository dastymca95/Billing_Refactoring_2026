"""Phase U3 deterministic processors for remaining utility vendors.

Wave 3 covers utility vendors that have enough training data for a safe
deterministic dry-run parser, with special care for community/master bills.
The module intentionally reuses the U1/U2 safety path for finalization:
validated GLs, property lookup, proportional tax allocation, preview-only
dry-runs, and no Dropbox calls from automated tests.
"""

from __future__ import annotations

import calendar
import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from webapp.backend import settings

from .document_ingestion import ingest_document
from .utility_processor_common import (
    UtilityChargeLine,
    classify_utility_line,
    load_chart_of_accounts,
    load_vendor_config,
    money,
)
from .utility_wave2_processors import (
    ParsedUtilityInvoice,
    ProcessBatchResult,
    _between,
    _clean,
    _finalize_invoice,
    _first,
    _fmt_date,
    _invoice_to_preview_dict,
    _manual_review_to_dict,
    _norm,
    _parse_date,
)


_LOG = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


@dataclass(frozen=True)
class Wave3VendorSpec:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    default_gl: str = ""
    invoice_month_source: str = "service_period_end"
    community_billing: bool = False


SPECS: dict[str, Wave3VendorSpec] = {
    "clarksville_gas_and_water": Wave3VendorSpec(
        "clarksville_gas_and_water",
        "Clarksville Gas and Water",
        ("Clarksville Gas and Water",),
        default_gl="6955",
    ),
    "knoxville_utilities_board": Wave3VendorSpec(
        "knoxville_utilities_board",
        "Knoxville Utilities Board",
        ("Knoxville Utilities Board", "KUB"),
        default_gl="6915",
        invoice_month_source="invoice_date",
        community_billing=True,
    ),
    "kentucky_utilities": Wave3VendorSpec(
        "kentucky_utilities",
        "Kentucky Utilities",
        ("Kentucky Utilities", "KU", "LG&E KU"),
        default_gl="6910",
        invoice_month_source="invoice_date",
        community_billing=True,
    ),
    "tennessee_american_water": Wave3VendorSpec(
        "tennessee_american_water",
        "Tennessee American Water",
        ("Tennessee American Water",),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "union_city_energy_authority": Wave3VendorSpec(
        "union_city_energy_authority",
        "Union City Energy Authority",
        ("Union City Energy Authority",),
        default_gl="6920",
        invoice_month_source="invoice_date",
    ),
    "weakley_county_municipal_electric_system": Wave3VendorSpec(
        "weakley_county_municipal_electric_system",
        "Weakley County Municipal Electric System",
        ("Weakley County Municipal Electric System",),
        default_gl="6920",
        invoice_month_source="invoice_date",
    ),
    "birmingham_water_works": Wave3VendorSpec(
        "birmingham_water_works",
        "Birmingham Water Works",
        ("Birmingham Water Works", "Central Alabama Water", "CAW"),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "city_of_mcminnville_water_sewer_dept": Wave3VendorSpec(
        "city_of_mcminnville_water_sewer_dept",
        "City of McMinnville Water & Sewer Dept",
        ("City of McMinnville", "City of McMinnville Water"),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "city_of_chattanooga_wastewater_department": Wave3VendorSpec(
        "city_of_chattanooga_wastewater_department",
        "City of Chattanooga Wastewater Department",
        ("City of Chattanooga Wastewater Department",),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "city_of_martin": Wave3VendorSpec(
        "city_of_martin",
        "City of Martin",
        ("City of Martin",),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "city_of_union_city": Wave3VendorSpec(
        "city_of_union_city",
        "City of Union City",
        ("City of Union City", "City of Union City Water & Sewer Department"),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "guardian_water_power": Wave3VendorSpec(
        "guardian_water_power",
        "Guardian Water & Power",
        ("Guardian Water & Power", "Guardian Water and Power"),
        default_gl="6955",
        invoice_month_source="invoice_date",
    ),
    "hopkinsville_electric_system": Wave3VendorSpec(
        "hopkinsville_electric_system",
        "Hopkinsville Electric System",
        ("Hopkinsville Electric System",),
        default_gl="6920",
        invoice_month_source="invoice_date",
    ),
}


def process_clarksville_gas_and_water_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("clarksville_gas_and_water", **kwargs)


def process_knoxville_utilities_board_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("knoxville_utilities_board", **kwargs)


def process_kentucky_utilities_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("kentucky_utilities", **kwargs)


def process_tennessee_american_water_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("tennessee_american_water", **kwargs)


def process_union_city_energy_authority_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("union_city_energy_authority", **kwargs)


def process_weakley_county_municipal_electric_system_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("weakley_county_municipal_electric_system", **kwargs)


def process_birmingham_water_works_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("birmingham_water_works", **kwargs)


def process_city_of_mcminnville_water_sewer_dept_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("city_of_mcminnville_water_sewer_dept", **kwargs)


def process_city_of_chattanooga_wastewater_department_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("city_of_chattanooga_wastewater_department", **kwargs)


def process_city_of_martin_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("city_of_martin", **kwargs)


def process_city_of_union_city_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("city_of_union_city", **kwargs)


def process_guardian_water_power_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("guardian_water_power", **kwargs)


def process_hopkinsville_electric_system_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("hopkinsville_electric_system", **kwargs)


def process_wave3_utility_batch(
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
    valid_gls = load_chart_of_accounts()
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    parsed: list[ParsedUtilityInvoice] = []

    files = sorted([p for p in inp.iterdir() if p.is_file()], key=lambda p: p.name.lower())
    _progress(progress_callback, current_step=f"Reading {spec.display_name} file(s)", files_total=len(files))

    for index, path in enumerate(files, start=1):
        if _cancelled(should_cancel_callback, run_context):
            break
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append({"filename": path.name, "reason": f"unsupported_extension:{path.suffix.lower()}"})
            continue
        try:
            candidate = ingest_document(path, vendor_hint=spec.display_name)
            document_text = candidate.document_text or ""
            if spec.key == "weakley_county_municipal_electric_system" and path.suffix.lower() in {
                ".jpg",
                ".jpeg",
                ".png",
                ".tif",
                ".tiff",
            }:
                document_text = _weakley_image_ocr_text(path, document_text)
            invoices = _parse_document(spec, document_text, path.name)
            for inv in invoices:
                if candidate.source_type in {"image", "screenshot", "pdf_scanned"} and (
                    candidate.text_quality_score < 0.55 or "weak_text_quality" in (candidate.warnings or [])
                ):
                    inv.manual_review_reasons.append("ocr_confidence_low")
                    inv.debug_info["ingestion_warnings"] = list(candidate.warnings or [])
                    inv.debug_info["text_quality_score"] = candidate.text_quality_score
                if candidate.needs_vision:
                    inv.manual_review_reasons.append("vision_recommended")
                _finalize_invoice(inv, spec, cfg, valid_gls)  # type: ignore[arg-type]
                for row in _invoice_to_preview_dict(inv).get("rows", []):
                    row.get("_meta", {})["utility_wave"] = "U3"
            parsed.extend(invoices)
            _progress(
                progress_callback,
                current_step=f"Parsed {path.name}",
                files_done=index,
                invoices_created=len(parsed),
                rows_created=sum(len(i.line_items) for i in parsed),
            )
        except Exception as exc:  # pragma: no cover - operator batch defense
            _LOG.exception("Wave 3 utility processor failed for %s", path)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    invoices_json = [_invoice_to_preview_dict(inv) for inv in parsed if inv.line_items]
    for inv in invoices_json:
        for row in inv.get("rows") or []:
            row.setdefault("_meta", {})["utility_wave"] = "U3"
    review_json = [_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons]
    row_count = sum(len(inv.get("rows") or []) for inv in invoices_json)
    workbook_path = out / f"{vendor_key}_resman_import_{timestamp}.xlsx"

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
        resman_workbook_path=None,
        manual_review_workbook_path=None,
        debug_csv_path=None,
        log_path="",
        errors=errors + [f"{s['filename']}: {s['reason']}" for s in skipped],
    )


def _parse_document(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    return {
        "clarksville_gas_and_water": _parse_clarksville,
        "knoxville_utilities_board": _parse_knoxville_kub,
        "kentucky_utilities": _parse_kentucky_utilities,
        "tennessee_american_water": _parse_tennessee_american_water,
        "union_city_energy_authority": _parse_union_city_energy_authority,
        "weakley_county_municipal_electric_system": _parse_weakley_electric,
        "birmingham_water_works": _parse_birmingham_water,
        "city_of_mcminnville_water_sewer_dept": _parse_city_mcminnville_water,
        "city_of_chattanooga_wastewater_department": _parse_chattanooga_wastewater,
        "city_of_martin": _parse_city_martin,
        "city_of_union_city": _parse_city_union_city,
        "guardian_water_power": _parse_guardian_water,
        "hopkinsville_electric_system": _parse_hopkinsville_electric,
    }[spec.key](spec, text, source_file)


def _parse_clarksville(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"(?:ACCOUNT\s*NO\.?|Account\s*#)\s*[:.]?\s*([0-9-]+\.[0-9]+)", text, flags=re.I)
    due = _parse_date(_first(r"DUE DATE.*?([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", text, flags=re.I | re.S))
    period = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})\s+to\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", text, re.I)
    start = _parse_date(period.group(1) if period else "")
    end = _parse_date(period.group(2) if period else "")
    service_address = (
        _first(
            r"BILLING\s+STATEM\s*ENT\s+[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+to\s+"
            r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+([^\n]+)",
            text,
            flags=re.I,
        )
        or _first(r"METER LOCATION\s*\n?([0-9A-Z .#-]+)", text, flags=re.I)
    )
    block = _between(text, "Current Billing", "Total Balance") or text
    lines = _utility_named_lines(block, ["Water", "Sewer", "Gas"])
    tax = _money_after_label(block, "Sales Tax")
    total = _money_after_label(block, "Total Current") or sum((l.money for l in lines), Decimal("0.00")) + tax
    return [_invoice(spec, account, "", end or due, due or end, start, end, service_address, lines, tax, source_file, total)]


def _parse_knoxville_kub(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account Number:\s*([0-9]+)", text, flags=re.I)
    invoice_date = _parse_date(_first(r"Billing Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    due = _parse_date(_first(r"Total Amount Due by\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    total = _money_after_label(text, "Current Charges for Period") or _money_after_label(text, "Total Amount Due by")
    summary = _between(text, "Summary of Charges by Address", "Billing Adjustments") or text
    lines: list[UtilityChargeLine] = []
    first_address = ""
    for m in re.finditer(r"(\d{3,6}\s+[A-Za-z0-9 .#-]+?)\s+\$([\d,]+\.\d{2})", summary):
        label = _clean(m.group(1))
        if "day billing cycle" in label.lower():
            continue
        if not first_address:
            first_address = label
        lines.append(UtilityChargeLine(label, money(m.group(2)), gl_account=spec.default_gl, source_page=1))
    adjustment = _money_after_label(text, "Round It Up Contribution")
    if adjustment:
        lines.append(UtilityChargeLine("Round It Up Contribution", adjustment, gl_account=spec.default_gl, source_page=1))
    if not lines and total:
        lines = [UtilityChargeLine("Current Utility Charges", total, gl_account=spec.default_gl, source_page=1)]
    return [_invoice(spec, account, "", invoice_date, due, invoice_date, invoice_date, first_address, lines, Decimal("0.00"), source_file, total)]


def _parse_kentucky_utilities(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account\s*#\s*([0-9-]+)", text, flags=re.I)
    due = _parse_date(_first(r"AMOUNT DUE\s+DUE DATE.*?\$?[\d,]+\.\d{2}\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I | re.S))
    invoice_date = _parse_date(_first(r"Total Current Charges as of\s+(\d{1,2}/\d{1,2}/\d{2})", text, flags=re.I))
    service_address = _first(r"Service Address:\s*([^\n]+)", text, flags=re.I)
    service = _money_after_label(text, "Current Electric Charges")
    tax = _money_after_label(text, "Current Taxes and Fees")
    total = _money_after_label(text, "Total Current Charges as of") or service + tax
    lines = [UtilityChargeLine("Current Electric Charges", service or total, gl_account=spec.default_gl, source_page=1)]
    return [_invoice(spec, account, "", invoice_date, due, invoice_date, invoice_date, service_address, lines, tax, source_file, total)]


def _parse_tennessee_american_water(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account No\.?\s*([0-9-]+)", text, flags=re.I)
    invoice_date = _parse_date(_first(r"BillingDate:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    due = _parse_date(_first(r"Payment Due By:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    sp = re.search(r"ServicePeriod:\s*([A-Za-z]+)\s+(\d{1,2})\s+to\s+([A-Za-z]+)\s+(\d{1,2})", text, re.I)
    start = _parse_month_day(sp.group(1), sp.group(2), invoice_date.year if invoice_date and sp else None) if sp else None
    end = _parse_month_day(sp.group(3), sp.group(4), invoice_date.year if invoice_date and sp else None) if sp else None
    service_address = (
        _first(r"Service to:\s*([^\n]+?)(?:\s+Amount\b|\n|$)", text, flags=re.I)
        or _multiline_after(text, "Service Address:", stop="BillingDate")
    )
    service_address = re.sub(
        r"\s+Thank you for using AutoPay\..*?(?=\d{3,6}\s+[A-Z])",
        " ",
        service_address,
        flags=re.I,
    )
    service = _money_after_label(text, "ServiceRelatedCharges")
    tax = _money_after_label(text, "Taxes")
    fees = _money_after_label(text, "FeesandAdjustments")
    total = _money_after_label(text, "Total AmountDue") or service + tax + fees
    base = service + fees
    fire_amount = _money_after_label(text, "Fire Service")
    private_fire_amount = _money_after_label(text, "Private Fire Service Charge")
    water_amount = _money_after_label(text, "Water Service")
    if fire_amount or private_fire_amount or classify_utility_line(text) == "fire_protection_service":
        line_description = "Fire Protection Service"
        line_type = "fire_protection_service"
    elif water_amount:
        line_description = "Water Service"
        line_type = "water_service"
    else:
        line_description = "Service Related Charges"
        line_type = "service"
    lines = [
        UtilityChargeLine(
            line_description,
            base if base != 0 else total,
            line_type=line_type,
            source_page=1,
            metadata={
                "service_related_charges": str(service),
                "fees_and_adjustments": str(fees),
                "detected_fire_amount": str(fire_amount or private_fire_amount or Decimal("0.00")),
            },
        )
    ]
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, tax, source_file, total)]


def _parse_union_city_energy_authority(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"BANK DRAFT\s+([0-9-]+)", text, flags=re.I) or _first(r"\b(\d{6}-\d{6})\b", text)
    invoice_date = _parse_date(_first(r"\b([A-Za-z]{3}\s+\d{1,2}\s+\d{4})\b", text))
    due = _parse_date(
        _first(r"NET\s+AMOUNT\s+DUE:?.*?([A-Za-z]{3}\s+\d{1,2}\s+\d{4})", text, flags=re.I | re.S)
    )
    service_address = _first(r"VILLAGES OF AUTUMNWOOD\s+([0-9A-Z ]+APT\s+[A-Z0-9-]+)", text, flags=re.I)
    total = _money_after_label(text, "TOTAL CURRENT CHARGES") or _money_after_label(text, "NET AMOUNT DUE")
    service = _money_before_label(text, "TN Sales Tax") or total
    tax = total - service if total > service else Decimal("0.00")
    lines = [UtilityChargeLine("Metered Electric", service, gl_account=spec.default_gl, source_page=1)]
    return [_invoice(spec, account, "", invoice_date, due, invoice_date, invoice_date, service_address, lines, tax, source_file, total)]


def _parse_weakley_electric(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _weakley_account_number(text)
    invoice_date = _weakley_date_near(text, ("METER", "READ"))
    due = _weakley_date_near(text, ("PENALTY", "DUE", "AFTER")) or _weakley_date_near(text, ("NET", "AMOUNT"))
    service_address = _weakley_service_address(text)
    total = _weakley_money_after_any(
        text,
        (
            "TOTAL CURRENT CHARGES",
            "TOTALCURRENTCHARGES",
            "CURRENT MONTH'S CHARGES",
            "CURRENTMONTH'SCHARGES",
            "NET AMOUNT DUE",
            "NETAMOUNTDUE",
            "NET AMOUNT OUE",
            "NETAMOUNTOUE",
            "NET AMOUNT CUE",
            "NETAMOUNTCUE",
        ),
    )
    connect = _weakley_money_after_any(text, ("CONNECT FEE", "CONNECTION FEE", "RECONNECT FEE"))
    service = _weakley_metered_electric_amount(text)
    tax = _weakley_money_after_any(text, ("SALES TAX", "ELECTRIC TAX", "TAX"))

    if total and not service:
        service = total - connect - tax
    if service < Decimal("0.00"):
        service = Decimal("0.00")

    history = _weakley_history_match(account, total or service + connect + tax, service_address)
    start = history.get("service_period_start") or None
    end = history.get("service_period_end") or None
    if not invoice_date:
        invoice_date = history.get("invoice_date") or None
    if not start or not end:
        start, end = _weakley_default_service_period(invoice_date, due)
    if not service_address and history.get("service_address"):
        service_address = str(history["service_address"])

    lines: list[UtilityChargeLine] = []
    if service:
        lines.append(UtilityChargeLine("Metered Electric", service, gl_account=spec.default_gl, source_page=1))
    if connect:
        lines.append(
            UtilityChargeLine(
                "Connect Fee",
                connect,
                line_type="connection_fee",
                gl_account="6956",
                taxable=False,
                source_page=1,
            )
        )
    if not total:
        total = service + connect + tax
    inv = _invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, tax, source_file, total)
    if history.get("property_abbreviation"):
        inv.property_abbreviation = str(history["property_abbreviation"])
    if history.get("gl_account") and lines:
        inv.debug_info["weakley_history_gl"] = str(history["gl_account"])
    inv.debug_info["weakley_ocr_hardened"] = True
    return [inv]


def _weakley_image_ocr_text(path: Path, base_text: str) -> str:
    """Return a merged OCR view for photographed Weakley bills.

    The bill layout is stable, but Tesseract's best single variant often
    captures either account/date fields or the charge table, not both. Merging
    a small set of local OCR passes gives the deterministic parser the same
    evidence a human sees without calling external AI or mutating the image.
    """

    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return base_text
    texts = [base_text or ""]
    try:
        with Image.open(path) as img:
            base = ImageOps.grayscale(img)
            up2 = base.resize((base.width * 2, base.height * 2))
            up3 = base.resize((base.width * 3, base.height * 3))
            variants = [
                (base, "--psm 6"),
                (base, "--psm 11"),
                (ImageEnhance.Contrast(base).enhance(2.2), "--psm 6"),
                (ImageEnhance.Contrast(base).enhance(2.2), "--psm 11"),
                (ImageEnhance.Sharpness(ImageEnhance.Contrast(up2).enhance(2.0)).enhance(1.6), "--psm 11"),
                (ImageEnhance.Sharpness(ImageEnhance.Contrast(up3).enhance(2.2)).enhance(2.0), "--psm 6"),
                (
                    ImageEnhance.Sharpness(ImageEnhance.Contrast(up3).enhance(2.2))
                    .enhance(2.0)
                    .point(lambda px: 255 if px > 170 else 0)
                    .filter(ImageFilter.MedianFilter(size=3)),
                    "--psm 11",
                ),
            ]
            for image, config in variants:
                try:
                    text = pytesseract.image_to_string(image, config=config).strip()
                except Exception:
                    continue
                if text:
                    texts.append(text)
    except Exception:
        return base_text
    return _weakley_merge_ocr_lines(texts)


def _weakley_merge_ocr_lines(texts: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw in (text or "").splitlines():
            line = _clean(raw)
            if not line:
                continue
            key = re.sub(r"[^A-Z0-9]+", "", line.upper())
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return "\n".join(lines[:260])


def _weakley_account_number(text: str) -> str:
    candidates: list[str] = []
    patterns = [
        r"(?:ACCOUNT|RECOUNT)\s*(?:NUMBER|NO|N0)?\s*[:|\]\s]*([0-9]{5,6})\s*[- ]\s*([0-9]{5,6})",
        r"\b([0-9]{6})\s*[- ]\s*([0-9]{5,6})\b",
    ]
    for pattern in patterns:
        for left, right in re.findall(pattern, text or "", flags=re.I):
            candidates.append(_weakley_repair_account(left, right))
    compact = re.sub(r"[^A-Z0-9-]+", "", (text or "").upper())
    for left, right in re.findall(r"([0-9]{6})-?([0-9]{5,6})", compact):
        candidates.append(_weakley_repair_account(left, right))
    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _weakley_repair_account(left: str, right: str) -> str:
    left_digits = re.sub(r"\D", "", left or "")
    right_digits = re.sub(r"\D", "", right or "")
    if not left_digits or not right_digits:
        return ""
    raw = f"{left_digits} - {right_digits}"
    known = _weakley_known_accounts()
    if raw in known:
        return raw
    best = ""
    best_score = 0.0
    compact_raw = f"{left_digits}{right_digits}"
    for known_account in known:
        k_left, _, k_right = known_account.partition(" - ")
        if k_left != left_digits:
            continue
        score = SequenceMatcher(None, compact_raw, f"{k_left}{k_right}").ratio()
        if score > best_score:
            best_score = score
            best = known_account
    if best and best_score >= 0.82:
        return best
    if len(right_digits) == 5:
        for known_account in known:
            k_left, _, k_right = known_account.partition(" - ")
            if k_left == left_digits and k_right.startswith(right_digits):
                return known_account
    return raw


_WEAKLEY_KNOWN_ACCOUNTS: set[str] | None = None


def _weakley_known_accounts() -> set[str]:
    global _WEAKLEY_KNOWN_ACCOUNTS
    if _WEAKLEY_KNOWN_ACCOUNTS is not None:
        return _WEAKLEY_KNOWN_ACCOUNTS
    out: set[str] = set()
    path = settings.PROJECT_ROOT / "Gl Codes" / "General Ledger Report.csv"
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if _norm(row.get("Vendor") or "") != _norm("Weakley County Municipal Electric System"):
                        continue
                    hay = f"{row.get('Reference') or ''} {row.get('Description') or ''}"
                    for left, right in re.findall(r"\b([0-9]{6})\s*[- ]\s*([0-9]{5,6})\b", hay):
                        out.add(f"{left} - {right}")
        except Exception:
            out = set()
    _WEAKLEY_KNOWN_ACCOUNTS = out
    return out


def _weakley_service_address(text: str) -> str:
    patterns = [
        r"SERVICE\s*(?:ADDRESS|ADORESS|ADORESS|AD0RESS)\s*[:|\]\s]*([0-9]{2,6}\s+[A-Z0-9 .'-]+?(?:APT|UNIT|#)\s*[A-Z0-9-]+)",
        r"\b([0-9]{2,6}\s+BEAUMONT\s+ST\s+(?:APT\s+)?[A-Z0-9-]+)\b",
        r"\b([0-9]{2,6}\s+BELMONT\s+ST\s+(?:APT\s+)?[A-Z0-9-]+)\b",
    ]
    for pattern in patterns:
        hit = _first(pattern, text or "", flags=re.I)
        if not hit:
            continue
        hit = re.sub(r"\b(?:OOUEER\S*|WEANS\S*|WCMES|PAY|PHONE).*$", "", hit, flags=re.I)
        hit = _clean(hit).upper()
        hit = hit.replace(" BELMONT ", " BEAUMONT ")
        return hit
    return ""


def _weakley_money_after_any(text: str, labels: tuple[str, ...]) -> Decimal:
    for label in labels:
        compact_label = re.sub(r"[^A-Z0-9]+", "", label.upper())
        source_lines = (text or "").splitlines()
        for index, raw in enumerate(source_lines):
            compact_line = re.sub(r"[^A-Z0-9.,$]+", "", raw.upper())
            if compact_label not in compact_line:
                continue
            amounts = _weakley_amounts("\n".join(source_lines[index : index + 4]))
            if amounts:
                return amounts[0]
        pattern = re.escape(label).replace(r"\ ", r"\s*")
        match = re.search(rf"{pattern}.{{0,80}}?([0-9]{{1,4}}[.,][0-9]{{2}})", text or "", re.I | re.S)
        if match:
            return _weakley_money(match.group(1))
    return Decimal("0.00")


def _weakley_metered_electric_amount(text: str) -> Decimal:
    for raw in (text or "").splitlines():
        if not re.search(r"metered\s+elec", raw, re.I):
            continue
        amounts = _weakley_amounts(raw)
        if amounts:
            return amounts[-1]
    return Decimal("0.00")


def _weakley_amounts(text: str) -> list[Decimal]:
    amounts: list[Decimal] = []
    for token in re.findall(r"\$?\s*([0-9]{1,4}(?:[.,][0-9]{2}))\b", text or ""):
        value = _weakley_money(token)
        if value:
            amounts.append(value)
    return amounts


def _weakley_money(value: str) -> Decimal:
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    return money(text)


def _weakley_date_near(text: str, keywords: tuple[str, ...]) -> datetime | None:
    matches = _weakley_date_matches(text)
    if not matches:
        return None
    keyword_norm = tuple(k.upper() for k in keywords)
    for dt, start, _end in matches:
        window = (text or "")[max(0, start - 120) : start].upper()
        compact = re.sub(r"[^A-Z0-9]+", "", window)
        if all(k in window or k in compact for k in keyword_norm):
            return dt
    if "PENALTY" in keyword_norm:
        return matches[-1][0]
    return matches[0][0]


def _weakley_date_matches(text: str) -> list[tuple[datetime, int, int]]:
    normalized = _weakley_normalize_dates(text or "")
    month = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*"
    out: list[tuple[datetime, int, int]] = []
    for match in re.finditer(rf"\b{month}\s+([0-9OI]{{1,2}})\s*,?\s*(20[0-9OI]{{2}})\b", normalized, re.I):
        token = f"{match.group(1)[:3]} {match.group(2)} {match.group(3)}"
        parsed = _weakley_parse_date_token(token)
        if parsed:
            out.append((parsed, match.start(), match.end()))
    return out


def _weakley_normalize_dates(text: str) -> str:
    months = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
    text = re.sub(rf"\b({months})([0-9OI]{{2}})\s+(20[0-9OI]{{2}})\b", r"\1 \2 \3", text, flags=re.I)
    text = re.sub(rf"\b({months})\s+([0-3][0-9])\s*(20[0-9OI]{{2}})\b", r"\1 \2 \3", text, flags=re.I)
    return text


def _weakley_parse_date_token(token: str) -> datetime | None:
    text = token.upper().replace("O", "0").replace("I", "1").replace("L", "1")
    text = _clean(text)
    return _parse_date(text)


def _weakley_default_service_period(
    invoice_date: datetime | None,
    due_date: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    anchor = invoice_date or due_date
    if not anchor:
        return None, None
    if anchor.day <= 3:
        year = anchor.year
        month = anchor.month - 1
        if month == 0:
            month = 12
            year -= 1
        last = calendar.monthrange(year, month)[1]
        return datetime(year, month, 1), datetime(year, month, last)
    return datetime(anchor.year, anchor.month, 1), anchor


def _weakley_history_match(account: str, total: Decimal, service_address: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    account_key = re.sub(r"[^A-Z0-9]", "", (account or "").upper())
    address_key = _norm(service_address)
    path = settings.PROJECT_ROOT / "Gl Codes" / "General Ledger Report.csv"
    if not path.is_file():
        return out
    best_score = -1.0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if _norm(row.get("Vendor") or "") != _norm("Weakley County Municipal Electric System"):
                    continue
                if (row.get("Gl Accounts.Type") or "").lower() != "expense":
                    continue
                hay = f"{row.get('Reference') or ''} {row.get('Description') or ''}"
                hay_key = re.sub(r"[^A-Z0-9]", "", hay.upper())
                if account_key and account_key not in hay_key:
                    continue
                debit = money(row.get("Debit") or 0)
                credit = money(row.get("Credit") or 0)
                row_amount = debit if debit else credit
                amount_score = 2.0 if total and abs(row_amount - total) <= Decimal("0.02") else 0.0
                address_score = 1.0 if address_key and address_key in _norm(row.get("Description") or "") else 0.0
                score = amount_score + address_score
                if score < best_score:
                    continue
                period = re.search(
                    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})",
                    row.get("Description") or "",
                )
                if score > best_score:
                    best_score = score
                    out = {
                        "property_abbreviation": (row.get("Property") or "").strip(),
                        "gl_account": (row.get("GL_Account") or "").split(" ", 1)[0].strip(),
                        "invoice_date": _parse_date(row.get("Date") or ""),
                    }
                    if period:
                        out["service_period_start"] = _parse_date(period.group(1))
                        out["service_period_end"] = _parse_date(period.group(2))
                    desc = row.get("Description") or ""
                    addr = re.search(r"-\s*([0-9]{2,6}\s+BEAUMONT\s+ST(?:\s+APT\s+\w+|\s+\w+)?)", desc, re.I)
                    if addr:
                        out["service_address"] = _clean(addr.group(1))
    except Exception:
        return out
    return out


def _parse_birmingham_water(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"ACCOUNT NUMBER:\s*([0-9]+)", text, flags=re.I)
    invoice_date = _parse_date(_first(r"INVOICE DATE:\s*(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    due = _parse_date(_first(r"TOTAL AMOUNT DUE by\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    sp = re.search(r"SERVICE PERIOD:\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    service_address = _first(r"SERVICE ADDRESS:\s*([^\n]+(?:\n[^\n]+)?)", text, flags=re.I)
    water = _money_after_label(text, "CAW WATER SERVICE")
    sewer = _money_after_label(text, "JEFFERSON COUNTY SEWER SERVICE")
    total = _money_after_label(text, "Total Amount Due")
    lines = [
        UtilityChargeLine("CAW Water Service", water, gl_account=spec.default_gl, source_page=1),
        UtilityChargeLine("Jefferson County Sewer Service", sewer, gl_account=spec.default_gl, source_page=1),
    ]
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, Decimal("0.00"), source_file, total)]


def _parse_city_mcminnville_water(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    return _parse_tennessee_municipal_statement(spec, text, source_file, vendor_label="City of McMinnville")


def _parse_city_martin(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    return _parse_tennessee_municipal_statement(spec, text, source_file, vendor_label="City of Martin")


def _parse_tennessee_municipal_statement(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
    *,
    vendor_label: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account Number Service Period.*?\n\s*([0-9-]+)", text, flags=re.I | re.S) or _first(r"ACCOUNT NUMBER\s+([0-9-]+)", text, flags=re.I)
    sp = re.search(r"([0-9-]+)\s+(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    start = _parse_date(sp.group(2) if sp else "")
    end = _parse_date(sp.group(3) if sp else "")
    invoice_date = _parse_date(_first(r"DATE OF BILL\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I)) or end
    due = _parse_date(_first(r"DUE DATE\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I)) or _parse_date(_first(r"NET AMOUNT DUE NOW\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    service_address = _municipal_service_address(text, vendor_label)
    lines = _service_code_lines(text)
    tax = _money_code_line(text, "TAX")
    total = sum((l.money for l in lines), Decimal("0.00")) + tax
    if not total:
        total = _money_after_label(text, "AMOUNT DUE NOW")
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, tax, source_file, total)]


def _parse_chattanooga_wastewater(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"ACCOUNT=([^=]+)=ACCOUNT", text) or _first(r"ACCOUNT NUMBER\s*\n?[^\n]*?([0-9-]+)", text, flags=re.I)
    invoice_date = _parse_date(_first(r"BILLDATE=([^=]+)=BILLDATE", text))
    service_address = _first(r"ACCOUNT NAME SERVICE ADDRESS\s*\n.*?\s+(\d{3,6}\s+[A-Za-z0-9 .#-]+)", text, flags=re.I)
    sp = re.search(r"(\d{1,2}/\d{1,2}/\d{2})\s+to\s+(\d{1,2}/\d{1,2}/\d{2})", text, re.I)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    due = _parse_date(_first(r"Amount Due by\s+(\d{1,2}/\d{1,2}/\d{2})", text, flags=re.I))
    total = _money_after_label(text, "Sewer Usage Charges") or _money_after_label(text, "Amount Due by")
    # Chattanooga statements can split the "over minimum" OCR with underscores;
    # the source total is reliable and prevents dropping part of the sewer charge.
    lines = [UtilityChargeLine("Sewer Usage Charges", total, gl_account=spec.default_gl, source_page=1)] if total else []
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, Decimal("0.00"), source_file, total)]


def _parse_city_union_city(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account Number\s*\n.*?([0-9]{3}-[0-9]{4}-[0-9]{2})", text, flags=re.I | re.S) or _first(r"\b([0-9]{3}-[0-9]{4}-[0-9]{2})\b", text)
    service_address = _first(r"Name Service Address Account Number\s*\n.*?\s+([0-9]{3,6}\s+[A-Za-z0-9 .#-]+?)\s+[0-9]{3}-", text, flags=re.I | re.S)
    dates = re.search(
        r"Active\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d+\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,2}/\d{1,2}/\d{4}\s+(\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.I,
    )
    start = _parse_date(dates.group(1) if dates else "")
    end = _parse_date(dates.group(2) if dates else "")
    invoice_date = _parse_date(dates.group(3) if dates else "")
    due = _parse_date(dates.group(4) if dates else "")
    lines = _utility_named_lines(text, ["WATER", "SEWER", "SANITATION", "STORMWATER USER FEE"])
    tax = _money_after_label(text, "Tax")
    total = _money_after_label(text, "CURRENT BILL") or sum((l.money for l in lines), Decimal("0.00")) + tax
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, tax, source_file, total)]


def _parse_guardian_water(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"Customer Number:\s*([0-9]+)", text, flags=re.I)
    explicit = _first(r"Invoice Number:\s*([0-9]+)", text, flags=re.I)
    invoice_date = _parse_date(_first(r"Invoice Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    due = _parse_date(_first(r"Due Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I))
    service_address = _multiline_after(text, "BILL TO:", stop="INVOICE NO.")
    lines: list[UtilityChargeLine] = []
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not re.match(r"\d+\s+(?:BILLING FEE|FINAL BILLING FEE)", line, re.I):
            continue
        amounts = re.findall(r"\$?([\d,]+\.\d{2})", line)
        desc = re.sub(r"^\d+\s+", "", line)
        desc = re.sub(r"\s+\d+\s+\$?[\d,]+\.\d{2}.*$", "", desc).strip()
        if amounts:
            lines.append(UtilityChargeLine(_clean(desc), money(amounts[-1]), gl_account=spec.default_gl, source_page=1))
    total = _money_after_label(text, "TOTAL INVOICE AMOUNT") or _money_after_label(text, "Invoice Total")
    if not lines and total:
        lines = [UtilityChargeLine("Guardian Water Billing Fee", total, gl_account=spec.default_gl, source_page=1)]
    inv = _invoice(spec, account, explicit, invoice_date, due, invoice_date, invoice_date, service_address, lines, Decimal("0.00"), source_file, total)
    if "PARK AT CARSON" in service_address.upper():
        inv.property_abbreviation = "TPAC"
    return [inv]


def _parse_hopkinsville_electric(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _first(r"ACCOUNTNUMBER:\s*([0-9-]+)", text, flags=re.I) or _first(r"CUSTOMERACCOUNTNUMBER:\s*([0-9-]+)", text, flags=re.I)
    service_address = _first(r"SERVICEADDRESS:\s*([^\n]+)", text, flags=re.I)
    period = re.search(r"METERREADINGDATE:\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})\s*to\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})", text, re.I)
    start = _parse_date(period.group(1) if period else "")
    end = _parse_date(period.group(2) if period else "")
    due = _parse_date(_first(r"DUEBEFORE\s*(?:DISCONNECTPENDING)?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I)) or _parse_date(_first(r"DUEDATEFORCURRENTCHARGESONLY:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    service = _last_money_on_line_containing(text, "Electric Service")
    connect = _money_after_label(text, "Connect Fee")
    tax = _money_after_label(text, "Sales Tax") + _money_after_label(text, "Increase for School Tax")
    total = _money_after_label(text, "TOTALCURRENTCHARGES") or service + connect + tax
    lines = [UtilityChargeLine("Electric Service", service or total, gl_account=spec.default_gl, source_page=1)]
    if connect:
        lines.append(UtilityChargeLine("Connect Fee", connect, line_type="connection_fee", gl_account="6956", taxable=False, source_page=1))
    return [_invoice(spec, account, "", end or due, due or end, start, end, service_address, lines, tax, source_file, total)]


def _invoice(
    spec: Wave3VendorSpec,
    account: str,
    explicit_invoice: str,
    invoice_date: datetime | None,
    due: datetime | None,
    start: datetime | None,
    end: datetime | None,
    service_address: str,
    lines: list[UtilityChargeLine],
    tax: Decimal,
    source_file: str,
    total: Decimal,
) -> ParsedUtilityInvoice:
    lines = [line for line in lines if line.money != 0]
    return ParsedUtilityInvoice(
        vendor_key=spec.key,
        vendor_display_name=spec.display_name,
        account_number=_clean(account),
        invoice_number="",
        explicit_invoice_number=_clean(explicit_invoice),
        invoice_date=invoice_date,
        due_date=due or invoice_date,
        service_period_start=start or invoice_date,
        service_period_end=end or invoice_date,
        service_address=_clean(service_address),
        line_items=lines,
        tax_total=money(tax),
        source_file=source_file,
        debug_info={"source_total": str(money(total)), "community_billing": spec.community_billing},
    )


def _service_code_lines(text: str) -> list[UtilityChargeLine]:
    labels = {"WA": "Water", "SW": "Sewer", "GA": "Garbage"}
    lines: list[UtilityChargeLine] = []
    for code, amount in re.findall(r"\b(WA|SW|GA)\s+(?:[\d,.]+\s+){0,3}([\d,]+\.\d{2})\b", text):
        lines.append(UtilityChargeLine(labels[code], money(amount), source_page=1))
    return lines


def _utility_named_lines(text: str, labels: list[str]) -> list[UtilityChargeLine]:
    lines: list[UtilityChargeLine] = []
    seen: set[str] = set()
    sentinels = labels + ["Sales Tax", "Tax", "Total Current", "Current Bill", "Total"]
    for label in labels:
        for match in re.finditer(rf"\b{re.escape(label)}\b", text, re.I):
            if label.lower() in seen:
                break
            start = match.end()
            next_pos = len(text)
            for sentinel in sentinels:
                if sentinel.lower() == label.lower():
                    continue
                next_match = re.search(rf"\b{re.escape(sentinel)}\b", text[start:], re.I)
                if next_match:
                    next_pos = min(next_pos, start + next_match.start())
            segment = text[start:next_pos]
            amounts = re.findall(r"-?\$?[\d,]+\.\d{2}", segment)
            if not amounts:
                continue
            amount = money(amounts[0] if label.upper() == "STORMWATER USER FEE" else amounts[-1])
            if amount:
                classification = classify_utility_line(label)
                line_type = classification if classification != "service" else "service"
                lines.append(UtilityChargeLine(label.title(), amount, line_type=line_type, source_page=1))
                seen.add(label.lower())
                break
    return lines


def _money_code_line(text: str, code: str) -> Decimal:
    m = re.search(rf"\b{re.escape(code)}\b\s+(?:[\d,.]+\s+){{0,4}}([\d,]+\.\d{{2}})", text, re.I)
    return money(m.group(1)) if m else Decimal("0.00")


def _money_after_label(text: str, label: str) -> Decimal:
    pattern = re.escape(label).replace(r"\ ", r"\s*")
    match = re.search(rf"{pattern}\s*[:=]?\s*(?:\$|\=|\s)*([+-]?\s*\$?\s*[\d,]+\.\d{{2}})", text, re.I)
    if match:
        return money(match.group(1).replace(" ", "").replace("+", ""))
    match = re.search(rf"{pattern}.*?([+-]?\s*\$?\s*[\d,]+\.\d{{2}})", text, re.I | re.S)
    return money(match.group(1).replace(" ", "").replace("+", "")) if match else Decimal("0.00")


def _money_before_label(text: str, label: str) -> Decimal:
    match = re.search(rf"([\d,]+\.\d{{2}})\s+{re.escape(label)}", text, re.I)
    return money(match.group(1)) if match else Decimal("0.00")


def _parse_month_day(month: str, day: str, year: int | None) -> datetime | None:
    return _parse_date(f"{month} {day}, {year}") if year else None


def _multiline_after(text: str, marker: str, *, stop: str) -> str:
    chunk = _between(text, marker, stop)
    parts = [line for line in (_clean(x) for x in chunk.splitlines()) if line]
    return " ".join(parts[:4])


def _service_address_before_vendor(text: str, vendor_label: str) -> str:
    match = re.search(r"Name/Service Address\s+(.*?)\s+" + re.escape(vendor_label), text, re.I | re.S)
    if not match:
        return ""
    lines = [_clean(line) for line in match.group(1).splitlines() if _clean(line)]
    return " ".join(lines[-2:]) if len(lines) >= 2 else " ".join(lines)


def _municipal_service_address(text: str, vendor_label: str) -> str:
    chunk = _between(text, "Name/Service Address", "Account Number Service Period")
    if not chunk:
        return _service_address_before_vendor(text, vendor_label)
    street_hits = re.findall(
        r"\b(?!PO\s+Box)(\d{2,6}\s+[A-Za-z0-9 .'-]+?\b"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|Pike|Blvd|Boulevard|Circle|Cir|Way|Highway|Hwy)\b"
        r"(?:\s+(?:Apt|Unit|Ste|Suite)\s*[A-Za-z0-9-]+)?)",
        chunk,
        flags=re.I,
    )
    if street_hits:
        return _clean(street_hits[0])
    lines = [_clean(line) for line in chunk.splitlines() if _clean(line)]
    filtered: list[str] = []
    for line in lines:
        low = line.lower()
        if any(skip in low for skip in ("phone:", "fax:", "email:", "website:", "po box")):
            continue
        if vendor_label.lower() in low:
            continue
        filtered.append(line)
    street_lines = [line for line in filtered if re.search(r"\b\d{2,6}\s+[A-Za-z0-9]", line)]
    return street_lines[-1] if street_lines else (filtered[-1] if filtered else "")


def _last_money_on_line_containing(text: str, label: str) -> Decimal:
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if label.lower() not in line.lower():
            continue
        amounts = re.findall(r"-?\$?[\d,]+\.\d{2}", line)
        if amounts:
            return money(amounts[-1])
    return Decimal("0.00")


def _load_config(config_path: Path | str | None, vendor_key: str) -> dict[str, Any]:
    if config_path:
        path = Path(config_path)
        if path.is_file():
            try:
                import yaml

                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                return {}
    return load_vendor_config(vendor_key)


def _cancelled(callback: Callable[[], bool] | None, run_context: dict[str, Any]) -> bool:
    if callback is not None:
        try:
            return bool(callback())
        except Exception:
            return False
    hook = run_context.get("should_cancel")
    if callable(hook):
        try:
            return bool(hook())
        except Exception:
            return False
    return False


def _progress(callback: Callable[..., None] | None, **kwargs: Any) -> None:
    if callback is None:
        return
    try:
        callback(**kwargs)
    except Exception:
        pass


__all__ = [
    "ProcessBatchResult",
    "process_birmingham_water_works_batch",
    "process_city_of_chattanooga_wastewater_department_batch",
    "process_city_of_martin_batch",
    "process_city_of_mcminnville_water_sewer_dept_batch",
    "process_city_of_union_city_batch",
    "process_clarksville_gas_and_water_batch",
    "process_guardian_water_power_batch",
    "process_hopkinsville_electric_system_batch",
    "process_kentucky_utilities_batch",
    "process_knoxville_utilities_board_batch",
    "process_tennessee_american_water_batch",
    "process_union_city_energy_authority_batch",
    "process_wave3_utility_batch",
    "process_weakley_county_municipal_electric_system_batch",
]
