"""Phase U2 deterministic processors for utility vendors with old-script references.

The old project had separate scripts for Alabama Power, EPB Fiber, City of
Henderson, CDE Lightband, and Nolin RECC.  This module keeps the useful
business rules but routes every vendor through the Phase U1 utility safety
helpers: no standalone tax rows, validated GLs, valid property/unit lookups,
dry-run safe output, and consistent web preview rows.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from utils.canonical_vendors import canonical_vendor_name
from utils.property_lookup import UnitMatch, lookup_unit, match_by_address

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
    looks_like_raw_address,
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
    accounting_date: datetime | None = None


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
        invoice_month_source="accounting_date",
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
    duplicate_review_rows: list[dict[str, Any]] = []
    seen_file_hashes: dict[str, str] = {}
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
        file_hash = _file_sha256(path)
        duplicate_of = seen_file_hashes.get(file_hash)
        if duplicate_of:
            duplicate_review_rows.append(_duplicate_file_review_row(path.name, duplicate_of, file_hash))
            _progress(
                progress_callback,
                current_step=f"Skipped duplicate {path.name}",
                files_done=index,
                invoices_created=len(parsed),
                rows_created=sum(len(i.line_items) for i in parsed),
                warnings_count=len(duplicate_review_rows),
            )
            continue
        seen_file_hashes[file_hash] = path.name
        try:
            candidate = ingest_document(path, vendor_hint=spec.display_name)
            invoices = _parse_document(spec, candidate.document_text, path.name, pages=candidate.pages)
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
    review_json = [
        *[_manual_review_to_dict(inv) for inv in parsed if inv.manual_review_reasons],
        *duplicate_review_rows,
    ]
    row_count = sum(len(inv.get("rows") or []) for inv in invoices_json)
    workbook_path = out / f"{vendor_key}_resman_import_{timestamp}.xlsx"
    review_path = out / f"{vendor_key}_manual_review_{timestamp}.xlsx"

    summary = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vendor_key": vendor_key,
        "processing_mode": "deterministic",
        "dry_run": dry_run,
        "files_total": len(files),
        "files_processed": len(files) - len(skipped) - len(duplicate_review_rows),
        "files_skipped_unsupported": len(skipped),
        "files_skipped_duplicate": len(duplicate_review_rows),
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


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _duplicate_file_review_row(filename: str, duplicate_of: str, file_hash: str) -> dict[str, Any]:
    return {
        "source_file": filename,
        "account_number": "",
        "invoice_number": "",
        "invoice_date": "",
        "property_abbreviation": "",
        "location": "",
        "total_amount": 0.0,
        "line_items_total": 0.0,
        "source_total_amount": None,
        "line_count": 0,
        "reasons": ["duplicate_upload_exact_file"],
        "duplicate_of": duplicate_of,
        "file_hash": file_hash,
        "reconciliation": {
            "diagnosis": (
                f"This uploaded file is byte-for-byte identical to {duplicate_of}. "
                "It was skipped so the same invoice is not posted multiple times."
            )
        },
    }


# ---------------------------------------------------------------------------
# Vendor parsers
# ---------------------------------------------------------------------------


def _parse_document(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
    *,
    pages: Iterable[Any] | None = None,
) -> list[ParsedUtilityInvoice]:
    if spec.key == "the_city_of_henderson":
        return _parse_henderson(spec, text or "", source_file, pages=pages)
    parser: Callable[[Wave2VendorSpec, str, str], list[ParsedUtilityInvoice]] = {
        "alabama_power": _parse_alabama_power,
        "epb_fiber_optics": _parse_epb_fiber,
        "cde_lightband": _parse_cde_lightband,
        "nolin_recc_smarthub": _parse_nolin_recc,
    }[spec.key]
    return parser(spec, text or "", source_file)


def _alabama_summary_amount(text: str, label: str) -> Decimal:
    compact = _clean(text)
    money_pattern = r"([+-]?\s*\$?\s*[\d,]+\.\d{2})"
    if label == "Payment Received":
        pattern = (
            r"\bPayment\s+Received\b"
            r"(?:\s+No\s+Payment\s+Received|\s+On\s*\d{1,2}/\d{1,2}/\d{2,4}\s+Thank\s+You!?)?"
            rf"\s+{money_pattern}"
        )
    elif label == "Account Establishment Charge":
        pattern = rf"(?<!Past Due )\b{re.escape(label)}\b\s+{money_pattern}"
    elif label == "Late Payment Charge - Electric":
        pattern = rf"\b(?:Late\s+Payment\s+Charge\s*-\s*Electric|Late\s+Pymt\s+Chg)\b\s+{money_pattern}"
    else:
        pattern = rf"\b{re.escape(label)}\b\s+{money_pattern}"
    return money(_first(pattern, compact, flags=re.IGNORECASE))


def _alabama_service_address_and_period(text: str) -> tuple[str, datetime | None, datetime | None]:
    match = re.search(
        r"(?P<addr>\d+\s+[A-Z0-9 .#-]+?)\s+"
        r"(?P<start>[A-Za-z]+ \d{1,2}, \d{4})\s*-\s*"
        r"(?P<end>[A-Za-z]+ \d{1,2}, \d{4})",
        text,
        re.IGNORECASE,
    )
    if match:
        return (
            _clean(match.group("addr")),
            _parse_date(match.group("start")),
            _parse_date(match.group("end")),
        )

    lines = _lines(text)
    for idx, line in enumerate(lines):
        if not re.search(r"\bService Address\b", line, flags=re.IGNORECASE):
            continue
        candidates = [
            re.sub(
                r"^.*?\bService Address\b(?:\s+Service Period)?(?:\s+Manage Your Account)?\s*",
                "",
                line,
                flags=re.IGNORECASE,
            )
        ]
        candidates.extend(lines[idx + 1 : idx + 3])
        for candidate in candidates:
            cleaned = _alabama_clean_service_address_candidate(candidate)
            if cleaned:
                return cleaned, None, None

    phone_line_address = _first(
        r"Primary Phone Number on file:\s*.*?\bat\s+(\d{3,6}\s+[A-Z0-9 .#-]+)",
        text,
        flags=re.IGNORECASE,
    )
    return _alabama_clean_service_address_candidate(phone_line_address), None, None


def _alabama_clean_service_address_candidate(value: str) -> str:
    candidate = _clean(value)
    if not re.match(r"^\d{3,6}\b", candidate):
        return ""
    candidate = re.sub(
        r"\s+[A-Za-z]+ \d{1,2}, \d{4}\s*-\s*[A-Za-z]+ \d{1,2}, \d{4}.*$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.split(
        r"\s+(?:AlabamaPower\.com|Pay Bill|Opening Bill|Final Bill|Manage Your Account|Payment Options)\b",
        candidate,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _clean(candidate)


def _alabama_is_unit_service_address(service_address: str) -> bool:
    return bool(
        re.search(
            r"\b(?:APT|APARTMENT|UNIT|#)\s*[A-Z0-9-]+\b",
            service_address or "",
            flags=re.IGNORECASE,
        )
    )


def _alabama_street_number(service_address: str) -> int | None:
    match = re.match(r"\s*(\d{2,5})\b", service_address or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _alabama_service_descriptor(text: str, service_address: str) -> str:
    compact = _clean(text)
    address = _clean(service_address)
    if not address:
        return ""
    descriptor = _first(
        rf"{re.escape(address)}\s+"
        r"[A-Za-z]+ \d{1,2}, \d{4}\s*-\s*[A-Za-z]+ \d{1,2}, \d{4}\s+"
        r"(.{0,120}?)(?=AlabamaPower\.com|Billing Summary|Payments Since|Pay Bill|$)",
        compact,
        flags=re.IGNORECASE,
    )
    descriptor = re.sub(
        r"\b(?:AlabamaPower\.com|or Mobile App|Start,\s*stop.*)$",
        "",
        descriptor,
        flags=re.IGNORECASE,
    )
    return _clean(descriptor)


def _alabama_service_kind(text: str, service_address: str) -> str:
    """Classify Alabama Power service by bill evidence before GL assignment."""

    compact = _clean(text)
    address_hay = f"{service_address or ''} {_alabama_service_descriptor(text, service_address)}".upper()
    street_number = _alabama_street_number(service_address)
    if (
        "CURRENT LIGHTING SERVICE" in compact.upper()
        or "UNREG NESC LIGHTS" in address_hay
        or re.search(r"\bLIGHTING\s+CHARGE\b", compact, flags=re.IGNORECASE)
    ):
        return "lighting"
    if re.search(r"\b(?:OFC|OFFICE)\b", address_hay):
        return "office"
    if re.search(r"\b(?:HOUSE\s+METER|HSE|EHSE|BASEMENT)\b", address_hay):
        return "house_meter"
    if street_number is not None and street_number > 800:
        return "external"
    match = _match_service_address(service_address)
    if match and match.unit_number:
        return "residential_unit"
    return "common"


def _alabama_service_label(text: str, service_address: str) -> str:
    kind = _alabama_service_kind(text, service_address)
    if kind == "lighting":
        return "Outdoor Lighting Service"
    if kind == "office":
        return "Office Electric Service"
    if kind == "house_meter":
        return "House Meter Electric Service"
    if kind == "external":
        return "Due From Other Electric Service"
    if kind == "residential_unit":
        return "Residential Electric Service"
    return "Common Area Electric Service"


def _alabama_service_gl(
    service_address: str,
    description: str,
    valid_gls: dict[str, str],
) -> str:
    desc = description or ""
    if "Due From Other" in desc and "1285" in valid_gls:
        return "1285"
    if any(token in desc for token in ("Outdoor Lighting", "Office Electric", "House Meter", "Common Area")):
        return "6915" if "6915" in valid_gls else ""
    street_number = _alabama_street_number(service_address)
    if street_number is not None and street_number > 800 and "1285" in valid_gls:
        return "1285"
    match = _match_service_address(service_address)
    if match and match.unit_number:
        return "6920" if "6920" in valid_gls else ""
    return "6915" if "6915" in valid_gls else ""


def _alabama_common_service_context(text: str, service_address: str) -> str:
    """Return the bill's common-area clue for non-unit Alabama service."""

    kind = _alabama_service_kind(text, service_address)
    if kind == "residential_unit":
        return ""

    compact = _clean(text)
    address_text = (service_address or "").upper()
    pieces: list[str] = []
    if kind == "lighting":
        pieces.append("Outdoor Lighting")
    elif kind == "office":
        pieces.append("Office")
    elif kind == "house_meter":
        pieces.append("House Meter")
    elif kind == "external":
        pieces.append("Due From Other")
    elif service_address:
        pieces.append("Common Area")

    rate = _first(
        r"Current\s+Electric\s+Service\s*-\s*([A-Z]{2,4}\s*-\s*[A-Za-z ()/-]+?)(?=Next\s+Scheduled\s+Read\s+Date|Service\s+Period|Meter\s+#|Meter\s+Reading|$)",
        compact,
        flags=re.IGNORECASE,
    )
    rate = re.sub(r"^(?:FD|LPS|LPM)\s*-\s*", "", _clean(rate), flags=re.IGNORECASE)
    if rate and not re.search(r"\bFamily\s+Dwelling\b", rate, flags=re.IGNORECASE):
        pieces.append(rate)

    return " - ".join(dict.fromkeys(piece for piece in pieces if piece))


