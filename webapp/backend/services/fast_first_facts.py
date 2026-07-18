"""Quality gate for the shadow-only fast facts extraction profile."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .. import settings


ROUTE_VERSION = "fast-first-facts/1.0"


def production_enabled() -> bool:
    """Fail closed unless both execution and golden-parity approvals exist."""
    return bool(
        settings.AI_FAST_FIRST_FACTS_ONLY_ENABLED
        and settings.AI_FAST_FIRST_GOLDEN_PARITY_APPROVED
    )


def escalation_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field in ("vendor_name", "invoice_number", "total_amount"):
        if payload.get(field) in (None, "", 0, 0.0):
            reasons.append(f"required_fact_missing:{field}")
    rows = [item for item in payload.get("line_items") or [] if isinstance(item, dict)]
    if not rows:
        reasons.append("payable_rows_missing")
    status = str(payload.get("visual_extraction_status") or "").strip().lower()
    if status in {"partial", "aggregate_fallback", "unknown", ""}:
        reasons.append(f"visual_status:{status or 'missing'}")
    if payload.get("unresolved_visual_regions"):
        reasons.append("unresolved_visual_regions")
    warnings = " | ".join(str(value).lower() for value in payload.get("warnings") or [])
    for token, code in (
        ("handwrit", "row_identity_ambiguous"),
        ("ambiguous", "visual_ambiguity"),
        ("paid", "paid_evidence_uncertain"),
        ("crossed", "crossed_out_evidence_uncertain"),
        ("collapsed", "financial_content_collapsed"),
        ("skipped", "financial_content_skipped"),
    ):
        if token in warnings:
            reasons.append(code)
    if any(
        str(item.get("row_label") or item.get("location_candidate") or "").strip() == ""
        and str(item.get("section_header") or item.get("activity") or "").strip()
        for item in rows
    ):
        reasons.append("row_identity_ambiguous")
    if len(rows) == 1 and any(
        token in warnings for token in ("table", "matrix", "allocation", "invoice total fallback")
    ):
        reasons.append("financial_content_collapsed")
    reconciliations = payload.get("page_reconciliations") or []
    if any(
        str(item.get("status") or "").lower() != "reconciled"
        or abs(_decimal(item.get("difference"))) > Decimal("0.01")
        for item in reconciliations
        if isinstance(item, dict)
    ):
        reasons.append("page_reconciliation_failed")
    line_total = sum((_decimal(item.get("amount")) for item in rows), Decimal("0"))
    adders = sum(
        (_decimal(payload.get(key)) for key in ("tax_amount", "shipping_amount", "fees_amount")),
        Decimal("0"),
    )
    total = _decimal(payload.get("total_amount"))
    if rows and total and abs((line_total + adders) - total) > Decimal("0.01"):
        reasons.append("invoice_reconciliation_failed")
    return list(dict.fromkeys(reasons))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0).replace("$", "").replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


__all__ = ["ROUTE_VERSION", "escalation_reasons", "production_enabled"]
