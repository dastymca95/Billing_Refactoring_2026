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
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from utils.property_lookup import UnitMatch, lookup_unit, match_by_address
from utils.text_normalization import proper_case_preserve_acronyms
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
    _lines,
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
        invoice_month_source="due_date",
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
    "nashville_electric_service": Wave3VendorSpec(
        "nashville_electric_service",
        "Nashville Electric Service",
        ("Nashville Electric Service", "NES", "nespower.com"),
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
    "cumberland_emc": Wave3VendorSpec(
        "cumberland_emc",
        "Cumberland EMC",
        (
            "Cumberland EMC",
            "Cumberland Electric Membership Corporation",
            "cemc.org",
        ),
        default_gl="6920",
        invoice_month_source="invoice_date",
    ),
    "pleasant_view_utility_district": Wave3VendorSpec(
        "pleasant_view_utility_district",
        "Pleasant View Utility District",
        (
            "Pleasant View Utility District",
            "PVUD",
            "pvudwater.com",
        ),
        default_gl="6955",
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


def process_nashville_electric_service_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("nashville_electric_service", **kwargs)


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


def process_cumberland_emc_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("cumberland_emc", **kwargs)


def process_pleasant_view_utility_district_batch(**kwargs: Any) -> ProcessBatchResult:
    return process_wave3_utility_batch("pleasant_view_utility_district", **kwargs)


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
            if spec.key == "union_city_energy_authority" and path.suffix.lower() == ".pdf":
                stable_text = _union_city_stable_pdf_text(path)
                stable_score = _union_city_text_score(stable_text)
                if stable_score >= 5 and stable_score >= _union_city_text_score(document_text):
                    document_text = stable_text
            if spec.key == "city_of_union_city" and path.suffix.lower() == ".pdf":
                stable_text = _union_city_stable_pdf_text(path)
                stable_score = _city_union_water_text_score(stable_text)
                if stable_score >= 4 and stable_score >= _city_union_water_text_score(document_text):
                    document_text = stable_text
            if spec.key == "nashville_electric_service" and path.suffix.lower() == ".pdf":
                stable_text = _nashville_stable_pdf_text(path)
                stable_score = _nashville_text_score(stable_text)
                if stable_score >= 8 and stable_score >= _nashville_text_score(document_text):
                    document_text = stable_text
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
                _clear_soft_vision_review_if_valid(inv)
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
        "nashville_electric_service": _parse_nashville_electric_service,
        "weakley_county_municipal_electric_system": _parse_weakley_electric,
        "birmingham_water_works": _parse_birmingham_water,
        "city_of_mcminnville_water_sewer_dept": _parse_city_mcminnville_water,
        "city_of_chattanooga_wastewater_department": _parse_chattanooga_wastewater,
        "city_of_martin": _parse_city_martin,
        "city_of_union_city": _parse_city_union_city,
        "guardian_water_power": _parse_guardian_water,
        "hopkinsville_electric_system": _parse_hopkinsville_electric,
        "cumberland_emc": _parse_cumberland_emc,
        "pleasant_view_utility_district": _parse_pleasant_view_utility_district,
    }[spec.key](spec, text, source_file)


def _clear_soft_vision_review_if_valid(inv: ParsedUtilityInvoice) -> None:
    """Do not flag a scanned bill when deterministic parsing already reconciled it."""

    reasons = set(inv.manual_review_reasons or [])
    if reasons != {"vision_recommended"}:
        return
    validation = inv.debug_info.get("validation")
    if not isinstance(validation, dict) or not validation.get("ok"):
        return
    expected = money(inv.debug_info.get("source_total") or 0)
    actual = sum((line.money for line in inv.line_items), Decimal("0.00"))
    if expected > 0 and abs(actual - expected) > Decimal("0.02"):
        return
    if not (inv.invoice_number and inv.invoice_date and inv.due_date and inv.property_abbreviation and inv.line_items):
        return
    inv.manual_review_reasons = []


def _parse_pleasant_view_utility_district(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    """Parse one Pleasant View Utility District statement per file.

    PVUD prints the account as CUSTOMER NUMBER and exposes the complete
    payable detail on page 1. TAX is allocated across the named services by
    the shared finalizer, so no invalid standalone tax row is produced.
    """

    body = text or ""
    billing_date = _parse_date(
        _first(
            r"BILLING\s+DATE\s+CUSTOMER\s+NAME\s+SERVICE\s+ADDRESS\s*"
            r"(?:\r?\n|\s)+(\d{1,2}/\d{1,2}/\d{2,4})",
            body,
            flags=re.IGNORECASE,
        )
    )
    account = _first(
        r"CUSTOMER\s+PIN\s+NUMBER\s+SERVICE\s+PERIOD\s+DAYS\s+STATUS\s*"
        r"(?:\r?\n|\s)+NUMBER\s+FROM\s+TO\s*"
        r"(?:\r?\n|\s)+(\d+)",
        body,
        flags=re.IGNORECASE,
    )
    service_address = _first(
        r"\d{1,2}/\d{1,2}/\d{2,4}\s+KM\s+DEVELOPMENTS\s+LLC\s+"
        r"([^\r\n]+)",
        body,
        flags=re.IGNORECASE,
    )
    service_address = re.sub(
        r"\s+(?:CUSTOMER\s+PIN|SERVICE\s+PERIOD).*$",
        "",
        _clean(service_address),
        flags=re.IGNORECASE,
    )
    service_address = re.sub(r"\bAV\b", "Ave", service_address, flags=re.IGNORECASE)

    period_match = re.search(
        r"\b\d+\s+\d+\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+\d+\s+[A-Z]\b",
        body,
        flags=re.IGNORECASE,
    )
    service_start = _parse_date(period_match.group(1) if period_match else "")
    service_end = _parse_date(period_match.group(2) if period_match else "")
    due_date = _parse_date(
        _first(
            r"LATE\s+DATE\s+TOTAL\s+AMOUNT\s+DUE.*?"
            r"(\d{1,2}/\d{1,2}/\d{2,4})",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        or _first(
            r"BILLDATE\s+CUSTOMERNUMBER\s+DATEDUE\s+"
            r"\d{1,2}/\d{1,2}/\d{2,4}\s+\d+\s+"
            r"(\d{1,2}/\d{1,2}/\d{2,4})",
            body,
            flags=re.IGNORECASE,
        )
    )

    charge_patterns = (
        ("Water Service", r"^\s*WATER\s+SERVICE\b.*?\$\s*([0-9,]+\.\d{2})\s*$"),
        ("Sewer Service", r"^\s*SEWER\s+SERVICE\b.*?\$\s*([0-9,]+\.\d{2})\s*$"),
        ("Water Leak Relief", r"^\s*WTR\s+LEAK\s+RELIEF\b.*?\$\s*([0-9,]+\.\d{2})\s*$"),
    )
    line_items: list[UtilityChargeLine] = []
    for description, pattern in charge_patterns:
        amount = money(_first(pattern, body, flags=re.IGNORECASE | re.MULTILINE))
        if amount:
            line_items.append(
                UtilityChargeLine(
                    description,
                    amount,
                    gl_account=spec.default_gl,
                    source_page=1,
                    metadata={"pvud_source_label": description},
                )
            )

    tax_total = money(
        _first(
            r"^\s*TAX\b.*?\$\s*([0-9,]+\.\d{2})\s*$",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
    total_due = money(
        _first(
            r"LATE\s+DATE\s+TOTAL\s+AMOUNT\s+DUE\s*"
            r"(?:\r?\n|\s)+\$\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        or _first(
            r"SERVICEADDRESS\s+TOTALDUE.*?\$\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )

    matched_unit = match_by_address(service_address, expected_property_abbrev="PVT")
    invoice = ParsedUtilityInvoice(
        vendor_key=spec.key,
        vendor_display_name=spec.display_name,
        account_number=account,
        invoice_number="",
        invoice_date=billing_date,
        due_date=due_date,
        service_period_start=service_start,
        service_period_end=service_end,
        service_address=service_address,
        line_items=line_items,
        tax_total=tax_total,
        source_file=source_file,
        property_abbreviation=(matched_unit.property_abbreviation if matched_unit else ""),
        location=(matched_unit.unit_number if matched_unit else ""),
        debug_info={
            "source_total": str(total_due),
            "tax_total": str(tax_total),
            "parser": "pleasant_view_utility_district_page1",
            "property_match_strategy": matched_unit.strategy if matched_unit else "",
        },
    )
    if not line_items:
        invoice.manual_review_reasons.append("pvud_charge_lines_not_found")
    return [invoice]


def _parse_cumberland_emc(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    """Parse Cumberland EMC/CEMC residential electric statements.

    The reliable billing detail lives on page 2 in the Account Information
    and Current Activity sections. Page 1 is a remittance summary and can
    confuse generic AI extraction into using the wrong vendor/property.
    """

    body = text or ""
    account = _first(
        r"Account\s+Information\s+Account\s+Number:\s*([0-9]{8,})",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ) or _first(r"Account\s+Number:\s*([0-9]{8,})", body, flags=re.IGNORECASE)

    billing_date = _parse_date(
        _first(r"Billing\s+Date:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", body, flags=re.IGNORECASE)
    )
    due_date = _parse_date(
        _first(
            r"Current\s+Charges\s+Due\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
            body,
            flags=re.IGNORECASE,
        )
        or _first(r"Drafted\s+On\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", body, flags=re.IGNORECASE)
    )

    service_start = service_end = None
    period_match = re.search(
        r"Billing\s+Period:.*?([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*-\s*"
        r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not period_match:
        period_match = re.search(
            r"\b\d{6,}\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s+"
            r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\b",
            body,
        )
    if period_match:
        service_start = _parse_date(period_match.group(1))
        service_end = _parse_date(period_match.group(2))

    address = _first(
        r"Service\s+Address:\s*([0-9][A-Z0-9 .#'\-]+?)(?:\s+PLEASANT\s+VIEW\b|\s+Rate:|\n)",
        body,
        flags=re.IGNORECASE,
    )
    if not address:
        address_match = re.search(
            r"Service\s+Address:\s*(?P<street>[^\n\r]+)\s*(?:\n|\r\n?)\s*"
            r"(?P<city>PLEASANT\s+VIEW,\s*TN\s+\d{5})",
            body,
            flags=re.IGNORECASE,
        )
        if address_match:
            address = _clean(address_match.group("street"))

    current_charges = money(
        _first(
            r"Current\s+Charges\s+\$?\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        or _first(
            r"Current\s+Charges\s+Due\s+[0-9/]+\s+\$?\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
    )
    balance_forward = money(
        _first(
            r"Balance\s+Forward\s+\$?\s*([-]?[0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
    )
    total_due = money(
        _first(
            r"Current\s+Charges\s+Due\s+[0-9/]+\s+\$?\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        or _first(
            r"AUTOPAY\s+AMOUNT\s+\$?\s*([0-9,]+\.\d{2})",
            body,
            flags=re.IGNORECASE,
        )
        or current_charges
    )

    line_items: list[UtilityChargeLine] = []
    for line in _lines(body):
        patterns = (
            r"(Base\s+Charge)\s+\$?\s*([0-9,]+\.\d{2})",
            r"(Energy\s+Charge\s+[0-9,]+\s+kWh\s+@\s+[0-9.]+)\s+\$?\s*([0-9,]+\.\d{2})",
            r"(TVA\s+Fuel\s+Cost\s+[0-9,]+\s+kWh\s+@\s+[0-9.]+)\s+\$?\s*([0-9,]+\.\d{2})",
        )
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if not match:
                continue
            amount = money(match.group(2))
            if amount:
                line_items.append(
                    UtilityChargeLine(
                        _clean(match.group(1)),
                        amount,
                        gl_account=spec.default_gl,
                        source_page=2,
                    )
                )
            break

    if balance_forward > Decimal("0.00"):
        line_items.append(
            UtilityChargeLine(
                "Balance Forward",
                balance_forward,
                gl_account=spec.default_gl,
                source_page=2,
                metadata={"source": "previous_account_activity"},
            )
        )

    expected_total = total_due if total_due else current_charges + max(balance_forward, Decimal("0.00"))
    inv = ParsedUtilityInvoice(
        vendor_key=spec.key,
        vendor_display_name=spec.display_name,
        account_number=account,
        invoice_number="",
        invoice_date=billing_date,
        due_date=due_date,
        service_period_start=service_start,
        service_period_end=service_end,
        service_address=address,
        line_items=line_items,
        tax_total=Decimal("0.00"),
        source_file=source_file,
        debug_info={
            "source_total": str(expected_total),
            "current_charges": str(current_charges),
            "balance_forward": str(balance_forward),
            "parser": "cumberland_emc_page2_current_activity",
        },
    )
    if not line_items:
        inv.manual_review_reasons.append("cemc_current_activity_lines_not_found")
    return [inv]


def _parse_clarksville(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    master_invoices = _parse_clarksville_master_bill(spec, text, source_file)
    if master_invoices:
        return master_invoices

    account = _first(r"(?:ACCOUNT\s*NO\.?|Account\s*#)\s*[:.]?\s*([0-9-]+\.[0-9]+)", text, flags=re.I)
    due = _parse_date(
        _first(
            r"DATE\s+DUE\s+TOTAL\s+DUE\s*(?:\r?\n|\s)+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
            text,
            flags=re.I,
        )
        or _first(r"DUE\s+DATE.*?([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", text, flags=re.I | re.S)
    )
    period = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})\s+to\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", text, re.I)
    start = _parse_date(period.group(1) if period else "")
    end = _parse_date(period.group(2) if period else "")
    service_address = _clarksville_meter_location(text)
    block = _between(text, "Current Billing", "Total Balance") or text
    lines = _utility_named_lines(block, ["Water", "Sewer", "Gas"])
    tax = _money_after_label(block, "Sales Tax")
    total = _money_after_label(block, "Total Current") or sum((l.money for l in lines), Decimal("0.00")) + tax
    invoice_date, latest_reading, invoice_date_source = _clarksville_invoice_date_from_readings([end], due)
    inv = _invoice(
        spec,
        account,
        "",
        invoice_date,
        due or invoice_date,
        start,
        end,
        service_address,
        lines,
        tax,
        source_file,
        total,
    )
    _clarksville_attach_invoice_date_debug(inv, invoice_date_source, latest_reading)
    return [inv]


def _parse_clarksville_master_bill(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    """Split Clarksville apartment master bills into one invoice per account.

    Clarksville's portal can emit a single master PDF with a master account in
    the header and many child accounts in the meter table. ResMan needs the
    child accounts as separate invoices, so the master total is used only for
    reconciliation.
    """

    if "Account Total:" not in text or "Grand Total:" not in text:
        return []

    due = _parse_date(
        _first(
            r"DATE\s+DUE\s+TOTAL\s+DUE\s*(?:\r?\n|\s)+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
            text,
            flags=re.I,
        )
    )
    master_account = _first(
        r"BANK\s+ACCT\s+DRAFTED\s+ON\s+ACCOUNT\s+NO\.\s*(?:\r?\n|\s)+"
        r"\d{1,2}/\d{1,2}/\d{2,4}\s+([0-9-]+\.[0-9]+)",
        text,
        flags=re.I,
    )
    grand_total = _money_after_label(text, "Grand Total")
    if not grand_total:
        grand_total = _money_after_label(text, "TOTAL DUE")

    accounts: dict[str, dict[str, Any]] = {}
    current_account = ""
    current_page = 1

    def entry_for(account: str) -> dict[str, Any]:
        return accounts.setdefault(
            account,
            {
                "account": account,
                "service_address": "",
                "start": None,
                "end": None,
                "lines": [],
                "tax": Decimal("0.00"),
                "total": Decimal("0.00"),
                "page": current_page,
            },
        )

    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not line:
            continue
        page_match = re.search(r"\bPage\s+(\d+)\s+of\s+\d+\b", line, re.I)
        if page_match:
            current_page = int(page_match.group(1)) + 1
            continue

        total_match = re.search(
            r"Account\s+Total:\s*([0-9-]+\.[0-9]+)\s+(-?[\d,]+\.\d{2})",
            line,
            flags=re.I,
        )
        if total_match:
            account = total_match.group(1)
            amount = money(total_match.group(2))
            if account == master_account and amount == 0:
                current_account = ""
                continue
            entry = entry_for(account)
            entry["total"] = amount
            current_account = account
            continue

        tax_match = re.match(r"^TX\s+(-?[\d,]+\.\d{2})$", line, flags=re.I)
        if tax_match and current_account:
            entry_for(current_account)["tax"] += money(tax_match.group(1))
            continue

        detail = _parse_clarksville_master_detail_line(line)
        if not detail:
            continue

        account = detail["account"]
        if account == master_account:
            current_account = ""
            continue
        entry = entry_for(account)
        current_account = account
        entry["page"] = current_page
        if detail.get("service_address"):
            entry["service_address"] = detail["service_address"]
        if detail.get("start"):
            entry["start"] = detail["start"]
        if detail.get("end"):
            entry["end"] = detail["end"]
        label = _clarksville_service_label(detail["code"])
        if label:
            entry["lines"].append(
                UtilityChargeLine(
                    label,
                    detail["amount"],
                    gl_account=spec.default_gl,
                    source_page=current_page,
                    metadata={"raw_clarksville_code": detail["code"]},
                )
            )

    invoice_date, latest_reading, invoice_date_source = _clarksville_invoice_date_from_readings(
        [entry.get("end") for entry in accounts.values()],
        due,
    )

    invoices: list[ParsedUtilityInvoice] = []
    for account, entry in accounts.items():
        total = money(entry.get("total") or 0)
        if total <= 0:
            continue
        lines: list[UtilityChargeLine] = list(entry.get("lines") or [])
        line_total = sum((line.money for line in lines), Decimal("0.00"))
        tax = money(entry.get("tax") or 0)
        if not lines and total:
            lines = [
                UtilityChargeLine(
                    "Water & Sewer",
                    total,
                    gl_account=spec.default_gl,
                    source_page=int(entry.get("page") or 1),
                )
            ]
            tax = Decimal("0.00")
        elif abs((line_total + tax) - total) > Decimal("0.02"):
            summary_total = _clarksville_summary_total(text, account)
            if summary_total:
                total = summary_total

        service_address = _clean_clarksville_master_meter_location(
            str(entry.get("service_address") or "")
        )
        inv = _invoice(
            spec,
            account,
            "",
            invoice_date,
            due,
            entry.get("start"),
            entry.get("end"),
            service_address,
            lines,
            tax,
            source_file,
            total,
        )
        location = _clarksville_location_from_meter_location(service_address)
        if location:
            inv.property_abbreviation = "TEC"
            inv.location = location
        elif _clarksville_is_element_address(service_address):
            inv.property_abbreviation = "TEC"
            inv.debug_info["property_level_service"] = True
        inv.debug_info.update(
            {
                "master_account_number": master_account,
                "master_bill_total": str(grand_total),
                "master_bill_split": True,
                "source_page": int(entry.get("page") or 1),
            }
        )
        _clarksville_attach_invoice_date_debug(inv, invoice_date_source, latest_reading)
        invoices.append(inv)

    if invoices and grand_total:
        parsed_total = sum((money(inv.debug_info.get("source_total") or 0) for inv in invoices), Decimal("0.00"))
        if abs(parsed_total - grand_total) > Decimal("0.02"):
            for inv in invoices:
                inv.manual_review_reasons.append("master_bill_total_reconciliation_failed")
                inv.debug_info["master_bill_reconciliation"] = {
                    "expected": str(grand_total),
                    "parsed_child_total": str(money(parsed_total)),
                    "difference": str(money(grand_total - parsed_total)),
                }
    return invoices


def _parse_clarksville_master_detail_line(line: str) -> dict[str, Any] | None:
    account_pattern = r"(?P<account>[0-9]{3}-[0-9]{4}\.[0-9]{3})"
    amount_pattern = r"(?P<amount>-?[\d,]+\.\d{2})"
    date_pattern = r"(?P<start>\d{1,2}/\d{1,2}/\d{2,4})\s+(?P<end>\d{1,2}/\d{1,2}/\d{2,4})"

    match = re.match(
        rf"^(?P<meter>[A-Z0-9]+)\s+(?P<address>.+?)\s+"
        rf"(?P<code>WT|WA|SW|GS|GA)\s+.*?\s+{date_pattern}\s+"
        rf"{account_pattern}\s+{amount_pattern}$",
        line,
        flags=re.I,
    )
    if match:
        return {
            "account": match.group("account"),
            "service_address": match.group("address"),
            "code": match.group("code").upper(),
            "amount": money(match.group("amount")),
            "start": _parse_date(match.group("start")),
            "end": _parse_date(match.group("end")),
        }

    match = re.match(
        rf"^(?P<code>WT|WA|SW|GS|GA)\s+.*?\s+"
        rf"{account_pattern}\s+{amount_pattern}$",
        line,
        flags=re.I,
    )
    if match:
        return {
            "account": match.group("account"),
            "service_address": "",
            "code": match.group("code").upper(),
            "amount": money(match.group("amount")),
            "start": None,
            "end": None,
        }
    return None


def _clarksville_service_label(code: str) -> str:
    return {
        "WT": "Water",
        "WA": "Water",
        "SW": "Sewer",
        "GS": "Gas",
        "GA": "Gas",
    }.get(code.upper(), "")


def _clarksville_invoice_date_from_readings(
    reading_dates: list[Any],
    due: datetime | None,
) -> tuple[datetime | None, datetime | None, str]:
    latest_reading = max(
        (value for value in reading_dates if isinstance(value, datetime)),
        default=None,
    )
    due_based = due - timedelta(days=18) if due else None
    if due_based and latest_reading and due_based < latest_reading:
        return latest_reading, latest_reading, "latest_current_reading_floor"
    if due_based:
        return due_based, latest_reading, "due_date_minus_18_days"
    if latest_reading:
        return latest_reading, latest_reading, "latest_current_reading_fallback"
    return due, latest_reading, "due_date_fallback" if due else ""


def _clarksville_attach_invoice_date_debug(
    inv: ParsedUtilityInvoice,
    source: str,
    latest_reading: datetime | None,
) -> None:
    inv.debug_info["invoice_date_source"] = source
    if latest_reading:
        inv.debug_info["latest_current_reading_date"] = _fmt_date(latest_reading)
    if inv.invoice_date and inv.due_date:
        inv.debug_info["days_from_invoice_to_due"] = str((inv.due_date - inv.invoice_date).days)
    if inv.invoice_date and latest_reading:
        inv.debug_info["days_from_latest_reading_to_invoice"] = str((inv.invoice_date - latest_reading).days)


def _clarksville_summary_total(text: str, account: str) -> Decimal:
    match = re.search(
        rf"^{re.escape(account)}\s+.+?\s+(-?[\d,]+\.\d{{2}})$",
        text,
        flags=re.I | re.M,
    )
    return money(match.group(1)) if match else Decimal("0.00")


def _clean_clarksville_master_meter_location(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = re.sub(r"\s+LOT\s+\d+\b", "", text, flags=re.I)
    text = re.sub(r"^(\d+)\s+-\s*", r"\1 -", text)
    return _clean(text)


def _clarksville_location_from_meter_location(service_address: str) -> str:
    match = re.match(
        r"^(?P<number>\d+)\s+-\s*(?P<unit>[A-Z0-9]+)\s+(?P<street>.+)$",
        _clean(service_address),
        flags=re.I,
    )
    if not match:
        return ""
    number = match.group("number")
    unit = match.group("unit").upper()
    street = match.group("street").upper()
    if unit in {"FP", "FIRE", "MASTER"}:
        return ""
    variants: list[str] = []
    compact = re.match(r"^(\d+)([A-Z])$", unit)
    if compact:
        variants.append(f"{number}-{compact.group(1)} {compact.group(2)}")
    variants.extend(
        [
            f"{number}{unit}",
            f"{number} {unit}",
            f"{number}-{unit}",
            unit,
        ]
    )
    try:
        from utils.property_lookup import lookup_unit
    except Exception:
        lookup_unit = None  # type: ignore[assignment]
    if lookup_unit:
        for candidate in variants:
            match_result = lookup_unit("TEC", candidate)
            if match_result:
                return match_result.unit_number
    if "COBALT" in street:
        if compact:
            return f"{number}-{compact.group(1)} {compact.group(2)}"
        return f"{number} {unit}"
    if "TERMINAL" in street:
        return f"{number}{unit}"
    return variants[0] if variants else ""


def _clarksville_is_element_address(service_address: str) -> bool:
    text = service_address.upper()
    return "COBALT" in text or "TERMINAL" in text or "MASTER WAY" in text


def _clarksville_meter_location(text: str) -> str:
    """Extract Clarksville's bill-specific Meter Location.

    The PDF text usually renders as:

        BILLING PERIOD METER LOCATION
        Mar 24, 2026 to Apr 21, 2026 850 -A PROFESSIONAL PARK DR

    A generic ``METER LOCATION`` fallback used to capture ``Mar 24`` as the
    address, which then polluted ResMan descriptions. Prefer the address that
    appears after the service-period range and reject date-looking captures.
    """

    patterns = [
        r"BILLING\s+PERIOD\s+METER\s+LOCATION\s*(?:\r?\n|\s)+"
        r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+to\s+"
        r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+([^\n]+)",
        r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+to\s+"
        r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\s+([0-9][^\n]+)",
        r"METER\s+LOCATION\s*(?:\r?\n|\s)+([0-9][0-9A-Z .#-]+)",
    ]
    for pattern in patterns:
        hit = _first(pattern, text, flags=re.I)
        cleaned = _clean_clarksville_meter_location(hit)
        if cleaned:
            return cleaned
    return ""


def _clean_clarksville_meter_location(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = re.split(r"\b(?:The total|payment will|Water\b|Sewer\b|Gas\b|Sales Tax)\b", text, 1, flags=re.I)[0]
    text = _clean(text)
    if re.match(r"^[A-Za-z]{3,9}\s+\d{1,2}\b", text):
        return ""
    if not re.match(r"^\d", text):
        return ""
    text = re.sub(r"\s+-\s*([A-Za-z0-9])\b", r"-\1", text)
    text = re.sub(r"\s+#\s*", " #", text)
    return _clean(text)


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
    collective = _parse_kentucky_collective_bill(spec, text, source_file)
    if collective:
        return collective

    account = _first(r"Account\s*#\s*([0-9-]+)", text, flags=re.I)
    due = _parse_date(_first(r"AMOUNT DUE\s+DUE DATE.*?\$?[\d,]+\.\d{2}\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I | re.S))
    invoice_date = _parse_date(_first(r"Total Current Charges as of\s+(\d{1,2}/\d{1,2}/\d{2})", text, flags=re.I))
    service_address = _first(r"Service Address:\s*([^\n]+)", text, flags=re.I)
    service = _money_after_label(text, "Current Electric Charges")
    tax = _money_after_label(text, "Current Taxes and Fees")
    total = _money_after_label(text, "Total Current Charges as of") or service + tax
    lines = [UtilityChargeLine("Current Electric Charges", service or total, gl_account=spec.default_gl, source_page=1)]
    return [_invoice(spec, account, "", invoice_date, due, invoice_date, invoice_date, service_address, lines, tax, source_file, total)]


def _parse_kentucky_collective_bill(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    if not re.search(r"\bCollective\s+Account\b", text or "", flags=re.I):
        return []

    account = _normalize_ku_account(
        _first(r"Mailed\s+\d{1,2}/\d{1,2}/\d{2,4}\s+for\s+Collective\s+Account\s*#\s*([0-9 -]+)", text, flags=re.I)
        or _first(r"Collective\s+Account\s*#\s*([0-9 -]+)", text, flags=re.I)
    )
    invoice_date = _parse_date(
        _first(r"Mailed\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+for\s+Collective\s+Account", text, flags=re.I)
        or _first(r"Collective\s+Account\s+Balance\s+as\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I)
    )
    due = _parse_date(
        _first(r"AMOUNT\s+DUE\s+DUE\s+DATE\s+\$?[\d,]+\.\d{2}\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I | re.S)
        or _first(r"Amount\s+Due\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+\$?[\d,]+\.\d{2}", text, flags=re.I)
    )
    service_address = _clean(
        _first(r"Service\s+Address:\s*([^\n]+)", text, flags=re.I)
        or _first(r"Account\s+Name:\s*.*?\s+Service\s+Address:\s*([^\n]+)", text, flags=re.I)
    )
    total = (
        _money_after_label(text, "Current Utility Charges Billed")
        or _money_after_label(text, "Total Current Charges Billed")
        or _money_after_label(text, "Total Amount Due")
    )

    lines: list[UtilityChargeLine] = []
    read_dates: list[datetime] = []
    current_page = 1
    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        page_hit = re.match(r"Page\s+(\d+)\b", line, flags=re.I)
        if page_hit:
            try:
                current_page = int(page_hit.group(1))
            except ValueError:
                current_page = max(1, current_page)
        read_date = _parse_date(
            _first(r"^\d{3}\s+(\d{1,2}/\d{1,2}/\d{2,4})\b", line)
        )
        if read_date:
            read_dates.append(read_date)
        detail = re.match(
            r"(?P<account>\d{4}-\d{4}-\d{4})\s+(?P<address>.+?)\s+\$(?P<amount>[\d,]+\.\d{2})$",
            line,
            flags=re.I,
        )
        if not detail:
            continue
        detail_account = detail.group("account")
        detail_address = _clean(detail.group("address"))
        amount = money(detail.group("amount"))
        if amount == 0:
            continue
        detail_location = _ku_collective_location(detail_address)
        gl = "6910" if detail_location else "6915"
        lines.append(
            UtilityChargeLine(
                f"{detail_account} - {detail_address}",
                amount,
                gl_account=gl,
                source_page=current_page,
                metadata={
                    "ku_collective_detail_account": detail_account,
                    "ku_collective_detail_address": detail_address,
                    "ku_collective_detail_location": detail_location,
                    "ku_collective_line_type": "billable_unit" if detail_location else "common_area",
                    "row_location": detail_location,
                },
            )
        )

    read_date = read_dates[0] if read_dates else invoice_date
    service_period_start = read_date - timedelta(days=30) if read_date else invoice_date
    for line in lines:
        detail_address = str(line.metadata.get("ku_collective_detail_address") or "")
        detail_location = str(line.metadata.get("ku_collective_detail_location") or "")
        description_address = _ku_collective_description_address(
            detail_address,
            detail_location,
        )
        period = _ku_collective_description_period(service_period_start, read_date)
        invoice_description = " - ".join(
            part for part in (period, description_address) if part
        )
        line.metadata.update(
            {
                "row_service_address": description_address,
                "row_invoice_description": invoice_description,
                "row_line_item_description": (
                    f"{invoice_description} - Electric Service"
                    if invoice_description
                    else "Electric Service"
                ),
            }
        )
    inv = _invoice(
        spec,
        account,
        "",
        invoice_date,
        due or invoice_date,
        service_period_start,
        read_date,
        service_address,
        lines,
        Decimal("0.00"),
        source_file,
        total,
    )
    inv.debug_info.update(
        {
            "ku_collective_bill": True,
            "ku_detail_accounts_count": len(lines),
            "property_level_service": True,
        }
    )
    if not lines:
        inv.manual_review_reasons.append("ku_collective_detail_lines_missing")
    return [inv]


def _normalize_ku_account(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12:
        return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
    return _clean(value)


def _ku_collective_location(address: str) -> str:
    match = re.search(
        r"\b(?P<building>\d{3,6})\s+.+?\bAPT\s+(?P<unit>[A-Z0-9-]+)\b",
        address or "",
        flags=re.I,
    )
    if not match:
        return ""
    candidate = f"{match.group('building')}-{match.group('unit').upper()}"
    canonical = lookup_unit("BCA", candidate)
    return canonical.unit_number if canonical else ""


def _ku_collective_description_address(address: str, location: str) -> str:
    cleaned = _clean(address)
    if location:
        cleaned = re.sub(r"^\d{3,6}\s+", "", cleaned)
        cleaned = re.sub(r"\s+APT\s+[A-Z0-9-]+\b", "", cleaned, flags=re.I)
        cleaned = f"{location} {cleaned}"
    else:
        cleaned = re.sub(r"\s+(?:HSE|HOUSE)\b.*$", "", cleaned, flags=re.I)
    return proper_case_preserve_acronyms(cleaned)


def _ku_collective_description_period(
    start: datetime | None,
    end: datetime | None,
) -> str:
    if start and end:
        return f"{start.strftime('%m/%d/%y')}-{end.strftime('%m/%d/%y')}"
    if end:
        return end.strftime("%m/%d/%y")
    return ""


def _ku_is_billable_unit_address(address: str) -> bool:
    return bool(_ku_collective_location(address))


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
    account = (
        _first(r"ACCOUNT\s+NUMBER:\s*(\d{6}-\d{6})", text, flags=re.I)
        or _first(r"CUSTOMER\s+ACCOUNT\s+NO\.?:\s*(\d{6}-\d{6})", text, flags=re.I)
        or _first(r"BANK DRAFT\s+(\d{6}-\d{6})", text, flags=re.I)
        or _first(r"\b(\d{6}-\d{6})\b", text)
    )
    meter_date = _parse_date(
        _first(r"METER\s+READING\s+DATE:\s*([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4})", text, flags=re.I)
    )
    due = _union_city_due_date(text)
    invoice_date = meter_date or (due - timedelta(days=20) if due else None)
    days_billed_text = _first(r"DAYS\s+BILLED:\s*(\d{1,3})", text, flags=re.I)
    try:
        days_billed = int(days_billed_text) if days_billed_text else 0
    except ValueError:
        days_billed = 0
    period_end = invoice_date
    period_start = (
        invoice_date - timedelta(days=max(0, days_billed - 1))
        if invoice_date and days_billed
        else invoice_date
    )
    service_address = _union_city_service_address(text)
    total = _money_after_label(text, "TOTAL CURRENT CHARGES") or _money_after_label(text, "NET AMOUNT DUE")
    tax = _union_city_tax_amount(text)
    lines = _union_city_charge_lines(text, spec, service_address, total, tax)
    inv = _invoice(
        spec,
        account,
        "",
        invoice_date,
        due or invoice_date,
        period_start,
        period_end,
        service_address,
        lines,
        tax,
        source_file,
        total,
    )
    inv.debug_info["union_city_days_billed"] = days_billed
    inv.debug_info["union_city_meter_reading_date"] = meter_date.strftime("%Y-%m-%d") if meter_date else ""
    inv.debug_info["union_city_parser_hardened"] = True
    inv.location = _union_city_location_from_address(service_address)
    return [inv]


def _parse_nashville_electric_service(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"Account\s*#:\s*([0-9]+)", text, flags=re.I)
    due = _parse_date(_first(r"Current\s+balance\s+due\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", text, flags=re.I))
    period = re.search(
        r"Billing\s+period:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*-\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        text,
        re.I,
    )
    start = _parse_date(period.group(1)) if period else None
    end = _parse_date(period.group(2)) if period else None
    invoice_date = end
    total = _money_after_label(text, "Total amount due")

    invoices: list[ParsedUtilityInvoice] = []
    service_matches = list(
        re.finditer(
            r"Service\s+Address:\s*(?P<address>.+?)\s+Premise\s+ID:\s*(?P<premise>[0-9]+)",
            text,
            re.I,
        )
    )
    for idx, match in enumerate(service_matches):
        raw_address = _clean(match.group("address"))
        block_end = service_matches[idx + 1].start() if idx + 1 < len(service_matches) else len(text)
        block = text[match.end():block_end]
        block = re.split(
            r"\b(?:Other\s+Bill\s+Charges|For\s+more\s+information|Other\s+ways\s+to\s+pay)\b",
            block,
            maxsplit=1,
            flags=re.I,
        )[0]
        amount_due = _money_after_label(block, "Amount due")
        parsed_lines, tax = _nashville_charge_lines(block, raw_address, amount_due)
        if not parsed_lines and amount_due:
            parsed_lines = [
                UtilityChargeLine(
                    _nashville_default_line_label(raw_address),
                    amount_due,
                    gl_account=_nashville_gl_for_address(raw_address),
                    source_page=1,
                )
            ]
            tax = Decimal("0.00")
        inv = _invoice(
            spec,
            account,
            "",
            invoice_date,
            due or invoice_date,
            start,
            end,
            _nashville_normalize_address(raw_address),
            parsed_lines,
            tax,
            source_file,
            amount_due,
        )
        _nashville_apply_property_location(inv)
        invoices.append(inv)

    other_amount = _nashville_other_bill_charge_amount(text)
    if other_amount:
        service_address = _nashville_other_charge_service_address(text)
        inv = _invoice(
            spec,
            account,
            "",
            invoice_date,
            due or invoice_date,
            start,
            end,
            service_address,
            [
                UtilityChargeLine(
                    "Power of Change",
                    other_amount,
                    gl_account="6915",
                    taxable=False,
                    source_page=1,
                    metadata={"nes_other_bill_charge": "Power of Change Amount"},
                )
            ],
            Decimal("0.00"),
            source_file,
            other_amount,
        )
        inv.debug_info["property_level_service"] = True
        _nashville_apply_property_location(inv)
        invoices.append(inv)

    if not invoices and total:
        service_address = _nashville_fallback_property_address(text)
        parsed_lines, tax = _nashville_charge_lines(text, service_address, total)
        if not parsed_lines:
            parsed_lines = [
                UtilityChargeLine(
                    "Electric Service",
                    total,
                    gl_account=_nashville_gl_for_address(service_address),
                    source_page=1,
                )
            ]
            tax = Decimal("0.00")
        inv = _invoice(
            spec,
            account,
            "",
            invoice_date,
            due or invoice_date,
            start,
            end,
            service_address,
            parsed_lines,
            tax,
            source_file,
            total,
        )
        _nashville_apply_property_location(inv)
        invoices.append(inv)
    return invoices


def _nashville_charge_lines(
    block: str,
    service_address: str,
    amount_due: Decimal,
) -> tuple[list[UtilityChargeLine], Decimal]:
    lines: list[UtilityChargeLine] = []
    tax_total = Decimal("0.00")
    in_table = False
    for raw in _lines(block):
        if re.search(r"Type\s+of\s+charge\s+Calculation\s+Amount", raw, re.I):
            in_table = True
            continue
        if not in_table:
            continue
        if re.match(r"((?:Total\s+)?Amount\s+due|Meter\s+#|Current\s+Month|Previous\s+Month|Change\s+from)", raw, re.I):
            break
        match = re.match(r"(?P<label>.+?)\s+\$?(?P<amount>[\d,]+\.\d{2})$", raw)
        if not match:
            continue
        label = _clean(match.group("label"))
        amount = money(match.group("amount"))
        if amount == 0:
            continue
        if re.search(r"\bSales\s+Tax\b", label, re.I):
            tax_total += amount
            continue
        lines.append(
            UtilityChargeLine(
                _nashville_simplify_charge_label(label),
                amount,
                gl_account=_nashville_gl_for_line(service_address, label),
                source_page=1,
            )
        )

    if amount_due and lines:
        expected = sum((line.money for line in lines), Decimal("0.00")) + tax_total
        if abs(expected - amount_due) > Decimal("0.02"):
            return (
                [
                    UtilityChargeLine(
                        _nashville_default_line_label(service_address),
                        amount_due,
                        gl_account=_nashville_gl_for_address(service_address),
                        source_page=1,
                    )
                ],
                Decimal("0.00"),
            )
    return lines, tax_total


def _nashville_simplify_charge_label(label: str) -> str:
    cleaned = _clean(label)
    if re.match(r"Rental\s+Lights?\b", cleaned, re.I):
        count = _first(r"Rental\s+Lights?\s+([0-9]+)", cleaned, flags=re.I)
        return f"Rental Lights ({count})" if count else "Rental Lights"
    for prefix in (
        "Service Charge",
        "TVA Grid Access Charge",
        "Demand Minimum",
        "Demand Charge",
        "Capacity Charge",
        "Energy Charge",
        "TVA Fuel Cost Adjustment",
    ):
        if cleaned.lower().startswith(prefix.lower()):
            return prefix
    return cleaned


def _nashville_default_line_label(service_address: str) -> str:
    if re.search(r"\b(?:LNDR|Laundry|House|Common)\b", service_address or "", re.I):
        return "Common Area Electric Service"
    return "Electric Service"


def _nashville_gl_for_line(service_address: str, label: str) -> str:
    if re.search(r"\bRental\s+Lights?\b", label or "", re.I):
        return "6915"
    return _nashville_gl_for_address(service_address)


def _nashville_gl_for_address(service_address: str) -> str:
    match = _nashville_property_location_match(service_address)
    if not match or not match.unit_number:
        return "6915"
    return "6920"


def _nashville_normalize_address(value: str) -> str:
    text = _clean(value)
    text = text.upper()
    replacements = {
        "CLIFTON AVE": "Clifton Ave",
        "BRICK CHURCH PIKE": "Brick Church Pike",
        "UNIT": "Unit",
    }
    for src, dst in replacements.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.I)
    return _clean(text.title().replace("Tva", "TVA").replace(" Lndr", " LNDR"))


def _nashville_property_location_match(service_address: str) -> UnitMatch | None:
    text = _clean(service_address).upper()
    if re.search(r"\b2100\s+CLIFTON\s+AVE\s+UNIT\s+([0-9]+)\b", text):
        unit = _first(r"\b2100\s+CLIFTON\s+AVE\s+UNIT\s+([0-9]+)\b", text)
        return lookup_unit("TKA", f"2100-{int(unit)}") if unit.isdigit() else match_by_address(service_address)
    if re.search(r"\b2102\s+CLIFTON\s+AVE\s+UNIT\s+([0-9]+)\b", text):
        unit = _first(r"\b2102\s+CLIFTON\s+AVE\s+UNIT\s+([0-9]+)\b", text)
        return lookup_unit("TKA", f"2102-{int(unit):02d}") if unit.isdigit() else match_by_address(service_address)
    if "CLIFTON AVE" in text:
        return match_by_address("2100 Clifton Ave")
    if "BRICK CHURCH PIKE" in text:
        match = match_by_address("1400 Brick Church Pike")
        if match:
            return UnitMatch(
                property_abbreviation=match.property_abbreviation,
                property_name=match.property_name,
                unit_number="",
                address=match.address,
                strategy="nashville_property_level",
                confidence=0.8,
            )
    return match_by_address(service_address)


def _nashville_apply_property_location(inv: ParsedUtilityInvoice) -> None:
    match = _nashville_property_location_match(inv.service_address)
    if not match:
        return
    inv.property_abbreviation = match.property_abbreviation
    if match.unit_number:
        inv.location = match.unit_number
    else:
        inv.location = ""
        inv.debug_info["property_level_service"] = True


def _nashville_other_bill_charge_amount(text: str) -> Decimal:
    block = _between(text, "Other Bill Charges", "For more information") or ""
    if not block:
        start = re.search(r"Other\s+Bill\s+Charges", text, re.I)
        if not start:
            return Decimal("0.00")
        block = text[start.start():]
    return _money_after_label(block, "Power of Change Amount")


def _nashville_other_charge_service_address(text: str) -> str:
    first_service = _first(r"Service\s+Address:\s*(.+?)\s+Premise\s+ID:", text, flags=re.I)
    if first_service and "CLIFTON AVE" in first_service.upper():
        return "2100 Clifton Ave"
    return _nashville_fallback_property_address(text)


def _nashville_fallback_property_address(text: str) -> str:
    first_service = _first(r"Service\s+Address:\s*(.+?)\s+Premise\s+ID:", text, flags=re.I)
    if first_service:
        return _nashville_normalize_address(first_service)
    header = _first(
        r"NASH\s+BRICK\s+CHURCH\s+LLC\s+(?:Total\s+amount\s+due\s+)?([0-9]+\s+BRICK\s+CHURCH\s+PIKE\s+UNIT\s+[A-Z0-9-]+)",
        text,
        flags=re.I,
    )
    if header:
        return _nashville_normalize_address(header)
    return ""


def _nashville_stable_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _nashville_text_score(text: str) -> int:
    if not text:
        return 0
    hay = text.lower()
    compact = re.sub(r"\s+", "", hay)
    score = hay.count("service address")
    score += 4 if "nashville electric service" in hay or "nespower.com" in hay else 0
    score += 3 if "account #:" in hay else 0
    score += 3 if "billing period:" in hay else 0
    score += 3 if "type of charge" in hay and "amount due" in hay else 0
    score += 2 if "nashbrickchurchllc" in compact or "nash bami llc" in hay else 0
    return score


def _union_city_stable_pdf_text(path: Path) -> str:
    """Use the shared PDF extractor when pdfplumber-only ingestion falls to OCR.

    Union City PDFs in this layout have malformed page metadata that makes the
    generic ingestion path abandon digital text and OCR the rendered page. The
    lower-level extractor has a stronger fallback and recovers the clean text
    layer, which carries exact dates, addresses, and charge rows.
    """

    candidates: list[str] = []
    try:
        from utils.pdf_text_extractor import extract_pdf_text
    except Exception:
        result = None
    else:
        try:
            result = extract_pdf_text(
                path,
                digital_text_first=True,
                ocr_if_text_missing=True,
                ocr_dpi=200,
            )
        except Exception:
            result = None
    if result is not None:
        candidates.append("\n".join(page.text for page in result.pages if page.text))
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        candidates.append("\n".join((page.extract_text() or "") for page in reader.pages))
    except Exception:
        pass
    return max(candidates, key=_union_city_text_score, default="")


def _union_city_text_score(text: str) -> int:
    hay = (text or "").upper()
    score = 0
    for token in (
        "ACCOUNT NUMBER:",
        "SERVICE ADDRESS:",
        "METER READING DATE:",
        "DAYS BILLED:",
        "TOTAL CURRENT CHARGES",
        "PAST-DUE AFTER:",
        "TN SALES TAX",
    ):
        if token in hay:
            score += 1
    return score


def _city_union_water_text_score(text: str) -> int:
    """Score City of Union City Water & Sewer statements.

    This is intentionally separate from Union City Energy Authority scoring:
    both vendors share "Union City" in the name, but their layouts and
    accounting treatment are unrelated.
    """

    hay = (text or "").upper()
    score = 0
    for token in (
        "CITY OF UNION CITY - WATER",
        "WATER & SEWER DEPARTMENT",
        "SERVICE DATES",
        "STORMWATER USER FEE",
        "CURRENT BILL",
        "AMOUNT DUE",
        "PAID BY DRAFT",
    ):
        if token in hay:
            score += 1
    if re.search(r"\b\d{3}\s*-\s*\d{2}\s*\d{2}\s*-\s*\d{2}\b", hay):
        score += 1
    if re.search(r"\b\d{3,5}\s+HIGH\s+SCHOOL\s+DR\s*#\s*\d+\b", hay):
        score += 1
    return score


def _union_city_due_date(text: str) -> datetime | None:
    explicit = _first(
        r"PAST-?DUE\s+AFTER:\s*([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4})",
        text,
        flags=re.I,
    )
    if explicit:
        return _parse_date(explicit)
    compact = _first(
        r"PAST-?DUE\s+AFTER:\s*([A-Za-z]{3,9}\s+\d{1,2}\s*\d{4})",
        text,
        flags=re.I,
    )
    if compact:
        compact = re.sub(r"([A-Za-z]{3,9})\s+(\d{1,2})(\d{4})", r"\1 \2 \3", compact)
        return _parse_date(compact)
    coupon = _first(
        r"NET\s+AMOUNT\s+DUE:?.{0,80}?([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4})",
        text,
        flags=re.I | re.S,
    )
    return _parse_date(coupon)


def _union_city_service_address(text: str) -> str:
    lines = _lines(text)
    for idx, line in enumerate(lines):
        if not re.fullmatch(r"SERVICE\s+ADDRESS:?", line, flags=re.I):
            continue
        candidates: list[str] = []
        for nxt in lines[idx + 1: idx + 6]:
            if re.match(r"(METER\s+READING|DAYS\s+BILLED|ACCOUNT\s+NUMBER|CUSTOMER\s+NAME)", nxt, flags=re.I):
                break
            if re.fullmatch(r"VILLAGES\s+OF\s+AUTUMNWOOD", nxt, flags=re.I):
                continue
            candidates.append(nxt)
        if candidates:
            return _union_city_normalize_service_address(candidates[0])
    return _union_city_normalize_service_address(
        _first(
            r"VILLAGES\s+OF\s+AUTUMNWOOD\s+([0-9A-Z ]+(?:APT\s+[A-Z0-9-]+|DR|EXT))",
            text,
            flags=re.I,
        )
    )


def _union_city_normalize_service_address(value: str) -> str:
    cleaned = _clean(value).upper()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\bHIGH\s+SCHOOL\s+APT\b", "HIGH SCHOOL DR APT", cleaned)
    cleaned = re.sub(r"\bHIGH\s+SCHOOL\s*$", "HIGH SCHOOL DR", cleaned)
    cleaned = re.sub(r"\bHIGH\s+SCHOOL\s+DRIVE\b", "HIGH SCHOOL DR", cleaned)
    cleaned = re.sub(r"\bMATHEWS\s+EXT\b", "MATHEWS EXT", cleaned)
    return _clean(cleaned.title().replace(" Dr", " Dr").replace(" Tn", " TN"))


def _union_city_location_from_address(service_address: str) -> str:
    unit = _first(r"\bAPT\s+([A-Z0-9-]+)\b", service_address or "", flags=re.I)
    if unit:
        return unit.upper()
    street_unit = _first(r"^(\d{3,5})\s+(?:E\s+)?MAT(?:H|T)EWS\b", service_address or "", flags=re.I)
    return street_unit if street_unit else ""


def _union_city_gl_for_metered_electric(spec: Wave3VendorSpec, service_address: str) -> str:
    if re.search(r"\bAPT\b", service_address or "", flags=re.I):
        return spec.default_gl
    if _union_city_location_from_address(service_address):
        return spec.default_gl
    return ""


def _union_city_tax_amount(text: str) -> Decimal:
    tax = _first(
        r"TN\s+Sales\s+Tax\s*@\s*7\.0%\s*([\d,]+\.\d{2})",
        text,
        flags=re.I,
    )
    return money(tax)


def _union_city_charge_lines(
    text: str,
    spec: Wave3VendorSpec,
    service_address: str,
    total: Decimal,
    tax: Decimal,
) -> list[UtilityChargeLine]:
    lines: list[UtilityChargeLine] = []
    seen_charge_keys: set[tuple[str, Decimal]] = set()

    for match in re.finditer(
        r"(?P<label>\d+\s+[A-Z]+\s+OUTDOOR\s+LIGHT\s*\(\d+\))\s+0\s+0\s+\d+\s+(?P<amount>[\d,]+\.\d{2})",
        text,
        flags=re.I,
    ):
        label = _clean(match.group("label")).title()
        amount = money(match.group("amount"))
        key = (label.upper(), amount)
        if key in seen_charge_keys:
            continue
        seen_charge_keys.add(key)
        lines.append(
            UtilityChargeLine(
                label,
                amount,
                line_type="electric_common_service",
                gl_account="6915",
                source_page=1,
            )
        )

    pole_match = re.search(r"(POLE\s+CHARGE\s*\(\d+\))\s+([\d,]+\.\d{2})", text, flags=re.I)
    if pole_match:
        lines.append(
            UtilityChargeLine(
                _clean(pole_match.group(1)).title(),
                money(pole_match.group(2)),
                line_type="electric_common_service",
                gl_account="6915",
                source_page=1,
            )
        )

    meter_match = re.search(
        r"METERED\s+ELECTRIC\s+\d+\s+\d+\s+\d+\s+(?P<amount>[\d,]+\.\d{2})\s*TN\s+Sales\s+Tax",
        text,
        flags=re.I,
    )
    if meter_match:
        meter_gl = _union_city_gl_for_metered_electric(spec, service_address)
        lines.insert(
            0,
            UtilityChargeLine(
                "Metered Electric",
                money(meter_match.group("amount")),
                gl_account=meter_gl,
                source_page=1,
            ),
        )

    connect_match = re.search(r"\b(CONNECTION\s+FEE)\s+([\d,]+\.\d{2})", text, flags=re.I)
    if connect_match:
        lines.append(
            UtilityChargeLine(
                _clean(connect_match.group(1)).title(),
                money(connect_match.group(2)),
                line_type="connection_fee",
                gl_account="6956",
                taxable=False,
                source_page=1,
            )
        )

    if not lines and total:
        base = total - tax if tax and total > tax else total
        lines.append(UtilityChargeLine("Metered Electric", base, gl_account=spec.default_gl, source_page=1))
    return lines


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
    charge_lines, charge_tax, charge_debug = _weakley_current_charge_lines(text, spec)
    connect = _weakley_money_after_any(text, ("CONNECT FEE", "CONNECTION FEE", "RECONNECT FEE"))
    service = _weakley_metered_electric_amount(text)
    tax = charge_tax or _weakley_money_after_any(text, ("SALES TAX", "ELECTRIC TAX", "TAX"))

    if total and not service and not charge_lines:
        service = total - connect - tax
    if service < Decimal("0.00"):
        service = Decimal("0.00")

    history = _weakley_history_match(account, total or service + connect + tax, service_address)
    if not invoice_date:
        invoice_date = history.get("invoice_date") or None
    default_start, default_end = _weakley_default_service_period(invoice_date, due)
    history_start = history.get("service_period_start") or None
    history_end = history.get("service_period_end") or None
    if _weakley_history_period_is_plausible(history_start, history_end, default_start, default_end):
        start = history_start
        end = history_end
    else:
        start = default_start
        end = default_end
    if not service_address and history.get("service_address"):
        service_address = str(history["service_address"])

    lines: list[UtilityChargeLine] = list(charge_lines)
    if service and not any(classify_utility_line(line.description) == "electric_service" for line in lines):
        lines.append(UtilityChargeLine("Metered Electric", service, source_page=1))
    if connect and not any(classify_utility_line(line.description) == "connection_fee" for line in lines):
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
    inv.debug_info["weakley_charge_rows"] = charge_debug
    inv.debug_info["weakley_charge_line_total_before_tax"] = str(
        sum((line.money for line in charge_lines), Decimal("0.00"))
    )
    inv.debug_info["weakley_tax_total"] = str(tax)
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
    raw = _weakley_format_account(left_digits, right_digits)
    known = _weakley_known_accounts()
    spaced_raw = f"{left_digits} - {right_digits}"
    if spaced_raw in known or raw in known:
        return raw
    best = ""
    best_score = 0.0
    compact_raw = f"{left_digits}{right_digits}"
    for known_account in known:
        k_left, k_right = _weakley_account_parts(known_account)
        if k_left != left_digits:
            continue
        score = SequenceMatcher(None, compact_raw, f"{k_left}{k_right}").ratio()
        if score > best_score:
            best_score = score
            best = _weakley_format_account(k_left, k_right)
    if best and best_score >= 0.82:
        return best
    if len(right_digits) == 5:
        for known_account in known:
            k_left, k_right = _weakley_account_parts(known_account)
            if k_left == left_digits and k_right.startswith(right_digits):
                return _weakley_format_account(k_left, k_right)
    return raw


def _weakley_format_account(left: str, right: str) -> str:
    return f"{left}-{right}"


def _weakley_account_parts(value: str) -> tuple[str, str]:
    match = re.search(r"\b([0-9]{5,6})\s*-\s*([0-9]{5,6})\b", value or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2)


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
        r"SERVICE\s*(?:ADDRESS|ADORESS|AD0RESS)\s*[:|\]\s]*([0-9]{2,6}[^\r\n]+)",
        r"\b([0-9]{2,6}[ \t]+BEAUMONT[ \t]+ST(?:[ \t]+(?:APT|UNIT|#)?[ \t]*[A-Z0-9-]+)?)\b",
        r"\b([0-9]{2,6}[ \t]+BELMONT[ \t]+ST(?:[ \t]+(?:APT|UNIT|#)?[ \t]*[A-Z0-9-]+)?)\b",
    ]
    for pattern in patterns:
        hit = _first(pattern, text or "", flags=re.I)
        if not hit:
            continue
        hit = re.sub(
            r"\b(?:METER\s*READING\s*DATE|METERREADINGDATE|OOUEER\S*|WEANS\S*|WCMES|PAY|PHONE).*$",
            "",
            hit,
            flags=re.I,
        )
        hit = _clean(hit).upper()
        hit = hit.replace(" BELMONT ", " BEAUMONT ")
        return hit
    return ""


def _weakley_current_charge_lines(
    text: str,
    spec: Wave3VendorSpec,
) -> tuple[list[UtilityChargeLine], Decimal, list[dict[str, str]]]:
    lines: list[UtilityChargeLine] = []
    tax_total = Decimal("0.00")
    debug_rows: list[dict[str, str]] = []
    for raw in _weakley_current_charge_table_rows(text):
        parsed = _weakley_parse_charge_table_row(raw)
        if parsed is None:
            continue
        description, amount, is_tax = parsed
        if is_tax:
            tax_total += amount
            debug_rows.append(
                {
                    "raw": raw.strip(),
                    "description": description,
                    "amount": str(amount),
                    "classification": "tax",
                }
            )
            continue
        classification = classify_utility_line(description)
        line = UtilityChargeLine(
            description=description,
            amount=amount,
            line_type=classification,
            gl_account="6956" if classification == "connection_fee" else "",
            taxable=classification != "connection_fee",
            source_page=1,
            metadata={"weakley_raw_charge_row": raw.strip()},
        )
        lines.append(line)
        debug_rows.append(
            {
                "raw": raw.strip(),
                "description": description,
                "amount": str(amount),
                "classification": classification,
            }
        )
    return lines, money(tax_total), debug_rows


def _weakley_current_charge_table_rows(text: str) -> list[str]:
    rows: list[str] = []
    in_table = False
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line:
            continue
        upper = line.upper()
        compact = re.sub(r"[^A-Z0-9]+", "", upper)
        if not in_table:
            if "METERED" in upper and "ELECTRIC" in upper:
                in_table = True
            elif "SERVICE" in upper and "AMOUNT" in upper and "DAYS" in upper:
                in_table = True
                continue
            else:
                continue
        if any(
            token in compact
            for token in (
                "TOTALCURRENTCHARGES",
                "CURRENTMONTHSCHARGES",
                "MEMORANDUMBILL",
                "EVENPAYPLAN",
                "PREVIOUSLATEPAYMENTS",
                "PLEASEDETACH",
            )
        ):
            break
        if any(token in compact for token in ("SERVICEDAYSPREVIOUS", "BILLEDREADING")):
            continue
        rows.append(line)
    return rows


def _weakley_parse_charge_table_row(raw: str) -> tuple[str, Decimal, bool] | None:
    amounts = _weakley_amounts(raw)
    if not amounts:
        return None
    amount = amounts[-1]
    prefix = re.sub(r"\$?\s*[0-9]{1,4}(?:[.,][0-9]{2})\b.*$", "", raw).strip()
    prefix = re.sub(r"\s+\d+(?:\s+\d+){0,8}\s*$", "", prefix).strip()
    description = _weakley_charge_description(prefix)
    if not description:
        return None
    is_tax = bool(re.search(r"\b(?:sales|electric)?\s*tax\b", description, re.I))
    return description, amount, is_tax


def _weakley_charge_description(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    upper = text.upper()
    if any(token in upper for token in ("TOTAL", "PREVIOUS", "BALANCE", "MEMORANDUM", "PAYMENT")):
        return ""
    if re.search(r"\bSALES\s*TAX\b", upper):
        return "Sales Tax"
    if "METERED" in upper and "ELECTRIC" in upper:
        return "Metered Electric"
    if "LED" in upper and "LIGHT" in upper:
        watt = _first(r"\b(\d{2,4})\s*WATT\b", upper)
        return f"{watt or 'LED'} Watt LED Light" if watt else "LED Light"
    if "POLE" in upper and "CHARGE" in upper:
        return "Pole Charge"
    if any(token in upper for token in ("CONNECT", "RECONNECT", "CONNECTION")):
        return "Connect Fee"
    return _clean(text).title()


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


def _weakley_history_period_is_plausible(
    history_start: Any,
    history_end: Any,
    expected_start: datetime | None,
    expected_end: datetime | None,
) -> bool:
    if not isinstance(history_start, datetime) or not isinstance(history_end, datetime):
        return False
    if expected_end is None:
        return True
    return abs((history_end - expected_end).days) <= 45


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
    due = _parse_date(
        _first(r"TOTAL AMOUNT DUE\s+(?:by\s+)?(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I)
    )
    sp = re.search(r"SERVICE PERIOD:\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    service_address = _birmingham_water_service_address(text)
    water = (
        _money_after_label(text, "TOTAL CURRENT WATER CHARGES")
        or _money_after_label(text, "CAW WATER SERVICE")
    )
    sewer = (
        _money_after_label(text, "TOTAL SEWER CHARGES")
        or _money_after_label(text, "JEFFERSON COUNTY SEWER")
    )
    total = _money_after_label(text, "Total Amount Due")
    lines = [
        UtilityChargeLine("CAW Water Service", water, gl_account=spec.default_gl, source_page=1),
        UtilityChargeLine("Jefferson County Sewer Service", sewer, gl_account=spec.default_gl, source_page=1),
    ]
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, Decimal("0.00"), source_file, total)]


def _birmingham_water_service_address(text: str) -> str:
    value = _first(r"SERVICE ADDRESS:\s*([^\r\n]+)", text, flags=re.I)
    if not value:
        value = _first(r"SERVICE ADDRESS:\s*(.*?)(?:TOTAL CCFS|BILLING SUMMARY|BIRMINGHAM,?AL)", text, flags=re.I | re.S)
    value = re.sub(r"\bTOTAL\s+CCFS\b.*$", "", value or "", flags=re.I | re.S)
    value = re.sub(r"\bBIRMINGHAM,?\s*AL\b.*$", "", value, flags=re.I | re.S)
    return _clean(value)


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
    account = (
        _first(r"ACCOUNT=([^=]+)=ACCOUNT", text)
        or _first(r"ACCOUNT NUMBER\s*\n?[^\n]*?([0-9-]+)", text, flags=re.I)
    )
    invoice_date = _parse_date(_first(r"BILLDATE=([^=]+)=BILLDATE", text))
    service_address = _first(
        r"ACCOUNT NAME SERVICE ADDRESS\s*\n.*?\s+(\d{3,6}\s+[A-Za-z0-9 .#-]+)",
        text,
        flags=re.I,
    )

    header = re.search(
        r"BILLING\s+DATE\s+ACCOUNT\s+NUMBER\s+BILLING\s+ID\s+ACCOUNT\s+NAME\s+SERVICE\s+ADDRESS\s*\n"
        r"\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
        r"(?P<account>[0-9-]+)\s+\S+\s+\S+\s+"
        r"(?P<rest>[^\n]+)",
        text,
        flags=re.I,
    )
    if header:
        account = account or header.group("account")
        invoice_date = invoice_date or _parse_date(header.group("date"))
        service_hit = re.search(r"(\d{3,6}\s+.+)$", header.group("rest"))
        if service_hit:
            service_address = service_hit.group(1)

    service_address = _normalize_chattanooga_wastewater_address(service_address)
    sp = re.search(
        r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,3}\s+",
        text,
        re.I,
    )
    if not sp:
        sp = re.search(r"(\d{1,2}/\d{1,2}/\d{2})\s+to\s+(\d{1,2}/\d{1,2}/\d{2})", text, re.I)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    due = _parse_date(
        _first(r"Amount Due by\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I)
        or _first(r"Total Amount Due:\s*\$?[\d,]+\.\d{2}.*?(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I | re.S)
    )
    total = (
        _money_after_label(text, "Sewer Usage Charges")
        or _money_after_chattanooga_amount_due_by(text)
        or _money_after_label(text, "Amount Due by")
    )
    # Chattanooga statements can split the "over minimum" OCR with underscores;
    # the source total is reliable and prevents dropping part of the sewer charge.
    lines = [UtilityChargeLine("Sewer Usage Charges", total, gl_account=spec.default_gl, source_page=1)] if total else []
    return [_invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, Decimal("0.00"), source_file, total)]


def _normalize_chattanooga_wastewater_address(value: str) -> str:
    text = _clean(value)
    text = re.sub(r"\bAVE\b", "Ave", text, flags=re.I)
    text = re.sub(r"\bAPT\s*([A-Z0-9-]+)\b", r"Apt \1", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _money_after_chattanooga_amount_due_by(text: str) -> Decimal:
    hit = re.search(
        r"Amount\s+Due\s+by\s+\d{1,2}/\d{1,2}/\d{2,4}\s*:\s*\$?([\d,]+\.\d{2})",
        text,
        flags=re.I,
    )
    return money(hit.group(1)) if hit else Decimal("0.00")


_CITY_UNION_WATER_LABELS = ("Water", "Sewer", "Sanitation", "Stormwater User Fee")


def _parse_city_union_city(spec: Wave3VendorSpec, text: str, source_file: str) -> list[ParsedUtilityInvoice]:
    account = _city_union_water_account(text)
    service_address = _city_union_water_service_address(text, account)
    start, end, invoice_date, due = _city_union_water_dates(text)
    total = _money_after_label(text, "CURRENT BILL") or _money_after_label(text, "AMOUNT DUE")
    lines = _city_union_water_charge_lines(text, total)
    tax = _money_after_label(text, "Tax")

    line_total = sum((line.money for line in lines), Decimal("0.00"))
    if not tax and total:
        difference = (total - line_total).quantize(Decimal("0.01"))
        if Decimal("0.01") <= difference <= Decimal("2.00"):
            tax = difference

    lines = _city_union_fill_missing_charge(lines, total, tax)
    if not total:
        total = sum((line.money for line in lines), Decimal("0.00")) + tax

    inv = _invoice(spec, account, "", invoice_date, due, start, end, service_address, lines, tax, source_file, total)
    if re.search(r"\bFINAL\s+BILL\b", text or "", flags=re.I):
        inv.debug_info["invoice_suffix"] = " Final"
    _city_union_apply_high_school_override(inv)
    return [inv]


def _city_union_water_account(text: str) -> str:
    for match in re.finditer(r"\b(\d{3})\s*-\s*([0-9OIL ]{3,6})\s*-\s*([0-9OIL]{2})\b", text or "", re.I):
        left = _city_union_digits(match.group(1))
        middle = _city_union_digits(match.group(2))
        right = _city_union_digits(match.group(3))
        if len(left) == 3 and len(middle) >= 3 and len(right) == 2:
            return f"{left}-{middle[-4:].zfill(4)}-{right}"
    return ""


def _city_union_digits(value: str) -> str:
    text = (value or "").upper().replace("O", "0").replace("I", "1").replace("L", "1")
    return re.sub(r"\D", "", text)


def _city_union_water_service_address(text: str, account: str) -> str:
    account_pattern = r"\d{3}\s*-\s*[0-9OIL ]{3,6}\s*-\s*[0-9OIL]{2}"
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not re.search(account_pattern, line, flags=re.I):
            continue
        high_school = _first(
            r"(\d{3,5}\s+High\s+School\s+Dr\s*#\s*[A-Z0-9-]+)",
            line,
            flags=re.I,
        )
        if high_school:
            return _city_union_normalize_water_address(high_school)

    generic = (
        r"([0-9]{2,6}\s+[A-Za-z0-9 .'-]+?\s+"
        r"(?:Dr|Drive|St|Street|Ave|Avenue|Rd|Road|Ln|Lane|Ext|Way|Blvd)"
        r"(?:\s*(?:#|Apt|Unit)\s*[A-Z0-9-]+)?)"
    )
    hit = _first(rf"{generic}\s+{account_pattern}\b", text or "", flags=re.I)
    if not hit:
        hit = _first(rf"{generic}\s+\d{{3}}\s*-\s*", text or "", flags=re.I)
    return _city_union_normalize_water_address(hit)


def _city_union_normalize_water_address(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = re.sub(r"\bHIGH\s+SCHOOL\s+DRIVE\b", "High School Dr", text, flags=re.I)
    text = re.sub(r"\bHIGH\s+SCHOOL\s+DR\b", "High School Dr", text, flags=re.I)
    text = re.sub(r"\s*#\s*", " # ", text)
    text = re.sub(r"\bAPT\s+", "Apt ", text, flags=re.I)
    text = re.sub(r"\bUNIT\s+", "Unit ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -")
    return proper_case_preserve_acronyms(text)


def _city_union_water_dates(text: str) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
    start, end = _city_union_reading_period(text)
    header_dates = _city_union_header_dates(text)
    invoice_date: datetime | None = None
    due: datetime | None = None

    if end:
        after_end = [dt for dt in header_dates if dt > end]
        if after_end:
            invoice_date = min(after_end)
            due_candidates = [dt for dt in after_end if dt > invoice_date]
            if due_candidates:
                due = min(due_candidates)

    if (not start or not end) and len(header_dates) >= 2:
        ordered_pairs = [
            (header_dates[i], header_dates[i + 1])
            for i in range(len(header_dates) - 1)
            if header_dates[i] <= header_dates[i + 1]
        ]
        if ordered_pairs:
            start, end = ordered_pairs[0]

    if not invoice_date and len(header_dates) >= 3:
        invoice_date = header_dates[2] if start and end and header_dates[2] > end else header_dates[0]
    if not due and invoice_date:
        later = [dt for dt in header_dates if dt > invoice_date]
        if later:
            due = min(later)
    return start, end, invoice_date, due


def _city_union_header_dates(text: str) -> list[datetime]:
    head = re.split(r"\bPREVIOUS\s+BALANCE\b", text or "", 1, flags=re.I)[0]
    dates: list[datetime] = []
    for token in re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", head):
        parsed = _parse_date(token)
        if parsed:
            dates.append(parsed)
    return dates


def _city_union_reading_period(text: str) -> tuple[datetime | None, datetime | None]:
    patterns = [
        r"DATE\s+READING\s+DATE\s+READING\s+USAGE\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})\s+\d+\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d+",
        r"\b(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,8}\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,8}\s+\d+\s+WATER\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I | re.S)
        if not match:
            continue
        current_date = _parse_date(match.group(1))
        previous_date = _parse_date(match.group(2))
        if previous_date and current_date:
            return previous_date, current_date
    return None, None


def _city_union_water_charge_lines(text: str, total: Decimal) -> list[UtilityChargeLine]:
    parsed: dict[str, Decimal] = {}
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line:
            continue
        for label in _CITY_UNION_WATER_LABELS:
            if label in parsed:
                continue
            pattern = re.escape(label).replace(r"\ ", r"\s+")
            match = re.search(rf"\b{pattern}\b\s+\$?\s*([0-9]{{1,4}}\.[0-9]{{2}})\b", line, flags=re.I)
            if not match:
                continue
            amount = money(match.group(1))
            if amount and (not total or amount < total):
                parsed[label] = amount

    return [
        UtilityChargeLine(
            label,
            amount,
            line_type=classify_utility_line(label),
            gl_account=("6940" if label == "Sanitation" else "6995" if label == "Stormwater User Fee" else ""),
            source_page=1,
        )
        for label, amount in parsed.items()
    ]


def _city_union_fill_missing_charge(
    lines: list[UtilityChargeLine],
    total: Decimal,
    tax: Decimal,
) -> list[UtilityChargeLine]:
    if not total:
        return lines
    present = {line.description.lower(): line for line in lines}
    missing = [label for label in _CITY_UNION_WATER_LABELS if label.lower() not in present]
    if len(missing) != 1:
        return lines
    expected_services = (total - tax).quantize(Decimal("0.01"))
    known = sum((line.money for line in lines), Decimal("0.00"))
    amount = (expected_services - known).quantize(Decimal("0.01"))
    if amount <= 0 or amount >= total:
        return lines
    label = missing[0]
    lines.append(
        UtilityChargeLine(
            label,
            amount,
            line_type=classify_utility_line(label),
            gl_account=("6940" if label == "Sanitation" else "6995" if label == "Stormwater User Fee" else ""),
            source_page=1,
            metadata={"inferred_from_city_union_total": True},
        )
    )
    order = {label.lower(): index for index, label in enumerate(_CITY_UNION_WATER_LABELS)}
    return sorted(lines, key=lambda line: order.get(line.description.lower(), 99))


def _city_union_apply_high_school_override(inv: ParsedUtilityInvoice) -> None:
    if not re.search(r"\bHIGH\s+SCHOOL\s+DR\b", inv.service_address or "", flags=re.I):
        return
    inv.property_abbreviation = inv.property_abbreviation or "VOA"
    unit = _first(r"#\s*([A-Z0-9-]+)\b", inv.service_address, flags=re.I)
    if unit and not inv.location:
        inv.location = unit.upper()


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
    if _is_hopkinsville_electric_portal_summary(text):
        return [_parse_hopkinsville_electric_portal(spec, text, source_file)]

    account = _first(r"ACCOUNTNUMBER:\s*([0-9-]+)", text, flags=re.I) or _first(r"CUSTOMERACCOUNTNUMBER:\s*([0-9-]+)", text, flags=re.I)
    service_address = _first(r"SERVICEADDRESS:\s*([^\n]+)", text, flags=re.I)
    period = re.search(r"METERREADINGDATE:\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})\s*to\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})", text, re.I)
    start = _parse_date(period.group(1) if period else "")
    end = _parse_date(period.group(2) if period else "")
    due = _parse_date(_first(r"DUEBEFORE\s*(?:DISCONNECTPENDING)?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I)) or _parse_date(_first(r"DUEDATEFORCURRENTCHARGESONLY:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    service = _last_money_on_line_containing(text, "Electric Service")
    connect = _money_after_label(text, "Connect Fee")
    pole = _hopkinsville_charge_line_amount(text, r"POLE\s+CHARGE\s*\(\d+\)")
    outdoor_light = _hopkinsville_charge_line_amount(text, r"\d+\s+LED\s+OUTDOOR\s+LIGHT\s*\(\d+\)")
    tax = _hopkinsville_tax_total(text)
    total_current = _money_after_label(text, "TOTALCURRENTCHARGES") or service + connect + pole + outdoor_light + tax
    past_due = _money_after_label(text, "PREVIOUSBALANCE")
    total_due = _money_after_label(text, "NETAMOUNTDUE") or total_current + past_due
    service_gl = _hopkinsville_electric_service_gl(service_address, spec.default_gl)
    lines = [UtilityChargeLine("Electric Service", service or total_current, gl_account=service_gl, source_page=1)]
    if connect:
        lines.append(UtilityChargeLine("Connect Fee", connect, line_type="connection_fee", gl_account="6956", taxable=False, source_page=1))
    if pole:
        lines.append(
            UtilityChargeLine(
                "Pole Charge",
                pole,
                line_type="electric_common_service",
                gl_account="6915",
                source_page=1,
            )
        )
    if outdoor_light:
        label = _hopkinsville_charge_line_label(text, r"\d+\s+LED\s+OUTDOOR\s+LIGHT\s*\(\d+\)") or "LED Outdoor Light"
        lines.append(
            UtilityChargeLine(
                _hopkinsville_title(label),
                outdoor_light,
                line_type="electric_common_service",
                gl_account="6915",
                source_page=1,
            )
        )
    if past_due:
        lines.append(
            UtilityChargeLine(
                "Past Due Electric Service",
                past_due,
                gl_account=spec.default_gl,
                taxable=False,
                source_page=1,
                metadata={"source_label": "Previous Balance"},
            )
        )
    inv = _invoice(spec, account, "", end or due, due or end, start, end, service_address, lines, tax, source_file, total_due)
    _apply_hopkinsville_electric_address_override(inv)
    return [inv]


def _is_hopkinsville_electric_portal_summary(text: str) -> bool:
    hay = (text or "").lower()
    return (
        "hop-electric.utilitynexus.com" in hay
        or ("bill summary - customer portal" in hay and "electric service charges" in hay)
    )


def _parse_hopkinsville_electric_portal(
    spec: Wave3VendorSpec,
    text: str,
    source_file: str,
) -> ParsedUtilityInvoice:
    statement = _first(r"Statement\s*#\s*([0-9]+)", text, flags=re.I)
    account = (
        _first(r"hop-electric\.utilitynexus\.com/([0-9-]+)/view-statement", text, flags=re.I)
        or _first(r"\b([0-9]{5,}-[0-9]{3,})\b", Path(source_file).stem)
    )
    dates = re.search(
        r"Date\s+Due\s+Date\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
        text,
        re.I,
    )
    invoice_date = _parse_date(dates.group(1) if dates else "")
    due = _parse_date(dates.group(2) if dates else "")
    service_start = invoice_date - timedelta(days=30) if invoice_date else None
    service_end = invoice_date
    service_address = _hopkinsville_portal_service_address(text)
    electric = _money_after_label(text, "Electric Service Charges")
    other_debits = _money_after_label(text, "Other Debits")
    total = _money_after_label(text, "Statement Amount") or electric + other_debits

    lines: list[UtilityChargeLine] = []
    service_gl = _hopkinsville_electric_service_gl(service_address, spec.default_gl)
    if electric:
        lines.append(UtilityChargeLine("Electric Service Charges", electric, gl_account=service_gl, source_page=1))
    if other_debits:
        lines.append(
            UtilityChargeLine(
                "Past Due Electric Service",
                other_debits,
                gl_account=service_gl,
                taxable=False,
                source_page=1,
                metadata={"source_label": "Other Debits"},
            )
        )
    if not lines and total:
        lines.append(UtilityChargeLine("Electric Service Charges", total, gl_account=service_gl, source_page=1))

    inv = _invoice(
        spec,
        account,
        statement,
        invoice_date,
        due,
        service_start,
        service_end,
        service_address,
        lines,
        Decimal("0.00"),
        source_file,
        total,
    )
    inv.debug_info["portal_statement"] = "true"
    _apply_hopkinsville_electric_address_override(inv)
    return inv


def _hopkinsville_charge_line_label(text: str, label_pattern: str) -> str:
    pattern = re.compile(rf"\b({label_pattern})", re.I)
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        match = pattern.search(line)
        if match:
            return _clean(match.group(1))
    return ""


def _hopkinsville_charge_line_amount(text: str, label_pattern: str) -> Decimal:
    pattern = re.compile(rf"\b{label_pattern}", re.I)
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not pattern.search(line):
            continue
        amounts = re.findall(r"-?\$?[\d,]+\.\d{2}", line)
        if amounts:
            return money(amounts[-1])
    return Decimal("0.00")


def _hopkinsville_tax_total(text: str) -> Decimal:
    total = Decimal("0.00")
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not re.search(r"\b(?:Sales\s+Tax|Increase\s+for\s+School\s+Tax)\b", line, re.I):
            continue
        amounts = re.findall(r"-?\$?[\d,]+\.\d{2}", line)
        if amounts:
            total += money(amounts[-1])
    return total


def _hopkinsville_portal_service_address(text: str) -> str:
    chunk = _first(
        r"Statement\s*#\s*\d+\s+(.+?)\s+Date\s+Due\s+Date",
        text,
        flags=re.I | re.S,
    )
    if not chunk:
        return ""
    compact = _clean(re.sub(r"-\s+", "-", chunk.replace("\n", " ")))
    match = re.search(
        r"\b(\d{2,6}\s+[A-Za-z0-9 .'-]+?\b(?:DR|DRIVE|ST|STREET|RD|ROAD|AVE|AVENUE|LN|LANE|CT|COURT)\b"
        r"(?:\s+(?:APT|UNIT|STE|SUITE)\s*[A-Za-z0-9-]+)?)\s+HOPKINSVILLE\b",
        compact,
        flags=re.I,
    )
    return _clean(match.group(1)) if match else compact


def _apply_hopkinsville_electric_address_override(inv: ParsedUtilityInvoice) -> None:
    location = _hopkinsville_location_from_address(inv.service_address)
    if location:
        inv.location = location
    if re.search(r"\bDENZIL\s+DR\b", inv.service_address or "", re.I):
        inv.property_abbreviation = inv.property_abbreviation or "AMA"
        inv.debug_info["property_mapping_override"] = "hes_denzil_ama"


def _hopkinsville_location_from_address(service_address: str) -> str:
    match = re.search(
        r"\b(\d{2,6})\s+DENZIL\s+DR(?:IVE)?\s+APT\s+([A-Za-z0-9-]+)\b",
        service_address or "",
        flags=re.I,
    )
    if match:
        return f"{match.group(1)}-{match.group(2).upper()}"
    return ""


def _hopkinsville_title(value: str) -> str:
    words: list[str] = []
    cleaned = re.sub(r"\s*\(", " (", _clean(value))
    for part in cleaned.split():
        letters = re.sub(r"[^A-Za-z]", "", part).upper()
        if part.isdigit() or letters in {"LED"}:
            words.append(part.upper())
        else:
            words.append(part.capitalize())
    return " ".join(words)


def _hopkinsville_electric_service_gl(service_address: str, default_gl: str) -> str:
    address = _clean(service_address)
    if address and not re.search(r"\b(?:APT|UNIT|STE|SUITE)\b", address, re.I):
        return "6915"
    return default_gl


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
        r"\b(?!PO\s+Box)(\d{2,6}(?:-[A-Za-z0-9]+)?\s+[A-Za-z0-9 .'-]+?\b"
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
    "process_cumberland_emc_batch",
    "process_pleasant_view_utility_district_batch",
    "process_guardian_water_power_batch",
    "process_hopkinsville_electric_system_batch",
    "process_kentucky_utilities_batch",
    "process_knoxville_utilities_board_batch",
    "process_nashville_electric_service_batch",
    "process_tennessee_american_water_batch",
    "process_union_city_energy_authority_batch",
    "process_wave3_utility_batch",
    "process_weakley_county_municipal_electric_system_batch",
]