def _alabama_charge_description(base: str, text: str, service_address: str) -> str:
    context = _alabama_common_service_context(text, service_address)
    return f"{base} - {context}" if context else base


def _alabama_invoice_suffix(text: str) -> str:
    if re.search(r"\bFinal\s+Bill\b", text or "", flags=re.IGNORECASE):
        return " Final"
    if re.search(r"\bFinal\b\s*(?:\n|\r\n?)\s*\bBill\b", text or "", flags=re.IGNORECASE):
        return " Final"
    return ""


def _parse_alabama_power(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(r"\b(\d{5}-\d{5})\b", text)
    due = _parse_date(
        _first(r"Draft Date\s+([A-Za-z]+ \d{1,2}, \d{4})", text)
        or _first(r"Please\s+Pay\s+By\s+([A-Za-z]+ \d{1,2}, \d{4})", text, flags=re.I)
    )
    service_address, start, end = _alabama_service_address_and_period(text)
    invoice_date = end or due

    total_due = _alabama_summary_amount(text, "Total Due")
    total_current = money(_first(r"Total Current Electric Service\s+\$?\s*([\d,]+\.\d{2})", text))
    current_summary = _alabama_summary_amount(text, "Current Electric Service")
    current_lighting_summary = _alabama_summary_amount(text, "Current Lighting Service")
    current_amount = current_summary or total_current
    if not current_amount and current_lighting_summary:
        current_amount = current_lighting_summary
    service_label = _alabama_service_label(text, service_address)
    service_kind = _alabama_service_kind(text, service_address)

    lines: list[UtilityChargeLine] = []

    summary_labels = (
        "Past Due Electric Service",
        "Past Due Previous Location Balance",
        "Past Due Account Establishment Charge",
        "Late Payment Charge - Electric",
        "Account Establishment Charge",
    )
    for label in summary_labels:
        amount = _alabama_summary_amount(text, label)
        if amount:
            if label == "Past Due Previous Location Balance":
                description = f"Past Due {service_label}"
            elif label == "Past Due Electric Service":
                description = f"Past Due {service_label}"
            elif label == "Late Payment Charge - Electric":
                description = f"Late Charge - {service_label}"
            else:
                description = label
            lines.append(
                UtilityChargeLine(
                    description,
                    amount,
                    gl_account="",
                    taxable=False,
                    source_page=1,
                    metadata={"alabama_summary_label": label},
                )
            )

    has_prior_detail = any(line.description.lower().startswith("past due ") for line in lines)
    if not has_prior_detail:
        previous = _alabama_summary_amount(text, "Previous Bill Amount")
        payment = _alabama_summary_amount(text, "Payment Received")
        unpaid_previous = (previous + payment).quantize(Decimal("0.01")) if previous else Decimal("0.00")
        if unpaid_previous > Decimal("0.00"):
            lines.append(
                UtilityChargeLine(
                    f"Past Due {service_label}",
                    unpaid_previous,
                    gl_account="",
                    taxable=False,
                    source_page=1,
                    metadata={
                        "alabama_summary_label": "Previous Bill Amount",
                        "payment_received_excluded": str(payment),
                    },
                )
            )

    if current_amount:
        lines.append(
            UtilityChargeLine(
                service_label,
                current_amount,
                gl_account="",
                source_page=1,
                metadata={
                    "alabama_summary_label": (
                        "Current Lighting Service" if current_lighting_summary else "Current Electric Service"
                    ),
                    "alabama_service_kind": service_kind,
                },
            )
        )

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
            tax_total=Decimal("0.00"),
            source_file=source_file,
            debug_info={
                "source_total": str(total_due or total_current),
                "alabama_total_due": str(total_due),
                "alabama_total_current_electric_service": str(total_current),
                "alabama_total_current_lighting_service": str(current_lighting_summary),
                "alabama_service_kind": service_kind,
                "property_level_service": service_kind in {"lighting", "office", "house_meter", "external", "common"},
                "invoice_suffix": _alabama_invoice_suffix(text),
            },
        )
    ]


