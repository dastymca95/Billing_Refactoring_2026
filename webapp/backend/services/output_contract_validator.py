"""Post-generation ResMan row contract validation."""

from __future__ import annotations

import re
from typing import Any

from utils.text_normalization import (
    looks_like_city_state_zip,
    normalize_service_address_for_description,
    proper_case_preserve_acronyms,
)

from .utility_processor_common import (
    UTILITY_REQUIRED_COLUMNS,
    DEFAULT_UTILITY_GL,
    classify_utility_line,
    is_non_expense_line,
    load_chart_of_accounts,
    looks_like_raw_address,
)


def validate_row_contract(
    row: dict[str, Any],
    *,
    valid_gl_accounts: dict[str, str] | None = None,
    require_document_url: bool = False,
) -> list[str]:
    """Return blocking review flags for a generated ResMan row."""

    valid_gl_accounts = valid_gl_accounts if valid_gl_accounts is not None else load_chart_of_accounts()
    flags: list[str] = []
    for column in UTILITY_REQUIRED_COLUMNS:
        if column == "Document Url" and not require_document_url:
            continue
        if not str(row.get(column, "")).strip():
            flags.append(f"{_flag_column(column)}_missing")

    gl = str(row.get("GL Account") or "").strip()
    if gl and (not gl.isdigit() or (valid_gl_accounts and gl not in valid_gl_accounts)):
        flags.append("invalid_gl_account")

    location = str(row.get("Location") or "").strip()
    if looks_like_raw_address(location):
        flags.append("raw_address_in_location")

    invoice_description = str(row.get("Invoice Description") or "").strip()
    line_description = str(row.get("Line Item Description") or "").strip()
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    service_address = normalize_service_address_for_description(
        meta.get("service_address")
        or meta.get("ai_service_address")
        or meta.get("source_service_address")
        or "",
    )
    property_name = str(meta.get("matched_property_name") or meta.get("property_name") or "").strip()
    vendor_text = str(row.get("Vendor") or meta.get("ai_detected_vendor") or "").lower()
    property_level_service = gl in {"6810", "6760"} or any(
        token in vendor_text
        for token in ("landscap", "lawn", "pest", "termite", "exterminat")
    )
    one_off_invoice = str(meta.get("ai_category") or "").strip().lower() == "other_infrequent"

    if (
        service_address
        and invoice_description
        and service_address not in invoice_description
        and not property_level_service
        and not one_off_invoice
    ):
        flags.append("invoice_description_missing_service_address")
    if (
        service_address
        and property_name
        and property_name in invoice_description
        and service_address not in invoice_description
        and not property_level_service
        and not one_off_invoice
    ):
        flags.append("invoice_description_uses_property_instead_of_service_address")
    if looks_like_city_state_zip(invoice_description):
        flags.append("invoice_description_contains_city_state_zip")
    if invoice_description and invoice_description != proper_case_preserve_acronyms(invoice_description):
        flags.append("invoice_description_not_proper_case")
    if line_description and line_description != proper_case_preserve_acronyms(line_description):
        flags.append("line_item_description_not_proper_case")

    combined = f"{invoice_description} {line_description}"
    if is_non_expense_line(combined):
        flags.append("payment_or_previous_balance_expense_line")
    line_classification = classify_utility_line(line_description or invoice_description)
    meta_classification = str(meta.get("line_classification") or meta.get("line_type") or "")
    combined_classification = classify_utility_line(combined)
    if combined_classification == "tax" or line_classification == "tax":
        flags.append("standalone_tax_line")
    if line_classification == "connection_fee" and gl != "6956":
        flags.append("connection_fee_wrong_gl")
    if line_classification == "late_fee" and gl == "6956":
        flags.append("late_fee_wrong_connect_gl")
    if (
        line_classification == "fire_protection_service"
        or meta_classification == "fire_protection_service"
    ) and gl == DEFAULT_UTILITY_GL.get("water"):
        flags.append("fire_service_mapped_as_water")
    if line_classification in {"trash_service"} and gl != DEFAULT_UTILITY_GL.get("trash"):
        flags.append("trash_service_wrong_gl")

    if require_document_url and not str(row.get("Document Url") or "").strip():
        flags.append("document_url_missing")

    seen: set[str] = set()
    return [flag for flag in flags if not (flag in seen or seen.add(flag))]


def annotate_rows(
    rows: list[dict[str, Any]],
    *,
    require_document_url: bool = False,
) -> dict[str, int]:
    """Append row contract flags into each row's `_meta.manual_review_reasons`."""

    valid_gl_accounts = load_chart_of_accounts()
    rows_with_flags = 0
    flags_total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        flags = validate_row_contract(
            row,
            valid_gl_accounts=valid_gl_accounts,
            require_document_url=require_document_url,
        )
        if not flags:
            continue
        rows_with_flags += 1
        flags_total += len(flags)
        meta = row.setdefault("_meta", {})
        if not isinstance(meta, dict):
            row["_meta"] = meta = {}
        existing = meta.get("manual_review_reasons") or []
        if isinstance(existing, str):
            existing = [existing]
        merged = list(existing) + flags
        meta["manual_review_reasons"] = sorted(set(str(flag) for flag in merged if str(flag).strip()))
        meta["contract_blocking_reasons"] = flags
    return {"rows_with_flags": rows_with_flags, "flags_total": flags_total}


def _flag_column(column: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_")


__all__ = ["annotate_rows", "validate_row_contract"]
