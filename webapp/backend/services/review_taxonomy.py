"""Typed, versioned review identity independent from warning prose."""
from __future__ import annotations

import re
from enum import Enum

from typing import Any

from pydantic import BaseModel


REVIEW_TAXONOMY_VERSION = "accounting-review-taxonomy/1.0"


class ReviewCategory(str, Enum):
    HANDWRITTEN_DATE_AMBIGUOUS = "handwritten_date_ambiguous"
    ROW_IDENTITY_AMBIGUOUS = "row_identity_ambiguous"
    VISUAL_COMPONENT_CONFLICT = "visual_component_conflict"
    PROPERTY_UNRESOLVED = "property_unresolved"
    TOTAL_RECONCILIATION_FAILED = "total_reconciliation_failed"
    PAID_MARKER_AMBIGUOUS = "paid_marker_ambiguous"
    PAYMENT_TERMS_CONFLICT = "payment_terms_conflict"
    EXTRACTION_INPUT_TRUNCATED = "extraction_input_truncated"
    VISUAL_EXTRACTION_DEGRADED = "visual_extraction_degraded"
    VISUAL_EXTRACTION_FAILED = "visual_extraction_failed"
    AMOUNT_INFERRED = "amount_inferred"
    PROPERTY_INFERRED = "property_inferred"
    VISUAL_EXTRACTION_WARNING = "visual_extraction_warning"


class TypedReviewEvidence(BaseModel):
    taxonomy_version: str = REVIEW_TAXONOMY_VERSION
    category: ReviewCategory
    original_warning: str


_RULES: tuple[tuple[ReviewCategory, tuple[str, ...]], ...] = (
    (ReviewCategory.HANDWRITTEN_DATE_AMBIGUOUS, ("handwritten date", "handwritten service date", "date ambiguous")),
    (ReviewCategory.ROW_IDENTITY_AMBIGUOUS, ("row identity", "apt", "apartment", "unit ambiguous")),
    (ReviewCategory.VISUAL_COMPONENT_CONFLICT, (
        "component conflict", "window sill", "tub mat", "visual conflict",
        "source views disagreed", "column amounts are faint", "column labels and amounts are less clear",
    )),
    (ReviewCategory.PAYMENT_TERMS_CONFLICT, (
        "terms 30 days", "upon receipt", "due date contains", "payment wording conflicts",
    )),
    (ReviewCategory.PAID_MARKER_AMBIGUOUS, ("paid marker", "crossed out", "crossed-out", "paid ambiguous")),
    (ReviewCategory.TOTAL_RECONCILIATION_FAILED, ("reconcil", "invoice difference", "total mismatch")),
    (ReviewCategory.PROPERTY_UNRESOLVED, ("property unresolved", "property missing", "no property")),
    (ReviewCategory.EXTRACTION_INPUT_TRUNCATED, ("input truncated", "ai_input_truncated")),
    (ReviewCategory.VISUAL_EXTRACTION_FAILED, ("vision failed", "visual extraction failed")),
    (ReviewCategory.VISUAL_EXTRACTION_DEGRADED, ("unreadable image", "ocr reference rescue", "degraded")),
    (ReviewCategory.AMOUNT_INFERRED, ("amount inferred", "amount_inferred")),
    (ReviewCategory.PROPERTY_INFERRED, ("property inferred", "property_inferred", "weak ocr address")),
)


def categorize_warning(warning: str) -> TypedReviewEvidence:
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(warning).casefold()).split())
    for category, needles in _RULES:
        if any(" ".join(re.sub(r"[^a-z0-9]+", " ", needle.casefold()).split()) in normalized
               for needle in needles):
            return TypedReviewEvidence(category=category, original_warning=str(warning))
    return TypedReviewEvidence(
        category=ReviewCategory.VISUAL_EXTRACTION_WARNING,
        original_warning=str(warning),
    )


def migrate_invoice_review_codes(invoice: dict[str, Any]) -> None:
    """Adapt historical free-text-derived codes without deleting their prose."""
    rows = [row for row in invoice.get("rows") or [] if isinstance(row, dict)]
    warnings: list[str] = []
    legacy_codes = [str(code) for code in invoice.get("manual_review_codes") or []
                    if str(code).startswith("ai_warning_")]
    for row in rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        for warning in meta.get("ai_warnings") or []:
            text = str(warning).strip()
            if text and text not in warnings:
                warnings.append(text)
    if not warnings:
        # Older artifacts may retain only the truncated code. Preserve it as
        # explanatory migration evidence and fail closed to a typed warning.
        warnings.extend(legacy_codes)
    typed = [categorize_warning(warning) for warning in warnings]
    codes = [str(code) for code in invoice.get("manual_review_codes") or []
             if not str(code).startswith("ai_warning_")]
    for item in typed:
        if item.category.value not in codes:
            codes.append(item.category.value)
    invoice["manual_review_codes"] = codes
    invoice["typed_review_evidence"] = [item.model_dump(mode="json") for item in typed]
    for row in rows:
        meta = row.setdefault("_meta", {})
        if isinstance(meta, dict):
            meta["typed_review_evidence"] = invoice["typed_review_evidence"]
    issues = invoice.get("manual_review_issues")
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict) or not str(issue.get("code") or "").startswith("ai_warning_"):
                continue
            message = str(issue.get("message") or "")
            item = categorize_warning(message.removeprefix("AI warning: "))
            issue["code"] = item.category.value


__all__ = [
    "REVIEW_TAXONOMY_VERSION", "ReviewCategory", "TypedReviewEvidence",
    "categorize_warning", "migrate_invoice_review_codes",
]