def _parse_epb_fiber(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    if re.search(r"\bElectric\s+Power\s+Acct\s*:", text or "", flags=re.IGNORECASE):
        return _parse_epb_electric_power(spec, text, source_file)

    account = _first(r"AccountNumber:\s*([A-Z0-9-]+)", text)
    explicit = _first(r"InvoiceNumber:\s*([A-Z0-9-]+)", text)
    invoice_date = _parse_date(_first(r"BillingDate:\s*([A-Za-z]+\s*\d{1,2},\s*\d{4})", text))
    due = _parse_date(_first(r"PaymentDueDate:\s*([A-Za-z]+\s*\d{1,2},\s*\d{4})", text))
    river_canyon = re.search(
        r"\b(\d{3,6})\s*RIVER\s*CANYON\s*(?:RD|ROAD)\b",
        text or "",
        flags=re.IGNORECASE,
    )
    service_address = (
        f"{river_canyon.group(1)} River Canyon Rd"
        if river_canyon
        else _service_address_from_text(
            text,
            [
                r"(\d{3,6}\s*N\s*Chamberlain\s*Ave)",
                r"(\d{3,6}\s+[A-Za-z0-9 .'-]+(?:Ave|Avenue|St|Street|Rd|Road|Dr|Drive|Ct|Court))",
            ],
        )
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
            accounting_date=_previous_month_end(invoice_date),
        )
    ]


def _parse_epb_electric_power(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    account = _first(
        r"Electric\s+Power\s+Acct\s*:\s*([0-9]{2,4}-[0-9]{4}\.[0-9]{3})",
        text,
        flags=re.I,
    )
    invoice_date = _parse_date(_first(r"Billing\s+Date\s*:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    due = _parse_date(_first(r"Payment\s+Due\s+Date\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})", text, flags=re.I))
    service_address = _epb_electric_service_address(text)

    start = _parse_date(
        _first(
            r"Previous\s+KWH\s+Meter\s+Reading\s*-\s*Actual\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.I,
        )
        or _first(r"Previous\s+[A-Z]+\s+Meter\s+Reading\s*-\s*Actual\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I)
    )
    end = _parse_date(
        _first(
            r"New\s+KWH\s+Meter\s+Reading\s*-\s*Actual\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.I,
        )
        or _first(r"New\s+[A-Z]+\s+Meter\s+Reading\s*-\s*Actual\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.I)
    )

    electric = money(
        _first(
            r"Summary\s+of\s+New\s+Charges\s+Electric\s+Power\s+\$?\s*([\d,]+\.\d{2})",
            text,
            flags=re.I | re.S,
        )
        or _first(r"\bElectric\s+Power\s+\$?\s*([\d,]+\.\d{2})", text, flags=re.I)
    )
    tax = money(_first(r"\bSales\s+Tax\s+\$?\s*([\d,]+\.\d{2})", text, flags=re.I))
    total = money(_first(r"Total\s+New\s+Charges\s+\$?\s*([\d,]+\.\d{2})", text, flags=re.I))
    usage_kwh = _first(r"Total\s+KWH\s+Used\s+This\s+Period\s+(\d+)", text, flags=re.I)
    if electric == 0 and total > tax:
        electric = total - tax
    line_amount = electric if electric > 0 else total
    lines = [
        UtilityChargeLine(
            f"Current Electric Service - {usage_kwh} kWh" if usage_kwh else "Current Electric Service",
            line_amount,
            gl_account="",
            source_page=1,
            metadata={"epb_bill_type": "electric_power"},
        )
    ]

    return [
        ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            invoice_date=invoice_date or end,
            due_date=due or invoice_date or end,
            service_period_start=start,
            service_period_end=end,
            service_address=service_address,
            line_items=lines,
            tax_total=tax if electric > 0 else Decimal("0.00"),
            source_file=source_file,
            debug_info={
                "source_total": str(total),
                "epb_bill_type": "electric_power",
                "usage_kwh": usage_kwh,
            },
            accounting_date=_previous_month_end(invoice_date or end),
        )
    ]


def _previous_month_end(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return datetime(value.year, value.month, 1) - timedelta(days=1)


def _epb_electric_service_address(text: str) -> str:
    raw = _first(r"Service\s+Address\s*:\s*(.*?)\s+Rate\s+Class\s*:", text, flags=re.I | re.S)
    if not raw:
        return ""
    raw = re.sub(r"\bwww\.epb\.com\b", " ", raw, flags=re.I)
    raw = re.sub(r"\bChattanooga,\s*TN\s+\d{5}(?:-\d{4})?\b.*$", " ", raw, flags=re.I)
    return _clean(raw)


def _parse_henderson(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
    *,
    pages: Iterable[Any] | None = None,
) -> list[ParsedUtilityInvoice]:
    invoices: list[ParsedUtilityInvoice] = []
    for section, source_page in _henderson_statement_sections(text, pages):
        inv = _parse_henderson_statement(spec, section, source_file, source_page=source_page)
        if inv is not None:
            invoices.append(inv)
    if invoices:
        return invoices
    inv = _parse_henderson_statement(spec, text, source_file, source_page=1)
    return [inv] if inv is not None else []


def _henderson_statement_sections(
    text: str,
    pages: Iterable[Any] | None = None,
) -> list[tuple[str, int]]:
    """Return one City of Henderson statement section per account page.

    Henderson PDFs often include a bill page followed by an informational
    insert. When operators append another bill to the same PDF, the document
    becomes bill/insert/bill/insert. Parsing the whole text as one invoice
    silently drops the later account, so we segment on the actual account
    header and ignore insert-only pages.
    """

    sections: list[tuple[str, int]] = []
    header_re = re.compile(
        r"\bAccount\s+No\.\s+Due\s+Date\s+Amount\s+Due\s+After\s+Due\s+Date\b",
        re.IGNORECASE,
    )
    for ordinal, page in enumerate(pages or [], start=1):
        page_text = str(getattr(page, "text", "") or "").strip()
        if not page_text or not header_re.search(page_text):
            continue
        try:
            page_number = int(getattr(page, "page_number", ordinal) or ordinal)
        except (TypeError, ValueError):
            page_number = ordinal
        sections.append((page_text, page_number))
    if sections:
        return sections

    starts = [match.start() for match in header_re.finditer(text or "")]
    if not starts:
        return [(text or "", 1)] if str(text or "").strip() else []
    out: list[tuple[str, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text or "")
        section = (text or "")[start:end].strip()
        if section:
            out.append((section, idx + 1))
    return out


def _parse_henderson_statement(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
    *,
    source_page: int,
) -> ParsedUtilityInvoice | None:
    account = _first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*([0-9-]+)", text, flags=re.DOTALL)
    if not account:
        account = _first(r"Account No\.\s+Service Address\s*\n\s*([0-9-]+)", text)
    if not account:
        return None
    due = _parse_date(_first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*[0-9-]+\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.DOTALL))
    total = money(_first(r"Account No\.\s+Due Date\s+Amount Due.*?\n\s*[0-9-]+\s+\d{1,2}/\d{1,2}/\d{4}\s+([\d,]+\.\d{2})", text, flags=re.DOTALL))
    service_address = _first(r"Service Address\s*\n\s*([^\n]+)", text)
    sp = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})\s+([A-Za-z ]+)", text)
    start = _parse_date(sp.group(1) if sp else "")
    end = _parse_date(sp.group(2) if sp else "")
    service_family = _clean(sp.group(3) if sp else "Electric") or "Electric"
    invoice_date = end or due

    current_block = _between(text, "Current Billing", "Current Charges") or text
    current_charges = _henderson_current_billing_lines(current_block)
    raw_lines: list[UtilityChargeLine] = []
    tax_total = Decimal("0.00")
    non_service_components: list[dict[str, str]] = []
    for clean, amt in current_charges:
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
            non_service_components.append({"description": clean, "amount": str(amt)})
        else:
            raw_lines.append(
                UtilityChargeLine(
                    _henderson_service_label(clean),
                    amt,
                    source_page=source_page,
                    metadata={
                        "source_charge_label": clean,
                        "source_charge_components": [{"description": clean, "amount": str(amt)}],
                    },
                )
            )
    if not raw_lines and total > 0:
        raw_lines.append(UtilityChargeLine(_henderson_service_label(service_family), total, source_page=source_page))
        tax_total = Decimal("0.00")
    elif len(raw_lines) == 1 and non_service_components:
        line = raw_lines[0]
        raw_lines[0] = UtilityChargeLine(
            line.description,
            line.money,
            line_type=line.line_type,
            gl_account=line.gl_account,
            taxable=line.taxable,
            include_in_export=line.include_in_export,
            source_page=line.source_page,
            trace_id=line.trace_id,
            metadata={
                **line.metadata,
                "allocated_charge_components": non_service_components,
            },
        )
    return ParsedUtilityInvoice(
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
        debug_info={
            "source_total": str(total),
            "service_family": service_family,
            "source_page": source_page,
            "current_billing_components": [
                {"description": label, "amount": str(amount)}
                for label, amount in current_charges
            ],
            "allocated_charge_components": non_service_components,
        },
    )


def _henderson_current_billing_lines(current_block: str) -> list[tuple[str, Decimal]]:
    """Parse visible Current Billing rows while preserving charge labels."""

    out: list[tuple[str, Decimal]] = []
    text = _clean(current_block)
    # The PDF text extractor often breaks "Rate Increase for School Tax" across
    # two physical lines. Normalising whitespace lets us recover that label.
    pattern = re.compile(
        r"(Electric|Kentucky\s+Sales\s+Tax|Rate\s+Increase\s+for\s+School\s+Tax|911\s+Fee|[A-Za-z][A-Za-z ]{2,40})\s+([\d,]+\.\d{2})",
        re.IGNORECASE,
    )
    seen: set[tuple[str, Decimal]] = set()
    for match in pattern.finditer(text):
        label = _clean(match.group(1))
        label = re.sub(r"^Charge Code Amount\s+", "", label, flags=re.IGNORECASE)
        if re.fullmatch(r"Rate Increase for School", label, flags=re.IGNORECASE):
            label = "Rate Increase for School Tax"
        amount = money(match.group(2))
        if not label or amount == 0:
            continue
        key = (label.lower(), amount)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, amount))
    return out


