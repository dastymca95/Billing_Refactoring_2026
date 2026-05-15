"""AI-assisted invoice extraction and validation.

This module is intentionally a webapp integration layer. It does not replace
the deterministic vendor processors; it only handles unknown / variable
supplier invoices when the AI assist provider is explicitly enabled.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import settings
from . import ai_provider
from . import ai_vision
from . import ai_mapping_review
from . import canonical_rules
from . import document_ingestion
from . import invoice_format_rules
from . import support_documents
from .description_builder import build_invoice_description, build_line_item_description
from .template_rules import get_template_rules


_LOG = logging.getLogger(__name__)

AI_VENDOR_KEY = "ai_assisted"
AI_MANUAL_REVIEW_MESSAGE = (
    "AI invoice processing is not configured. This vendor requires manual "
    "review or a dedicated processor."
)
AI_VISION_REQUIRED_MESSAGE = (
    "This screenshot or photo does not contain readable embedded text. "
    "Enable AI Vision or upload a text-based PDF."
)
TAX_HANDLING_POLICIES = {"manual_review", "distribute_proportionally", "separate_tax_line"}
DATE_SOURCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("invoice_date", "explicit invoice date"),
    ("purchase_date", "purchase date"),
    ("ship_date", "ship date"),
    ("received_date", "received date"),
)

VARIABLE_VENDOR_HINTS: dict[str, tuple[str, ...]] = {
    "hd_supply": ("hd supply", "hdsupply"),
    "lowes": ("lowe's", "lowes", "lowe s"),
    "home_depot": ("home depot", "the home depot"),
    "maintenance_supplier": (
        "maintenance",
        "materials",
        "supply",
        "repair",
        "hardware",
        "appliance",
    ),
}


def processing_mode_for_vendor(vendor_key: str, detection: dict | None = None) -> str:
    """Return the configured processing mode for a non-deterministic vendor.

    Deterministic vendors are decided by ``batch_processor`` before this helper
    is called. Unknown and variable supplier invoices default to AI-assisted.
    """
    explicit = ""
    if isinstance(detection, dict):
        explicit = str(detection.get("processing_mode") or "").strip().lower()
    if explicit in {"deterministic", "ai_assisted", "hybrid"}:
        return explicit
    if vendor_key == "unknown" or vendor_key in VARIABLE_VENDOR_HINTS:
        return "ai_assisted"
    return "ai_assisted"


def should_route_to_ai(vendor_key: str, detection: dict | None = None) -> bool:
    return processing_mode_for_vendor(vendor_key, detection) in {"ai_assisted", "hybrid"}


def process_ai_vendor_files(
    *,
    batch_id: str,
    vendor_key: str,
    files: list[Path],
    detection: dict[str, dict],
    tracker: Any = None,
    should_cancel: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Process unknown / variable supplier files through the AI path.

    Returns a vendor-like payload that ``batch_processor`` can merge into the
    normal webapp result shape.
    """
    status = ai_provider.provider_status()
    invoices: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    processed_files = 0

    _tracker_start(tracker, len(files), status)
    if not status.enabled or not status.configured:
        for f in files:
            review = _manual_review_item(
                source_file=f.name,
                vendor_name=_vendor_hint_for_file(vendor_key, f, detection.get(f.name)),
                reasons=["ai_invoice_processing_not_configured"],
                message=AI_MANUAL_REVIEW_MESSAGE,
            )
            manual_review.append(review)
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": "ai_invoice_processing_not_configured",
                "message": AI_MANUAL_REVIEW_MESSAGE,
                "detection": detection.get(f.name),
            })
        _tracker_finish(tracker, invoices, manual_review, warning=True)
        return _payload(files, processed_files, invoices, manual_review, unsupported)

    references = load_references()
    template_schema = {
        "columns": get_template_rules().get("columns", []),
        "required_columns": get_template_rules().get("required_columns", []),
        "recommended_columns": get_template_rules().get("recommended_columns", []),
    }

    for index, f in enumerate(files, start=1):
        if should_cancel and should_cancel():
            break
        vendor_hint = _vendor_hint_for_file(vendor_key, f, detection.get(f.name))
        document_text = ""
        document_candidate: document_ingestion.DocumentCandidate | None = None
        try:
            _tracker_update(
                tracker,
                percent=_range_pct(index - 1, len(files), 8),
                stage="Scanning invoice",
                current_file=f.name,
                files_done=index - 1,
                files_total=len(files),
            )
            document_candidate = document_ingestion.ingest_document(
                f,
                vendor_hint=vendor_hint,
                max_pages=max(1, int(getattr(settings, "AI_MAX_PAGES", 5) or 5)),
            )
            document_text = document_candidate.document_text
            vision_images: list[str] = []
            use_vision = _should_use_vision_for_candidate(document_candidate, status)
            if use_vision:
                if f.suffix.lower() in ai_vision.IMAGE_EXTENSIONS:
                    vision_images = [ai_vision.image_path_as_data_url(f)]
                elif f.suffix.lower() == ".pdf":
                    max_vision_pages = max(
                        1,
                        int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2),
                    )
                    vision_images = ai_vision.render_pdf_pages_as_data_urls(
                        batch_id=batch_id,
                        filename=f.name,
                        page_numbers=list(range(1, max_vision_pages + 1)),
                    )
            elif not document_text.strip():
                raise ai_provider.AIProviderNotConfigured(AI_VISION_REQUIRED_MESSAGE)

            _tracker_update(
                tracker,
                percent=_range_pct(index - 1, len(files), 25),
                stage="Reading line items",
                current_file=f.name,
            )
            if vision_images:
                try:
                    raw = ai_provider.extract_invoice_vision_structured(
                        vendor_hint=vendor_hint,
                        document_text=document_text,
                        page_images_or_refs=vision_images,
                        template_schema=template_schema,
                        property_reference=references["properties"],
                        gl_reference=references["gl_accounts"],
                        vendor_reference=references["vendors"],
                    )
                    ai_vision.save_vision_trace_regions(
                        batch_id=batch_id,
                        source_file=f.name,
                        candidates=list(raw.get("vision_candidates") or []),
                        feeds_rows=[],
                    )
                    extraction_provider = status.vision_provider or status.provider
                    extraction_model = status.vision_model or status.model
                    extraction_mode = "ai_vision"
                except ai_provider.AIProviderError:
                    if not document_text.strip():
                        raise
                    _LOG.warning("AI vision failed for %s; falling back to text extraction.", f.name)
                    raw = ai_provider.extract_invoice_structured(
                        vendor_hint=vendor_hint,
                        document_text=document_text,
                        page_images_or_refs=[],
                        template_schema=template_schema,
                        property_reference=references["properties"],
                        gl_reference=references["gl_accounts"],
                        vendor_reference=references["vendors"],
                    )
                    warnings = _normalize_warnings(raw.get("warnings") or [])
                    if "ai_vision_failed_text_fallback_used" not in warnings:
                        warnings.append("ai_vision_failed_text_fallback_used")
                    raw["warnings"] = warnings
                    extraction_provider = status.provider
                    extraction_model = status.model
                    extraction_mode = "ai_text_after_vision_fallback"
            else:
                raw = ai_provider.extract_invoice_structured(
                    vendor_hint=vendor_hint,
                    document_text=document_text,
                    page_images_or_refs=[],
                    template_schema=template_schema,
                    property_reference=references["properties"],
                    gl_reference=references["gl_accounts"],
                    vendor_reference=references["vendors"],
                )
                extraction_provider = status.provider
                extraction_model = status.model
                extraction_mode = "ai_text"
            raw = _repair_ai_payload_from_ocr(raw, document_text, source_file=f.name)
            raw["_document_text"] = document_text
            raw["_source_file"] = f.name
            raw["_source_type"] = document_candidate.source_type if document_candidate else ""
            raw["_document_candidate"] = document_candidate.to_dict() if document_candidate else {}
            if document_candidate and document_candidate.warnings:
                warnings = _normalize_warnings(raw.get("warnings") or [])
                for warning in document_candidate.warnings:
                    if warning not in warnings:
                        warnings.append(warning)
                raw["warnings"] = warnings
            _tracker_update(
                tracker,
                percent=_range_pct(index - 1, len(files), 60),
                stage="Validating totals",
                current_file=f.name,
            )
            normalized = validate_ai_extraction(raw, references=references)
            normalized["ai_provider"] = extraction_provider
            normalized["ai_model"] = extraction_model
            normalized["ai_extraction_mode"] = extraction_mode
            normalized = ai_mapping_review.apply_learned_mappings_to_normalized(
                normalized
            )
            support_link = support_documents.upload_source_document_to_dropbox(
                batch_id=batch_id,
                source_file=f.name,
                vendor_name=normalized.get("vendor_name") or vendor_hint,
                invoice_date=normalized.get("invoice_date"),
                dry_run=dry_run,
            )
            if not support_link.success and support_link.review_code:
                _append_review_issue(
                    normalized,
                    code=support_link.review_code,
                    message=support_link.review_message,
                    severity="medium",
                )
            inv = ai_result_to_invoice(
                normalized,
                batch_id=batch_id,
                source_file=f.name,
                vendor_key=vendor_key,
                support_document_url=support_link.url,
                support_document_status=support_link.status,
                support_document_dropbox_path=support_link.dropbox_path,
            )
            invoices.append(inv)
            if normalized["manual_review_reasons"]:
                manual_review.append(
                    _manual_review_item(
                        source_file=f.name,
                        vendor_name=normalized.get("vendor_name") or vendor_hint,
                        invoice_number=normalized.get("invoice_number", ""),
                        invoice_date=normalized.get("invoice_date", ""),
                        total_amount=normalized.get("total_amount", 0),
                        account_number=normalized.get("account_number", ""),
                        property_abbreviation=normalized.get("property_abbreviation", ""),
                        location=normalized.get("location", ""),
                        service_address=normalized.get("service_address", ""),
                        line_count=len(inv.get("rows") or []),
                        reasons=normalized["manual_review_reasons"],
                        reason_codes=normalized.get("manual_review_codes", []),
                        message="AI extraction needs operator review.",
                    )
                )
            processed_files += 1
            _tracker_update(
                tracker,
                percent=_range_pct(index, len(files), 82),
                stage="Building ResMan template",
                current_file=f.name,
                files_done=index,
                invoices_created=len(invoices),
                warnings_count=len(manual_review),
            )
        except ai_provider.AIProviderNotConfigured as exc:
            provider_message = str(exc) or AI_MANUAL_REVIEW_MESSAGE
            is_vision_required = "Vision" in provider_message or "screenshot" in provider_message
            reason = (
                "ai_vision_not_configured"
                if is_vision_required
                else "ai_invoice_processing_not_configured"
            )
            message = provider_message if is_vision_required else AI_MANUAL_REVIEW_MESSAGE
            manual_review.append(
                _manual_review_item(
                    f.name,
                    vendor_hint,
                    reasons=[reason],
                    message=message,
                )
            )
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": reason,
                "message": message,
                "detection": detection.get(f.name),
            })
        except (ai_provider.AIProviderInvalidJSON, ai_provider.AIProviderInvalidSchema):
            reason = "ai_response_invalid_json"
            manual_review.append(
                _manual_review_item(
                    source_file=f.name,
                    vendor_name=vendor_hint,
                    reasons=[reason],
                    message="AI returned an invalid extraction payload. Review this file manually.",
                )
            )
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": reason,
                "message": "AI returned an invalid extraction payload. Review this file manually.",
                "detection": detection.get(f.name),
            })
        except Exception as exc:
            _LOG.warning("AI invoice processing failed for %s: %s", f.name, exc)
            fallback = _try_local_ocr_fallback_invoice(
                batch_id=batch_id,
                source_file=f.name,
                vendor_key=vendor_key,
                vendor_hint=vendor_hint,
                document_text=document_text,
                references=references,
                failure_reason=str(exc),
            )
            if fallback is not None:
                inv, normalized = fallback
                invoices.append(inv)
                if normalized["manual_review_reasons"]:
                    manual_review.append(
                        _manual_review_item(
                            source_file=f.name,
                            vendor_name=normalized.get("vendor_name") or vendor_hint,
                            invoice_number=normalized.get("invoice_number", ""),
                            invoice_date=normalized.get("invoice_date", ""),
                            total_amount=normalized.get("total_amount", 0),
                            account_number=normalized.get("account_number", ""),
                            property_abbreviation=normalized.get("property_abbreviation", ""),
                            location=normalized.get("location", ""),
                            service_address=normalized.get("service_address", ""),
                            line_count=len(inv.get("rows") or []),
                            reasons=normalized["manual_review_reasons"],
                            reason_codes=normalized.get("manual_review_codes", []),
                            message="Local OCR fallback created a reviewable invoice after AI provider failure.",
                        )
                    )
                processed_files += 1
                continue
            reason = "ai_processing_failed"
            manual_review.append(
                _manual_review_item(
                    source_file=f.name,
                    vendor_name=vendor_hint,
                    reasons=[reason],
                    message="AI invoice processing failed. Review this file manually.",
                )
            )
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": reason,
                "message": "AI invoice processing failed. Review this file manually.",
                "detection": detection.get(f.name),
            })

    _tracker_finish(tracker, invoices, manual_review, warning=bool(unsupported))
    return _payload(files, processed_files, invoices, manual_review, unsupported)