def _henderson_service_label(label: str) -> str:
    clean = _clean(label)
    if clean.lower() == "electric":
        return "Electric Service"
    return clean or "Electric Service"


def _parse_cde_lightband(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    invoices: list[ParsedUtilityInvoice] = []
    for page_index, section in enumerate(_cde_statement_sections(text), start=1):
        account = _first(r"Account #:\s*([0-9-]+)", section)
        statement = _parse_date(_first(r"Statement Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", section))
        due = _parse_date(_first(r"Date Due:\s*(\d{1,2}/\d{1,2}/\d{2,4})", section))
        service_address = _parse_cde_service_address(section)
        sp = re.search(
            r"Service Period:\s*(\d{1,2}/\d{1,2}/\d{2})\s+to\s+(\d{1,2}/\d{1,2}/\d{2})",
            section,
            re.I,
        )
        start = _parse_date(sp.group(1) if sp else "")
        end = _parse_date(sp.group(2) if sp else "")
        charge_lines, tax_total, source_total, charge_debug = _parse_cde_charge_lines(
            section,
            source_page=page_index,
        )
        # CDE can issue separate electric and telecom statement pages under the
        # same account. Allocate each page's tax before those pages are merged;
        # otherwise electric tax can leak into telecom lines (or vice versa).
        section_allocation = allocate_tax_proportionally(charge_lines, tax_total)
        section_lines: list[UtilityChargeLine] = []
        for line in section_allocation.lines:
            metadata = dict(line.metadata)
            metadata["cde_service_period_start"] = start
            metadata["cde_service_period_end"] = end
            section_lines.append(
                UtilityChargeLine(
                    description=line.description,
                    amount=line.money,
                    line_type=line.line_type,
                    gl_account=line.gl_account,
                    taxable=line.taxable,
                    include_in_export=line.include_in_export,
                    source_page=line.source_page,
                    trace_id=line.trace_id,
                    metadata=metadata,
                )
            )
        invoices.append(
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
                line_items=section_lines,
                tax_total=Decimal("0.00"),
                source_file=source_file,
                debug_info={
                    "source_total": str(source_total),
                    "charge_lines": charge_debug,
                    "cde_service_kind": (
                        str(charge_debug[0].get("block") or "").lower()
                        if charge_debug
                        else ""
                    ),
                    "source_page": page_index,
                    "section_tax_total": str(tax_total),
                    "section_tax_allocation": {
                        str(index): str(amount)
                        for index, amount in section_allocation.allocation_by_index.items()
                    },
                },
            )
        )
    return _merge_cde_account_sections(invoices)


def _merge_cde_account_sections(
    invoices: list[ParsedUtilityInvoice],
) -> list[ParsedUtilityInvoice]:
    """Merge electric/telecom pages that belong to one CDE account invoice."""

    grouped: dict[str, list[ParsedUtilityInvoice]] = {}
    order: list[str] = []
    for index, invoice in enumerate(invoices):
        key = invoice.account_number or f"__missing_account_{index}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(invoice)

    merged: list[ParsedUtilityInvoice] = []
    for key in order:
        sections = grouped[key]
        base = sections[0]
        if len(sections) == 1:
            base.debug_info.pop("invoice_suffix", None)
            merged.append(base)
            continue

        base.line_items = [line for section in sections for line in section.line_items]
        base.tax_total = sum((section.tax_total for section in sections), Decimal("0.00"))
        base.invoice_date = max(
            (section.invoice_date for section in sections if section.invoice_date),
            default=base.invoice_date,
        )
        base.due_date = max(
            (section.due_date for section in sections if section.due_date),
            default=base.due_date,
        )
        base.service_period_start = min(
            (section.service_period_start for section in sections if section.service_period_start),
            default=base.service_period_start,
        )
        base.service_period_end = max(
            (section.service_period_end for section in sections if section.service_period_end),
            default=base.service_period_end,
        )
        base.debug_info["source_total"] = str(
            sum(
                (money(section.debug_info.get("source_total") or 0) for section in sections),
                Decimal("0.00"),
            )
        )
        base.debug_info["charge_lines"] = [
            charge
            for section in sections
            for charge in list(section.debug_info.get("charge_lines") or [])
        ]
        base.debug_info["cde_service_kind"] = "mixed"
        base.debug_info["cde_sections"] = [
            {
                "source_page": section.debug_info.get("source_page"),
                "service_kind": section.debug_info.get("cde_service_kind"),
                "service_period_start": _fmt_iso(section.service_period_start),
                "service_period_end": _fmt_iso(section.service_period_end),
                "source_total": section.debug_info.get("source_total"),
                "tax_total": section.debug_info.get("section_tax_total"),
            }
            for section in sections
        ]
        base.debug_info.pop("invoice_suffix", None)
        merged.append(base)
    return merged