def _try_local_ocr_fallback_invoice(
    *,
    batch_id: str,
    source_file: str,
    vendor_key: str,
    vendor_hint: str,
    document_text: str,
    references: dict[str, list[dict[str, Any]]],
    failure_reason: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a flagged invoice from local OCR when the provider is unavailable.

    This is intentionally conservative: it never marks the invoice ready by
    itself, but it avoids an empty template when OCR exposes enough vendor,
    amount, or historical mapping evidence for an operator-reviewable row.
    """
    if not (document_text or "").strip():
        return None
    safe_vendor_hint = "" if vendor_hint.lower().startswith("unknown") else vendor_hint
    raw: dict[str, Any] = {
        "vendor_name": safe_vendor_hint,
        "invoice_number": "",
        "invoice_date": "",
        "due_date": "",
        "bill_or_credit": "Bill",
        "account_number": "",
        "service_address": "",
        "service_period_start": "",
        "service_period_end": "",
        "property_candidate": "",
        "property_abbreviation": "",
        "invoice_description": "",
        "line_items": [],
        "subtotal": 0,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": 0,
        "confidence": 0.55,
        "warnings": [
            "provider_unavailable_local_ocr_fallback",
            _flagify(failure_reason)[:80] if failure_reason else "provider_unavailable",
        ],
        "needs_manual_review": True,
    }
    raw = _repair_ai_payload_from_ocr(raw, document_text, source_file=source_file)
    if (
        not _clean(raw.get("vendor_name"))
        and not _money(raw.get("total_amount"))
        and not list(raw.get("line_items") or [])
    ):
        return None
    raw["_document_text"] = document_text
    raw["_source_file"] = source_file
    normalized = validate_ai_extraction(raw, references=references)
    normalized["ai_provider"] = "local_ocr_fallback"
    normalized["ai_model"] = "local_tesseract"
    normalized["ai_extraction_mode"] = "local_ocr_after_provider_failure"
    inv = ai_result_to_invoice(
        normalized,
        batch_id=batch_id,
        source_file=source_file,
        vendor_key=vendor_key,
    )
    return inv, normalized


def _should_use_vision_for_file(path: Path, document_text: str, status: ai_provider.AIProviderStatus) -> bool:
    """Decide whether an uploaded invoice should be sent as an image.

    Screenshots/photos are visual documents by definition, so when vision is
    explicitly enabled they should not be forced through OCR first. PDFs use
    vision only in explicit/weak-text cases to preserve deterministic and
    text-based performance.
    """
    if not status.vision_enabled:
        return False
    suffix = path.suffix.lower()
    mode = (status.vision_mode or "fallback_only").strip().lower()
    if suffix in ai_vision.IMAGE_EXTENSIONS:
        return True
    if suffix != ".pdf":
        return False
    if mode in {"always", "primary", "vision_first"}:
        return True
    if not (document_text or "").strip():
        return True
    if mode in {"fallback_only", "auto", "weak_text"}:
        return _ocr_quality_score(document_text) < 0.45
    return False


def _should_use_vision_for_candidate(
    candidate: document_ingestion.DocumentCandidate,
    status: ai_provider.AIProviderStatus,
) -> bool:
    """Use normalized ingestion quality to decide if vision is warranted."""
    if not status.vision_enabled:
        return False
    source_type = (candidate.source_type or "").strip().lower()
    mode = (status.vision_mode or "fallback_only").strip().lower()
    if source_type in {"image", "screenshot"}:
        return True
    if source_type != "pdf_scanned":
        return mode in {"always", "primary", "vision_first"} and source_type == "pdf_digital"
    if mode in {"always", "primary", "vision_first"}:
        return True
    quality = candidate.extraction_quality or {}
    if not (candidate.document_text or "").strip():
        return True
    if mode in {"fallback_only", "auto", "weak_text"}:
        try:
            return float(quality.get("text_quality_score") or 0) < 0.45
        except Exception:
            return True
    return False


def extract_document_text(path: Path) -> str:
    """Backward-compatible text extractor backed by DocumentCandidate."""
    return document_ingestion.ingest_document(
        path,
        max_pages=max(1, int(getattr(settings, "AI_MAX_PAGES", 5) or 5)),
    ).document_text


def _extract_pdf_image_text(path: Path) -> str:
    """Best-effort OCR for scanned PDFs before the external AI call.

    Vision remains the primary path for image-only PDFs when configured, but
    local OCR gives the text model a fallback when the vision provider is
    temporarily unavailable or rate-limited.
    """
    try:
        import pypdfium2 as pdfium  # type: ignore
        from PIL import ImageEnhance, ImageOps  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return ""

    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return ""

    texts: list[str] = []
    try:
        page_limit = max(1, int(getattr(settings, "AI_MAX_PAGES", 2) or 2))
        for page_index in range(min(len(doc), page_limit)):
            try:
                page = doc[page_index]
                width = float(page.get_width() or 612)
                scale = min(3.0, max(2.0, 1900 / width))
                img = page.render(scale=scale).to_pil()
                img = ImageOps.grayscale(img)
                img = ImageEnhance.Contrast(img).enhance(1.8)
                page_texts: list[str] = []
                for label, config in (
                    ("OCR_PDF_TABLE_PASS", "--psm 6"),
                    ("OCR_PDF_PAGE_PASS", "--psm 3"),
                ):
                    try:
                        text = pytesseract.image_to_string(img, config=config).strip()
                    except Exception:
                        text = ""
                    if text and text not in page_texts:
                        page_texts.append(f"{label} page {page_index + 1}\n{text}")
                if page_texts:
                    texts.extend(page_texts)
            except Exception:
                continue
    finally:
        close = getattr(doc, "close", None)
        if callable(close):
            close()
    return "\n\n".join(texts)


def _extract_image_text(path: Path) -> str:
    """Best-effort local OCR for pasted screenshots/photos.

    This keeps screenshot processing useful even when external AI vision is
    not configured. If OCR tooling is unavailable or the image is unreadable,
    callers can still fall through to the explicit AI Vision path.
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return ""

    try:
        with Image.open(path) as img:
            width, height = img.size
            target_width = min(2600, max(width, 1800))
            if width and width < target_width:
                ratio = target_width / float(width)
                img = img.resize((int(width * ratio), max(1, int(height * ratio))))
            img = ImageOps.grayscale(img)
            img = ImageEnhance.Contrast(img).enhance(1.65)
            passes: list[str] = []

            def add_pass(label: str, image: Any, config: str) -> None:
                try:
                    text = pytesseract.image_to_string(image, config=config).strip()
                except Exception:
                    return
                if not text:
                    return
                if text not in passes:
                    passes.append(f"{label}\n{text}")

            # PSM 6 is much better for pasted invoice screenshots with
            # rectangular line-item tables; the default page-layout pass often
            # drops right-side amount columns.
            add_pass("OCR_FULL_TABLE_PASS", img, "--psm 6")
            add_pass("OCR_PAGE_LAYOUT_PASS", img, "--psm 3")
            try:
                crop = img.crop((int(img.width * 0.58), int(img.height * 0.25), img.width, int(img.height * 0.82)))
                crop = ImageEnhance.Contrast(crop).enhance(2.1)
                add_pass("OCR_AMOUNT_COLUMN_PASS", crop, "--psm 6")
            except Exception:
                pass
            return "\n\n".join(passes)
    except Exception:
        return ""


def load_references() -> dict[str, list[dict[str, Any]]]:
    return {
        "vendors": _load_vendor_reference(),
        "properties": _load_property_reference(),
        "gl_accounts": _load_gl_reference(),
    }


def validate_ai_extraction(
    payload: dict[str, Any],
    *,
    references: dict[str, list[dict[str, Any]]] | None = None,
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("AI extraction payload must be a JSON object.")
    references = references or load_references()
    issues: list[dict[str, str]] = []
    warnings = _normalize_warnings(payload.get("warnings") or [])

    def add_issue(code: str, message: str, severity: str = "medium") -> None:
        if not any(issue["code"] == code for issue in issues):
            issues.append({"code": code, "message": message, "severity": severity})

    vendor_name = _clean(payload.get("vendor_name"))
    canonical_vendor = _canonical_vendor(vendor_name, references["vendors"])
    if not vendor_name:
        add_issue("vendor_name_missing", "Vendor name is missing from the AI extraction.", "high")
    elif not canonical_vendor:
        add_issue(
            "vendor_mapping_required",
            f"Vendor '{vendor_name}' was extracted but is not confirmed in the ResMan Vendor List. Confirm the vendor mapping.",
        )
    vendor_for_rows = canonical_vendor

    raw_invoice_date, invoice_date_source = _choose_invoice_date_source(payload)
    invoice_date, invoice_date_ok = _normalize_date(raw_invoice_date)
    due_date, due_date_ok = _normalize_date(payload.get("due_date"))
    if not invoice_date:
        add_issue("invoice_date_missing", "Invoice date is missing.", "high")
    elif not invoice_date_ok:
        add_issue("invalid_invoice_date", f"Invoice date '{raw_invoice_date}' could not be normalized.", "high")
    elif invoice_date_source != "invoice_date":
        source_label = dict(DATE_SOURCE_FIELDS).get(invoice_date_source, invoice_date_source)
        add_issue(
            f"invoice_date_inferred_from_{invoice_date_source}",
            f"Invoice date was not explicit; {source_label} was used as the invoice date.",
        )
    if payload.get("due_date") and not due_date_ok:
        add_issue("invalid_due_date", f"Due date '{payload.get('due_date')}' could not be normalized.")

    total_amount = _money(payload.get("total_amount"))
    property_abbreviation = _clean(payload.get("property_abbreviation"))
    property_candidate = _clean(payload.get("property_candidate"))
    service_address = _clean(payload.get("service_address"))
    account_number = _resolve_account_number(payload)
    service_period_start, service_period_end, service_period_source = _resolve_service_period(payload)
    source_invoice_number = _clean(payload.get("invoice_number"))
    invoice_number_policy_applied = False
    invoice_number = invoice_format_rules.render_invoice_number(
        {
            **payload,
            "vendor_name": vendor_for_rows or vendor_name,
            "raw_vendor_name": vendor_name,
            "account_number": account_number,
            "invoice_date": invoice_date,
            "service_period_start": service_period_start,
            "service_period_end": service_period_end,
            "service_address": service_address,
            "property_candidate": property_candidate,
            "property_abbreviation": property_abbreviation,
        },
        {},
        fallback="",
        source_file=_clean(payload.get("_source_file")),
        total_amount=total_amount,
    )
    if invoice_number:
        invoice_number_policy_applied = True
    else:
        invoice_number = source_invoice_number
    invoice_number_generated = False
    if not invoice_number:
        invoice_number = _derive_required_invoice_number(
            payload,
            invoice_date=invoice_date,
            total_amount=total_amount,
            service_period_start=service_period_start,
            service_period_end=service_period_end,
        )
        invoice_number_generated = bool(invoice_number)
    if invoice_number_policy_applied and source_invoice_number and source_invoice_number != invoice_number:
        add_issue(
            "invoice_number_formatted_from_policy",
            (
                f"Invoice number was formatted by the active Formats rule as '{invoice_number}'. "
                f"The source invoice number was '{source_invoice_number}'."
            ),
        )
    if invoice_number_generated:
        add_issue(
            "invoice_number_generated",
            "No explicit invoice number was found; a stable bill number was generated from visible bill/account/source details. Confirm if the vendor requires an exact invoice number.",
        )
    elif not invoice_number:
        add_issue("invoice_number_missing", "Invoice number is missing and could not be generated from bill context.", "high")
    property_abbreviation, location, property_match = _resolve_property_context(
        property_abbreviation=property_abbreviation,
        property_candidate=property_candidate,
        service_address=service_address,
        properties=references["properties"],
    )
    if not property_abbreviation and vendor_for_rows:
        fallback_property, fallback_reason = _required_property_fallback(
            vendor_name=vendor_for_rows,
            property_candidate=property_candidate,
            service_address=service_address,
            document_text=str(payload.get("_document_text") or ""),
        )
        if fallback_property:
            property_abbreviation, location, property_match = _resolve_property_context(
                property_abbreviation=fallback_property,
                property_candidate=property_candidate,
                service_address=service_address,
                properties=references["properties"],
            )
            if property_abbreviation:
                add_issue(
                    "property_prefilled_from_history",
                    (
                        "Property was prefilled from vendor/property history "
                        f"({fallback_property}). Confirm before export."
                    ),
                )
                if fallback_reason:
                    add_issue(fallback_reason, "Property fallback used local reference history.")
    if not property_abbreviation:
        add_issue(
            "property_mapping_required",
            "Property could not be confirmed from the known property/unit references. Confirm the property before exporting.",
        )
    raw_location = _clean(payload.get("location"))
    if raw_location and not location:
        add_issue(
            "location_unresolved",
            "Location could not be validated as a known unit/location. Raw addresses are not written to the Location column.",
        )
    elif service_address and not location:
        add_issue(
            "location_unresolved",
            "Service address was captured, but no known unit/location was confirmed. Location was left blank.",
        )

    line_items = payload.get("line_items")
    if not isinstance(line_items, list) or not line_items:
        add_issue(
            "line_items_missing",
            "No line items were returned. The invoice total was used as a fallback line.",
            "high",
        )
        total_fallback = _money(payload.get("total_amount"))
        line_items = [{
            "description": payload.get("invoice_description") or "Invoice total",
            "amount": total_fallback,
            "confidence": payload.get("confidence"),
            "reason": "No line items returned by AI; using invoice total.",
        }]

    normalized_items: list[dict[str, Any]] = []
    gl_issue_seen = False
    zero_amount_excluded = 0
    skipped_zero_items: list[dict[str, Any]] = []
    for idx, item in enumerate(line_items, start=1):
        item = item if isinstance(item, dict) else {}
        description = _clean(item.get("description")) or f"Line item {idx}"
        amount = _money(item.get("amount"))
        if abs(amount) <= 0.0 and not settings.AI_INCLUDE_ZERO_AMOUNT_LINES:
            zero_amount_excluded += 1
            skipped_zero_items.append(item)
            continue
        raw_item_confidence = _confidence_or_none(item.get("confidence"))
        raw_gl_candidate = _clean(item.get("gl_account_candidate"))
        gl_account = ai_mapping_review.validate_gl_account(raw_gl_candidate)
        gl_suggestion_source = "ai_validated" if gl_account else ""
        if not gl_account:
            suggested = _suggest_valid_gl_candidate(
                description=description,
                vendor_name=vendor_for_rows or vendor_name,
                ai_suggested_gl=raw_gl_candidate,
            )
            if suggested:
                gl_account = suggested
                gl_suggestion_source = "candidate_engine"
        gl_candidate = gl_account["gl_code"] if gl_account else ""
        gl_name = gl_account["gl_name"] if gl_account else ""
        if not gl_account:
            gl_issue_seen = True
        normalized_items.append({
            "description": description,
            "quantity": _nullable_float(item.get("quantity")),
            "unit_price": _nullable_money(item.get("unit_price")),
            "amount": amount,
            "gl_account_candidate": gl_candidate,
            "source_gl_candidate": raw_gl_candidate,
            "gl_suggestion_source": gl_suggestion_source,
            "gl_name": gl_name,
            "expense_type": _clean(item.get("expense_type")) or "General",
            "is_replacement_reserve": bool(item.get("is_replacement_reserve")),
            "confidence": raw_item_confidence,
            "reason": _clean(item.get("reason")),
        })
    if not normalized_items and total_amount:
        fallback_item = _build_total_fallback_line_item(
            payload=payload,
            skipped_items=skipped_zero_items,
            total_amount=total_amount,
            vendor_name=vendor_for_rows or vendor_name,
        )
        raw_gl_candidate = _clean(fallback_item.get("gl_account_candidate"))
        gl_account = ai_mapping_review.validate_gl_account(raw_gl_candidate)
        gl_suggestion_source = "ai_validated" if gl_account else ""
        if not gl_account:
            suggested = _suggest_valid_gl_candidate(
                description=_clean(fallback_item.get("description")),
                vendor_name=vendor_for_rows or vendor_name,
                ai_suggested_gl=raw_gl_candidate,
            )
            if suggested:
                gl_account = suggested
                gl_suggestion_source = "candidate_engine"
        if not gl_account:
            gl_issue_seen = True
        normalized_items.append({
            "description": _clean(fallback_item.get("description")) or "Invoice total",
            "quantity": 1.0,
            "unit_price": total_amount,
            "amount": total_amount,
            "gl_account_candidate": gl_account["gl_code"] if gl_account else "",
            "source_gl_candidate": raw_gl_candidate,
            "gl_suggestion_source": gl_suggestion_source or "invoice_total_fallback",
            "gl_name": gl_account["gl_name"] if gl_account else "",
            "expense_type": _clean(fallback_item.get("expense_type")) or "General",
            "is_replacement_reserve": False,
            "confidence": _confidence_or_none(fallback_item.get("confidence")),
            "reason": _clean(fallback_item.get("reason")),
        })
        add_issue(
            "line_item_amounts_missing_total_fallback",
            "Line item amounts were not visible; the invoice total was posted as one review line.",
        )
    if zero_amount_excluded:
        add_issue(
            "zero_amount_line_excluded",
            f"{zero_amount_excluded} zero-dollar line item(s) were excluded from payable ResMan rows.",
        )
    if not normalized_items:
        add_issue(
            "line_items_missing",
            "No payable line items remained after validation. Review this invoice manually.",
            "high",
        )
    if gl_issue_seen:
        add_issue(
            "gl_mapping_required",
            "One or more line items have missing or invalid ResMan GL account codes. Confirm GL mapping.",
        )

    subtotal = _money(payload.get("subtotal"))
    tax_amount = _money(payload.get("tax_amount"))
    shipping_amount = _money(payload.get("shipping_amount"))
    fees_amount = _money(payload.get("fees_amount"))
    line_total = _round_money(sum(i["amount"] for i in normalized_items))
    tax_handling = _tax_handling_policy(payload.get("tax_handling"))
    tax_amount_inferred = False
    if (
        total_amount
        and line_total
        and abs(line_total - total_amount) <= 0.01
        and abs(tax_amount) > 0.009
    ):
        # Some utility/supplier bills expose tax as explicit payable line
        # items while also repeating the tax subtotal in the footer. The
        # ResMan rows already reconcile to the invoice total, so do not add
        # that footer tax a second time during validation.
        tax_amount = 0.0
    invoice_difference = _round_money(total_amount - (line_total + tax_amount + shipping_amount + fees_amount))
    if (
        total_amount
        and line_total
        and invoice_difference > 0.009
        and abs(tax_amount) <= 0.009
        and abs(shipping_amount) <= 0.009
        and abs(fees_amount) <= 0.009
        and tax_handling == "distribute_proportionally"
    ):
        # Supplier screenshots often OCR the footer poorly. If the line table
        # and invoice total are clear, treat the difference as the default
        # distributed tax/difference bucket instead of failing the invoice.
        tax_amount = invoice_difference
        tax_amount_inferred = True
    distributed_reconciliation_applied = False
    reconciled_total = _round_money(line_total + tax_amount + shipping_amount + fees_amount)
    total_reconciliation_passed = bool(total_amount) and abs(reconciled_total - total_amount) <= 0.01
    if (
        not total_reconciliation_passed
        and tax_handling == "distribute_proportionally"
        and total_amount
        and line_total
        and abs(total_amount - line_total) <= max(1.0, abs(total_amount) * 0.03)
    ):
        # OCR occasionally over/under-reads a small amount in scanned tables.
        # The default tax/difference policy later adjusts payable line rows to
        # the invoice total, so this is a controlled reconciliation rather than
        # a blocking extraction failure.
        distributed_reconciliation_applied = True
        reconciled_total = total_amount
        total_reconciliation_passed = True
    if not total_amount:
        add_issue("total_amount_missing", "Invoice total amount is missing.", "high")
    elif not total_reconciliation_passed:
        add_issue(
            "total_reconciliation_failed",
            (
                f"Line items plus tax/shipping/fees total {reconciled_total:.2f}, "
                f"but invoice total is {total_amount:.2f}."
            ),
            "high",
        )

    if abs(tax_amount) > 0 and tax_handling == "manual_review":
        add_issue(
            "tax_handling_requires_review",
            "Sales tax was detected. Confirm whether to distribute tax or map it to a separate GL before export.",
        )
    elif abs(tax_amount) > 0 and tax_handling == "separate_tax_line":
        add_issue(
            "tax_gl_mapping_required",
            "Sales tax is configured as a separate line, but a validated tax GL mapping is required.",
        )

    for warning in warnings:
        code = "ai_warning_" + _flagify(warning)[:60]
        if warning == "ai_input_truncated":
            add_issue(code, "AI input text was truncated before extraction. Review for missing lines.")
        elif warning == "ocr_reference_rescue_used":
            add_issue(
                code,
                "OCR could not reliably read this image, so the backend used local vendor/property/GL references to create a reviewable invoice row.",
            )
        elif warning == "amount_inferred_from_vendor_history":
            add_issue(
                code,
                "Invoice amount was inferred from historical postings because the screenshot total was not machine-readable. Verify the amount before export.",
                "high",
            )
        elif warning == "property_inferred_from_vendor_history":
            add_issue(
                code,
                "Property was inferred from this vendor's historical postings. Confirm the property before export.",
            )
        elif warning == "property_address_detected_in_ocr":
            add_issue(
                code,
                "Property was prefilled from a weak OCR address match. Confirm the property before export.",
            )
        elif warning == "ai_vision_recommended_unreadable_image":
            add_issue(
                code,
                "This image is too degraded for text-only OCR. A vision-capable AI model is recommended for exact fields.",
            )
        elif warning == "ai_vision_failed_text_fallback_used":
            add_issue(
                code,
                "AI vision failed for this file, so the backend fell back to text/OCR extraction. Review the result carefully.",
                "high",
            )
        else:
            add_issue(code, f"AI warning: {warning}")

    required_fields_present = bool(vendor_name and invoice_number and invoice_date and total_amount and normalized_items)
    dates_valid = invoice_date_ok and due_date_ok
    provider_confidence = _confidence_or_none(payload.get("confidence"))
    confidence_source = "provider" if provider_confidence is not None else "backend_derived"
    confidence = provider_confidence
    if confidence is None:
        confidence = _derive_invoice_confidence(
            required_fields_present=required_fields_present,
            line_item_count=len(normalized_items),
            dates_valid=dates_valid,
            total_reconciliation_passed=total_reconciliation_passed,
            issues=issues,
        )
    confidence = _cap_confidence_for_issues(confidence, issues)
    if confidence < 0.70:
        add_issue(
            "ai_confidence_low",
            f"AI extraction confidence is {confidence:.0%}, below the 70% review threshold.",
        )

    normalized_items = [
        {
            **item,
            "confidence": (
                item["confidence"]
                if item["confidence"] is not None
                else _derive_line_item_confidence(
                    parent_confidence=confidence,
                    item=item,
                    total_reconciliation_passed=total_reconciliation_passed,
                    gl_accounts=references["gl_accounts"],
                )
            ),
            "reason": item["reason"]
            or _derive_line_item_reason(
                item=item,
                total_reconciliation_passed=total_reconciliation_passed,
                gl_accounts=references["gl_accounts"],
            ),
        }
        for item in normalized_items
    ]

    review_issues = sorted(issues, key=lambda issue: (issue["severity"], issue["code"]))
    reason_messages = [issue["message"] for issue in review_issues]
    reason_codes = [issue["code"] for issue in review_issues]

    result = {
        "vendor_name": vendor_for_rows,
        "raw_vendor_name": vendor_name,
        "category": _clean(payload.get("category")),
        "invoice_number": invoice_number,
        "source_invoice_number": source_invoice_number,
        "invoice_date": invoice_date,
        "invoice_date_source": invoice_date_source,
        "due_date": due_date,
        "bill_or_credit": _clean(payload.get("bill_or_credit")) or "Bill",
        "account_number": account_number,
        "invoice_number_generated": invoice_number_generated,
        "invoice_number_policy_applied": invoice_number_policy_applied,
        "service_address": service_address,
        "service_period_start": service_period_start,
        "service_period_end": service_period_end,
        "service_period_source": service_period_source,
        "property_candidate": property_candidate,
        "property_abbreviation": property_abbreviation,
        "location": location,
        "property_match": property_match,
        "invoice_description": _clean(payload.get("invoice_description")),
        "composed_invoice_description": "",
        "line_items": normalized_items,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "shipping_amount": shipping_amount,
        "fees_amount": fees_amount,
        "total_amount": total_amount,
        "tax_amount_inferred": tax_amount_inferred,
        "tax_handling": tax_handling,
        "zero_amount_lines_excluded": zero_amount_excluded,
        "confidence": confidence,
        "confidence_source": confidence_source,
        "warnings": warnings,
        "manual_review_reasons": reason_messages,
        "manual_review_codes": reason_codes,
        "manual_review_issues": review_issues,
        "validation_summary": {
            "valid": True,
            "required_fields_present": required_fields_present,
            "line_item_count": len(normalized_items),
            "dates_valid": dates_valid,
            "total_reconciliation_passed": total_reconciliation_passed,
            "reconciled_total": reconciled_total,
            "invoice_total": total_amount,
            "confidence": confidence,
            "confidence_source": confidence_source,
            "invoice_number_generated": invoice_number_generated,
            "distributed_reconciliation_applied": distributed_reconciliation_applied,
            "service_period_start": service_period_start,
            "service_period_end": service_period_end,
            "service_period_source": service_period_source,
        },
    }
    return canonical_rules.canonicalize_normalized_invoice(
        result,
        references=references,
        rules_override=rules_override,
    )


def _repair_ai_payload_from_ocr(
    payload: dict[str, Any],
    document_text: str,
    *,
    source_file: str = "",
) -> dict[str, Any]:
    """Patch recoverable OCR/table misses before validation.

    This is deliberately conservative: it only fills missing totals and
    replaces all-zero line items when the OCR text exposes explicit invoice
    table rows with amounts. It keeps AI/vendor output reviewable while
    preventing a readable supplier screenshot from producing an empty grid.
    """
    if not isinstance(payload, dict) or not (document_text or "").strip():
        return payload
    repaired = dict(payload)
    repaired["_document_text"] = document_text
    repaired["_source_file"] = source_file
    parsed = _extract_supplier_table_from_ocr(document_text)
    parsed_recovered = bool(
        parsed.get("line_items")
        or parsed.get("subtotal")
        or parsed.get("tax_amount")
        or parsed.get("total_amount")
    )
    property_context = _extract_property_context_from_ocr(document_text)
    explicit_invoice_number = _extract_explicit_invoice_number_from_ocr(document_text)
    service_period_start, service_period_end, service_period_source = _extract_service_period_from_text(document_text)

    warnings = _normalize_warnings(repaired.get("warnings") or [])
    did_repair = False
    did_table_repair = False
    if explicit_invoice_number:
        current_invoice_number = _clean(repaired.get("invoice_number"))
        if not current_invoice_number or (
            "lowe" in document_text.lower() and current_invoice_number != explicit_invoice_number
        ):
            repaired["invoice_number"] = explicit_invoice_number
            did_repair = True
    if not _money(repaired.get("total_amount")) and parsed.get("total_amount"):
        repaired["total_amount"] = parsed["total_amount"]
        did_repair = True
        did_table_repair = True
    if not _money(repaired.get("tax_amount")) and parsed.get("tax_amount"):
        repaired["tax_amount"] = parsed["tax_amount"]
        did_repair = True
        did_table_repair = True
    if not _money(repaired.get("subtotal")) and parsed.get("subtotal"):
        repaired["subtotal"] = parsed["subtotal"]
        did_repair = True
        did_table_repair = True
    if (
        not _money(repaired.get("total_amount"))
        and parsed.get("subtotal")
        and not _money(parsed.get("tax_amount"))
    ):
        repaired["total_amount"] = parsed["subtotal"]
        did_repair = True
        did_table_repair = True

    existing_items = repaired.get("line_items")
    existing_list = existing_items if isinstance(existing_items, list) else []
    existing_payable = [item for item in existing_list if isinstance(item, dict) and abs(_money(item.get("amount"))) > 0.009]
    parsed_items = parsed.get("line_items") if isinstance(parsed.get("line_items"), list) else []
    parsed_payable = [item for item in parsed_items if abs(_money(item.get("amount"))) > 0.009]
    if parsed_payable and not existing_payable:
        repaired["line_items"] = parsed_items
        did_repair = True
        did_table_repair = True
    if property_context.get("property_candidate") and not _clean(repaired.get("property_candidate")):
        repaired["property_candidate"] = property_context["property_candidate"]
        did_repair = True
    if property_context.get("service_address") and not _clean(repaired.get("service_address")):
        repaired["service_address"] = property_context["service_address"]
        did_repair = True
    if service_period_start and not _clean(repaired.get("service_period_start")):
        repaired["service_period_start"] = service_period_start
        repaired["service_period_source"] = service_period_source
        did_repair = True
    if service_period_end and not _clean(repaired.get("service_period_end")):
        repaired["service_period_end"] = service_period_end
        repaired["service_period_source"] = service_period_source
        did_repair = True

    repaired, rescued = _rescue_unreadable_invoice_payload(
        repaired,
        document_text,
        source_file=source_file,
    )
    did_repair = did_repair or rescued

    if did_repair:
        warnings = _normalize_warnings(repaired.get("warnings") or warnings)
        if parsed_recovered and did_table_repair:
            warnings.append("OCR table fallback recovered invoice totals or line amounts from the screenshot.")
        repaired["warnings"] = warnings
        provider_confidence = _confidence_or_none(repaired.get("confidence"))
        if (
            (parsed_recovered or explicit_invoice_number)
            and (provider_confidence is None or provider_confidence < 0.72)
        ):
            repaired["confidence"] = 0.72
    return repaired


def _append_review_issue(
    normalized: dict[str, Any],
    *,
    code: str,
    message: str,
    severity: str = "medium",
) -> None:
    """Attach a post-validation review issue to a normalized AI invoice."""
    clean_code = str(code or "").strip()
    clean_message = str(message or "").strip()
    if not clean_code or not clean_message:
        return
    reasons = list(normalized.get("manual_review_reasons") or [])
    codes = list(normalized.get("manual_review_codes") or [])
    issues = list(normalized.get("manual_review_issues") or [])
    if clean_code not in codes:
        codes.append(clean_code)
    if clean_message not in reasons:
        reasons.append(clean_message)
    if not any((issue or {}).get("code") == clean_code for issue in issues if isinstance(issue, dict)):
        issues.append({
            "code": clean_code,
            "message": clean_message,
            "severity": severity,
        })
    normalized["manual_review_reasons"] = reasons
    normalized["manual_review_codes"] = codes
    normalized["manual_review_issues"] = issues


def _rescue_unreadable_invoice_payload(
    payload: dict[str, Any],
    document_text: str,
    *,
    source_file: str = "",
) -> tuple[dict[str, Any], bool]:
    """Create a reviewable invoice from references when OCR/AI is unreadable.

    Extremely degraded screenshots can still expose enough vendor/address shape
    for a human to recognize the bill, while text-only OCR returns almost no
    usable fields. In that situation the worst UX is an empty template. This
    rescue path only uses validated local references and historical postings,
    keeps the invoice flagged for review, and never marks inferred values as
    export-ready evidence.
    """
    if not _needs_reference_rescue(payload, document_text):
        return payload, False

    vendor_match = _fuzzy_vendor_rule_from_text(document_text, source_file=source_file)
    if not vendor_match:
        return payload, False

    vendor_row, score, reason = vendor_match
    vendor_name = _clean(vendor_row.get("vendor_name"))
    if not vendor_name:
        return payload, False

    repaired = dict(payload)
    warnings = _normalize_warnings(repaired.get("warnings") or [])
    did_repair = False

    if not _clean(repaired.get("vendor_name")):
        repaired["vendor_name"] = vendor_name
        did_repair = True
    if not _clean(repaired.get("invoice_description")):
        repaired["invoice_description"] = _vendor_default_gl_description(vendor_name) or _vendor_rule_category(vendor_name) or "Invoice"
        did_repair = True

    property_abbr, property_reason = _historical_property_for_vendor(
        vendor_name=vendor_name,
        vendor_row=vendor_row,
        document_text=document_text,
    )
    if property_abbr and not _clean(repaired.get("property_abbreviation")):
        repaired["property_abbreviation"] = property_abbr
        did_repair = True
    if property_abbr and not _clean(repaired.get("property_candidate")):
        repaired["property_candidate"] = property_abbr
        did_repair = True

    total_amount = _money(repaired.get("total_amount"))
    amount_source = ""
    if not total_amount:
        amount_candidate = _historical_amount_for_vendor(
            vendor_name=vendor_name,
            property_abbreviation=property_abbr,
        )
        if amount_candidate:
            total_amount = amount_candidate
            repaired["subtotal"] = amount_candidate
            repaired["total_amount"] = amount_candidate
            repaired["tax_amount"] = 0
            amount_source = "vendor_history"
            did_repair = True

    default_gl = _vendor_rule_default_gl(vendor_name) or _vendor_category_default_gl(vendor_name)
    existing_items = repaired.get("line_items")
    existing_list = existing_items if isinstance(existing_items, list) else []
    existing_payable = [
        item for item in existing_list
        if isinstance(item, dict) and abs(_money(item.get("amount"))) > 0.009
    ]
    if total_amount and not existing_payable:
        repaired["line_items"] = [{
            "description": _vendor_default_gl_description(vendor_name) or "Invoice total",
            "quantity": 1,
            "unit_price": total_amount,
            "amount": total_amount,
            "gl_account_candidate": default_gl["gl_code"] if default_gl else "",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": min(0.68, max(0.55, score)),
            "reason": (
                "Reference rescue: OCR/AI could not read payable lines; "
                "vendor history supplied the review line."
            ),
        }]
        did_repair = True

    if did_repair:
        if "ocr_reference_rescue_used" not in warnings:
            warnings.append("ocr_reference_rescue_used")
        if amount_source == "vendor_history" and "amount_inferred_from_vendor_history" not in warnings:
            warnings.append("amount_inferred_from_vendor_history")
        if property_reason and property_reason not in warnings:
            warnings.append(property_reason)
        if "ai_vision_recommended_unreadable_image" not in warnings:
            warnings.append("ai_vision_recommended_unreadable_image")
        repaired["warnings"] = warnings
        repaired["confidence"] = min(
            0.68,
            max(
                _confidence_or_none(repaired.get("confidence")) or 0.0,
                score,
                0.55,
            ),
        )
        repaired.setdefault("mapping_provenance", [])
        if isinstance(repaired["mapping_provenance"], list):
            repaired["mapping_provenance"].append({
                "field": "vendor_name",
                "value": vendor_name,
                "source": "ocr_reference_rescue",
                "confidence": round(score, 2),
                "reason": reason,
            })
            if property_abbr:
                repaired["mapping_provenance"].append({
                    "field": "property_abbreviation",
                    "value": property_abbr,
                    "source": "vendor_history",
                    "confidence": 0.70,
                    "reason": property_reason or "Historical vendor/property fallback.",
                })
            if default_gl:
                repaired["mapping_provenance"].append({
                    "field": "gl_account",
                    "value": default_gl["gl_code"],
                    "source": "vendor_history",
                    "confidence": 0.90,
                    "reason": default_gl.get("gl_name") or "Vendor default GL.",
                })
    return repaired, did_repair


def _needs_reference_rescue(payload: dict[str, Any], document_text: str) -> bool:
    warnings = " ".join(_normalize_warnings(payload.get("warnings") or [])).lower()
    line_items = payload.get("line_items")
    item_list = line_items if isinstance(line_items, list) else []
    payable_items = [
        item for item in item_list
        if isinstance(item, dict) and abs(_money(item.get("amount"))) > 0.009
    ]
    core_empty = (
        not _clean(payload.get("vendor_name"))
        and not _money(payload.get("total_amount"))
        and not payable_items
    )
    if core_empty:
        return True
    if not _clean(payload.get("vendor_name")) and not payable_items:
        return True
    if "garbled" in warnings and (not payable_items or not _clean(payload.get("vendor_name"))):
        return True
    return _ocr_quality_score(document_text) < 0.18 and not payable_items


def _ocr_quality_score(document_text: str) -> float:
    text = _normalize_key(document_text)
    if not text:
        return 0.0
    tokens = [token for token in text.split() if len(token) >= 3]
    if not tokens:
        return 0.0
    useful_terms = {
        "invoice", "vendor", "total", "amount", "date", "service", "account",
        "address", "description", "quantity", "balance", "due", "tax",
    }
    useful_hits = sum(1 for token in tokens if token in useful_terms)
    digit_hits = sum(1 for token in tokens if any(ch.isdigit() for ch in token))
    dictionaryish = sum(1 for token in tokens if re.fullmatch(r"[a-z]{4,}", token))
    return min(1.0, (useful_hits * 0.10) + (digit_hits * 0.025) + (dictionaryish / max(len(tokens), 1) * 0.30))


_GENERIC_VENDOR_TOKENS = {
    "the", "and", "inc", "llc", "corp", "corporation", "company", "co",
    "services", "service", "supply", "supplies", "maintenance", "management",
    "apartments", "properties", "property", "group", "department",
    "pest", "control", "tree", "page", "cash", "high", "serv",
    "screen", "screens", "screenshot",
}


def _fuzzy_vendor_rule_from_text(
    document_text: str,
    *,
    source_file: str = "",
) -> tuple[dict[str, Any], float, str] | None:
    norm_text = _normalize_key(f"{document_text} {source_file}")
    tokens = [token for token in norm_text.split() if len(token) >= 4]
    token_set = set(tokens)
    if not tokens:
        return None

    best: tuple[dict[str, Any], float, str] | None = None
    for row in _vendor_rule_rows():
        labels = _vendor_rule_labels(row)
        row_best_score = 0.0
        row_best_base_score = 0.0
        row_reason = ""
        for label in labels:
            label_norm = _normalize_key(label)
            if not label_norm:
                continue
            original_label_tokens = [token for token in label_norm.split() if len(token) >= 3]
            label_tokens = [
                token for token in label_norm.split()
                if len(token) >= 4 and token not in _GENERIC_VENDOR_TOKENS
            ]
            if not label_tokens:
                continue
            phrase_present = label_norm in norm_text
            if len(original_label_tokens) > 1 and len(label_tokens) < 2 and not phrase_present:
                continue
            exact_hits = sum(1 for label_token in label_tokens if label_token in token_set)
            if len(label_tokens) > 1 and not phrase_present:
                continue
            if len(label_tokens) == 1 and exact_hits < 1:
                continue
            token_scores: list[float] = []
            for label_token in label_tokens[:4]:
                if label_token in token_set:
                    token_scores.append(1.0)
                    continue
                token_scores.append(max(
                    difflib.SequenceMatcher(None, label_token, token).ratio()
                    for token in tokens
                ))
            if not token_scores:
                continue
            base_score = max(token_scores)
            if len(label_tokens) > 1:
                base_score = sum(token_scores) / len(token_scores)
            score = base_score
            if row.get("default_gl_code"):
                score += 0.08
            if _split_vendor_rule_list(row.get("source_properties_observed")):
                score += 0.04
            score = min(1.0, score)
            if score > row_best_score:
                row_best_score = score
                row_best_base_score = base_score
                row_reason = f"OCR fuzzy vendor match against '{label}'."
        if row_best_score < 0.78 or row_best_base_score < 0.84:
            continue
        if best is None or row_best_score > best[1]:
            best = (row, row_best_score, row_reason)
    return best


def _vendor_rule_labels(row: dict[str, Any]) -> list[str]:
    labels = [
        _clean(row.get("vendor_name")),
        _clean(row.get("normalized_vendor_key")).replace("_", " "),
    ]
    labels.extend(_split_vendor_rule_list(row.get("aliases")))
    labels.extend(_split_vendor_rule_list(row.get("detection_keywords")))
    return [label for label in labels if label]


def _split_vendor_rule_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    if not text:
        return []
    return [_clean(part) for part in re.split(r"[|,]", text) if _clean(part)]


def _historical_property_for_vendor(
    *,
    vendor_name: str,
    vendor_row: dict[str, Any],
    document_text: str,
) -> tuple[str, str]:
    observed = _split_vendor_rule_list(vendor_row.get("source_properties_observed"))
    if not observed:
        observed = _historical_properties_for_vendor(vendor_name)
    if not observed:
        return "", ""

    references = load_references()
    scored: list[tuple[float, str, str]] = []
    for abbr in observed:
        score, reason = _property_text_score(abbr, document_text, references["properties"])
        scored.append((score, abbr, reason))
    scored.sort(reverse=True, key=lambda item: item[0])
    if scored and scored[0][0] >= 0.35:
        return scored[0][1], scored[0][2]

    top = _historical_top_property_for_vendor(vendor_name)
    if top:
        return top, "property_inferred_from_vendor_history"
    if len(observed) == 1:
        return observed[0], "property_inferred_from_vendor_history"
    return "", ""


def _required_property_fallback(
    *,
    vendor_name: str,
    property_candidate: str,
    service_address: str,
    document_text: str,
) -> tuple[str, str]:
    """Prefill a required property from validated local history when possible.

    ResMan export requires Property Abbreviation. For AI-assisted invoices we
    still avoid writing raw AI text, but we can use local vendor rules and GL
    history to prefill a reviewable property when the service address or
    property name clearly points at one of the vendor's known properties.
    """
    vendor_row = _vendor_rule_for_name(vendor_name)
    if not vendor_row:
        return "", ""
    for evidence in (service_address, property_candidate, document_text):
        if not _clean(evidence):
            continue
        prop, reason = _historical_property_for_vendor(
            vendor_name=vendor_name,
            vendor_row=vendor_row,
            document_text=evidence,
        )
        if prop:
            return prop, reason or "property_inferred_from_vendor_history"
    return "", ""


def _property_text_score(
    property_abbreviation: str,
    document_text: str,
    properties: list[dict[str, Any]],
) -> tuple[float, str]:
    needle = _normalize_key(document_text)
    abbr_key = _normalize_key(property_abbreviation)
    score = 0.0
    reason = ""
    if abbr_key and abbr_key in needle:
        score += 0.75
        reason = "property_abbreviation_detected_in_ocr"

    for prop in properties:
        abbr = _clean(prop.get("Property Abbreviation") or prop.get("property_abbreviation"))
        if _normalize_key(abbr) != abbr_key:
            continue
        prop_name = _normalize_key(prop.get("Property Name") or prop.get("property_name"))
        address = _normalize_key(prop.get("Address") or prop.get("address") or prop.get("Service Address"))
        city = _normalize_key(prop.get("City") or prop.get("city"))
        if prop_name:
            name_tokens = [token for token in prop_name.split() if token not in _GENERIC_VENDOR_TOKENS]
            hits = sum(1 for token in name_tokens if token in needle)
            if hits:
                candidate_score = min(0.85, hits / max(len(name_tokens), 1))
                if candidate_score > score:
                    score = candidate_score
                    reason = "property_name_detected_in_ocr"
        if address:
            address_tokens = [
                token for token in address.split()
                if len(token) >= 4 and token not in {"street", "avenue", "drive", "road"}
            ]
            hits = sum(1 for token in address_tokens if token in needle)
            if hits:
                candidate_score = min(0.90, 0.25 + hits / max(len(address_tokens), 1))
                if candidate_score > score:
                    score = candidate_score
                    reason = "property_address_detected_in_ocr"
        if city and city in needle and score < 0.45:
            score = 0.45
            reason = "property_city_detected_in_ocr"
    return score, reason


@lru_cache(maxsize=1)
def _vendor_expense_history_rows() -> tuple[dict[str, Any], ...]:
    path = settings.PROJECT_ROOT / "Gl Codes" / "General Ledger Report.csv"
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return tuple(rows)
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                for row in csv.DictReader(fh):
                    account = _clean(row.get("GL_Account"))
                    code_match = re.match(r"^(\d{3,6})\b", account)
                    if not code_match:
                        continue
                    account_type = _clean(row.get("Gl Accounts.Type") or row.get("Type")).lower()
                    if account_type and account_type != "expense":
                        continue
                    vendor = _clean(row.get("Vendor"))
                    prop = _clean(row.get("Property"))
                    amount = _money(row.get("Debit")) or abs(_money(row.get("Net Amount")))
                    if not vendor or not prop or amount <= 0:
                        continue
                    rows.append({
                        "vendor_name": vendor,
                        "property_abbreviation": prop,
                        "gl_code": code_match.group(1),
                        "gl_name": account[code_match.end():].strip(" -"),
                        "amount": amount,
                        "date": _clean(row.get("Date")),
                        "description": _clean(row.get("Description")),
                    })
            return tuple(rows)
        except (OSError, UnicodeDecodeError):
            rows = []
            continue
    return tuple(rows)


def _historical_rows_for_vendor(vendor_name: str) -> list[dict[str, Any]]:
    target = ai_mapping_review.mapping_key(vendor_name)
    if not target:
        return []
    return [
        row for row in _vendor_expense_history_rows()
        if ai_mapping_review.mapping_key(row.get("vendor_name")) == target
    ]


def _historical_properties_for_vendor(vendor_name: str) -> list[str]:
    rows = _historical_rows_for_vendor(vendor_name)
    counts = Counter(_clean(row.get("property_abbreviation")) for row in rows)
    return [prop for prop, _count in counts.most_common() if prop]


def _historical_top_property_for_vendor(vendor_name: str) -> str:
    properties = _historical_properties_for_vendor(vendor_name)
    return properties[0] if properties else ""


def _historical_amount_for_vendor(
    *,
    vendor_name: str,
    property_abbreviation: str = "",
) -> float:
    rows = _historical_rows_for_vendor(vendor_name)
    if property_abbreviation:
        rows = [
            row for row in rows
            if _normalize_key(row.get("property_abbreviation")) == _normalize_key(property_abbreviation)
        ]
    amounts = [_money(row.get("amount")) for row in rows if _money(row.get("amount")) > 0]
    if not amounts:
        return 0.0
    counts = Counter(amounts)
    amount, count = counts.most_common(1)[0]
    if count >= 2:
        return amount
    if len(amounts) == 1:
        return amount
    return 0.0


def _build_total_fallback_line_item(
    *,
    payload: dict[str, Any],
    skipped_items: list[dict[str, Any]],
    total_amount: float,
    vendor_name: str,
) -> dict[str, Any]:
    """Build one payable review line when the invoice only exposes a total.

    Some supplier invoices show quantities/items but no line-level dollars
    (Rasa Floors is one example). The ResMan grid still needs a payable row;
    this fallback keeps the invoice visible and reviewable instead of silently
    producing an empty template.
    """
    item_descriptions: list[str] = []
    for item in skipped_items:
        desc = _clean(item.get("description"))
        if not desc:
            continue
        lower = desc.lower()
        if lower in {"invoice total", "total", "miscellaneous", "general"}:
            continue
        if "zero" in lower and "dollar" in lower:
            continue
        item_descriptions.append(desc)
    gl_default_description = _vendor_default_gl_description(vendor_name)
    invoice_description = _clean(payload.get("invoice_description"))
    description_parts = [
        part for part in (
            invoice_description if invoice_description.lower() not in {
                "invoice total",
                "general invoice",
                "miscellaneous",
            } else "",
            gl_default_description,
            " / ".join(_unique_strings(item_descriptions)[:4]),
        )
        if part
    ]
    description = " - ".join(_unique_strings(description_parts))[:180] or "Invoice total"
    return {
        "description": description,
        "quantity": 1.0,
        "unit_price": total_amount,
        "amount": total_amount,
        "gl_account_candidate": "",
        "expense_type": "General",
        "is_replacement_reserve": False,
        "confidence": payload.get("confidence") or 0.70,
        "reason": "Invoice total fallback: source line items did not expose payable line amounts.",
    }


def _extract_property_context_from_ocr(document_text: str) -> dict[str, str]:
    """Recover property/address hints from common invoice address blocks."""
    lines = [_clean(line) for line in (document_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return {}

    labels = ("install at", "ship to", "service address", "service at")
    block: list[str] = []
    for idx, line in enumerate(lines):
        lower = line.lower()
        if not any(label in lower for label in labels):
            continue
        after_label = re.sub(
            r"^.*?(?:install\s+at|ship\s+to|service\s+address|service\s+at)\s*[:;]?",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        if after_label:
            block.append(after_label)
        block.extend(lines[idx + 1: idx + 8])
        break
    if not block:
        return {}

    property_parts: list[str] = []
    address_parts: list[str] = []
    for raw_line in block:
        line = _clean(raw_line)
        if not line:
            continue
        if _is_context_stop_line(line):
            break
        line = _clean_ocr_address_line(line)
        if not line:
            continue
        if address_parts and re.match(r"^(?:apt|unit|suite|ste|#)\b", line, re.IGNORECASE):
            address_parts.append(line)
        elif _looks_like_city_state_line(line):
            address_parts.append(line)
        elif _looks_like_address_line(line):
            before, address = _split_address_prefix(line)
            if before and not address_parts:
                property_parts.append(before)
            address_parts.append(address or line)
        elif not address_parts:
            property_parts.append(line)

    property_candidate = _clean(" ".join(property_parts))
    service_address = _clean(" ".join(address_parts))
    if property_candidate:
        property_candidate = re.sub(
            r"\b(?:sold\s+to|install\s+at|ship\s+to|bill\s+to|pdf\s+invoice)\b[:;]?",
            " ",
            property_candidate,
            flags=re.IGNORECASE,
        )
        property_candidate = _clean(re.sub(r"\s+", " ", property_candidate))
    return {
        "property_candidate": property_candidate,
        "service_address": service_address,
    }


def _is_context_stop_line(line: str) -> bool:
    return bool(re.search(
        r"\b(invoice\s+date|invoice\s+number|order\s+date|install\s+date|unit\s+#|"
        r"telephone|po\s+number|style/item|style\s+item|please\s+remit|sales\s+representative)\b",
        line,
        re.IGNORECASE,
    ))


def _clean_ocr_address_line(line: str) -> str:
    line = re.sub(r"^[^\w#]+", "", line)
    line = re.sub(r"^(?:pdf\s+invoice|invoice)\s+", "", line, flags=re.IGNORECASE)
    line = line.replace("#8", "#B")
    return _clean(line)


def _looks_like_address_line(line: str) -> bool:
    if _looks_like_city_state_line(line):
        return True
    return bool(re.search(
        r"(?:\d{2,}|\bapt\b|\bunit\b|#\w+).*\b(?:st|street|ave|avenue|dr|drive|rd|road|"
        r"blvd|boulevard|ct|court|ln|lane|way|pl|place|pkwy|parkway)\b",
        line,
        re.IGNORECASE,
    ))


def _looks_like_city_state_line(line: str) -> bool:
    return bool(re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", line.upper()))


def _split_address_prefix(line: str) -> tuple[str, str]:
    match = re.search(r"\b\d{2,}[\w-]*\b", line)
    if not match or match.start() == 0:
        return "", line
    return _clean(line[:match.start()]), _clean(line[match.start():])


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _clean(value)
        if not clean:
            continue
        key = _normalize_key(clean)
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _extract_supplier_table_from_ocr(document_text: str) -> dict[str, Any]:
    lines = [_clean(line) for line in (document_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return {}

    parsed_items: list[dict[str, Any]] = []
    sku_pattern = re.compile(
        r"^(?P<sku>[A-Z]?-?\d{3,}[A-Z0-9-]*)\s+"
        r"(?:(?P<unit>\d{1,4}(?:,\d{3})*\.\d{2})\s+)?"
        r"(?P<amount>\d{1,4}(?:,\d{3})*\.\d{2})[)\]\|}]*$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        match = sku_pattern.match(line)
        if not match:
            continue
        sku = match.group("sku")
        unit_price = _money(match.group("unit") or match.group("amount"))
        amount = _money(match.group("amount"))
        description = ""
        gl_candidate = ""
        for lookahead in lines[idx + 1: idx + 8]:
            normalized = lookahead.lower()
            if sku_pattern.match(lookahead):
                break
            if normalized.startswith("gl code"):
                gl_candidate = lookahead.split(":", 1)[-1].strip() if ":" in lookahead else lookahead
                continue
            if (
                not description
                and len(lookahead) > 2
                and not re.fullmatch(r"[a-zA-Z]", lookahead)
                and "total" not in normalized
                and "invoice" not in normalized
                and "sales" not in normalized
            ):
                description = lookahead
        quantity = None
        if unit_price and amount:
            ratio = amount / unit_price
            rounded = round(ratio)
            if abs(ratio - rounded) <= 0.05:
                quantity = float(rounded)
        parsed_items.append({
            "description": description or sku,
            "quantity": quantity,
            "unit_price": unit_price if unit_price else None,
            "amount": amount,
            "gl_account_candidate": gl_candidate,
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": 0.72 if amount else 0.50,
            "reason": "Recovered from local OCR table fallback.",
        })

    subtotal = _extract_money_after_label(
        lines,
        (
            r"(?:lines?\s+total|qty\s+shipped\s+total|total\s+merchandise|merchandise)\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
            r"\bTot(?:al)?\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
        ),
    )
    tax = _extract_money_after_label(
        lines,
        (
            r"(?:sales\s*tax|salesta|lar\s*sales\s*tax)\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
        ),
    )
    total = _extract_money_after_label(
        lines,
        (
            r"(?:invoice\s+tot(?:al|s)?|amount\s+due)\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
        ),
    )
    payable_total = _round_money(sum(_money(item.get("amount")) for item in parsed_items))
    if not subtotal and payable_total:
        subtotal = payable_total
    if not total and subtotal and tax:
        total = _round_money(subtotal + tax)
    if not tax and total and subtotal and total > subtotal:
        tax = _round_money(total - subtotal)
    return {
        "line_items": parsed_items,
        "subtotal": subtotal,
        "tax_amount": tax,
        "total_amount": total,
    }


def _extract_explicit_invoice_number_from_ocr(document_text: str) -> str:
    text = document_text or ""
    patterns = (
        r"lowe['’]?\s*s?\s+invoice\s+number[:\s|#]+([A-Z0-9-]{3,})",
        r"\binvoice\s+number[:\s|#]+([A-Z0-9-]{3,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _clean(match.group(1)).strip("[]()|.,;:")
    return ""


def _resolve_service_period(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return normalized service/billing period start, end, and source."""
    pairs = (
        ("service_period_start", "service_period_end", "ai"),
        ("service_start_date", "service_end_date", "ai"),
        ("billing_period_start", "billing_period_end", "ai"),
        ("period_start", "period_end", "ai"),
    )
    for start_key, end_key, source in pairs:
        start, start_ok = _normalize_date(payload.get(start_key))
        end, end_ok = _normalize_date(payload.get(end_key))
        if start and end and start_ok and end_ok:
            return start, end, source

    for field in ("service_period", "billing_period", "period", "service_dates", "date_range"):
        start, end, source = _extract_service_period_from_text(_clean(payload.get(field)))
        if start and end:
            return start, end, f"ai_{field}"

    return _extract_service_period_from_text(str(payload.get("_document_text") or ""))


def _derive_required_invoice_number(
    payload: dict[str, Any],
    *,
    invoice_date: str,
    total_amount: float,
    service_period_start: str = "",
    service_period_end: str = "",
) -> str:
    """Generate a stable non-empty bill number when a vendor has no invoice #.

    ResMan requires Number, but utility-style bills often expose only account
    or statement context. The generated value is deterministic and reviewable;
    it is never presented as a vendor-confirmed invoice number.
    """
    explicit = _extract_explicit_invoice_number_from_ocr(str(payload.get("_document_text") or ""))
    if explicit:
        return _sanitize_invoice_number(explicit)

    configured = invoice_format_rules.generate_required_invoice_number(
        payload,
        invoice_date=invoice_date,
        total_amount=total_amount,
        service_period_start=service_period_start,
        service_period_end=service_period_end,
    )
    if configured:
        return configured

    account = _clean(payload.get("account_number"))
    if not account:
        account = _extract_account_number_from_text(str(payload.get("_document_text") or ""))

    date_label = _invoice_number_date_token(invoice_date)
    if account:
        return _sanitize_invoice_number(f"BILL-{account}-{date_label}")

    source_file = _clean(payload.get("_source_file"))
    seed = "|".join(
        [
            _clean(payload.get("vendor_name")),
            _clean(payload.get("service_address")),
            date_label,
            f"{_money(total_amount):.2f}",
            source_file,
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:8].upper()
    return _sanitize_invoice_number(f"BILL-{date_label}-{digest}")


def _resolve_account_number(payload: dict[str, Any]) -> str:
    """Return the best account number, preserving vendor prefixes from OCR.

    AI providers often normalize utility account numbers to digits only even
    when the bill visibly uses a leading account-family letter (for example
    EPB's `C10181446`). Formats rules depend on the account number token, so
    reconcile the provider value with OCR candidates before rendering.
    """
    extracted = _clean(payload.get("account_number"))
    ocr_best = _extract_account_number_from_text(str(payload.get("_document_text") or ""))
    if not extracted:
        return ocr_best
    if not ocr_best:
        return extracted

    extracted_key = _account_compare_key(extracted)
    ocr_key = _account_compare_key(ocr_best)
    if ocr_key == extracted_key:
        return ocr_best
    if ocr_key.endswith(extracted_key) and re.search(r"[A-Za-z]", ocr_best):
        return ocr_best
    if extracted_key and extracted_key in ocr_key and re.search(r"[A-Za-z]", ocr_best):
        return ocr_best
    return extracted


def _sanitize_invoice_number(value: str) -> str:
    clean = _clean(value)
    clean = re.sub(r"[\x00-\x1f\x7f]+", "", clean)
    clean = clean.strip(" ._-")
    return clean[:40]


def _invoice_number_date_token(value: str) -> str:
    normalized, ok = _normalize_date(value)
    if normalized and ok:
        return datetime.strptime(normalized, "%m/%d/%Y").strftime("%Y%m%d")
    return datetime.now().strftime("%Y%m%d")


def _extract_account_number_from_text(text: str) -> str:
    patterns: tuple[tuple[str, int], ...] = (
        (r"\bACCOUNT=([A-Z0-9._-]{3,})=ACCOUNT\b", 115),
        (r"\baccount\s+(?:number|no|#)?[:\s]+([A-Z][A-Z0-9._-]{5,})", 105),
        (r"\baccount\s+(?:number|no|#)?[:\s]+([0-9][A-Z0-9._-]{5,})", 80),
        (r"\bcustomer\s+(?:number|no|#)?[:\s]+([A-Z0-9._-]{3,})", 70),
        # Payment stubs often show account + due date + amount without a label.
        (r"\b([A-Z]\d{6,12})\s+[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\s+\$?\d", 95),
        (r"\b([A-Z]\d{6,12})\b", 55),
    )
    candidates: list[tuple[int, str]] = []
    for pattern, base_score in patterns:
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            raw = _clean(match.group(1)).strip("[]()|.,;:")
            candidate = _clean_account_candidate(raw)
            if not candidate:
                continue
            score = base_score
            if re.search(r"[A-Za-z]", candidate):
                score += 12
            if len(candidate) > 14:
                score -= len(candidate) - 14
            candidates.append((score, candidate))
    if not candidates:
        return ""
    candidates.sort(key=lambda part: (part[0], -len(part[1])), reverse=True)
    best = candidates[0][1]
    # Prefer a shorter repeated candidate when a high-score OCR label grabbed
    # one stray trailing digit but the exact account also appears elsewhere.
    for _, candidate in candidates[1:]:
        if (
            re.match(r"^[A-Z]\d{6,}$", best)
            and re.match(r"^[A-Z]\d{6,}$", candidate)
            and best.startswith(candidate)
            and 0 < len(best) - len(candidate) <= 2
        ):
            return candidate
    return best


def _clean_account_candidate(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "", _clean(value)).strip("._-")
    if not candidate:
        return ""
    if sum(ch.isdigit() for ch in candidate) < 3:
        return ""
    # OCR commonly reads zero as O inside account numbers. Keep legitimate
    # leading account-family letters, normalize the rest where digits dominate.
    if re.match(r"^[A-Za-z][A-Za-z0-9._-]+$", candidate):
        prefix = candidate[0].upper()
        body = candidate[1:].replace("O", "0").replace("o", "0")
        candidate = prefix + body
    elif sum(ch.isdigit() for ch in candidate) >= max(3, len(candidate) - 2):
        candidate = candidate.replace("O", "0").replace("o", "0")
    return candidate[:40]


def _account_compare_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", _clean(value)).upper().replace("O", "0")


def _extract_service_period_from_text(text: str) -> tuple[str, str, str]:
    if not text:
        return "", "", ""
    compact_text = re.sub(r"\s+", " ", text)
    date = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    patterns = (
        rf"\b({date})\s*(?:to|through|thru|[-–—])\s*({date})\s*(?:=|\(|\b)",
        rf"\b(?:service|billing|bill)\s+period[:\s]+({date})\s*(?:to|through|thru|[-–—])\s*({date})",
        rf"\b(?:from|service\s+from)[:\s]+({date})\s*(?:to|through|thru|[-–—])\s*({date})",
    )
    for pattern in patterns:
        match = re.search(pattern, compact_text, re.IGNORECASE)
        if not match:
            continue
        start, start_ok = _normalize_date(match.group(1))
        end, end_ok = _normalize_date(match.group(2))
        if start and end and start_ok and end_ok:
            return start, end, "ocr_service_period"
    return "", "", ""


def _extract_money_after_label(lines: list[str], patterns: tuple[str, ...]) -> float:
    for line in lines:
        cleaned = line.replace("§", "5")
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                return _money(match.group(1))
    return 0.0


def merge_text_and_vision_results(
    text_normalized: dict[str, Any] | None,
    vision_normalized: dict[str, Any],
) -> dict[str, Any]:
    """Merge text and vision candidates without blindly overwriting.

    Text extraction remains primary when validation already confirmed it.
    Vision boosts confidence when it agrees and adds manual-review flags when
    important fields conflict.
    """
    if not text_normalized:
        merged = dict(vision_normalized)
        summary = dict(merged.get("validation_summary") or {})
        summary["vision_used"] = True
        summary["text_vision_agreement_fields"] = []
        summary["text_vision_conflict_fields"] = []
        merged["validation_summary"] = summary
        return merged

    merged = dict(text_normalized)
    reasons = list(merged.get("manual_review_reasons") or [])
    codes = list(merged.get("manual_review_codes") or [])
    issues = list(merged.get("manual_review_issues") or [])
    agreements: list[str] = []
    conflicts: list[str] = []

    for field in ("vendor_name", "invoice_number", "invoice_date", "due_date", "total_amount"):
        text_value = text_normalized.get(field)
        vision_value = vision_normalized.get(field)
        if _blank(text_value) or _blank(vision_value):
            if _blank(text_value) and not _blank(vision_value):
                merged[field] = vision_value
            continue
        if _values_agree(text_value, vision_value, money_field=field == "total_amount"):
            agreements.append(field)
        else:
            conflicts.append(field)

    if agreements and not conflicts:
        merged["confidence"] = max(
            float(merged.get("confidence") or 0),
            float(vision_normalized.get("confidence") or 0),
            0.90,
        )
    if conflicts:
        code = "ai_text_vision_conflict"
        if code not in codes:
            codes.append(code)
            message = (
                "Text extraction and vision assist disagreed on: "
                + ", ".join(field.replace("_", " ") for field in conflicts)
                + ". Review before export."
            )
            reasons.append(message)
            issues.append({"code": code, "message": message, "severity": "high"})

    merged["vision_candidates"] = list(vision_normalized.get("vision_candidates") or [])
    summary = dict(merged.get("validation_summary") or {})
    summary["vision_used"] = True
    summary["text_vision_agreement_fields"] = agreements
    summary["text_vision_conflict_fields"] = conflicts
    summary["confidence"] = merged.get("confidence")
    merged["validation_summary"] = summary
    merged["manual_review_reasons"] = reasons
    merged["manual_review_codes"] = codes
    merged["manual_review_issues"] = issues
    return merged


def ai_result_to_invoice(
    normalized: dict[str, Any],
    *,
    batch_id: str,
    source_file: str,
    vendor_key: str,
    support_document_url: str = "",
    support_document_status: str = "",
    support_document_dropbox_path: str = "",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    items = list(normalized.get("line_items") or [])
    adders = [
        ("Sales tax", normalized.get("tax_amount", 0)),
        ("Shipping", normalized.get("shipping_amount", 0)),
        ("Fees", normalized.get("fees_amount", 0)),
    ]
    for label, amount in adders:
        if abs(float(amount or 0)) > 0 and normalized.get("tax_handling") == "separate_tax_line":
            items.append({
                "description": label,
                "amount": _money(amount),
                "quantity": None,
                "unit_price": None,
                "gl_account_candidate": "",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": normalized.get("confidence", 0),
                "reason": "Synthetic line generated to reconcile invoice total.",
            })
    if normalized.get("tax_handling") == "distribute_proportionally":
        items = _distribute_invoice_difference(items, normalized)

    for idx, item in enumerate(items, start=1):
        confidence = _float(item.get("confidence"), normalized.get("confidence", 0.0))
        review_reasons = list(normalized.get("manual_review_reasons") or [])
        validation_codes = list(normalized.get("manual_review_codes") or [])
        if confidence and confidence < 0.70:
            if "AI extraction confidence is below the review threshold." not in review_reasons:
                review_reasons.append("AI extraction confidence is below the review threshold.")
            if "ai_confidence_low" not in validation_codes:
                validation_codes.append("ai_confidence_low")
        if not support_document_url:
            document_message = "Document Url is required by Canonical Rules before export. Upload/link the source document."
            if document_message not in review_reasons:
                review_reasons.append(document_message)
            if "required_document_url" not in validation_codes:
                validation_codes.append("required_document_url")
        invoice_description = _compose_invoice_description(normalized, item)
        line_item_description = _compose_line_item_description(normalized, item)
        row = {
            "Invoice Number": normalized.get("invoice_number"),
            "Bill or Credit": normalized.get("bill_or_credit") or "Bill",
            "Invoice Date": normalized.get("invoice_date"),
            "Accounting Date": normalized.get("invoice_date"),
            "Vendor": normalized.get("vendor_name"),
            "Invoice Description": invoice_description,
            "Line Item Number": idx,
            "Property Abbreviation": normalized.get("property_abbreviation"),
            "Location": normalized.get("location"),
            "GL Account": item.get("gl_account_candidate"),
            "Line Item Description": line_item_description,
            "Amount": _money(item.get("amount")),
            "Expense Type": item.get("expense_type") or "General",
            "Is Replacement Reserve": bool(item.get("is_replacement_reserve")),
            "Due Date": normalized.get("due_date"),
            "Quantity": item.get("quantity") if item.get("quantity") is not None else 1,
            "Unit Price": item.get("unit_price") if item.get("unit_price") is not None else _money(item.get("amount")),
            "Tax": False,
            "Document Url": support_document_url or None,
            "_meta": {
                "source_file": source_file,
                "source_page": 1,
                "manual_review_reasons": review_reasons,
                "match_strategy": "ai_assisted",
                "match_confidence": f"{confidence:.2f}" if confidence else "",
                "service_period_start": normalized.get("service_period_start"),
                "service_period_end": normalized.get("service_period_end"),
                "service_period_source": normalized.get("service_period_source") or "",
                "service_period_inferred": bool(normalized.get("service_period_source")) and normalized.get("service_period_source") != "ai",
                "support_document_status": support_document_status or "source_pdf",
                "support_document_url": support_document_url,
                "support_document_dropbox_path": support_document_dropbox_path,
                "ai_generated": True,
                "ai_invoice_number_generated": normalized.get("invoice_number_generated", False),
                "ai_invoice_number_policy_applied": normalized.get("invoice_number_policy_applied", False),
                "ai_source_invoice_number": normalized.get("source_invoice_number"),
                "ai_detected_vendor": normalized.get("raw_vendor_name"),
                "ai_property_candidate": normalized.get("property_candidate"),
                "ai_service_address": normalized.get("service_address"),
                "ai_source_gl_candidate": item.get("source_gl_candidate"),
                "ai_gl_suggestion_source": item.get("gl_suggestion_source"),
                "ai_generated_description": True,
                "ai_source_line_description": item.get("description"),
                "ai_tax_handling": normalized.get("tax_handling"),
                "ai_tax_amount_inferred": normalized.get("tax_amount_inferred", False),
                "ai_invoice_date_source": normalized.get("invoice_date_source"),
                "ai_zero_amount_lines_excluded": normalized.get("zero_amount_lines_excluded", 0),
                "ai_confidence": confidence,
                "ai_confidence_low": confidence < 0.70 if confidence else True,
                "ai_validation_flags": validation_codes,
                "ai_warnings": normalized.get("warnings") or [],
                "ai_mapping_provenance": normalized.get("mapping_provenance") or [],
                    "ai_provenance": {
                        "provider": normalized.get("ai_provider") or ai_provider.provider_status().provider,
                        "model": normalized.get("ai_model") or ai_provider.provider_status().model,
                        "extraction_mode": normalized.get("ai_extraction_mode") or "ai_assisted",
                        "reason": item.get("reason"),
                        "confidence_source": normalized.get("confidence_source"),
                        "invoice_total": normalized.get("total_amount"),
                        "base_amount": item.get("base_amount", item.get("amount")),
                        "allocated_tax_amount": item.get("allocated_tax_amount", 0),
                        "tax_amount": normalized.get("tax_amount"),
                        "subtotal": normalized.get("subtotal"),
                        "shipping_amount": normalized.get("shipping_amount"),
                        "fees_amount": normalized.get("fees_amount"),
                },
            },
        }
        rows.append(row)

    return {
        "vendor_key": AI_VENDOR_KEY,
        "source_file": source_file,
        "file_name": source_file,
        "source_page": 1,
        "invoice_number": normalized.get("invoice_number"),
        "account_number": normalized.get("account_number"),
        "invoice_date": normalized.get("invoice_date"),
        "total_amount": normalized.get("total_amount"),
        "confidence": normalized.get("confidence"),
        "manual_review_reasons": normalized.get("manual_review_reasons", []),
        "manual_review_codes": normalized.get("manual_review_codes", []),
        "validation_summary": normalized.get("validation_summary", {}),
        "rows": rows,
        "debug_info": {
            "source_file": source_file,
            "source_page": 1,
            "processing_mode": "ai_assisted",
            "original_vendor_key": vendor_key,
        },
    }


def _distribute_invoice_difference(
    items: list[dict[str, Any]],
    normalized: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return rows whose payable amounts reconcile to the invoice total.

    Variable supplier screenshots often expose merchandise lines clearly while
    tax, freight, or fees are blurry. The operator asked for ResMan lines to
    close to the invoice total by default, so we proportionally distribute the
    positive/negative difference across payable lines while keeping the source
    amount in metadata for review and alternate tax policies.
    """
    if not items:
        return items
    invoice_total = _money(normalized.get("total_amount"))
    if not invoice_total:
        return [
            {**item, "base_amount": _money(item.get("amount")), "allocated_tax_amount": 0}
            for item in items
        ]
    base_amounts = [_money(item.get("amount")) for item in items]
    base_total = _round_money(sum(base_amounts))
    if abs(base_total) <= 0.009:
        return [
            {**item, "base_amount": amount, "allocated_tax_amount": 0}
            for item, amount in zip(items, base_amounts)
        ]
    adjustment = _round_money(invoice_total - base_total)
    if abs(adjustment) <= 0.009:
        return [
            {**item, "base_amount": amount, "allocated_tax_amount": 0}
            for item, amount in zip(items, base_amounts)
        ]

    running = 0.0
    adjusted: list[dict[str, Any]] = []
    for idx, (item, base_amount) in enumerate(zip(items, base_amounts)):
        is_last = idx == len(items) - 1
        share = (
            _round_money(adjustment - running)
            if is_last
            else _round_money(adjustment * (max(base_amount, 0) / base_total))
        )
        if not is_last:
            running = _round_money(running + share)
        next_amount = _round_money(base_amount + share)
        next_item = {
            **item,
            "base_amount": base_amount,
            "allocated_tax_amount": share,
            "amount": next_amount,
        }
        quantity = _float(item.get("quantity"), 0)
        if quantity > 0:
            next_item["unit_price"] = _round_money(next_amount / quantity)
        adjusted.append(next_item)
    return adjusted


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _values_agree(left: Any, right: Any, *, money_field: bool = False) -> bool:
    if money_field:
        return abs(_money(left) - _money(right)) <= 0.01
    return _clean(left).lower() == _clean(right).lower()


def _payload(
    files: list[Path],
    files_processed: int,
    invoices: list[dict[str, Any]],
    manual_review: list[dict[str, Any]],
    unsupported: list[dict[str, Any]],
) -> dict[str, Any]:
    rows_total = sum(len(inv.get("rows", [])) for inv in invoices)
    return {
        "vendor_key": AI_VENDOR_KEY,
        "success": not unsupported,
        "summary": {
            "processing_mode": "ai_assisted",
            "files_total": len(files),
            "files_processed": files_processed,
            "files_unsupported": len(unsupported),
            "invoices_produced": len(invoices),
            "rows_total": rows_total,
            "line_items": rows_total,
            "manual_review_total": len(manual_review),
            "invoices_flagged_for_review": len(manual_review),
        },
        "invoices": invoices,
        "manual_review_rows": manual_review,
        "unsupported_files": unsupported,
    }


def _manual_review_item(
    source_file: str,
    vendor_name: str = "",
    *,
    account_number: str = "",
    invoice_number: str = "",
    invoice_date: str = "",
    property_abbreviation: str = "",
    location: str = "",
    service_address: str = "",
    total_amount: float = 0.0,
    line_count: int = 0,
    reasons: list[str] | None = None,
    reason_codes: list[str] | None = None,
    message: str = "",
) -> dict[str, Any]:
    return {
        "source_file": source_file,
        "vendor": vendor_name,
        "account_number": account_number,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "property_abbreviation": property_abbreviation,
        "location": location,
        "service_address": service_address,
        "total_amount": _money(total_amount),
        "line_count": line_count,
        "reasons": _human_review_reasons(reasons),
        "reason_codes": reason_codes or [],
        "message": message,
        "match_strategy": "ai_assisted",
        "match_confidence": "low",
        "service_period_source": "ai",
    }


def _vendor_hint_for_file(vendor_key: str, path: Path, detection: dict | None = None) -> str:
    if vendor_key and vendor_key != "unknown":
        return vendor_key.replace("_", " ")
    hay = path.stem.lower()
    reason = str((detection or {}).get("reason") or "").lower()
    for display, needles in VARIABLE_VENDOR_HINTS.items():
        if any(n in hay or n in reason for n in needles):
            return display.replace("_", " ")
    return "unknown vendor"


def _tracker_start(tracker: Any, files_total: int, status: ai_provider.AIProviderStatus) -> None:
    if tracker is None:
        return
    try:
        tracker.start_stage("ai_fallback", detail="AI-assisted invoice processing")
        tracker.update(
            status="processing",
            processing_mode="ai_assisted",
            ai_enabled=status.enabled and status.configured,
            ai_stage="Scanning invoice",
            current_step="Scanning invoice",
            files_total=files_total,
        )
    except Exception:
        pass


def _tracker_update(tracker: Any, *, percent: float, stage: str, **fields: Any) -> None:
    if tracker is None:
        return
    try:
        tracker.update(
            percent=percent,
            processing_mode="ai_assisted",
            ai_stage=stage,
            current_step=stage,
            **fields,
        )
        tracker.update_stage("ai_fallback", detail=stage, percent=percent)
    except Exception:
        pass


def _tracker_finish(
    tracker: Any,
    invoices: list[dict[str, Any]],
    manual_review: list[dict[str, Any]],
    *,
    warning: bool,
) -> None:
    if tracker is None:
        return
    try:
        if warning:
            tracker.warn_stage("ai_fallback", detail=f"{len(manual_review)} item(s) need review")
        else:
            tracker.complete_stage("ai_fallback", detail=f"{len(invoices)} invoice(s)")
    except Exception:
        pass


def _range_pct(index: int, total: int, target: float) -> float:
    if total <= 0:
        return target
    base = 5.0
    span = 82.0
    return min(95.0, base + (index / total) * span + target / max(total, 1) * 0.1)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _flagify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:80]


def _normalize_warnings(values: Any) -> list[str]:
    warnings: list[str] = []
    if not isinstance(values, list):
        values = [values]
    for value in values:
        if isinstance(value, dict):
            text = (
                value.get("message")
                or value.get("warning")
                or value.get("reason")
                or value.get("detail")
            )
        else:
            text = value
        clean = _clean(text)
        if clean:
            warnings.append(clean)
    return warnings


def _confidence_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence <= 0:
        return None
    return max(0.0, min(1.0, confidence))


def _derive_invoice_confidence(
    *,
    required_fields_present: bool,
    line_item_count: int,
    dates_valid: bool,
    total_reconciliation_passed: bool,
    issues: list[dict[str, str]],
) -> float:
    confidence = 0.42
    if required_fields_present:
        confidence += 0.18
    if line_item_count > 0:
        confidence += 0.12
    if dates_valid:
        confidence += 0.08
    if total_reconciliation_passed:
        confidence += 0.16

    codes = {issue["code"] for issue in issues}
    high_penalty_codes = {
        "invoice_number_missing",
        "invoice_date_missing",
        "invalid_invoice_date",
        "line_items_missing",
        "total_amount_missing",
        "total_reconciliation_failed",
        "vendor_name_missing",
    }
    mapping_penalty_codes = {
        "vendor_mapping_required",
        "vendor_mapping_not_found",
        "property_mapping_required",
        "property_or_service_address_missing",
        "property_abbreviation_missing",
        "location_unresolved",
        "gl_mapping_required",
        "ambiguous_gl_mapping",
        "tax_handling_requires_review",
    }
    confidence -= 0.10 * len(codes & high_penalty_codes)
    confidence -= 0.035 * len(codes & mapping_penalty_codes)
    return max(0.25, min(0.92, round(confidence, 2)))


def _cap_confidence_for_issues(confidence: float, issues: list[dict[str, str]]) -> float:
    codes = {issue["code"] for issue in issues}
    if {"total_reconciliation_failed", "line_items_missing", "total_amount_missing"} & codes:
        return min(confidence, 0.68)
    if {"invoice_number_missing", "invoice_date_missing", "invalid_invoice_date"} & codes:
        return min(confidence, 0.72)
    if {"vendor_name_missing"} & codes:
        return min(confidence, 0.70)
    return max(0.0, min(1.0, round(confidence, 2)))


def _derive_line_item_confidence(
    *,
    parent_confidence: float,
    item: dict[str, Any],
    total_reconciliation_passed: bool,
    gl_accounts: list[dict[str, Any]],
) -> float:
    confidence = min(parent_confidence, 0.86)
    if item.get("description"):
        confidence += 0.03
    if abs(float(item.get("amount") or 0)) > 0:
        confidence += 0.03
    if total_reconciliation_passed:
        confidence += 0.04
    gl_candidate = _clean(item.get("gl_account_candidate"))
    if gl_candidate and _is_known_gl(gl_candidate, gl_accounts):
        confidence += 0.03
    elif not gl_candidate:
        confidence -= 0.08
    else:
        confidence -= 0.04
    return max(0.30, min(0.92, round(confidence, 2)))


def _derive_line_item_reason(
    *,
    item: dict[str, Any],
    total_reconciliation_passed: bool,
    gl_accounts: list[dict[str, Any]],
) -> str:
    gl_candidate = _clean(item.get("gl_account_candidate"))
    if not gl_candidate:
        return "Backend-derived confidence from the extracted description and amount; GL mapping still needs review."
    if not _is_known_gl(gl_candidate, gl_accounts):
        return "Backend-derived confidence from extracted line details; suggested GL was not found in the reference."
    if total_reconciliation_passed:
        return "Backend-derived confidence from line amount, description, GL candidate, and reconciled invoice total."
    return "Backend-derived confidence from line amount and description; invoice total reconciliation still needs review."


def _choose_invoice_date_source(payload: dict[str, Any]) -> tuple[Any, str]:
    for field, _label in DATE_SOURCE_FIELDS:
        value = payload.get(field)
        if _clean(value):
            return value, field
    return "", "invoice_date"


def _tax_handling_policy(value: Any) -> str:
    requested = _clean(value).lower()
    if requested not in TAX_HANDLING_POLICIES:
        requested = _clean(getattr(settings, "AI_TAX_HANDLING", "manual_review")).lower()
    if requested not in TAX_HANDLING_POLICIES:
        requested = "manual_review"
    return requested


def _compose_invoice_description(normalized: dict[str, Any], item: dict[str, Any]) -> str:
    canonical = _clean(normalized.get("canonical_invoice_description"))
    if canonical:
        return build_invoice_description({**normalized, "service_address": normalized.get("service_address")}).description or canonical
    configured = invoice_format_rules.render_invoice_description(normalized, item)
    if configured:
        return configured

    service_prefix = _service_bill_description_prefix(normalized)
    if service_prefix:
        item_desc = _concise_item_description(str(item.get("description") or ""))
        if item_desc:
            return f"{service_prefix} - {item_desc}"[:180]
        return service_prefix[:180]

    parts: list[str] = []
    date = _short_date(str(normalized.get("invoice_date") or ""))
    vendor = str(normalized.get("vendor_name") or normalized.get("raw_vendor_name") or "").strip()
    prop = str(normalized.get("property_abbreviation") or "").strip()
    item_desc = _concise_item_description(str(item.get("description") or ""))
    if date:
        parts.append(date)
    if vendor:
        parts.append(vendor)
    if prop:
        parts.append(prop)
    if item_desc:
        parts.append(item_desc)
    if not parts:
        return str(normalized.get("invoice_description") or item.get("description") or "Invoice").strip()
    return " - ".join(parts)[:180]


def _compose_line_item_description(normalized: dict[str, Any], item: dict[str, Any]) -> str:
    raw = _clean(item.get("description")) or "Invoice total"
    canonical = _clean(item.get("canonical_line_item_description"))
    if canonical:
        return build_line_item_description(normalized, item).description or canonical
    configured = invoice_format_rules.render_line_item_description(
        normalized,
        item,
        fallback=raw,
    )
    if configured:
        return configured

    service_prefix = _service_bill_description_prefix(normalized)
    if not service_prefix:
        return raw
    normalized_raw = _normalize_key(raw)
    normalized_prefix = _normalize_key(service_prefix)
    if normalized_prefix and normalized_raw.startswith(normalized_prefix):
        return raw[:240]
    return f"{service_prefix} - {raw}"[:240]


def _service_bill_description_prefix(normalized: dict[str, Any]) -> str:
    bill_or_credit = _clean(normalized.get("bill_or_credit")).lower()
    if bill_or_credit and bill_or_credit != "bill":
        return ""
    result = build_invoice_description(normalized)
    return result.description


def _service_period_label(normalized: dict[str, Any]) -> str:
    start = _short_date(str(normalized.get("service_period_start") or ""))
    end = _short_date(str(normalized.get("service_period_end") or ""))
    if start and end:
        return f"{start}-{end}"
    return ""


def _short_date(value: str) -> str:
    normalized, ok = _normalize_date(value)
    if not normalized or not ok:
        return value
    try:
        return datetime.strptime(normalized, "%m/%d/%Y").strftime("%m/%d/%y")
    except ValueError:
        return normalized


def _concise_item_description(value: str) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()
    if not clean:
        return ""
    generic = {
        "hardware and miscellaneous items",
        "maintenance supplies",
        "miscellaneous",
        "general supplies",
        "invoice total",
    }
    if clean.lower() in generic:
        return ""
    words = clean.split()
    if len(words) > 8:
        clean = " ".join(words[:8])
    return clean[:72]


_REVIEW_REASON_LABELS = {
    "ai_invoice_processing_not_configured": "AI invoice processing is not configured for this vendor.",
    "ai_vision_not_configured": "Screenshot/photo processing needs readable OCR text or AI Vision enabled.",
    "ai_response_invalid_json": "AI returned an invalid extraction payload. Review this file manually.",
    "ai_processing_failed": "AI invoice processing failed. Review this file manually.",
    "manual_review_required": "Manual review is required.",
}


def _human_review_reasons(reasons: list[str] | None) -> list[str]:
    if not reasons:
        return [_REVIEW_REASON_LABELS["manual_review_required"]]
    human: list[str] = []
    for reason in reasons:
        clean = _clean(reason)
        human.append(_REVIEW_REASON_LABELS.get(clean, clean))
    return human


def _money(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
            if value.startswith("(") and value.endswith(")"):
                value = "-" + value[1:-1]
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0.0
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _nullable_money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _money(value)


def _round_money(value: float) -> float:
    return _money(value)


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _nullable_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y/%m/%d",
)


def _normalize_date(value: Any) -> tuple[str, bool]:
    s = _clean(value)
    if not s:
        return "", True
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y"), True
        except ValueError:
            continue
    return s, False


def _normalize_key(value: str) -> str:
    s = str(value or "").lower().replace("&", " and ")
    s = re.sub(r"['â€™]s\b", "s", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_STREET_SUFFIXES = {
    "ave": "avenue",
    "av": "avenue",
    "avenue": "avenue",
    "st": "street",
    "street": "street",
    "dr": "drive",
    "drive": "drive",
    "rd": "road",
    "road": "road",
    "blvd": "boulevard",
    "boulevard": "boulevard",
    "ct": "court",
    "court": "court",
    "ln": "lane",
    "lane": "lane",
    "pkwy": "parkway",
    "parkway": "parkway",
}


def _parse_service_address_for_property(value: str) -> dict[str, str]:
    """Return canonical address/unit pieces from invoice service text."""
    normalized = _normalize_key(value)
    if not normalized:
        return {"address_key": "", "unit": ""}
    tokens = normalized.split()
    if not tokens:
        return {"address_key": "", "unit": ""}
    unit = ""
    first = tokens[0]
    if re.match(r"^\d+[a-z]\d+$", first):
        match = re.match(r"^(\d+)([a-z]\d+)$", first)
        if match:
            tokens[0] = match.group(1)
            unit = match.group(2).upper()
    elif re.match(r"^\d+$", first) and len(tokens) > 1 and re.match(r"^[a-z]\d+$", tokens[1]):
        unit = tokens[1].upper()
        tokens.pop(1)
    elif len(tokens) > 1 and tokens[0] in {"apt", "unit", "suite", "ste"}:
        unit = tokens[1].upper()
        tokens = tokens[2:]
    address_tokens: list[str] = []
    for token in tokens:
        if token in {"apt", "unit", "suite", "ste"}:
            break
        if re.fullmatch(r"[a-z]{2}", token) or re.fullmatch(r"\d{5}(?:\d{4})?", token):
            break
        address_tokens.append(_STREET_SUFFIXES.get(token, token))
        if token in _STREET_SUFFIXES:
            break
    return {
        "address_key": " ".join(address_tokens).strip(),
        "unit": unit,
    }


def _property_address_key(value: str) -> str:
    tokens = _normalize_key(value).split()
    out: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[a-z]{2}", token) or re.fullmatch(r"\d{5}(?:\d{4})?", token):
            break
        out.append(_STREET_SUFFIXES.get(token, token))
        if token in _STREET_SUFFIXES:
            break
    return " ".join(out).strip()


def _normalize_property_unit(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _canonical_vendor(vendor_name: str, vendors: list[dict[str, Any]]) -> str:
    if not vendor_name:
        return ""
    needle = _normalize_key(vendor_name)
    for vendor in vendors:
        name = _clean(vendor.get("vendor_name") or vendor.get("Vendor"))
        key = _clean(vendor.get("vendor_id") or vendor.get("Company Abbreviation")).replace("_", " ")
        if needle and needle in {_normalize_key(name), _normalize_key(key)}:
            return name
    try:
        candidates = ai_mapping_review.vendor_candidates(vendor_name, limit=1).get("candidates") or []
    except Exception:
        candidates = []
    if candidates:
        top = candidates[0]
        score = float(top.get("score") or 0)
        name = _clean(top.get("vendor_name"))
        if name and score >= 0.86:
            return name
    return ""


def _resolve_property_context(
    *,
    property_abbreviation: str,
    property_candidate: str,
    service_address: str,
    properties: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    """Return confirmed property abbreviation, valid location, and matched row.

    AI text is never written directly to Location. We only emit a location when
    it comes from a known property/unit row.
    """
    abbr_needle = _normalize_key(property_abbreviation)
    candidate_needle = _normalize_key(property_candidate)
    address_needle = _normalize_key(service_address)
    parsed_service = _parse_service_address_for_property(service_address)

    matches: list[tuple[str, str, dict[str, Any]]] = []
    address_matches: list[tuple[str, str, dict[str, Any]]] = []
    for prop in properties:
        prop_abbr = _clean(
            prop.get("Property Abbreviation")
            or prop.get("property_abbreviation")
            or prop.get("Abbreviation")
            or prop.get("abbreviation")
        )
        prop_name = _clean(prop.get("Property Name") or prop.get("property_name"))
        unit = _clean(prop.get("Unit") or prop.get("Unit Number") or prop.get("unit"))
        address = _clean(prop.get("Address") or prop.get("Service Address") or prop.get("address"))
        exact_property = bool(abbr_needle and _normalize_key(prop_abbr) == abbr_needle)
        exact_name = bool(candidate_needle and _normalize_key(prop_name) == candidate_needle)
        exact_address = bool(address_needle and _normalize_key(address) == address_needle)
        prop_address_key = _property_address_key(address)
        parsed_address_match = bool(
            parsed_service.get("address_key")
            and prop_address_key
            and prop_address_key == parsed_service["address_key"]
        )
        parsed_unit_match = bool(
            parsed_service.get("unit")
            and _normalize_property_unit(unit) == parsed_service["unit"]
        )
        if parsed_address_match:
            address_matches.append((prop_abbr, unit, dict(prop)))
            if parsed_unit_match:
                return prop_abbr, unit, dict(prop)
        if exact_property or exact_name or exact_address:
            matches.append((prop_abbr, unit, dict(prop)))
    if matches:
        abbreviations = {abbr for abbr, _, _ in matches if abbr}
        units = {unit for _, unit, _ in matches if unit}
        abbr = next(iter(abbreviations)) if len(abbreviations) == 1 else matches[0][0]
        unit = next(iter(units)) if len(units) == 1 else ""
        return abbr, unit, matches[0][2]
    if address_matches:
        abbreviations = {abbr for abbr, _, _ in address_matches if abbr}
        if len(abbreviations) == 1:
            abbr = next(iter(abbreviations))
            units = {
                unit for _, unit, _ in address_matches
                if _normalize_property_unit(unit) == parsed_service.get("unit")
            }
            location = next(iter(units)) if len(units) == 1 else ""
            return abbr, location, address_matches[0][2]

    # Fall back to the review candidate engine for real-world invoice text.
    # Vendor screenshots often include a street address plus ZIP+4, while the
    # property file may only store the street. If a single property
    # abbreviation is confidently suggested, prefill the property but leave
    # Location blank when multiple units share that address.
    try:
        response = ai_mapping_review.property_candidates(
            query=property_candidate or property_abbreviation,
            service_address=service_address,
            limit=20,
        )
        candidates = [
            c for c in (response.get("candidates") or [])
            if float(c.get("score") or 0) >= 0.74 and _clean(c.get("property_abbreviation"))
        ]
    except Exception:
        candidates = []
    if candidates:
        abbreviations = {
            _clean(c.get("property_abbreviation"))
            for c in candidates
            if _clean(c.get("property_abbreviation"))
        }
        if len(abbreviations) == 1:
            abbr = next(iter(abbreviations))
            locations = {
                _clean(c.get("location"))
                for c in candidates
                if _clean(c.get("location"))
            }
            location = next(iter(locations)) if len(locations) == 1 else ""
            return abbr, location, dict(candidates[0])
    return "", "", {}


def _suggest_valid_gl_candidate(
    *,
    description: str,
    vendor_name: str,
    ai_suggested_gl: str,
) -> dict[str, str] | None:
    """Return a valid numeric GL candidate when the mapping engine is confident.

    Variable supplier invoices often include vendor-side categories such as
    HARDWARE or MISCELLANEOUS. Those are not valid ResMan GL values, but the
    mapping engine can still produce a high-confidence numeric candidate. We
    prefill only strong validated candidates and keep the review flag so the
    operator remains in control.
    """
    enriched_description = " ".join(
        part for part in (
            description,
            _vendor_rule_category(vendor_name),
            _vendor_default_gl_description(vendor_name),
            vendor_name,
        )
        if part
    )
    try:
        candidates = ai_mapping_review.gl_candidates(
            line_item_description=enriched_description,
            vendor_name=vendor_name,
            ai_suggested_gl=ai_suggested_gl,
            limit=3,
        ).get("candidates") or []
    except Exception:
        return None
    for candidate in candidates:
        if candidate.get("valid") is False:
            continue
        score = float(candidate.get("score") or 0)
        gl_code = _clean(candidate.get("gl_code") or candidate.get("gl_account"))
        if score >= 0.9 and gl_code:
            account = ai_mapping_review.validate_gl_account(gl_code)
            if account:
                return account
    vendor_default = _vendor_rule_default_gl(vendor_name)
    if vendor_default:
        return vendor_default
    category_default = _vendor_category_default_gl(vendor_name)
    if category_default:
        return category_default
    for candidate in candidates:
        if candidate.get("valid") is False:
            continue
        score = float(candidate.get("score") or 0)
        gl_code = _clean(candidate.get("gl_code") or candidate.get("gl_account"))
        if score >= 0.82 and gl_code:
            account = ai_mapping_review.validate_gl_account(gl_code)
            if account:
                return account
    return None


@lru_cache(maxsize=1)
def _vendor_rule_rows() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    try:
        import yaml  # type: ignore
    except Exception:
        return tuple(rows)
    vendors_dir = settings.VENDORS_DIR
    if not vendors_dir.is_dir():
        return tuple(rows)
    for path in vendors_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        identity = data.get("vendor_identity") if isinstance(data.get("vendor_identity"), dict) else {}
        accounting_source = data.get("accounting_source") if isinstance(data.get("accounting_source"), dict) else {}
        accounting = data.get("accounting_mapping") if isinstance(data.get("accounting_mapping"), dict) else {}
        rows.append({
            "vendor_name": _clean(identity.get("vendor_name")),
            "normalized_vendor_key": _clean(identity.get("normalized_vendor_key")),
            "category": _clean(identity.get("category")),
            "aliases": identity.get("aliases") if isinstance(identity.get("aliases"), list) else [],
            "detection_keywords": (
                identity.get("detection_keywords")
                if isinstance(identity.get("detection_keywords"), list)
                else []
            ),
            "source_properties_observed": (
                accounting_source.get("source_properties_observed")
                if isinstance(accounting_source.get("source_properties_observed"), list)
                else []
            ),
            "default_gl_code": _clean(accounting.get("default_gl_code")),
            "default_gl_description": _clean(accounting.get("default_gl_description")),
        })
    return tuple(rows)


def _vendor_rule_default_gl(vendor_name: str) -> dict[str, str] | None:
    """Return the vendor's validated configured default GL when present."""
    row = _vendor_rule_for_name(vendor_name)
    if not row:
        return None
    code = row.get("default_gl_code") or ""
    if not code:
        return None
    return ai_mapping_review.validate_gl_account(code)


def _vendor_default_gl_description(vendor_name: str) -> str:
    row = _vendor_rule_for_name(vendor_name)
    if not row:
        return ""
    return _clean(row.get("default_gl_description"))


def _vendor_rule_category(vendor_name: str) -> str:
    row = _vendor_rule_for_name(vendor_name)
    if not row:
        return ""
    return _clean(row.get("category"))


def _vendor_rule_for_name(vendor_name: str) -> dict[str, str] | None:
    vendor_key = ai_mapping_review.mapping_key(vendor_name)
    if not vendor_key:
        return None
    for row in _vendor_rule_rows():
        if not row.get("vendor_name"):
            continue
        if (
            ai_mapping_review.mapping_key(row["vendor_name"]) == vendor_key
            or row.get("normalized_vendor_key") == vendor_key
        ):
            return row
    return None


def _vendor_category_default_gl(vendor_name: str) -> dict[str, str] | None:
    """Return a same-category default GL only when the category is unambiguous.

    This is deliberately conservative: it helps variable invoices such as TK
    Elevator inherit the validated Elevator category default (6615) while
    avoiding broad guesses for noisy categories with many possible GLs.
    """
    target = _vendor_rule_for_name(vendor_name)
    if not target or not target.get("category"):
        return None

    valid_codes: set[str] = set()
    for row in _vendor_rule_rows():
        if row.get("category") != target["category"]:
            continue
        code = row.get("default_gl_code") or ""
        account = ai_mapping_review.validate_gl_account(code)
        if account:
            valid_codes.add(account["gl_code"])
    if len(valid_codes) != 1:
        return None
    return ai_mapping_review.validate_gl_account(next(iter(valid_codes)))


def _is_known_gl(candidate: str, gl_accounts: list[dict[str, Any]]) -> bool:
    if not candidate:
        return False
    norm = _normalize_key(candidate)
    code = re.search(r"\d{3,6}", candidate)
    for account in gl_accounts:
        account_code = _clean(account.get("gl_code") or account.get("code"))
        account_desc = _clean(
            account.get("gl_description")
            or account.get("chart_of_accounts_description")
            or account.get("description")
        )
        if code and account_code == code.group(0):
            return True
        if account_desc and _normalize_key(account_desc) in norm:
            return True
    return False


def _load_vendor_reference() -> list[dict[str, Any]]:
    path = settings.PROJECT_ROOT / "Vendors" / "Vendor List.csv"
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                for row in csv.DictReader(fh):
                    name = _clean(row.get("Vendor"))
                    if not name:
                        continue
                    rows.append({
                        "vendor_name": name,
                        "vendor_id": _clean(row.get("Company Abbreviation")),
                        "default_gl": _clean(row.get("Default GL")),
                        "active": _clean(row.get("Active")),
                        "status": _clean(row.get("Status")),
                    })
            return rows
        except (OSError, UnicodeDecodeError):
            rows = []
            continue
    return rows


def _load_property_reference() -> list[dict[str, Any]]:
    candidates = [
        settings.PROJECT_ROOT / "Properties" / "Properties.csv",
        settings.PROJECT_ROOT / "Properties" / "Unit Info Clean.csv",
    ]
    rows: list[dict[str, Any]] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    rows.append(dict(row))
        except Exception:
            continue
    return rows


def _load_gl_reference() -> list[dict[str, Any]]:
    path = settings.GENERAL_LEDGER_REFERENCE
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        accounts = data.get("detected_gl_accounts") or []
        return [a for a in accounts if isinstance(a, dict)]
    except Exception:
        return []


__all__ = [
    "AI_MANUAL_REVIEW_MESSAGE",
    "AI_VENDOR_KEY",
    "ai_result_to_invoice",
    "extract_document_text",
    "load_references",
    "process_ai_vendor_files",
    "processing_mode_for_vendor",
    "should_route_to_ai",
    "validate_ai_extraction",
]