_CDE_SUBTOTAL_RE = re.compile(
    r"<<\s*(?P<kind>ELECTRIC|TELECOM)\s+SUB-TOTAL\s*>>\s+\$?(?P<amount>[\d,]+\.\d{2})",
    re.IGNORECASE,
)
_CDE_AMOUNT_LINE_RE = re.compile(
    r"^(?P<label>[A-Za-z0-9][A-Za-z0-9&/().,\- #]+?)\s+\$?(?P<amount>-?\d[\d,]*\.\d{2})$"
)
_CDE_INTERNET_KEYWORDS = (
    "internet",
    "broadband",
    "wifi",
    "wi-fi",
    "smartbiz",
    "ruckus",
    "1gb",
    "dynamic ip",
    "access point",
)
_CDE_PHONE_KEYWORDS = (
    "phone",
    "e911",
    "subscriber line",
    "number porting",
    "universal service",
    "federal excise",
    "telecom",
)
_CDE_BALANCE_CHARGE_LABELS = {
    "prior balance",
    "previous balance",
    "balance forward",
    "past due balance",
}


def _cde_statement_sections(text: str) -> list[str]:
    raw_lines = (text or "").splitlines()
    starts = [
        idx
        for idx, line in enumerate(raw_lines)
        if re.search(r"Account #:\s*\d[\d-]+\s+Bill Summary", line, re.IGNORECASE)
    ]
    if len(starts) <= 1:
        return [text or ""]
    sections: list[str] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(raw_lines)
        section = "\n".join(raw_lines[start:end]).strip()
        if section:
            sections.append(section)
    return sections or [text or ""]


def _parse_cde_service_address(section: str) -> str:
    raw = _first(r"Service:\s*([^\n]+)", section)
    raw = re.sub(r"\s+(?:Paid by Autopay on:|Your bank|account will be).*$", "", raw, flags=re.I)
    raw = re.sub(r"\s+Termination after:.*$", "", raw, flags=re.I)
    raw = re.sub(r"\s+\d{1,2}/\d{1,2}/\d{2,4}(?:\s+.*)?$", "", raw)
    return _clean(raw)


def _parse_cde_charge_lines(
    section: str,
    *,
    source_page: int,
) -> tuple[list[UtilityChargeLine], Decimal, Decimal, list[dict[str, str]]]:
    lines = _lines(section)
    parsed: list[UtilityChargeLine] = []
    tax_total = Decimal("0.00")
    subtotal_total = Decimal("0.00")
    debug: list[dict[str, str]] = []

    subtotals: list[tuple[int, str, Decimal]] = []
    for idx, line in enumerate(lines):
        match = _CDE_SUBTOTAL_RE.search(line)
        if match:
            subtotals.append((idx, match.group("kind").upper(), money(match.group("amount"))))
    if not subtotals:
        return parsed, tax_total, subtotal_total, debug

    for subtotal_idx, block_kind, block_total in subtotals:
        subtotal_total += block_total
        block_lines = _cde_lines_before_subtotal(lines, subtotal_idx)
        block_labels: list[str] = []
        for line in block_lines:
            match = _CDE_AMOUNT_LINE_RE.match(line)
            if match:
                block_labels.append(_clean(match.group("label")))
        block_start_count = len(parsed)
        for raw_line in block_lines:
            match = _CDE_AMOUNT_LINE_RE.match(raw_line)
            if not match:
                continue
            label = _clean(match.group("label").strip(" :-"))
            is_balance_charge = _is_cde_balance_charge_label(label)
            if _is_cde_non_charge_label(label) and not is_balance_charge:
                continue
            amount = money(match.group("amount"))
            if amount == 0:
                continue
            classification = classify_utility_line(label)
            export_label = label
            metadata = {"cde_charge_block": block_kind.lower()}
            if is_balance_charge and classification == "previous_balance":
                export_label, classification = _cde_balance_charge_description(
                    block_kind,
                    block_labels,
                )
                metadata.update(
                    {
                        "cde_balance_charge": "true",
                        "cde_source_label": label,
                    }
                )
            debug.append(
                {
                    "block": block_kind.lower(),
                    "label": label,
                    "export_label": export_label,
                    "amount": str(amount),
                    "classification": classification,
                }
            )
            if classification == "tax":
                tax_total += amount
                continue
            parsed.append(
                UtilityChargeLine(
                    export_label,
                    amount,
                    line_type=classification if classification != "service" else "service",
                    gl_account=_cde_gl_for_charge(export_label, block_kind, block_labels),
                    taxable=classification not in {"connection_fee", "late_fee"},
                    source_page=source_page,
                    metadata=metadata,
                )
            )

        if len(parsed) == block_start_count:
            fallback_label = "Broadband Service" if block_kind == "TELECOM" else "Electric Service"
            parsed.append(
                UtilityChargeLine(
                    fallback_label,
                    block_total,
                    gl_account="6139" if block_kind == "TELECOM" else "",
                    source_page=source_page,
                    metadata={"cde_charge_block": block_kind.lower(), "fallback_line": "true"},
                )
            )
            tax_total = Decimal("0.00")

    return parsed, tax_total, subtotal_total, debug


def _cde_lines_before_subtotal(lines: list[str], subtotal_idx: int) -> list[str]:
    start_idx = -1
    for idx in range(subtotal_idx - 1, -1, -1):
        lower = lines[idx].lower()
        if "lost in the mail" in lower or "not responsible for bills" in lower:
            start_idx = idx
            break
        if _CDE_SUBTOTAL_RE.search(lines[idx]):
            start_idx = idx
            break
    if start_idx < 0:
        for idx in range(subtotal_idx - 1, -1, -1):
            lower = lines[idx].lower()
            if "meter read date" in lower or "power f" in lower:
                start_idx = idx
                break
    if start_idx < 0:
        start_idx = max(-1, subtotal_idx - 12)
    return lines[start_idx + 1 : subtotal_idx]


def _is_cde_non_charge_label(label: str) -> bool:
    lower = _clean(label).lower().rstrip(":")
    if lower in {
        "electric",
        "broadband",
        "total due",
        "total current charges",
    }:
        return True
    if lower.startswith(
        (
            "account #",
            "name",
            "service",
            "rate class",
            "meter read",
            "period days",
            "current",
            "last",
            "office hours",
            "po box",
            "p.o. box",
            "statement date",
            "date due",
            "phone number",
            "pay after",
        )
    ):
        return True
    return False


def _is_cde_balance_charge_label(label: str) -> bool:
    lower = _clean(label).lower().rstrip(":")
    return lower in _CDE_BALANCE_CHARGE_LABELS


def _cde_balance_charge_description(
    block_kind: str,
    block_labels: list[str],
) -> tuple[str, str]:
    if block_kind.upper() != "TELECOM":
        if any("security light" in candidate.lower() for candidate in block_labels):
            return "Past Due Outdoor Lighting Service", "electric_common_service"
        return "Past Due Electric Service", "electric_service"
    block_has_phone = any(
        any(keyword in candidate.lower() for keyword in _CDE_PHONE_KEYWORDS)
        for candidate in block_labels
    )
    if block_has_phone:
        return "Past Due Phone Service", "internet_fiber_service"
    return "Past Due Broadband Service", "internet_fiber_service"


def _cde_gl_for_charge(label: str, block_kind: str, block_labels: list[str]) -> str:
    lower = label.lower()
    if classify_utility_line(label) == "connection_fee":
        return "6956"
    if "security light" in lower:
        return "6915"
    if block_kind.upper() != "TELECOM":
        return ""
    if any(keyword in lower for keyword in _CDE_INTERNET_KEYWORDS):
        return "6139"
    if any(keyword in lower for keyword in _CDE_PHONE_KEYWORDS):
        return "6178"
    block_has_phone = any(
        any(keyword in candidate.lower() for keyword in _CDE_PHONE_KEYWORDS)
        for candidate in block_labels
    )
    return "6178" if block_has_phone else "6139"


def _parse_nolin_recc(
    spec: Wave2VendorSpec,
    text: str,
    source_file: str,
) -> list[ParsedUtilityInvoice]:
    billing_date = _parse_date(_first(r"Billing Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text))
    due = _parse_date(
        _first(
            r"(?:FINAL\s+)?PAYMENT\s+WILL\s+DRAFT\s+ON\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        or _first(
            r"Amount\s+Due\s+By(?:\s+5\s*pm)?\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        or _first(r"\bDUE\s+DATE\s+(\d{1,2}/\d{1,2}/\d{4})", text, flags=re.IGNORECASE)
    )
    global_bill_type = _first(
        r"Bill\s+Type:\s*(NEW ACCOUNT|FINAL|REGULAR)",
        text,
        flags=re.IGNORECASE,
    )

    accounts: dict[str, dict[str, Any]] = {}
    account_order: list[str] = []

    def upsert(account: str, **values: Any) -> dict[str, Any]:
        account = _clean(account)
        if not account:
            return {}
        if account not in accounts:
            accounts[account] = {"account": account}
            account_order.append(account)
        row = accounts[account]
        for key, value in values.items():
            if value is None or value == "":
                continue
            if key in {"amount", "source_total"}:
                value = money(value)
            row[key] = value
        return row

    # Master Nolin statements contain several accounts in a compact summary
    # table. The older parser only accepted apartment-address rows; this keeps
    # property-level meters such as outdoor lights and carport electric visible.
    summary_re = re.compile(
        r"^(?P<account>\d{9})\s+(?P<bill_type>NEW ACCOUNT|FINAL|REGULAR)\s+"
        r"(?P<addr>.+?)\s+\$?\.?(?:\d[\d,]*\.\d{2}|00)\s+"
        r"\$?(?P<current>[\d,]+\.\d{2})\s+\$?(?P<total>[\d,]+\.\d{2})$",
        re.IGNORECASE,
    )
    for line in _lines(text):
        match = summary_re.match(line)
        if not match:
            continue
        bill_type = _clean(match.group("bill_type")).upper()
        upsert(
            match.group("account"),
            bill_type=bill_type,
            service_address=_clean(match.group("addr")),
            amount=match.group("current"),
            source_total=match.group("total"),
        )

    account_info_matches = list(
        re.finditer(
            r"Account\s+Information.{0,220}?Account\s+Number:\s*(?P<account>\d{9})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    for idx, match in enumerate(account_info_matches):
        account = match.group("account")
        block_end = account_info_matches[idx + 1].start() if idx + 1 < len(account_info_matches) else len(text)
        block = text[match.start():block_end]
        preamble = text[max(0, match.start() - 650):match.start()]
        period_start, period_end = _nolin_meter_period_from_preamble(preamble)
        service_address = _nolin_block_value(block, "Service Address", "Location Description")
        location_desc = _nolin_block_value(block, "Location Description", "Rate Schedule")
        current_amount = _first(
            r"Current\s+Month\s+Charges\s+\$?([\d,]+\.\d{2})",
            block,
            flags=re.IGNORECASE,
        )
        block_due = _parse_date(
            _first(
                r"(?:FINAL\s+)?PAYMENT\s+WILL\s+DRAFT\s+ON\s+(\d{1,2}/\d{1,2}/\d{4})",
                block,
                flags=re.IGNORECASE,
            )
            or _first(r"\bDUE\s+DATE\s+(\d{1,2}/\d{1,2}/\d{4})", block, flags=re.IGNORECASE)
        )
        upsert(
            account,
            service_period_start=period_start,
            service_period_end=period_end,
            service_address=service_address,
            location_description=location_desc,
            amount=current_amount,
            source_total=current_amount,
            due_date=block_due,
        )

    if not accounts:
        header_account = _first(r"\bAccount\s+#\s*(\d{9})", text, flags=re.IGNORECASE)
        header_amount = _first(
            r"Amount\s+Due\s+By(?:\s+5\s*pm)?\s+\d{1,2}/\d{1,2}/\d{4}\s+\$?([\d,]+\.\d{2})",
            text,
            flags=re.IGNORECASE,
        )
        if header_account and header_amount:
            upsert(header_account, bill_type=global_bill_type, amount=header_amount, source_total=header_amount)

    invoices: list[ParsedUtilityInvoice] = []
    for account in account_order:
        data = accounts[account]
        amount = money(data.get("amount") or data.get("source_total") or 0)
        if amount <= 0:
            continue
        service_address = _clean(data.get("service_address") or "")
        location_desc = _clean(data.get("location_description") or "")
        line_desc, line_gl = _nolin_line_description_and_gl(service_address, location_desc)
        bill_type = _clean(data.get("bill_type") or global_bill_type).upper()
        suffix_final = " Final" if bill_type == "FINAL" else ""
        period_start = data.get("service_period_start") or billing_date
        period_end = data.get("service_period_end") or billing_date
        property_level = _nolin_is_property_level_service(service_address, location_desc)
        invoice = ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number=account,
            invoice_number="",
            invoice_date=billing_date or period_end,
            due_date=data.get("due_date") or due or billing_date or period_end,
            service_period_start=period_start,
            service_period_end=period_end,
            service_address=service_address,
            line_items=[
                UtilityChargeLine(
                    line_desc,
                    amount,
                    gl_account=line_gl,
                    source_page=1,
                    metadata={
                        "nolin_location_description": location_desc,
                        "nolin_bill_type": bill_type,
                    },
                )
            ],
            tax_total=Decimal("0.00"),
            source_file=source_file,
            location="" if property_level else _nolin_unit_from_service_address(service_address),
            property_abbreviation=_nolin_property_from_service_address(service_address),
            debug_info={
                "bill_type": bill_type,
                "invoice_suffix": suffix_final,
                "source_total": str(data.get("source_total") or amount),
                "location_description": location_desc,
                "property_level_service": property_level,
            },
        )
        invoices.append(invoice)
    return invoices


def _nolin_block_value(block: str, label: str, next_label: str) -> str:
    value = _first(
        rf"{re.escape(label)}:\s*(.*?)\s+{re.escape(next_label)}:",
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"\b(?:No\s+Payment\s+Received|Payment\(s\)\s+Received|Previous\s+Balance|"
        r"Balance\s+Forward|Late\s+Fee|Current\s+Activity)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return _clean(value)


def _nolin_meter_period_from_preamble(preamble: str) -> tuple[datetime | None, datetime | None]:
    matches = list(
        re.finditer(
            r"(\d{1,2}/\d{1,2}/\d{2})\s+(\d{1,2}/\d{1,2}/\d{2})",
            preamble or "",
        )
    )
    if not matches:
        return None, None
    last = matches[-1]
    return _parse_date(last.group(1)), _parse_date(last.group(2))


def _nolin_line_description_and_gl(service_address: str, location_desc: str) -> tuple[str, str]:
    hay = _norm(f"{service_address} {location_desc}")
    if "outdoor light" in hay:
        return "Outdoor Lights", "6915"
    if "carport" in hay:
        return "Carport Electric Service", "6915"
    return "Electric Service", ""


def _nolin_is_property_level_service(service_address: str, location_desc: str) -> bool:
    hay = _norm(f"{service_address} {location_desc}")
    if re.search(r"\bapt\s+[a-z0-9-]+\b", service_address or "", flags=re.IGNORECASE):
        return False
    return any(token in hay for token in ("outdoor lights", "carport", "pine valley apts", "p valley apts"))


def _nolin_unit_from_service_address(service_address: str) -> str:
    match = re.search(r"\bAPT\s+([A-Z0-9-]+)\b", service_address or "", flags=re.IGNORECASE)
    return _clean(match.group(1)) if match else ""


def _nolin_property_from_service_address(service_address: str) -> str:
    hay = _norm(service_address)
    if "pine valley" in hay or "p valley" in hay:
        return "VILLASPV"
    return ""


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
    property_level_service = bool(inv.debug_info.get("property_level_service"))
    if not property_level_service and not inv.location and service_match and service_match.unit_number:
        inv.location = service_match.unit_number
    if inv.location and looks_like_raw_address(inv.location):
        inv.location = ""
    if spec.key == "cde_lightband":
        _apply_cde_property_override(inv)

    month_anchor = {
        "accounting_date": inv.accounting_date,
        "invoice_date": inv.invoice_date,
        "due_date": inv.due_date,
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
        difference = expected - actual
        inv.manual_review_reasons.append("amount_reconciliation_failed")
        inv.debug_info["reconciliation"] = {
            "expected": str(expected),
            "actual": str(actual),
            "difference": str(difference),
            "direction": "missing_source_charges" if difference > 0 else "line_items_exceed_source_total",
            "diagnosis": (
                "Parsed line items are lower than the bill total; inspect omitted fees, taxes, or service rows."
                if difference > 0
                else "Parsed line items exceed the bill total; inspect duplicate rows or non-payable balances."
            ),
        }

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
    property_level_service = bool(inv.debug_info.get("property_level_service"))
    description_service_address = str(
        inv.debug_info.get("description_service_address")
        or inv.service_address
    )
    description_unit_number = ""
    if not property_level_service and inv.location and match and match.unit_number:
        unit_pattern = re.escape(str(inv.location).strip())
        address_already_has_unit = re.search(
            rf"(?:#|apt|unit)\s*{unit_pattern}\b",
            description_service_address,
            flags=re.I,
        )
        if not address_already_has_unit:
            description_unit_number = inv.location
    desc_context = {
        "category": "utilities",
        "service_period_start": inv.service_period_start,
        "service_period_end": inv.service_period_end,
        "service_address": description_service_address,
        "unit_number": description_unit_number,
        "property_name": match.property_name if match else "",
        "property_abbreviation": inv.property_abbreviation,
        "property_level_service": property_level_service,
    }
    invoice_desc_result = build_invoice_description(desc_context)
    invoice_desc = invoice_desc_result.description
    inv.manual_review_reasons.extend(invoice_desc_result.review_flags)
    inv.manual_review_reasons = sorted(set(r for r in inv.manual_review_reasons if r))
    desc_meta = {
        "service_address": description_service_address,
        "matched_property_name": match.property_name if match else "",
        "description_components": invoice_desc_result.components_used,
        "description_fallback_used": invoice_desc_result.fallback_used,
        "description_review_flags": list(invoice_desc_result.review_flags),
    }
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(inv.line_items, start=1):
        row_location = (
            str(line.metadata.get("row_location") or "")
            if "row_location" in line.metadata
            else inv.location
        )
        line_desc_context = dict(desc_context)
        line_desc_context["service_period_start"] = line.metadata.get(
            "cde_service_period_start",
            desc_context["service_period_start"],
        )
        line_desc_context["service_period_end"] = line.metadata.get(
            "cde_service_period_end",
            desc_context["service_period_end"],
        )
        line_desc_result = build_line_item_description(
            line_desc_context,
            {"description": line.description},
        )
        inv.manual_review_reasons.extend(line_desc_result.review_flags)
        inv.manual_review_reasons = sorted(set(r for r in inv.manual_review_reasons if r))
        row_invoice_desc = str(
            line.metadata.get("row_invoice_description")
            or invoice_desc
        )
        line_desc = str(
            line.metadata.get("row_line_item_description")
            or line_desc_result.description
        )
        row_service_address = str(
            line.metadata.get("row_service_address")
            or description_service_address
        )
        rows.append(
            {
                "Invoice Number": inv.invoice_number,
                "Bill or Credit": "Bill",
                "Invoice Date": _fmt_date(inv.invoice_date),
                "Accounting Date": _fmt_date(inv.accounting_date or inv.invoice_date),
                "Vendor": inv.vendor_display_name,
                "Invoice Description": row_invoice_desc,
                "Line Item Number": str(idx),
                "Property Abbreviation": inv.property_abbreviation,
                "Location": row_location,
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
                    "service_address": row_service_address,
                    "utility_wave": "U2",
                    "tax_allocated": line.metadata.get("tax_allocated", ""),
                    "source_charge_components": line.metadata.get("source_charge_components", []),
                    "allocated_charge_components": line.metadata.get("allocated_charge_components", []),
                    "line_type": line.line_type,
                    "source_page": line.source_page,
                },
            }
        )
    return rows


def _invoice_to_preview_dict(inv: ParsedUtilityInvoice) -> dict[str, Any]:
    rows = _rows_for_invoice(inv)
    line_items_total = sum((line.money for line in inv.line_items), Decimal("0.00"))
    source_total = money(inv.debug_info.get("source_total") or 0)
    invoice_total = source_total if source_total else line_items_total
    return {
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "billing_date": _fmt_iso(inv.invoice_date),
        "accounting_date": _fmt_iso(inv.accounting_date or inv.invoice_date),
        "service_period": (
            f"{_fmt_iso(inv.service_period_start)} -> {_fmt_iso(inv.service_period_end)}"
            if inv.service_period_start or inv.service_period_end
            else ""
        ),
        "property_abbreviation": inv.property_abbreviation,
        "location": inv.location,
        "service_address": inv.service_address,
        "total_amount": float(invoice_total),
        "line_items_total": float(line_items_total),
        "source_total_amount": float(source_total) if source_total else None,
        "manual_review_reasons": list(inv.manual_review_reasons),
        "rows": rows,
        "source_file": inv.source_file,
        "support_document_status": inv.support_document_status,
        "debug_info": inv.debug_info,
    }


def _manual_review_to_dict(inv: ParsedUtilityInvoice) -> dict[str, Any]:
    line_items_total = sum((line.money for line in inv.line_items), Decimal("0.00"))
    source_total = money(inv.debug_info.get("source_total") or 0)
    return {
        "source_file": inv.source_file,
        "account_number": inv.account_number,
        "invoice_number": inv.invoice_number,
        "invoice_date": _fmt_date(inv.invoice_date),
        "property_abbreviation": inv.property_abbreviation,
        "location": inv.location,
        "total_amount": float(source_total or line_items_total),
        "line_items_total": float(line_items_total),
        "source_total_amount": float(source_total) if source_total else None,
        "line_count": len(inv.line_items),
        "reasons": list(inv.manual_review_reasons),
        "reconciliation": inv.debug_info.get("reconciliation"),
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
    if classification == "electric_common_service":
        return "6915" if "6915" in valid_gls else ""
    if spec.key == "cde_lightband":
        charge_block = str(line.metadata.get("cde_charge_block") or "").lower()
        if charge_block == "electric" or classification in {
            "electric_service",
            "electric_common_service",
        }:
            if inv.location:
                return "6920" if "6920" in valid_gls else ""
            if inv.debug_info.get("property_level_service"):
                return "6915" if "6915" in valid_gls else ""
        if charge_block == "telecom" and line.gl_account in {"6139", "6178"}:
            return line.gl_account if line.gl_account in valid_gls else ""
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
    if classification == "stormwater_service":
        stormwater_gl = default_gl_for_line(line.description, vendor_key=spec.key, vendor_config=cfg)
        return stormwater_gl if stormwater_gl in valid_gls else ""
    if classification == "late_fee":
        if spec.key == "alabama_power" and any(
            token in (line.description or "").lower() for token in ("electric", "lighting")
        ):
            return _alabama_service_gl(inv.service_address, line.description, valid_gls)
        late_gl = default_gl_for_line(line.description, vendor_key=spec.key, vendor_config=cfg)
        return late_gl if late_gl in valid_gls and late_gl != "6956" else ""
    if spec.key == "alabama_power" and classification in {"electric_service", "electric_common_service"}:
        return _alabama_service_gl(inv.service_address, line.description, valid_gls)
    if spec.key == "nolin_recc_smarthub" and classification == "electric_service":
        if inv.debug_info.get("property_level_service"):
            return "6915" if "6915" in valid_gls else ""
        return "6920" if "6920" in valid_gls else ""
    if line.gl_account and line.gl_account in valid_gls:
        if line.gl_account == "6956" and classification != "connection_fee":
            return ""
        return line.gl_account
    historical = str(history.get("gl_account") or "")
    if historical in valid_gls and historical in UTILITY_GL_CODES and historical != "6956":
        return historical
    if spec.key == "epb_fiber_optics" and line.metadata.get("epb_bill_type") == "electric_power":
        fallback = "6915" if _epb_is_common_area_service(inv.service_address) else "6920"
        if fallback in valid_gls:
            return fallback
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


def _epb_is_common_area_service(service_address: str) -> bool:
    text = _norm(service_address)
    return any(token in text for token in ("hsmtr", "house meter", "bldg office", "building office", "office"))


def _match_service_address(service_address: str) -> UnitMatch | None:
    address = _address_only(service_address)
    if not address:
        return None
    return match_by_address(address) or match_by_address(service_address)


def _address_only(value: str) -> str:
    text = _clean(value)
    text = re.sub(r"\b(?:paid by autopay|service period|rate class|total due).*$", "", text, flags=re.I).strip()
    text = re.sub(r"\b(?:hsmtr|house meter|meter|rec|common|bldg|building)\b.*$", "", text, flags=re.I).strip()
    return text or _clean(value)


def _apply_cde_property_override(inv: ParsedUtilityInvoice) -> None:
    normalized = _norm(inv.service_address)
    account_digits = re.sub(r"\D", "", inv.account_number or "")
    propark = _cde_propark_service_context(inv.service_address, inv.account_number)
    if propark:
        inv.property_abbreviation = "OG-PPA"
        inv.location = str(propark.get("location") or "")
        inv.debug_info["description_service_address"] = propark.get(
            "description_service_address",
            inv.service_address,
        )
        if propark.get("property_level_service"):
            inv.debug_info["property_level_service"] = True
        else:
            inv.debug_info.pop("property_level_service", None)
        inv.debug_info["cde_service_context"] = propark.get("context")
        inv.debug_info["property_mapping_override"] = "cde_417402_propark"
        return
    harmony = _cde_harmony_service_context(inv.service_address, inv.account_number)
    if harmony:
        inv.property_abbreviation = "THSA"
        inv.location = str(harmony.get("location") or "")
        inv.debug_info["description_service_address"] = harmony.get(
            "description_service_address",
            inv.service_address,
        )
        if harmony.get("property_level_service"):
            inv.debug_info["property_level_service"] = True
        else:
            inv.debug_info.pop("property_level_service", None)
        inv.debug_info["cde_service_context"] = harmony.get("context")
        inv.debug_info["property_mapping_override"] = "cde_387667_harmony"
        return
    if account_digits.startswith("436602") or "375 s lancaster rd" in normalized:
        inv.property_abbreviation = "FAL"
        suffix = _cde_lancaster_unit_suffix(inv.service_address)
        if suffix:
            inv.location = suffix
        elif re.search(r"\b(?:hm|house meter|office|compactor|laundry|common)\b", normalized):
            inv.location = ""
            inv.debug_info["property_level_service"] = True
        inv.debug_info["property_mapping_override"] = "cde_436602_375_s_lancaster_fal"


def _cde_propark_service_context(
    service_address: str,
    account_number: str,
) -> dict[str, Any]:
    """Resolve ProPark apartment units without treating house meters as units."""

    account_digits = re.sub(r"\D", "", account_number or "")
    normalized = _norm(service_address)
    if not account_digits.startswith("417402") and "850 professional park dr" not in normalized:
        return {}

    suffix_match = re.search(
        r"\b850\s+PROFESSIONAL\s+(?:PARK|PK)\s+DR\s*(.*?)\s*$",
        service_address or "",
        flags=re.IGNORECASE,
    )
    suffix = _clean(suffix_match.group(1) if suffix_match else "")
    unit_match = re.fullmatch(r"([ABC])\s*-?\s*(\d{3})", suffix, flags=re.IGNORECASE)
    if unit_match:
        candidate = f"{unit_match.group(1).upper()}-{unit_match.group(2)}"
        canonical = lookup_unit("OG-PPA", candidate)
        if canonical:
            return {
                "location": canonical.unit_number,
                "property_level_service": False,
                "context": "apartment_unit",
                "description_service_address": (
                    f"850 Professional Park Dr {canonical.unit_number}"
                ),
            }

    house_meter = re.fullmatch(r"([ABC])\s*HM(\d*)", suffix, flags=re.IGNORECASE)
    if house_meter:
        meter_number = f" {house_meter.group(2)}" if house_meter.group(2) else ""
        return {
            "location": "",
            "property_level_service": True,
            "context": "house_meter",
            "description_service_address": (
                "850 Professional Park Dr - Building "
                f"{house_meter.group(1).upper()} House Meter{meter_number}"
            ),
        }
    if re.search(r"\b(?:CLUBHS|CLUBHOUSE|HOUSE\s+METER|COMMON)\b", suffix, flags=re.IGNORECASE):
        return {
            "location": "",
            "property_level_service": True,
            "context": "common_area",
            "description_service_address": "850 Professional Park Dr Clubhouse",
        }
    return {"location": "", "property_level_service": False, "context": "unresolved"}


def _cde_harmony_service_context(
    service_address: str,
    account_number: str,
) -> dict[str, Any]:
    """Resolve Harmony units and preserve its Office/House Meter context."""

    account_digits = re.sub(r"\D", "", account_number or "")
    normalized = _norm(service_address)
    if not account_digits.startswith("387667") and "841 professional park dr" not in normalized:
        return {}

    suffix_match = re.search(
        r"\b841\s+PROFESSIONAL\s+(?:PARK|PK)\s+DR(?:IVE)?\s*(.*?)\s*$",
        service_address or "",
        flags=re.IGNORECASE,
    )
    suffix = _clean(suffix_match.group(1) if suffix_match else "")
    unit_match = re.fullmatch(r"(\d{3})", suffix)
    if unit_match:
        candidate = f"841-{unit_match.group(1)}"
        canonical = lookup_unit("THSA", candidate)
        if canonical:
            return {
                "location": canonical.unit_number,
                "property_level_service": False,
                "context": "apartment_unit",
                "description_service_address": (
                    f"841 Professional Park Dr {unit_match.group(1)}"
                ),
            }

    if re.fullmatch(r"(?:OFC|OFFICE)", suffix, flags=re.IGNORECASE):
        return {
            "location": "",
            "property_level_service": True,
            "context": "office",
            "description_service_address": "841 Professional Park Dr Office",
        }
    if re.fullmatch(r"(?:HM|HOUSE\s+METER)", suffix, flags=re.IGNORECASE):
        return {
            "location": "",
            "property_level_service": True,
            "context": "house_meter",
            "description_service_address": "841 Professional Park Dr House Meter",
        }
    return {
        "location": "",
        "property_level_service": False,
        "context": "unresolved",
        "description_service_address": _clean(service_address),
    }


def _cde_lancaster_unit_suffix(service_address: str) -> str:
    match = re.search(
        r"\b375\s+S\s+LANCASTER\s+R(?:D|OAD)\s+(?!HM\b)(\d{1,4})(?:\b|$)",
        service_address or "",
        flags=re.I,
    )
    return match.group(1) if match else ""


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
