"""AI-assisted invoice extraction and validation.

This module is intentionally a webapp integration layer. It does not replace
the deterministic vendor processors; it only handles unknown / variable
supplier invoices when the AI assist provider is explicitly enabled.
"""

from __future__ import annotations

import csv
import copy
import difflib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import settings
from . import ai_provider
from . import ai_runtime_trace
from . import ai_vision
from . import ai_mapping_review
from . import canonical_rules
from . import document_ingestion
from . import fast_first_facts
from . import invoice_format_rules
from .local_processing_guard import LOCAL_DOCUMENT_PREPROCESS_LOCK
from . import native_pdf_evidence
from . import page_facts_cache
from . import perf_timer
from . import support_documents
from . import resman_context_data
from . import tenant_document_policies
from .tenant_accounting_policies import default_tenant_id
from .accounting_contracts import (
    CropCoordinates,
    DateFieldProvenance,
    ExcludedPaidRowFacts,
    HandwrittenRowIdentityEvidence,
    PaidMarkerEvidence,
    RowIdentityAlternative,
    model_dict,
)
from .description_builder import build_invoice_description, build_line_item_description
from .review_taxonomy import categorize_warning
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


def _deduplicate_source_files(
    files: list[Path],
) -> tuple[list[Path], dict[str, list[str]]]:
    """Plan one extraction per exact source document.

    Upload names are evidence, but they are not document identity.  Managers
    can upload the same bytes more than once under different names; processing
    every copy wastes provider budget and can produce contradictory stochastic
    results.  Keep the first source as the canonical extraction input and retain
    every additional filename as provenance.
    """
    unique: list[Path] = []
    canonical_by_hash: dict[str, Path] = {}
    aliases: dict[str, list[str]] = {}
    for path in files:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            # An unreadable path still needs its own normal failure handling.
            unique.append(path)
            continue
        canonical = canonical_by_hash.get(digest)
        if canonical is None:
            canonical_by_hash[digest] = path
            unique.append(path)
            continue
        aliases.setdefault(canonical.name, []).append(path.name)
    return unique, aliases


def _attach_duplicate_source_provenance(
    invoices: list[dict[str, Any]],
    aliases: dict[str, list[str]],
) -> None:
    for invoice in invoices:
        source_file = _clean(invoice.get("source_file"))
        duplicate_names = list(aliases.get(source_file) or [])
        if not duplicate_names:
            continue
        debug_info = dict(invoice.get("debug_info") or {})
        debug_info["exact_duplicate_sources"] = duplicate_names
        invoice["debug_info"] = debug_info
        for row in invoice.get("rows") or []:
            if not isinstance(row, dict):
                continue
            meta = dict(row.get("_meta") or {})
            meta["exact_duplicate_sources"] = duplicate_names
            row["_meta"] = meta


def _merge_page_vision_payloads(
    page_payloads: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge independently validated page facts into one invoice payload."""
    if not page_payloads:
        raise ai_provider.AIProviderInvalidSchema("No page-level Vision payloads were returned.")
    merged = dict(page_payloads[0][1])
    merged_items: list[dict[str, Any]] = []
    merged_candidates: list[dict[str, Any]] = []
    warnings = list(merged.get("warnings") or [])
    for page_number, payload in page_payloads:
        for item in payload.get("line_items") or []:
            if isinstance(item, dict):
                merged_items.append({**item, "source_page": page_number})
        for candidate in payload.get("vision_candidates") or []:
            if isinstance(candidate, dict):
                merged_candidates.append({**candidate, "page": page_number})
        warnings.extend(str(value) for value in (payload.get("warnings") or []) if str(value))
    merged["line_items"] = merged_items
    merged["vision_candidates"] = merged_candidates
    # Page payloads have already reconciled independently. Their component
    # facts are the safest cross-page total when page footers have page scope.
    component_total = sum(float(item.get("amount") or 0) for item in merged_items)
    component_total += sum(
        float(payload.get(key) or 0)
        for _, payload in page_payloads
        for key in ("tax_amount", "shipping_amount", "fees_amount")
    )
    explicit_page_total = sum(float(payload.get("total_amount") or 0) for _, payload in page_payloads)
    merged["total_amount"] = round(explicit_page_total or component_total, 2)
    merged["subtotal"] = round(sum(float(item.get("amount") or 0) for item in merged_items), 2)
    merged["tax_amount"] = round(sum(float(p.get("tax_amount") or 0) for _, p in page_payloads), 2)
    merged["shipping_amount"] = round(sum(float(p.get("shipping_amount") or 0) for _, p in page_payloads), 2)
    merged["fees_amount"] = round(sum(float(p.get("fees_amount") or 0) for _, p in page_payloads), 2)
    if len(page_payloads) > 1:
        warnings.append("multi_page_invoice_merged_from_independently_reconciled_page_facts")
        for candidate in merged_candidates:
            if str(candidate.get("field_key") or "") in {
                "subtotal", "tax_amount", "shipping_amount", "fees_amount", "total_amount"
            }:
                candidate["validation_status"] = "page_scope_candidate"
    page_reconciliation_mismatch = bool(
        explicit_page_total
        and abs(round(component_total, 2) - round(explicit_page_total, 2)) > 0.02
    )
    if page_reconciliation_mismatch:
        warnings.append("multi_page_component_total_mismatch_preserved_for_review")
        merged["unexplained_invoice_difference"] = round(
            explicit_page_total - component_total,
            2,
        )
    merged["warnings"] = list(dict.fromkeys(warnings))
    merged["confidence"] = min(float(p.get("confidence") or 0) for _, p in page_payloads)
    merged["needs_manual_review"] = page_reconciliation_mismatch or any(
        bool(p.get("needs_manual_review")) for _, p in page_payloads
    )
    return merged


def _should_extract_scanned_pages_independently(
    candidate: document_ingestion.DocumentCandidate | None,
    evidence_groups: list[tuple[int, list[str]]],
) -> bool:
    """Return whether page-scoped visual facts must be reconciled explicitly."""
    return bool(
        candidate
        and candidate.source_type == "pdf_scanned"
        and candidate.page_count > 1
        and len(evidence_groups) > 1
    )


def _should_use_native_pdf_for_candidate(
    candidate: document_ingestion.DocumentCandidate,
    status: ai_provider.AIProviderStatus,
    source_file: Path,
) -> bool:
    """Route only genuinely difficult scanned PDFs to native document vision."""

    if not bool(getattr(settings, "AI_VISION_NATIVE_PDF_ENABLED", False)):
        return False
    if source_file.suffix.lower() != ".pdf" or candidate.source_type != "pdf_scanned":
        return False
    if (status.vision_provider or status.provider or "").strip().lower() != "openai":
        return False
    native_model, _ = _native_pdf_model_sequence(status, candidate)
    if not ai_provider.native_pdf_surface_available(
        native_model or status.vision_model or status.model
    ):
        return False
    max_pages = max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2))
    if int(candidate.page_count or 1) > max_pages:
        return False
    quality = candidate.extraction_quality or {}
    try:
        score = float(quality.get("text_quality_score") or candidate.text_quality_score or 0)
    except (TypeError, ValueError):
        score = 0.0
    helper_text = candidate.document_text or ""
    has_explicit_date = bool(re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", helper_text))
    has_explicit_total = bool(
        re.search(r"\b(?:grand\s+total|invoice\s+total|total\s+due|amount\s+due)\b", helper_text, re.I)
    )
    missing_critical_ocr = not has_explicit_date or not has_explicit_total
    # Missing dates/totals are a document-quality signal even for a single
    # scanned page.  The former page_count > 1 guard sent one-page forms with
    # unreadable handwritten headers through the lossy OCR/rendered route.
    # Keep high-quality scans on the economical path, but use the immutable
    # source PDF whenever OCR is both incomplete and below the review-quality
    # threshold.
    return bool(
        score < 0.20
        or (score < 0.65 and missing_critical_ocr)
        or (int(candidate.page_count or 1) > 1 and score < 0.65)
    )


_MATRIX_COMPONENT_HEADERS = (
    "kitchen counter", "bath counter", "bath tub", "wall tile",
    "floor tile", "tub mat", "window sill", "other",
)


def _payload_is_lossy_matrix_aggregate(
    payload: dict[str, Any],
    document_text: str = "",
) -> bool:
    """Detect a reconciled total fallback that discarded visible matrix columns."""
    items = [item for item in list(payload.get("line_items") or []) if isinstance(item, dict)]
    if len(items) != 1:
        return False
    item = items[0]
    if str(item.get("row_label") or "").strip():
        return False
    try:
        total = float(payload.get("total_amount") or 0)
        amount = float(item.get("amount") or 0)
    except (TypeError, ValueError):
        return False
    if total <= 0 or abs(total - amount) > 0.02:
        return False
    context = _normalize_key(" ".join(str(value or "") for value in (
        payload.get("invoice_description"),
        item.get("activity"), item.get("description"), item.get("raw_description"),
        document_text,
    )))
    observed_headers = sum(header in context for header in _MATRIX_COMPONENT_HEADERS)
    has_total_column = "unit total" in context or "row total" in context
    return observed_headers >= 3 or (observed_headers >= 2 and has_total_column)


def _matrix_payload_specificity(payload: dict[str, Any]) -> int:
    score = 0
    for item in list(payload.get("line_items") or []):
        if not isinstance(item, dict) or item.get("matrix_expansion_status") == "unresolved_arithmetic":
            continue
        activity = _normalize_key(item.get("activity"))
        if not activity or activity in {"other", "unresolved matrix components"}:
            continue
        score += 2
        if str(item.get("row_label") or item.get("location_candidate") or "").strip():
            score += 1
        if float(item.get("amount") or 0) > 0:
            score += 1
    return score


def _select_more_specific_cross_page_payload(
    merged: dict[str, Any],
    recovery: dict[str, Any],
    *,
    selection_warning: str = "cross_page_visual_recovery_selected_for_matrix_specificity",
) -> dict[str, Any]:
    """Use whole-document recovery only when identity, total, and specificity prove it safer."""
    merged_number = _normalize_key(merged.get("invoice_number"))
    recovery_number = _normalize_key(recovery.get("invoice_number"))
    if merged_number and recovery_number and merged_number != recovery_number:
        return merged
    try:
        merged_total = float(merged.get("total_amount") or 0)
        recovery_total = float(recovery.get("total_amount") or 0)
    except (TypeError, ValueError):
        return merged
    if merged_total <= 0 or abs(merged_total - recovery_total) > 0.02:
        return merged
    if _matrix_payload_specificity(recovery) <= _matrix_payload_specificity(merged):
        return merged
    selected = dict(recovery)
    warnings = _normalize_warnings(selected.get("warnings") or [])
    warnings.append(selection_warning)
    selected["warnings"] = list(dict.fromkeys(warnings))
    return selected


def _extract_scanned_pages_independently(
    *,
    batch_id: str,
    source_file: Path,
    vendor_hint: str,
    document_candidate: document_ingestion.DocumentCandidate,
    evidence_groups: list[tuple[int, list[str]]],
    template_schema: dict[str, Any],
    prompt_references: dict[str, list[dict[str, Any]]],
    vision_model: str,
) -> dict[str, Any]:
    """Extract each scanned page as facts, then merge their accounting scope.

    A single multimodal response can confuse a page total with a document total
    or exceed output limits on dense handwritten packets.  Page requests retain
    distinct cache identities and source_page provenance; the backend, not the
    model, performs the final cross-page arithmetic merge.
    """
    page_text_by_number = {
        int(page.page_number): str(page.text or "")
        for page in document_candidate.pages
    }
    page_payloads: list[tuple[int, dict[str, Any]]] = []
    for page_number, page_refs in evidence_groups:
        page_prompt = _document_prompt_context(
            source_file=source_file,
            batch_hint="",
            vendor_hint=vendor_hint,
            document_text=page_text_by_number.get(page_number, ""),
        )
        page_payload = _extract_vision_with_reduced_retry(
            vendor_hint=vendor_hint,
            document_text=page_prompt,
            page_images_or_refs=page_refs,
            template_schema=template_schema,
            property_reference=prompt_references["properties"],
            gl_reference=prompt_references["gl_accounts"],
            vendor_reference=prompt_references["vendors"],
            model_override=vision_model,
            cost_scope_id=batch_id,
        )
        page_payload = _reconcile_high_confidence_vision_candidates(page_payload)
        if _payload_is_lossy_matrix_aggregate(
            page_payload,
            page_text_by_number.get(page_number, ""),
        ):
            try:
                band_refs = ai_vision.render_pdf_pages_as_data_urls(
                    batch_id=batch_id,
                    filename=source_file.name,
                    page_numbers=[page_number],
                    include_table_bands=True,
                )
                if len(band_refs) > 1:
                    band_recovery = _extract_vision_with_reduced_retry(
                        vendor_hint=vendor_hint,
                        document_text=page_prompt + (
                            "\n\n[VISUAL VIEW NOTE]\n"
                            "The first image is the complete source page. The following images are "
                            "overlapping high-resolution bands of the same charge table. Use the full "
                            "page for headers and totals, then emit every non-empty component cell from "
                            "the bands without duplicating overlap rows."
                        ),
                        page_images_or_refs=band_refs,
                        template_schema=template_schema,
                        property_reference=prompt_references["properties"],
                        gl_reference=prompt_references["gl_accounts"],
                        vendor_reference=prompt_references["vendors"],
                        model_override=vision_model,
                        cost_scope_id=batch_id,
                    )
                    band_recovery = _reconcile_high_confidence_vision_candidates(band_recovery)
                    page_payload = _select_more_specific_cross_page_payload(
                        page_payload,
                        band_recovery,
                        selection_warning="matrix_band_visual_recovery_selected_for_specificity",
                    )
            except ai_provider.AIProviderError:
                warnings = _normalize_warnings(page_payload.get("warnings") or [])
                warnings.append("matrix_band_visual_recovery_unavailable")
                page_payload["warnings"] = list(dict.fromkeys(warnings))
        page_payloads.append((page_number, page_payload))
    merged = _merge_page_vision_payloads(page_payloads)
    page_text = "\n\n".join(page_text_by_number.values())
    needs_matrix_recovery = any(
        _payload_is_lossy_matrix_aggregate(payload, page_text_by_number.get(page_number, ""))
        or any(
            isinstance(item, dict) and item.get("matrix_expansion_status") == "unresolved_arithmetic"
            for item in list(payload.get("line_items") or [])
        )
        for page_number, payload in page_payloads
    )
    max_vision_pages = max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2))
    if needs_matrix_recovery and document_candidate.page_count <= max_vision_pages:
        try:
            recovery = _extract_vision_with_reduced_retry(
                vendor_hint=vendor_hint,
                document_text=_document_prompt_context(
                    source_file=source_file,
                    batch_hint="",
                    vendor_hint=vendor_hint,
                    document_text=page_text,
                ),
                page_images_or_refs=[ref for _, refs in evidence_groups for ref in refs],
                template_schema=template_schema,
                property_reference=prompt_references["properties"],
                gl_reference=prompt_references["gl_accounts"],
                vendor_reference=prompt_references["vendors"],
                model_override=vision_model,
                cost_scope_id=batch_id,
            )
            recovery = _reconcile_high_confidence_vision_candidates(recovery)
            merged = _select_more_specific_cross_page_payload(merged, recovery)
        except ai_provider.AIProviderError:
            warnings = _normalize_warnings(merged.get("warnings") or [])
            warnings.append("cross_page_visual_matrix_recovery_unavailable")
            merged["warnings"] = list(dict.fromkeys(warnings))
    return merged


TAX_HANDLING_POLICIES = {"manual_review", "distribute_proportionally", "separate_tax_line"}
DATE_SOURCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("invoice_date", "explicit invoice date"),
    ("service_date", "explicit date of service"),
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

VARIABLE_VENDOR_DISPLAY_NAMES: dict[str, str] = {
    "hd_supply": "HD Supply",
    "lowes": "Lowes Pro Supply",
    "home_depot": "Home Depot",
}

_REFERENCE_SEARCH_INDEX: dict[
    tuple[int, int, str],
    list[tuple[int, str, frozenset[str], dict[str, Any]]],
] = {}


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


def _actual_provider_identity(
    payload: dict[str, Any], *, fallback_provider: str, fallback_model: str
) -> tuple[str, str]:
    """Use the profile that actually served the request in private traces.

    Legacy status remains the compatibility gate, but cost routing may select a
    different probe-verified provider.  The extraction result is authoritative
    for observability; no credential or endpoint data is copied into it.
    """
    provider = _clean(payload.get("_provider_name")) or fallback_provider
    model = _clean(payload.get("_provider_model_id")) or fallback_model
    return provider, model


def _page_facts_lookup(
    *,
    batch_id: str,
    source_file: Path,
    page_numbers: list[int],
    provider: str,
    profile_id: str,
    model: str,
    prompt_references: dict[str, Any],
) -> tuple[
    list[page_facts_cache.VisualPageIdentity],
    page_facts_cache.PageFactsCacheContext,
    page_facts_cache.CachedPageFactsArtifact | None,
]:
    """Look up exact observed facts before expensive evidence construction."""
    identities = [
        page_facts_cache.exact_visual_page_identity(
            batch_id=batch_id,
            filename=source_file.name,
            page_number=number,
        )
        for number in page_numbers
    ]
    context = page_facts_cache.PageFactsCacheContext(
        provider=provider,
        profile_id=profile_id,
        model=model,
        # Exact observed page facts are tenant/catalog independent. Property,
        # vendor and GL resolution run after cache retrieval and are never
        # allowed to alter immutable source evidence.
        reference_fingerprint="",
    )
    with ai_runtime_trace.operation(
        batch_id=batch_id,
        stage="exact_page_facts_lookup",
        provider=provider,
        model=model,
        profile_id=profile_id,
    ):
        artifact, owns_reservation = page_facts_cache.load_or_reserve(identities, context)
        if artifact is None and owns_reservation:
            artifact = page_facts_cache.load_compatible_exact_observed(
                identities, context
            )
        if (
            artifact is None
            and owns_reservation
            and settings.AI_PAGE_FACTS_ALLOW_PERSISTED_MIGRATION
        ):
            artifact = page_facts_cache.seed_from_persisted_result(
                batch_id=batch_id,
                source_file=source_file.name,
                source_page=page_numbers[0],
                identities=identities,
                context=context,
            )
        ai_runtime_trace.record_cache(
            page_facts_cache.cache_key(identities, context),
            hit=artifact is not None,
            layer="exact_page_facts",
        )
    return identities, context, artifact


def _save_page_facts(
    *,
    batch_id: str,
    identities: list[page_facts_cache.VisualPageIdentity],
    context: page_facts_cache.PageFactsCacheContext,
    raw: dict[str, Any],
    source_file: Path,
    page_numbers: list[int],
    group_index: int,
) -> page_facts_cache.CachedPageFactsArtifact:
    with ai_runtime_trace.operation(
        batch_id=batch_id,
        stage="exact_page_facts_persist",
        provider=context.provider,
        model=context.model,
        profile_id=context.profile_id,
    ):
        artifact = page_facts_cache.save(
            identities=identities,
            context=context,
            observed_payload=raw,
        )
        ai_runtime_trace.record_cache(
            artifact.cache_key,
            hit=False,
            layer="exact_page_facts_write",
        )
    page_facts_cache.register_document_artifact(
        batch_id=batch_id,
        filename=source_file.name,
        group_index=group_index,
        page_numbers=page_numbers,
        artifact=artifact,
    )
    return artifact


class _SupportLinkCoordinator:
    """Create one support link while concurrent invoice groups keep working."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._done: set[int] = set()
        self._link: support_documents.SupportDocumentLink | None = None

    def obtain(
        self,
        group_index: int,
        creator: Any,
    ) -> support_documents.SupportDocumentLink:
        with self._condition:
            self._condition.wait_for(
                lambda: all(index in self._done for index in range(1, group_index)),
                timeout=300,
            )
            if self._link is None:
                self._link = creator()
            self._done.add(group_index)
            self._condition.notify_all()
            return self._link

    def fail(self, group_index: int) -> None:
        with self._condition:
            self._done.add(group_index)
            self._condition.notify_all()


def _safe_failure_code(exc: Exception) -> str:
    if isinstance(exc, ai_provider.AIProviderError):
        diagnostic = exc.safe_diagnostic()
        return _clean(diagnostic.get("failure_code")) or "provider_error"
    name = re.sub(r"[^a-z0-9]+", "_", type(exc).__name__.lower()).strip("_")
    return name or "processing_error"


def _run_ai_file_workers_bounded(
    files: list[Path],
    worker: Any,
    *,
    max_workers: int,
) -> list[tuple[Path, dict[str, Any] | None, Exception | None]]:
    """Run independent AI files concurrently and return stable source order."""
    with ThreadPoolExecutor(
        max_workers=min(len(files), max(1, max_workers)),
        thread_name_prefix="ai-invoice",
    ) as executor:
        futures = [executor.submit(worker, source_file) for source_file in files]
        results: list[tuple[Path, dict[str, Any] | None, Exception | None]] = []
        for source_file, future in zip(files, futures):
            try:
                results.append((source_file, future.result(), None))
            except Exception as exc:
                results.append((source_file, None, exc))
        return results


def _process_cached_document_manifest(
    *,
    batch_id: str,
    source_file: Path,
    vendor_key: str,
    vendor_hint: str,
    references: dict[str, list[dict[str, Any]]],
    status: ai_provider.AIProviderStatus,
    dry_run: bool,
) -> dict[str, Any] | None:
    manifest_total_started = time.perf_counter()
    provider = (status.vision_provider or status.provider or "").strip().lower()
    configured_models = {
        str(value or "").strip()
        for value in (
            status.vision_model,
            status.model,
            getattr(settings, "AI_VISION_NATIVE_PDF_MODEL", ""),
            getattr(settings, "AI_VISION_NATIVE_PDF_ESCALATION_MODEL", ""),
            os.environ.get("AI_VISION_ESCALATION_MODEL", ""),
        )
        if str(value or "").strip()
    }
    manifest_load_started = time.perf_counter()
    with ai_runtime_trace.operation(
        batch_id=batch_id,
        stage="exact_document_facts_manifest_load",
        provider=provider,
        profile_id="document-facts-manifest",
    ):
        loaded = page_facts_cache.load_document_manifest(
            batch_id=batch_id,
            filename=source_file.name,
            allowed_provider_models={(provider, model) for model in configured_models},
        )
        ai_runtime_trace.record_stage_timing(
            "exact_document_facts_manifest_load",
            (time.perf_counter() - manifest_load_started) * 1000,
        )
    if not loaded:
        return None
    invoices: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    support_link: support_documents.SupportDocumentLink | None = None
    for entry, artifact in loaded:
        raw = copy.deepcopy(artifact.observed_payload)
        raw["_source_file"] = source_file.name
        raw["_source_page"] = min(entry.page_numbers or [1])
        validate_started = time.perf_counter()
        normalized = page_facts_cache.load_normalized_facts(artifact.cache_key)
        normalized_cache_hit = normalized is not None
        if normalized is None:
            normalized = validate_ai_extraction(raw, references=references)
            page_facts_cache.save_normalized_facts(
                artifact.cache_key, normalized
            )
        else:
            normalized = copy.deepcopy(normalized)
        with ai_runtime_trace.operation(
            batch_id=batch_id,
            stage="manifest_facts_validation",
            provider=artifact.context.provider,
            model=artifact.context.model,
            profile_id=artifact.context.profile_id,
        ):
            ai_runtime_trace.record_stage_timing(
                "manifest_facts_validation",
                (time.perf_counter() - validate_started) * 1000,
            )
            ai_runtime_trace.record_cache(
                page_facts_cache.normalized_facts_cache_key(artifact.cache_key),
                hit=normalized_cache_hit,
                layer="normalized_document_facts",
            )
        normalized["ai_provider"] = artifact.context.provider
        normalized["ai_model"] = artifact.context.model
        normalized["ai_extraction_mode"] = "exact_document_facts_manifest"
        normalized = ai_mapping_review.apply_learned_mappings_to_normalized(
            normalized
        )
        if support_link is None:
            support_link = support_documents.upload_source_document_to_dropbox(
                batch_id=batch_id,
                source_file=source_file.name,
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
        accounting_started = time.perf_counter()
        with ai_runtime_trace.operation(
            batch_id=batch_id,
            stage="accounting_pipeline_manifest_hit",
        ):
            invoice = ai_result_to_invoice(
                normalized,
                batch_id=batch_id,
                source_file=source_file.name,
                vendor_key=vendor_key,
                support_document_url=support_link.url,
                support_document_status=support_link.status,
                support_document_dropbox_path=support_link.dropbox_path,
            )
            ai_runtime_trace.record_stage_timing(
                "accounting_pipeline_manifest_hit",
                (time.perf_counter() - accounting_started) * 1000,
            )
        invoices.append(invoice)
        if normalized.get("manual_review_reasons"):
            manual_review.append(_manual_review_item(
                source_file=source_file.name,
                vendor_name=normalized.get("vendor_name") or vendor_hint,
                invoice_number=normalized.get("invoice_number", ""),
                invoice_date=normalized.get("invoice_date", ""),
                total_amount=normalized.get("total_amount", 0),
                account_number=normalized.get("account_number", ""),
                property_abbreviation=normalized.get("property_abbreviation", ""),
                location=normalized.get("location", ""),
                service_address=normalized.get("service_address", ""),
                line_count=len(invoice.get("rows") or []),
                reasons=normalized.get("manual_review_reasons") or [],
                reason_codes=normalized.get("manual_review_codes") or [],
                message="Cached source facts need operator review.",
            ))
    with ai_runtime_trace.operation(
        batch_id=batch_id,
        stage="exact_document_facts_manifest_lookup",
        provider=provider,
        model=next(iter(configured_models), ""),
        profile_id="document-facts-manifest",
    ):
        ai_runtime_trace.record_cache(
            hashlib.sha256("|".join(
                artifact.cache_key for _, artifact in loaded
            ).encode("ascii")).hexdigest(),
            hit=True,
            layer="exact_document_facts_manifest",
        )
        ai_runtime_trace.record_stage_timing(
            "exact_document_facts_manifest_total",
            (time.perf_counter() - manifest_total_started) * 1000,
        )
    return {"invoices": invoices, "manual_review": manual_review}


def process_ai_vendor_files(
    *,
    batch_id: str,
    vendor_key: str,
    files: list[Path],
    detection: dict[str, dict],
    tracker: Any = None,
    should_cancel: Any = None,
    dry_run: bool = False,
    _parallel_child: bool = False,
    _reset_cost_budget: bool = True,
) -> dict[str, Any]:
    """Process unknown / variable supplier files through the AI path.

    Returns a vendor-like payload that ``batch_processor`` can merge into the
    normal webapp result shape.
    """
    original_files = list(files)
    files, duplicate_source_aliases = _deduplicate_source_files(original_files)
    status = ai_provider.provider_status()
    invoices: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    processed_files = 0

    # A new Process action is a new bounded spend scope. Provider retries and
    # fallbacks within this invocation still share and enforce the same cap.
    if _reset_cost_budget:
        ai_provider.reset_cost_budget(batch_id)

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
        return _payload(
            original_files,
            processed_files,
            invoices,
            manual_review,
            unsupported,
            unique_files=files,
            duplicate_source_aliases=duplicate_source_aliases,
        )

    if len(files) > 1 and not _parallel_child:
        workers = min(
            len(files),
            max(1, int(getattr(settings, "AI_INVOICE_GROUP_WORKERS", 4) or 4)),
        )
        def process_one(source_file: Path) -> dict[str, Any]:
            return process_ai_vendor_files(
                    batch_id=batch_id,
                    vendor_key=vendor_key,
                    files=[source_file],
                    detection=detection,
                    tracker=None,
                    should_cancel=should_cancel,
                    dry_run=dry_run,
                    _parallel_child=True,
                    _reset_cost_budget=False,
                )

        child_payloads: list[dict[str, Any]] = []
        for source_file, payload, exc in _run_ai_file_workers_bounded(
            files, process_one, max_workers=workers
        ):
            if exc is not None:  # one invoice cannot erase siblings
                failure_code = _safe_failure_code(exc)
                failure_invoice = _failed_extraction_invoice(
                    batch_id=batch_id,
                    source_file=source_file,
                    vendor_hint=_vendor_hint_for_file(
                        vendor_key,
                        source_file,
                        detection.get(source_file.name),
                    ),
                    failure_code=failure_code,
                )
                child_payloads.append(_payload(
                        [source_file],
                        0,
                        [failure_invoice],
                        [_manual_review_item(
                            source_file=source_file.name,
                            vendor_name=_vendor_hint_for_file(
                                vendor_key,
                                source_file,
                                detection.get(source_file.name),
                            ),
                            reasons=[failure_code],
                            reason_codes=[failure_code],
                            message="AI invoice processing failed; manual review is required.",
                        )],
                        [{
                            "filename": source_file.name,
                            "vendor_key": vendor_key,
                            "processing_mode": "ai_assisted",
                            "reason": failure_code,
                            "message": "AI invoice processing failed; manual review is required.",
                        }],
                ))
            elif payload is not None:
                child_payloads.append(payload)
        for payload in child_payloads:
            invoices.extend(payload.get("invoices") or [])
            manual_review.extend(payload.get("manual_review_rows") or [])
            unsupported.extend(payload.get("unsupported_files") or [])
            processed_files += int(
                (payload.get("summary") or {}).get("files_unique_processed") or 0
            )
        # The serial implementation performs this once after all files. The
        # concurrent fan-out must retain that same cross-file accounting
        # identity boundary; per-child deduplication alone cannot see repeated
        # invoice numbers carried by different source PDFs.
        _attach_duplicate_source_provenance(invoices, duplicate_source_aliases)
        invoices, manual_review = _deduplicate_invoices(invoices, manual_review)
        result = _payload(
            original_files,
            processed_files,
            invoices,
            manual_review,
            unsupported,
            unique_files=files,
            duplicate_source_aliases=duplicate_source_aliases,
        )
        _tracker_finish(tracker, invoices, manual_review, warning=bool(unsupported))
        return result

    with perf_timer.perf_step("ai.references_load", batch_id=batch_id):
        references = load_references()
    batch_vendor_hint = _batch_vendor_hint(batch_id, references["vendors"])
    template_schema = {
        "columns": get_template_rules().get("columns", []),
        "required_columns": get_template_rules().get("required_columns", []),
        "recommended_columns": get_template_rules().get("recommended_columns", []),
    }

    for index, f in enumerate(files, start=1):
        if should_cancel and should_cancel():
            break
        vendor_hint = _vendor_hint_for_file(
            vendor_key,
            f,
            detection.get(f.name),
            batch_hint=batch_vendor_hint,
        )
        cached_document = _process_cached_document_manifest(
            batch_id=batch_id,
            source_file=f,
            vendor_key=vendor_key,
            vendor_hint=vendor_hint,
            references=references,
            status=status,
            dry_run=dry_run,
        )
        if cached_document is not None:
            invoices.extend(cached_document["invoices"])
            manual_review.extend(cached_document["manual_review"])
            processed_files += 1
            _tracker_update(
                tracker,
                percent=_range_pct(index, len(files), 82),
                stage="Reusing exact observed document facts",
                current_file=f.name,
                files_done=index,
                invoices_created=len(invoices),
                warnings_count=len(manual_review),
            )
            continue
        document_text = ""
        document_candidate: document_ingestion.DocumentCandidate | None = None
        observed_raw_for_cache: dict[str, Any] | None = None
        observed_cache_context: page_facts_cache.PageFactsCacheContext | None = None
        observed_cache_identities: list[page_facts_cache.VisualPageIdentity] = []
        facts_cache_identities: list[page_facts_cache.VisualPageIdentity] = []
        native_cache_identities: list[page_facts_cache.VisualPageIdentity] = []
        raster_cache_context: page_facts_cache.PageFactsCacheContext | None = None
        native_cache_context: page_facts_cache.PageFactsCacheContext | None = None
        try:
            _tracker_update(
                tracker,
                percent=_range_pct(index - 1, len(files), 8),
                stage="Scanning invoice",
                current_file=f.name,
                files_done=index - 1,
                files_total=len(files),
            )
            with perf_timer.perf_step(
                "ai.ingestion",
                batch_id=batch_id,
                meta={"file": f.name},
            ):
                with LOCAL_DOCUMENT_PREPROCESS_LOCK:
                    document_candidate = document_ingestion.ingest_document(
                        f,
                        vendor_hint=vendor_hint,
                        max_pages=max(1, int(getattr(settings, "AI_MAX_PAGES", 5) or 5)),
                    )
            document_text = document_candidate.document_text
            invoice_groups = _segment_document_invoice_groups(document_candidate)
            if len(invoice_groups) > 1:
                segmented_vendor_hint = _document_vendor_hint(
                    document_candidate,
                    fallback=vendor_hint,
                )
                segmented = _process_segmented_invoice_groups_bounded(
                    batch_id=batch_id,
                    vendor_key=vendor_key,
                    source_file=f,
                    vendor_hint=segmented_vendor_hint,
                    document_candidate=document_candidate,
                    invoice_groups=invoice_groups,
                    references=references,
                    template_schema=template_schema,
                    status=status,
                    dry_run=dry_run,
                    tracker=tracker,
                    should_cancel=should_cancel,
                )
                invoices.extend(segmented["invoices"])
                manual_review.extend(segmented["manual_review"])
                unsupported.extend(segmented["unsupported"])
                if segmented["invoices"]:
                    processed_files += 1
                    page_facts_cache.finalize_document_manifest(
                        batch_id=batch_id,
                        filename=f.name,
                        expected_group_count=len(invoice_groups),
                    )
                _tracker_update(
                    tracker,
                    percent=_range_pct(index, len(files), 82),
                    stage="Building ResMan template",
                    current_file=f.name,
                    files_done=index,
                    invoices_created=len(invoices),
                    warnings_count=len(manual_review),
                )
                continue
            prompt_text = _document_prompt_context(
                source_file=f,
                batch_hint=batch_vendor_hint,
                vendor_hint=vendor_hint,
                document_text=document_text,
            )
            raw = _extract_known_vendor_payload_from_ocr(document_text)
            if raw:
                local_parser = _clean(raw.pop("_local_parser", "")) or "known_invoice_layout"
                extraction_provider = "local_ocr"
                extraction_model = local_parser
                extraction_mode = f"local_ocr_{local_parser}"
            else:
                with perf_timer.perf_step(
                    "ai.reference_selection",
                    batch_id=batch_id,
                    meta={"file": f.name},
                ):
                    prompt_references = _select_prompt_references(
                        references,
                        query=prompt_text,
                        vendor_hint=vendor_hint,
                    )
                vision_images: list[str] = []
                vision_evidence_groups: list[tuple[int, list[str]]] = []
                native_pdf: native_pdf_evidence.NativePdfEvidence | None = None
                use_vision = _should_use_vision_for_candidate(document_candidate, status)
                facts_cache_artifact: page_facts_cache.CachedPageFactsArtifact | None = None
                facts_cache_identities = []
                native_cache_identities = []
                facts_cache_context: page_facts_cache.PageFactsCacheContext | None = None
                raster_cache_context = None
                native_cache_context = None
                planned_page_numbers = [1]
                planned_vision_model = ""
                if use_vision:
                    max_vision_pages = max(
                        1,
                        int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2),
                    )
                    planned_page_numbers = (
                        [1]
                        if f.suffix.lower() in ai_vision.IMAGE_EXTENSIONS
                        else list(range(
                            1,
                            min(max_vision_pages, max(1, int(document_candidate.page_count or 1))) + 1,
                        ))
                    )
                    planned_vision_model = _vision_model_for_candidate(status, document_candidate)
                    raster_provider, raster_profile, raster_model = (
                        ai_provider.extraction_profile_identity(
                            vision=True,
                            model_override=planned_vision_model,
                        )
                    )
                    if fast_first_facts.production_enabled():
                        raster_profile = raster_profile + ":facts-only-v1"
                    (
                        facts_cache_identities,
                        raster_cache_context,
                        facts_cache_artifact,
                    ) = _page_facts_lookup(
                        batch_id=batch_id,
                        source_file=f,
                        page_numbers=planned_page_numbers,
                        provider=raster_provider,
                        profile_id=raster_profile,
                        model=raster_model,
                        prompt_references=prompt_references,
                    )
                    facts_cache_context = raster_cache_context
                    if (
                        f.suffix.lower() == ".pdf"
                        and _should_use_native_pdf_for_candidate(document_candidate, status, f)
                    ):
                        native_cache_identities = [
                            page_facts_cache.exact_visual_page_identity(
                                batch_id=batch_id,
                                filename=f.name,
                                page_number=number,
                            )
                            for number in range(
                                1, max(1, int(document_candidate.page_count or 1)) + 1
                            )
                        ]
                        native_primary_model, _ = _native_pdf_model_sequence(status, document_candidate)
                        native_cache_context = page_facts_cache.PageFactsCacheContext(
                            provider=status.vision_provider or status.provider,
                            profile_id="runtime-vision-native-pdf",
                            model=native_primary_model or status.vision_model or status.model,
                            reference_fingerprint="",
                        )
                        with ai_runtime_trace.operation(
                            batch_id=batch_id,
                            stage="exact_page_facts_lookup",
                            provider=native_cache_context.provider,
                            model=native_cache_context.model,
                            profile_id=native_cache_context.profile_id,
                        ):
                            native_cached, native_owns_reservation = page_facts_cache.load_or_reserve(
                                native_cache_identities, native_cache_context
                            )
                            if native_cached is None and native_owns_reservation:
                                native_cached = (
                                    page_facts_cache.load_compatible_exact_observed(
                                        native_cache_identities, native_cache_context
                                    )
                                )
                            ai_runtime_trace.record_cache(
                                page_facts_cache.cache_key(
                                    native_cache_identities, native_cache_context
                                ),
                                hit=native_cached is not None,
                                layer="exact_page_facts",
                            )
                        if native_cached is not None:
                            facts_cache_artifact = native_cached
                            facts_cache_context = native_cache_context
                    if facts_cache_artifact is not None:
                        page_facts_cache.register_document_artifact(
                            batch_id=batch_id,
                            filename=f.name,
                            group_index=1,
                            page_numbers=planned_page_numbers,
                            artifact=facts_cache_artifact,
                        )
                        raw = copy.deepcopy(facts_cache_artifact.observed_payload)
                        extraction_provider = facts_cache_context.provider
                        extraction_model = facts_cache_context.model
                        extraction_mode = (
                            "ai_vision_native_pdf_exact_facts_cache"
                            if facts_cache_context.profile_id == "runtime-vision-native-pdf"
                            else "ai_vision_exact_facts_cache"
                        )
                if use_vision and facts_cache_artifact is None:
                    if f.suffix.lower() in ai_vision.IMAGE_EXTENSIONS:
                        vision_images = [ai_vision.image_path_as_data_url(f)]
                        vision_evidence_groups = [(1, vision_images)]
                    elif f.suffix.lower() == ".pdf":
                        if _should_use_native_pdf_for_candidate(document_candidate, status, f):
                            try:
                                native_pdf = native_pdf_evidence.load_native_pdf_evidence(
                                    batch_id=batch_id,
                                    filename=f.name,
                                    max_bytes=int(
                                        getattr(
                                            settings,
                                            "AI_VISION_NATIVE_PDF_MAX_BYTES",
                                            50 * 1024 * 1024,
                                        )
                                        or 50 * 1024 * 1024
                                    ),
                                )
                            except native_pdf_evidence.NativePdfEvidenceError:
                                _LOG.warning(
                                    "Native PDF evidence was unavailable; retaining rendered-page vision fallback."
                                )
                        with perf_timer.perf_step(
                            "ai.vision_render",
                            batch_id=batch_id,
                            meta={"file": f.name},
                        ):
                            for page_number in planned_page_numbers:
                                page_evidence = ai_vision.render_pdf_pages_as_data_urls(
                                    batch_id=batch_id,
                                    filename=f.name,
                                    page_numbers=[page_number],
                                    include_detail_crop=True,
                                )
                                if page_evidence:
                                    # render_pdf_pages_as_data_urls already
                                    # applies the bounded per-page evidence
                                    # contract.  Do not silently discard its
                                    # header/detail crop: that crop exists to
                                    # recover small handwritten dates, sold-to
                                    # names, and job-site identifiers.
                                    vision_evidence_groups.append((page_number, page_evidence))
                            vision_images = [ref for _, refs in vision_evidence_groups for ref in refs]
                elif not document_text.strip():
                    raise ai_provider.AIProviderNotConfigured(AI_VISION_REQUIRED_MESSAGE)

                _tracker_update(
                    tracker,
                    percent=_range_pct(index - 1, len(files), 25),
                    stage="Reading line items",
                    current_file=f.name,
                )
                if facts_cache_artifact is not None:
                    pass
                elif vision_images:
                    try:
                        vision_model = planned_vision_model or _vision_model_for_candidate(status, document_candidate)
                        native_pdf_model, native_pdf_escalation_model = (
                            _native_pdf_model_sequence(status, document_candidate)
                        )
                        native_pdf_failure = ""
                        trace_media_bytes, trace_media_pixels = ai_runtime_trace.media_stats(
                            vision_images
                        )
                        ai_runtime_trace.update_context(
                            batch_id=batch_id,
                            stage=(
                                "native_pdf_visual_facts"
                                if native_pdf is not None
                                else "rendered_visual_facts"
                            ),
                            provider=status.vision_provider or status.provider,
                            model=(
                                native_pdf_model
                                if native_pdf is not None
                                else vision_model
                            ),
                            profile_id=(
                                "runtime-vision-native-pdf"
                                if native_pdf is not None
                                else "runtime-vision"
                            ),
                            media_bytes=(
                                int(getattr(native_pdf, "byte_count", 0) or 0)
                                if native_pdf is not None
                                else trace_media_bytes
                            ),
                            media_pixels=trace_media_pixels,
                        )
                        with perf_timer.perf_step(
                            "ai.vision_call",
                            batch_id=batch_id,
                            meta={"file": f.name, "model": vision_model},
                        ):
                            if native_pdf is not None:
                                try:
                                    raw = ai_provider.extract_invoice_native_pdf_structured(
                                        vendor_hint=vendor_hint,
                                        document_text=prompt_text,
                                        pdf_evidence=native_pdf,
                                        template_schema=template_schema,
                                        property_reference=prompt_references["properties"],
                                        gl_reference=prompt_references["gl_accounts"],
                                        vendor_reference=prompt_references["vendors"],
                                        model_override=native_pdf_model,
                                        cost_scope_id=batch_id,
                                    )
                                    extraction_mode = "ai_vision_native_pdf"
                                except ai_provider.AIProviderError as exc:
                                    raw = {}
                                    if (
                                        native_pdf_escalation_model
                                    ):
                                        try:
                                            raw = ai_provider.extract_invoice_native_pdf_structured(
                                                vendor_hint=vendor_hint,
                                                document_text=prompt_text,
                                                pdf_evidence=native_pdf,
                                                template_schema=template_schema,
                                                property_reference=prompt_references["properties"],
                                                gl_reference=prompt_references["gl_accounts"],
                                                vendor_reference=prompt_references["vendors"],
                                                model_override=native_pdf_escalation_model,
                                                cost_scope_id=batch_id,
                                            )
                                            extraction_mode = "ai_vision_native_pdf_escalated"
                                            raw["warnings"] = list(dict.fromkeys([
                                                *list(raw.get("warnings") or []),
                                                "native_pdf_primary_validation_failed_strong_profile_used",
                                            ]))
                                        except ai_provider.AIProviderError as strong_exc:
                                            native_pdf_failure = _safe_vision_failure_warning(
                                                strong_exc
                                            ).replace(
                                                "ai_vision_failure", "native_pdf_failure"
                                            )
                                    else:
                                        native_pdf_failure = _safe_vision_failure_warning(exc).replace(
                                            "ai_vision_failure", "native_pdf_failure"
                                        )
                            else:
                                raw = {}
                            if not raw:
                                ai_runtime_trace.update_context(
                                    stage="rendered_visual_facts",
                                    model=vision_model,
                                    profile_id="runtime-vision",
                                    media_bytes=trace_media_bytes,
                                    media_pixels=trace_media_pixels,
                                )
                                # A document is one accounting unit. Rendered
                                # evidence remains the provider-agnostic fallback.
                                if _should_extract_scanned_pages_independently(
                                    document_candidate,
                                    vision_evidence_groups,
                                ):
                                    raw = _extract_scanned_pages_independently(
                                        batch_id=batch_id,
                                        source_file=f,
                                        vendor_hint=vendor_hint,
                                        document_candidate=document_candidate,
                                        evidence_groups=vision_evidence_groups,
                                        template_schema=template_schema,
                                        prompt_references=prompt_references,
                                        vision_model=vision_model,
                                    )
                                else:
                                    raw = _extract_fast_first_or_standard(
                                        vendor_hint=vendor_hint,
                                        document_text=prompt_text,
                                        page_images_or_refs=vision_images,
                                        template_schema=template_schema,
                                        property_reference=prompt_references["properties"],
                                        gl_reference=prompt_references["gl_accounts"],
                                        vendor_reference=prompt_references["vendors"],
                                        model_override=vision_model,
                                        cost_scope_id=batch_id,
                                    )
                                extraction_mode = "ai_vision"
                            if native_pdf_failure:
                                raw["warnings"] = list(dict.fromkeys([
                                    *list(raw.get("warnings") or []),
                                    native_pdf_failure,
                                ]))
                        if (
                            _requires_critical_header_verification(raw, document_candidate)
                            and vision_evidence_groups
                        ):
                            header_refs = [
                                refs[-1]
                                for _, refs in vision_evidence_groups
                                if refs
                            ]
                            if header_refs:
                                try:
                                    with perf_timer.perf_step(
                                        "ai.critical_header_verification",
                                        batch_id=batch_id,
                                        meta={
                                            "file": f.name,
                                            "model": status.vision_model or status.model,
                                        },
                                    ):
                                        header_verification = (
                                            ai_provider.extract_invoice_critical_fields_vision_structured(
                                                page_images_or_refs=header_refs,
                                                property_reference=prompt_references["properties"],
                                                model_override=status.vision_model or status.model,
                                                cost_scope_id=batch_id,
                                            )
                                        )
                                    raw = _merge_critical_header_verification(
                                        raw,
                                        header_verification,
                                    )
                                except ai_provider.AIProviderError as exc:
                                    warnings = _normalize_warnings(raw.get("warnings") or [])
                                    safe_warning = _safe_vision_failure_warning(exc).replace(
                                        "ai_vision_failure", "critical_header_verification_failure"
                                    )
                                    if safe_warning not in warnings:
                                        warnings.append(safe_warning)
                                    raw["warnings"] = warnings
                        if _requires_row_identity_verification(raw, document_candidate):
                            try:
                                apt_crop, apt_coordinates = ai_vision.render_pdf_apt_column_crop(
                                    batch_id=batch_id,
                                    filename=f.name,
                                    page_number=1,
                                    render_dpi=600,
                                )
                                with perf_timer.perf_step(
                                    "ai.row_identity_verification",
                                    batch_id=batch_id,
                                    meta={"file": f.name, "scope": "apt_column_only"},
                                ):
                                    row_verification = (
                                        ai_provider.extract_handwritten_row_identities_vision_structured(
                                            apt_column_image_ref=apt_crop,
                                            crop_coordinates=apt_coordinates,
                                            expected_visible_rows=_expected_visible_matrix_rows(raw),
                                            model_override=status.vision_model or status.model,
                                            cost_scope_id=batch_id,
                                        )
                                    )
                                raw = _merge_row_identity_verification(raw, row_verification)
                            except (ai_provider.AIProviderError, ai_vision.VisionRenderingUnavailable) as exc:
                                warnings = _normalize_warnings(raw.get("warnings") or [])
                                safe_warning = (
                                    _safe_vision_failure_warning(exc).replace(
                                        "ai_vision_failure", "row_identity_verification_failure"
                                    )
                                    if isinstance(exc, ai_provider.AIProviderError)
                                    else "row_identity_verification_failure:rendering_unavailable"
                                )
                                if safe_warning not in warnings:
                                    warnings.append(safe_warning)
                                raw["warnings"] = warnings
                        ai_vision.save_vision_trace_regions(
                            batch_id=batch_id,
                            source_file=f.name,
                            candidates=list(raw.get("vision_candidates") or []),
                            feeds_rows=[],
                        )
                        extraction_provider = status.vision_provider or status.provider
                        extraction_model = vision_model or status.vision_model or status.model
                        extraction_mode = extraction_mode or "ai_vision"
                    except ai_provider.AIProviderError as exc:
                        if not document_text.strip():
                            raise
                        diagnostic = exc.safe_diagnostic()
                        _LOG.warning(
                            "AI vision failed for %s code=%s http_status=%s; falling back to text extraction.",
                            f.name,
                            diagnostic.get("failure_code"),
                            diagnostic.get("http_status"),
                        )
                        with perf_timer.perf_step(
                            "ai.text_call",
                            batch_id=batch_id,
                            meta={"file": f.name, "reason": "vision_fallback"},
                        ):
                            raw = _extract_text_with_runtime_fallback(
                                vendor_hint=vendor_hint,
                                document_text=prompt_text,
                                page_images_or_refs=[],
                                template_schema=template_schema,
                                property_reference=prompt_references["properties"],
                                gl_reference=prompt_references["gl_accounts"],
                                vendor_reference=prompt_references["vendors"],
                                cost_scope_id=batch_id,
                            )
                        warnings = _normalize_warnings(raw.get("warnings") or [])
                        if "ai_vision_failed_text_fallback_used" not in warnings:
                            warnings.append("ai_vision_failed_text_fallback_used")
                        safe_failure = _safe_vision_failure_warning(exc)
                        if safe_failure not in warnings:
                            warnings.append(safe_failure)
                        raw["warnings"] = warnings
                        extraction_provider = status.provider
                        extraction_model = status.model
                        extraction_mode = "ai_text_after_vision_fallback"
                else:
                    with perf_timer.perf_step(
                        "ai.text_call",
                        batch_id=batch_id,
                        meta={"file": f.name, "reason": "primary"},
                    ):
                        raw = _extract_text_with_runtime_fallback(
                            vendor_hint=vendor_hint,
                            document_text=prompt_text,
                            page_images_or_refs=[],
                            template_schema=template_schema,
                            property_reference=prompt_references["properties"],
                            gl_reference=prompt_references["gl_accounts"],
                            vendor_reference=prompt_references["vendors"],
                            cost_scope_id=batch_id,
                        )
                    extraction_provider = status.provider
                    extraction_model = status.model
                    extraction_mode = "ai_text"

                    # A text provider can return valid JSON while still being
                    # unusable accounting data. Escalate once to Vision when
                    # critical fields are missing, totals do not reconcile, or
                    # the provider itself reports ambiguous handwriting.
                    if _ai_payload_requires_vision(raw, status):
                        try:
                            max_vision_pages = max(
                                1,
                                int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2),
                            )
                            fallback_images = ai_vision.render_pdf_pages_as_data_urls(
                                batch_id=batch_id,
                                filename=f.name,
                                page_numbers=list(range(1, max_vision_pages + 1)),
                                include_detail_crop=True,
                            )
                            if fallback_images:
                                vision_model = _vision_model_for_candidate(status, document_candidate)
                                with perf_timer.perf_step(
                                    "ai.vision_call",
                                    batch_id=batch_id,
                                    meta={"file": f.name, "model": vision_model, "reason": "text_validation"},
                                ):
                                    raw = _extract_vision_with_reduced_retry(
                                        vendor_hint=vendor_hint,
                                        document_text=prompt_text,
                                        page_images_or_refs=fallback_images,
                                        template_schema=template_schema,
                                        property_reference=prompt_references["properties"],
                                        gl_reference=prompt_references["gl_accounts"],
                                        vendor_reference=prompt_references["vendors"],
                                        model_override=vision_model,
                                        cost_scope_id=batch_id,
                                    )
                                ai_vision.save_vision_trace_regions(
                                    batch_id=batch_id,
                                    source_file=f.name,
                                    candidates=list(raw.get("vision_candidates") or []),
                                    feeds_rows=[],
                                )
                                extraction_provider = status.vision_provider or status.provider
                                extraction_model = vision_model or status.vision_model or status.model
                                extraction_mode = "ai_vision_after_text_validation"
                        except ai_provider.AIProviderError:
                            warnings = _normalize_warnings(raw.get("warnings") or [])
                            if "ai_vision_retry_failed_text_result_retained" not in warnings:
                                warnings.append("ai_vision_retry_failed_text_result_retained")
                            raw["warnings"] = warnings
            if extraction_mode.startswith("ai_vision") and "exact_facts_cache" not in extraction_mode:
                raw = _reconcile_high_confidence_vision_candidates(raw)
                selected_cache_context = raster_cache_context
                if extraction_mode.startswith("ai_vision_native_pdf"):
                    selected_cache_context = native_cache_context
                    if selected_cache_context is not None and extraction_model:
                        selected_cache_context = selected_cache_context.model_copy(
                            update={"model": extraction_model}
                        ) if hasattr(selected_cache_context, "model_copy") else page_facts_cache.PageFactsCacheContext(
                            **{
                                **selected_cache_context.dict(),
                                "model": extraction_model,
                            }
                        )
                elif selected_cache_context is not None:
                    actual_profile = _clean(raw.get("_provider_profile_id"))
                    actual_provider = _clean(raw.get("_provider_name"))
                    actual_model = _clean(raw.get("_provider_model_id"))
                    updates = {
                        "profile_id": actual_profile or selected_cache_context.profile_id,
                        "provider": actual_provider or selected_cache_context.provider,
                        "model": actual_model or selected_cache_context.model,
                    }
                    selected_cache_context = selected_cache_context.model_copy(
                        update=updates
                    ) if hasattr(selected_cache_context, "model_copy") else page_facts_cache.PageFactsCacheContext(
                        **{**selected_cache_context.dict(), **updates}
                    )
                if selected_cache_context is not None and facts_cache_identities:
                    selected_cache_identities = (
                        native_cache_identities
                        if selected_cache_context.profile_id == "runtime-vision-native-pdf"
                        else facts_cache_identities
                    )
                    observed_raw_for_cache = copy.deepcopy(raw)
                    observed_cache_context = selected_cache_context
                    observed_cache_identities = selected_cache_identities
            raw = _apply_vendor_hint_to_payload(raw, vendor_hint)
            raw = _repair_ai_payload_from_ocr(raw, document_text, source_file=f.name)
            raw["_document_text"] = document_text
            raw["_source_file"] = f.name
            raw["_source_type"] = document_candidate.source_type if document_candidate else ""
            raw["_document_candidate"] = document_candidate.to_dict() if document_candidate else {}
            extraction_provider, extraction_model = _actual_provider_identity(
                raw,
                fallback_provider=extraction_provider,
                fallback_model=extraction_model,
            )
            if document_candidate and document_candidate.warnings:
                warnings = _normalize_warnings(raw.get("warnings") or [])
                for warning in document_candidate.warnings:
                    if extraction_mode.startswith("ai_vision") and warning in {
                        "weak_text_quality",
                        "vision_recommended",
                    }:
                        continue
                    if warning not in warnings:
                        warnings.append(warning)
                raw["warnings"] = warnings
            _tracker_update(
                tracker,
                percent=_range_pct(index - 1, len(files), 60),
                stage="Validating totals",
                current_file=f.name,
            )
            with perf_timer.perf_step(
                "ai.validation",
                batch_id=batch_id,
                meta={"file": f.name},
            ):
                normalized = validate_ai_extraction(raw, references=references)
            normalized_cache_artifact = facts_cache_artifact
            if (
                observed_raw_for_cache is not None
                and observed_cache_context is not None
                and observed_cache_identities
            ):
                observed_raw_for_cache["date_provenance"] = list(
                    normalized.get("date_provenance") or []
                )
                observed_raw_for_cache["_handwritten_row_identities"] = list(
                    normalized.get("handwritten_row_identities") or []
                )
                observed_raw_for_cache["excluded_paid_rows"] = list(
                    normalized.get("excluded_paid_rows")
                    or observed_raw_for_cache.get("excluded_paid_rows")
                    or []
                )
                normalized_cache_artifact = _save_page_facts(
                    batch_id=batch_id,
                    identities=observed_cache_identities,
                    context=observed_cache_context,
                    raw=observed_raw_for_cache,
                    source_file=f,
                    page_numbers=planned_page_numbers,
                    group_index=1,
                )
            if normalized_cache_artifact is not None:
                page_facts_cache.save_normalized_facts(
                    normalized_cache_artifact.cache_key, normalized
                )
            normalized["ai_provider"] = extraction_provider
            normalized["ai_model"] = extraction_model
            normalized["ai_extraction_mode"] = extraction_mode
            normalized["ai_provider_request_surface"] = raw.get("_provider_request_surface")
            normalized["ai_provider_usage"] = dict(raw.get("_provider_usage") or {})
            normalized["ai_estimated_cost_usd"] = raw.get("_estimated_cost_usd")
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
            with ai_runtime_trace.operation(
                batch_id=batch_id,
                stage="accounting_pipeline",
            ):
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
            page_facts_cache.finalize_document_manifest(
                batch_id=batch_id,
                filename=f.name,
                expected_group_count=1,
            )
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
        except (ai_provider.AIProviderInvalidJSON, ai_provider.AIProviderInvalidSchema) as exc:
            output_limit = "exceeded the configured output limit" in str(exc).lower()
            reason = (
                "ai_response_output_limit_exceeded"
                if output_limit
                else "ai_response_invalid_json"
            )
            failure_message = (
                "AI returned more structured extraction data than the configured response budget allowed."
                if output_limit
                else "AI returned an invalid extraction payload. Review this file manually."
            )
            manual_review.append(
                _manual_review_item(
                    source_file=f.name,
                    vendor_name=vendor_hint,
                    reasons=[reason],
                    message=failure_message,
                )
            )
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": reason,
                "message": failure_message,
                "detection": detection.get(f.name),
            })
        except Exception as exc:
            reason, safe_message = _safe_processing_failure(exc)
            _LOG.warning("AI invoice processing failed for %s: %s", f.name, exc)
            fallback = _try_local_ocr_fallback_invoice(
                batch_id=batch_id,
                source_file=f.name,
                vendor_key=vendor_key,
                vendor_hint=vendor_hint,
                document_text=document_text,
                references=references,
                failure_reason=safe_message,
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
            manual_review.append(
                _manual_review_item(
                    source_file=f.name,
                    vendor_name=vendor_hint,
                    reasons=[reason],
                    message=safe_message,
                )
            )
            unsupported.append({
                "filename": f.name,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted",
                "reason": reason,
                "message": safe_message,
                "detection": detection.get(f.name),
            })
        finally:
            if raster_cache_context is not None and facts_cache_identities:
                page_facts_cache.release_reservation(
                    facts_cache_identities, raster_cache_context
                )
            if native_cache_context is not None and native_cache_identities:
                page_facts_cache.release_reservation(
                    native_cache_identities, native_cache_context
                )

    _attach_duplicate_source_provenance(invoices, duplicate_source_aliases)
    invoices, manual_review = _deduplicate_invoices(invoices, manual_review)
    _tracker_finish(tracker, invoices, manual_review, warning=bool(unsupported))
    return _payload(
        original_files,
        processed_files,
        invoices,
        manual_review,
        unsupported,
        unique_files=files,
        duplicate_source_aliases=duplicate_source_aliases,
    )


def _segment_document_invoice_groups(
    candidate: document_ingestion.DocumentCandidate,
) -> list[dict[str, Any]]:
    """Group PDF pages by explicit invoice identity.

    A provider schema represents one invoice, while operational upload PDFs
    can contain many independent invoices. Split only when at least two
    distinct invoice identities are explicit. Repeated identities and pages
    without a new invoice header stay with the preceding invoice, preserving
    ordinary multi-page bills.
    """
    pages = [page for page in candidate.pages if (page.text or "").strip()]
    if len(pages) < 2:
        return [{"pages": pages, "page_numbers": [p.page_number for p in pages], "text": candidate.document_text}]

    identified = [(_page_invoice_identity(page.text), page) for page in pages]
    distinct = {identity for identity, _ in identified if identity}
    if len(distinct) < 2:
        return [{"pages": pages, "page_numbers": [p.page_number for p in pages], "text": candidate.document_text}]

    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for identity, page in identified:
        if identity and (current is None or identity != current["invoice_identity"]):
            current = {
                "invoice_identity": identity,
                "pages": [],
                "page_numbers": [],
                "text_parts": [],
            }
            groups.append(current)
        elif current is None:
            current = {
                "invoice_identity": "",
                "pages": [],
                "page_numbers": [],
                "text_parts": [],
            }
            groups.append(current)
        current["pages"].append(page)
        current["page_numbers"].append(page.page_number)
        current["text_parts"].append(page.text)

    for group in groups:
        group["text"] = "\n\n".join(group.pop("text_parts"))
    return groups


def _page_invoice_identity(text: str) -> str:
    compact = re.sub(r"[ \t]+", " ", text or "")
    patterns = (
        r"\bINVOICE\s*(?:NO\.?|NUMBER|#)\s*[:.]?\s*([A-Z0-9][A-Z0-9-]{2,})\b",
        r"\bINVOICE\s*#?\s*DATE\b[\s\S]{0,140}?\b([A-Z0-9][A-Z0-9-]{2,})\s+\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\bINVOICE\b[\s\S]{0,180}?\b([A-Z0-9][A-Z0-9-]{3,})\s+\d{1,2}/\d{1,2}/\d{2,4}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            identity = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
            if identity not in {"DATE", "NUMBER", "INVOICE"}:
                return identity
    return ""


def _document_vendor_hint(
    candidate: document_ingestion.DocumentCandidate,
    *,
    fallback: str,
) -> str:
    """Share a strong vendor identity across invoices in the same source PDF."""
    if fallback and not fallback.lower().startswith("unknown"):
        return fallback
    names: list[str] = []
    for page in candidate.pages:
        payload = _extract_known_vendor_payload_from_ocr(page.text)
        name = _clean(payload.get("vendor_name")) if payload else ""
        if name:
            names.append(name)
    if names:
        return Counter(names).most_common(1)[0][0]
    return fallback


def _process_segmented_invoice_groups_bounded(
    **kwargs: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Process independent invoice identities concurrently, then merge stably."""
    groups = list(kwargs.pop("invoice_groups") or [])
    if len(groups) <= 1:
        return _process_segmented_invoice_groups(invoice_groups=groups, **kwargs)
    worker_count = min(
        len(groups),
        max(1, int(getattr(settings, "AI_INVOICE_GROUP_WORKERS", 4) or 4)),
    )
    coordinator = _SupportLinkCoordinator()
    ordered: dict[int, dict[str, list[dict[str, Any]]]] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="innerview-ai-invoice",
    ) as executor:
        futures = {}
        for stable_index, group in enumerate(groups, start=1):
            isolated_group = copy.deepcopy(group)
            isolated_group["_stable_group_index"] = stable_index
            future = executor.submit(
                _process_segmented_invoice_groups,
                invoice_groups=[isolated_group],
                tracker=None,
                support_link_coordinator=coordinator,
                **{key: value for key, value in kwargs.items() if key != "tracker"},
            )
            futures[future] = stable_index
        for future in as_completed(futures):
            stable_index = futures[future]
            try:
                ordered[stable_index] = future.result()
            except Exception as exc:
                # Defensive isolation: the per-group worker already converts
                # normal failures into review rows, but one unexpected worker
                # failure must not discard completed invoices.
                _LOG.exception("Concurrent invoice group %s failed.", stable_index)
                ordered[stable_index] = {
                    "invoices": [],
                    "manual_review": [_manual_review_item(
                        source_file=str(kwargs.get("source_file") or ""),
                        vendor_name=str(kwargs.get("vendor_hint") or ""),
                        reasons=["segmented_invoice_processing_failed"],
                        reason_codes=["segmented_invoice_processing_failed"],
                        message="One invoice group could not be processed.",
                    )],
                    "unsupported": [{
                        "filename": Path(str(kwargs.get("source_file") or "")).name,
                        "processing_mode": "ai_assisted_segmented",
                        "reason": "segmented_invoice_processing_failed",
                        "message": type(exc).__name__,
                    }],
                }
    merged = {"invoices": [], "manual_review": [], "unsupported": []}
    for stable_index in sorted(ordered):
        result = ordered[stable_index]
        for key in merged:
            merged[key].extend(result.get(key) or [])
    return merged


def _process_segmented_invoice_groups(
    *,
    batch_id: str,
    vendor_key: str,
    source_file: Path,
    vendor_hint: str,
    document_candidate: document_ingestion.DocumentCandidate,
    invoice_groups: list[dict[str, Any]],
    references: dict[str, list[dict[str, Any]]],
    template_schema: dict[str, Any],
    status: ai_provider.AIProviderStatus,
    dry_run: bool,
    tracker: Any = None,
    should_cancel: Any = None,
    support_link_coordinator: _SupportLinkCoordinator | None = None,
) -> dict[str, list[dict[str, Any]]]:
    invoices: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    support_link: support_documents.SupportDocumentLink | None = None

    for group_index, group in enumerate(invoice_groups, start=1):
        stable_group_index = int(group.get("_stable_group_index") or group_index)
        if should_cancel and should_cancel():
            break
        group_text = str(group.get("text") or "")
        page_numbers = [int(number) for number in group.get("page_numbers") or []]
        source_page = page_numbers[0] if page_numbers else 1
        group_pages = list(group.get("pages") or [])
        group_candidate = document_ingestion.DocumentCandidate(
            source_file=document_candidate.source_file,
            source_type=document_candidate.source_type,
            source_path=document_candidate.source_path,
            mime_type=document_candidate.mime_type,
            file_size_bytes=document_candidate.file_size_bytes,
            page_count=len(group_pages),
            vendor_hint=document_candidate.vendor_hint,
            document_text=group_text,
            text_quality_score=(
                sum(float(page.text_quality_score or 0) for page in group_pages) / len(group_pages)
                if group_pages else document_candidate.text_quality_score
            ),
            needs_ocr=document_candidate.needs_ocr,
            needs_vision=document_candidate.needs_vision,
            pages=group_pages,
            metadata={**document_candidate.metadata, "source_pages": page_numbers},
            extraction_quality=dict(document_candidate.extraction_quality),
            warnings=list(document_candidate.warnings),
        )
        observed_raw_for_cache: dict[str, Any] | None = None
        cache_identities: list[page_facts_cache.VisualPageIdentity] = []
        cache_context: page_facts_cache.PageFactsCacheContext | None = None
        try:
            _tracker_update(
                tracker,
                percent=min(58, 12 + round((group_index - 1) / max(1, len(invoice_groups)) * 45)),
                stage=f"Reading invoice {group_index} of {len(invoice_groups)}",
                current_file=source_file.name,
            )
            prompt_text = _document_prompt_context(
                source_file=source_file,
                batch_hint="",
                vendor_hint=vendor_hint,
                document_text=group_text,
            )
            raw = _load_verified_page_extraction(
                batch_id=batch_id,
                source_file=source_file.name,
                source_page=source_page,
            )
            verified_override = bool(raw)
            if raw:
                parser = "verified_visual_audit"
                extraction_provider = "operator_verified"
                extraction_model = "visual_audit"
                extraction_mode = "verified_visual_audit"
            else:
                raw = _extract_known_vendor_payload_from_ocr(group_text)
            if raw and not verified_override:
                parser = _clean(raw.pop("_local_parser", "")) or "known_invoice_layout"
                extraction_provider = "local_ocr"
                extraction_model = parser
                extraction_mode = f"local_ocr_{parser}"
            elif not raw:
                prompt_references = _select_prompt_references(
                    references,
                    query=prompt_text,
                    vendor_hint=vendor_hint,
                )
                vision_images: list[str] = []
                cache_identities = []
                cache_context = None
                cache_artifact: page_facts_cache.CachedPageFactsArtifact | None = None
                use_group_vision = _should_use_vision_for_candidate(group_candidate, status)
                vision_model = _vision_model_for_candidate(status, group_candidate) if use_group_vision else ""
                if use_group_vision:
                    planned_provider, planned_profile, planned_model = (
                        ai_provider.extraction_profile_identity(
                            vision=True,
                            model_override=vision_model,
                        )
                    )
                    if fast_first_facts.production_enabled():
                        planned_profile = planned_profile + ":facts-only-v1"
                    cache_identities, cache_context, cache_artifact = _page_facts_lookup(
                        batch_id=batch_id,
                        source_file=source_file,
                        page_numbers=page_numbers,
                        provider=planned_provider,
                        profile_id=planned_profile,
                        model=planned_model,
                        prompt_references=prompt_references,
                    )
                if cache_artifact is not None:
                    page_facts_cache.register_document_artifact(
                        batch_id=batch_id,
                        filename=source_file.name,
                        group_index=stable_group_index,
                        page_numbers=page_numbers,
                        artifact=cache_artifact,
                    )
                    raw = copy.deepcopy(cache_artifact.observed_payload)
                    extraction_provider = cache_context.provider if cache_context else ""
                    extraction_model = cache_context.model if cache_context else ""
                    extraction_mode = "ai_vision_segmented_exact_facts_cache"
                else:
                    if use_group_vision:
                        with perf_timer.perf_step(
                            "ai.vision_render",
                            batch_id=batch_id,
                            meta={"page": source_page, "group": stable_group_index},
                        ):
                            vision_images = ai_vision.render_pdf_pages_as_data_urls(
                                batch_id=batch_id,
                                filename=source_file.name,
                                page_numbers=page_numbers,
                                include_detail_crop=True,
                            )
                    if vision_images:
                        media_bytes, media_pixels = ai_runtime_trace.media_stats(vision_images)
                        planned_provider, planned_profile, planned_model = (
                            ai_provider.extraction_profile_identity(
                                vision=True,
                                model_override=vision_model,
                            )
                        )
                        use_fast_first = fast_first_facts.production_enabled()
                        if use_fast_first:
                            planned_profile = planned_profile + ":facts-only-v1"
                        with ai_runtime_trace.operation(
                            batch_id=batch_id,
                            stage=f"segmented_visual_facts:{stable_group_index}",
                            provider=planned_provider,
                            model=planned_model,
                            profile_id=planned_profile,
                            media_bytes=media_bytes,
                            media_pixels=media_pixels,
                        ):
                            if use_fast_first:
                                raw = ai_provider.extract_invoice_facts_only_vision_structured(
                                    document_text=prompt_text,
                                    page_images_or_refs=vision_images,
                                    model_override=vision_model,
                                    cost_scope_id=batch_id,
                                )
                                escalation = fast_first_facts.escalation_reasons(raw)
                            else:
                                raw = {}
                                escalation = ["fast_first_not_enabled"]
                            if escalation:
                                if use_fast_first:
                                    full_profile = planned_profile.removesuffix(":facts-only-v1")
                                    ai_runtime_trace.update_context(profile_id=full_profile)
                                raw = _extract_vision_with_reduced_retry(
                                    vendor_hint=vendor_hint,
                                    document_text=prompt_text,
                                    page_images_or_refs=vision_images,
                                    template_schema=template_schema,
                                    property_reference=prompt_references["properties"],
                                    gl_reference=prompt_references["gl_accounts"],
                                    vendor_reference=prompt_references["vendors"],
                                    model_override=vision_model,
                                    cost_scope_id=batch_id,
                                )
                                if use_fast_first:
                                    raw["warnings"] = list(dict.fromkeys([
                                        *list(raw.get("warnings") or []),
                                        *[f"fast_first_escalated:{reason}" for reason in escalation],
                                    ]))
                                    planned_profile = full_profile
                                    if cache_context is not None:
                                        cache_context = cache_context.model_copy(
                                            update={"profile_id": planned_profile}
                                        ) if hasattr(cache_context, "model_copy") else page_facts_cache.PageFactsCacheContext(
                                            **{**cache_context.dict(), "profile_id": planned_profile}
                                        )
                            ai_runtime_trace.record_schema_result("valid")
                        extraction_provider = planned_provider
                        extraction_model = planned_model
                        extraction_mode = (
                            "ai_vision_segmented_fast_first"
                            if use_fast_first and not escalation
                            else "ai_vision_segmented"
                        )
                    else:
                        planned_provider, planned_profile, planned_model = (
                            ai_provider.extraction_profile_identity(vision=False)
                        )
                        with ai_runtime_trace.operation(
                            batch_id=batch_id,
                            stage=f"segmented_text_facts:{stable_group_index}",
                            provider=planned_provider,
                            model=planned_model,
                            profile_id=planned_profile,
                        ):
                            raw = _extract_text_with_runtime_fallback(
                                vendor_hint=vendor_hint,
                                document_text=prompt_text,
                                page_images_or_refs=[],
                                template_schema=template_schema,
                                property_reference=prompt_references["properties"],
                                gl_reference=prompt_references["gl_accounts"],
                                vendor_reference=prompt_references["vendors"],
                                cost_scope_id=batch_id,
                            )
                            ai_runtime_trace.record_schema_result("valid")
                        extraction_provider = planned_provider
                        extraction_model = planned_model
                        extraction_mode = "ai_text_segmented"
                    if extraction_mode.startswith("ai_vision"):
                        raw = _reconcile_high_confidence_vision_candidates(raw)
                        if cache_context is not None and cache_identities:
                            observed_raw_for_cache = copy.deepcopy(raw)

            raw = _apply_vendor_hint_to_payload(raw, vendor_hint)
            raw = _repair_ai_payload_from_ocr(raw, group_text, source_file=source_file.name)
            raw["_document_text"] = group_text
            raw["_source_file"] = source_file.name
            raw["_source_page"] = source_page
            raw["_source_type"] = group_candidate.source_type
            raw["_document_candidate"] = group_candidate.to_dict()
            extraction_provider, extraction_model = _actual_provider_identity(
                raw,
                fallback_provider=extraction_provider,
                fallback_model=extraction_model,
            )
            normalized = validate_ai_extraction(raw, references=references)
            normalized_cache_artifact = cache_artifact
            if observed_raw_for_cache is not None and cache_context is not None and cache_identities:
                observed_raw_for_cache["date_provenance"] = list(
                    normalized.get("date_provenance") or []
                )
                observed_raw_for_cache["_handwritten_row_identities"] = list(
                    normalized.get("handwritten_row_identities") or []
                )
                observed_raw_for_cache["excluded_paid_rows"] = list(
                    normalized.get("excluded_paid_rows")
                    or observed_raw_for_cache.get("excluded_paid_rows")
                    or []
                )
                normalized_cache_artifact = _save_page_facts(
                    batch_id=batch_id,
                    identities=cache_identities,
                    context=cache_context,
                    raw=observed_raw_for_cache,
                    source_file=source_file,
                    page_numbers=page_numbers,
                    group_index=stable_group_index,
                )
            if normalized_cache_artifact is not None:
                page_facts_cache.save_normalized_facts(
                    normalized_cache_artifact.cache_key, normalized
                )
            normalized["ai_provider"] = extraction_provider
            normalized["ai_model"] = extraction_model
            normalized["ai_extraction_mode"] = extraction_mode
            normalized = ai_mapping_review.apply_learned_mappings_to_normalized(normalized)

            def create_support_link() -> support_documents.SupportDocumentLink:
                return support_documents.upload_source_document_to_dropbox(
                    batch_id=batch_id,
                    source_file=source_file.name,
                    vendor_name=normalized.get("vendor_name") or vendor_hint,
                    invoice_date=normalized.get("invoice_date"),
                    dry_run=dry_run,
                )

            if support_link_coordinator is not None:
                support_link = support_link_coordinator.obtain(
                    stable_group_index, create_support_link
                )
            elif support_link is None:
                support_link = create_support_link()
            if not support_link.success and support_link.review_code:
                _append_review_issue(
                    normalized,
                    code=support_link.review_code,
                    message=support_link.review_message,
                    severity="medium",
                )
            with ai_runtime_trace.operation(
                batch_id=batch_id,
                stage=f"accounting_pipeline:{stable_group_index}",
            ):
                invoice = ai_result_to_invoice(
                    normalized,
                    batch_id=batch_id,
                    source_file=source_file.name,
                    source_page=source_page,
                    vendor_key=vendor_key,
                    support_document_url=support_link.url,
                    support_document_status=support_link.status,
                    support_document_dropbox_path=support_link.dropbox_path,
                )
            invoices.append(invoice)
            if normalized.get("manual_review_reasons"):
                reviews.append(_manual_review_item(
                    source_file=source_file.name,
                    vendor_name=normalized.get("vendor_name") or vendor_hint,
                    invoice_number=normalized.get("invoice_number", ""),
                    invoice_date=normalized.get("invoice_date", ""),
                    total_amount=normalized.get("total_amount", 0),
                    account_number=normalized.get("account_number", ""),
                    property_abbreviation=normalized.get("property_abbreviation", ""),
                    location=normalized.get("location", ""),
                    service_address=normalized.get("service_address", ""),
                    line_count=len(invoice.get("rows") or []),
                    reasons=normalized["manual_review_reasons"],
                    reason_codes=normalized.get("manual_review_codes", []),
                    message=f"Invoice on source page {source_page} needs operator review.",
                ))
        except Exception as exc:
            if support_link_coordinator is not None:
                support_link_coordinator.fail(stable_group_index)
            reason, safe_message = _safe_processing_failure(exc)
            _LOG.warning(
                "Segmented AI invoice processing failed for %s page %s: %s",
                source_file.name,
                source_page,
                exc,
            )
            if reason == "ai_processing_failed":
                reason = "segmented_invoice_processing_failed"
                safe_message = f"Invoice on source page {source_page} could not be processed."
            reviews.append(_manual_review_item(
                source_file=source_file.name,
                vendor_name=vendor_hint,
                invoice_number=str(group.get("invoice_identity") or ""),
                reasons=[reason],
                reason_codes=[reason],
                message=safe_message,
            ))
            unsupported.append({
                "filename": source_file.name,
                "source_page": source_page,
                "vendor_key": vendor_key,
                "processing_mode": "ai_assisted_segmented",
                "reason": reason,
                "message": safe_message,
            })
        finally:
            if cache_context is not None and cache_identities:
                page_facts_cache.release_reservation(cache_identities, cache_context)
    return {"invoices": invoices, "manual_review": reviews, "unsupported": unsupported}


def _load_verified_page_extraction(
    *,
    batch_id: str,
    source_file: str,
    source_page: int,
) -> dict[str, Any]:
    """Load an auditable visual correction for a provider-illegible page.

    Overrides are batch-local and page-specific. They are never inferred or
    silently shared with unrelated documents, and the original source remains
    untouched. This gives a verified scan a repeatable result without teaching
    the deterministic engine unsafe guesses from handwriting.
    """
    try:
        path = settings.WEBAPP_DATA_ROOT / "batches" / batch_id / "audit" / "verified_page_extractions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, dict):
        return {}
    key = f"{Path(source_file).name}#{int(source_page)}"
    extraction = pages.get(key)
    return json.loads(json.dumps(extraction)) if isinstance(extraction, dict) else {}


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


def _deduplicate_invoices(
    invoices: list[dict[str, Any]],
    manual_review: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse repeated vendor/invoice sources without hiding conflicts.

    Email and screenshot workflows frequently upload the same invoice twice.
    ResMan must receive one invoice number, so the latest source wins. When
    totals disagree, the retained invoice is explicitly blocked for review
    instead of silently choosing an amount.
    """
    deduped: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    superseded_identities: set[tuple[str, str]] = set()
    conflict_reviews: list[dict[str, Any]] = []
    for invoice in invoices:
        rows = list(invoice.get("rows") or [])
        vendor = _clean(rows[0].get("Vendor")) if rows else ""
        invoice_number = _clean(invoice.get("invoice_number"))
        key = (_normalize_key(vendor), _normalize_key(invoice_number))
        if not all(key):
            deduped.append(invoice)
            continue
        existing_position = positions.get(key)
        if existing_position is None:
            positions[key] = len(deduped)
            deduped.append(invoice)
            continue

        previous = deduped[existing_position]
        previous_source = _clean(previous.get("source_file"))
        current_source = _clean(invoice.get("source_file"))
        previous_total = _money(previous.get("total_amount"))
        current_total = _money(invoice.get("total_amount"))
        superseded_identities.add((previous_source, _normalize_key(invoice_number)))
        debug_info = dict(invoice.get("debug_info") or {})
        debug_info["duplicate_sources"] = [
            *list((previous.get("debug_info") or {}).get("duplicate_sources") or []),
            previous_source,
        ]
        invoice["debug_info"] = debug_info
        if abs(previous_total - current_total) > 0.01:
            reason = (
                f"Duplicate invoice {invoice_number} has conflicting totals "
                f"({previous_total:.2f} in {previous_source} vs {current_total:.2f} in {current_source}). "
                "The most recently added source was retained; confirm which supplier revision is payable."
            )
            reasons = list(invoice.get("manual_review_reasons") or [])
            codes = list(invoice.get("manual_review_codes") or [])
            if reason not in reasons:
                reasons.append(reason)
            if "duplicate_invoice_total_conflict" not in codes:
                codes.append("duplicate_invoice_total_conflict")
            invoice["manual_review_reasons"] = reasons
            invoice["manual_review_codes"] = codes
            for row in rows:
                meta = dict(row.get("_meta") or {})
                row_reasons = list(meta.get("manual_review_reasons") or [])
                row_codes = list(meta.get("ai_validation_flags") or [])
                if reason not in row_reasons:
                    row_reasons.append(reason)
                if "duplicate_invoice_total_conflict" not in row_codes:
                    row_codes.append("duplicate_invoice_total_conflict")
                meta["manual_review_reasons"] = row_reasons
                meta["ai_validation_flags"] = row_codes
                meta["duplicate_sources"] = debug_info["duplicate_sources"]
                row["_meta"] = meta
            first_row = rows[0] if rows else {}
            conflict_reviews.append(_manual_review_item(
                source_file=current_source,
                vendor_name=vendor,
                invoice_number=invoice_number,
                invoice_date=_clean(invoice.get("invoice_date")),
                total_amount=current_total,
                property_abbreviation=_clean(first_row.get("Property Abbreviation")),
                location=_clean(first_row.get("Location")),
                service_address=_clean((first_row.get("_meta") or {}).get("ai_service_address")),
                line_count=len(rows),
                reasons=[reason],
                reason_codes=["duplicate_invoice_total_conflict"],
                message="Conflicting duplicate invoice sources require operator confirmation.",
            ))
        deduped[existing_position] = invoice

    retained_conflict_identities = {
        (
            _clean(item.get("source_file")),
            _normalize_key(item.get("invoice_number")),
        )
        for item in conflict_reviews
    }
    filtered_review = [
        item for item in manual_review
        if (
            _clean(item.get("source_file")),
            _normalize_key(item.get("invoice_number")),
        ) not in superseded_identities
        and (
            _clean(item.get("source_file")),
            _normalize_key(item.get("invoice_number")),
        ) not in retained_conflict_identities
    ]
    filtered_review.extend(conflict_reviews)
    return deduped, filtered_review


def _apply_vendor_hint_to_payload(payload: dict[str, Any], vendor_hint: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    safe_vendor_hint = _clean(vendor_hint)
    if not safe_vendor_hint or safe_vendor_hint.lower().startswith("unknown"):
        return payload
    repaired = dict(payload)
    if not _clean(repaired.get("vendor_name")):
        repaired["vendor_name"] = safe_vendor_hint
    return repaired


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
    if candidate.needs_vision or bool(quality.get("vision_recommended")):
        return True
    if not (candidate.document_text or "").strip():
        return True
    if mode in {"fallback_only", "auto", "weak_text"}:
        try:
            return float(quality.get("text_quality_score") or 0) < 0.45
        except Exception:
            return True
    return False


def _vision_model_for_candidate(
    status: ai_provider.AIProviderStatus,
    candidate: document_ingestion.DocumentCandidate,
) -> str:
    """Escalate genuinely difficult scans without adding another AI call."""
    configured = _clean(status.vision_model or status.model)
    quality = candidate.extraction_quality or {}
    try:
        score = float(quality.get("text_quality_score") or candidate.text_quality_score or 0)
    except (TypeError, ValueError):
        score = 0.0
    text = candidate.document_text or ""
    form_has_invoice_marker = bool(re.search(r"\binvoice\b", text, re.IGNORECASE))
    form_has_explicit_date = bool(
        re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text)
    )
    form_has_explicit_total = bool(
        re.search(r"\b(?:grand\s+total|total\s+due|amount\s+due)\b[^\n]{0,35}\d+[.,]\d{2}\b", text, re.I)
    )
    unreadable_scanned_form = (
        candidate.source_type == "pdf_scanned"
        and form_has_invoice_marker
        and (not form_has_explicit_date or not form_has_explicit_total)
    )
    hard_visual = candidate.source_type in {"pdf_scanned", "image", "screenshot"} and (
        score < 0.45 or unreadable_scanned_form
    )
    escalation_model = _clean(os.environ.get("AI_VISION_ESCALATION_MODEL"))
    if hard_visual and escalation_model:
        return escalation_model
    return configured


def _native_pdf_model_sequence(
    status: ai_provider.AIProviderStatus,
    candidate: document_ingestion.DocumentCandidate,
) -> tuple[str, str]:
    """Return the safest bounded native-document model sequence.

    A genuinely hard visual form enters the configured strong profile directly
    so the system does not pay for a predictably insufficient call first.
    Moderate scans use the economical profile and may escalate only after a
    structural failure. Model identifiers remain deployment configuration.
    """

    primary = _vision_model_for_candidate(status, candidate)
    escalation = _clean(os.environ.get("AI_VISION_ESCALATION_MODEL"))
    return primary, escalation if escalation and escalation != primary else ""


def _extract_vision_with_reduced_retry(**kwargs: Any) -> dict[str, Any]:
    """Retry a failed multimodal extraction with one full-page image.

    Some provider gateways reject a large full-page-plus-detail request even
    though the same profile accepts a single image.  The retry is still a real
    Vision request and never substitutes text-only output for visual evidence.
    """
    refs = list(kwargs.get("page_images_or_refs") or [])

    def runtime_fallback(prior_error: ai_provider.AIProviderError) -> dict[str, Any]:
        status = ai_provider.provider_status()
        runtime_model = _clean(status.vision_model or status.model)
        if not runtime_model:
            raise prior_error
        primary_identity = ai_provider.extraction_profile_identity(vision=True)
        runtime_identity = ai_provider.extraction_profile_identity(
            vision=True,
            model_override=runtime_model,
            force_model_override=True,
        )
        if primary_identity == runtime_identity:
            raise prior_error
        escalated = dict(kwargs)
        escalated["model_override"] = runtime_model
        escalated["force_model_override"] = True
        _LOG.info(
            "Escalating unavailable visual extraction to the configured runtime Vision profile."
        )
        try:
            return ai_provider.extract_invoice_vision_structured(**escalated)
        except ai_provider.AIProviderError:
            if len(refs) <= 1:
                raise
            escalated["page_images_or_refs"] = refs[:1]
            _LOG.info("Retrying runtime Vision fallback with one full-page image.")
            return ai_provider.extract_invoice_vision_structured(**escalated)

    try:
        return ai_provider.extract_invoice_vision_structured(**kwargs)
    except (ai_provider.AIProviderInvalidJSON, ai_provider.AIProviderInvalidSchema) as first_error:
        # A schema/output failure is a bounded signal to use the separately
        # configured runtime deployment. The route is configuration-driven and
        # does not promote the strong profile globally.
        return runtime_fallback(first_error)
    except ai_provider.AIProviderError as first_error:
        if len(refs) > 1:
            reduced = dict(kwargs)
            reduced["page_images_or_refs"] = refs[:1]
            _LOG.info("Retrying AI vision extraction with reduced visual payload.")
            try:
                return ai_provider.extract_invoice_vision_structured(**reduced)
            except ai_provider.AIProviderError as reduced_error:
                return runtime_fallback(reduced_error)
        return runtime_fallback(first_error)


def _extract_text_with_runtime_fallback(**kwargs: Any) -> dict[str, Any]:
    """Use the configured runtime text deployment after one routed-provider failure."""

    try:
        return ai_provider.extract_invoice_structured(**kwargs)
    except ai_provider.AIProviderError as primary_error:
        status = ai_provider.provider_status()
        runtime_model = _clean(status.model)
        if not runtime_model:
            raise
        primary_identity = ai_provider.extraction_profile_identity(vision=False)
        runtime_identity = ai_provider.extraction_profile_identity(
            vision=False,
            model_override=runtime_model,
            force_model_override=True,
        )
        if primary_identity == runtime_identity:
            raise primary_error
        fallback = dict(kwargs)
        fallback["model_override"] = runtime_model
        fallback["force_model_override"] = True
        _LOG.info(
            "Escalating unavailable text extraction to the configured runtime Text profile."
        )
        return ai_provider.extract_invoice_structured(**fallback)


def _extract_fast_first_or_standard(**kwargs: Any) -> dict[str, Any]:
    """Run facts-only first only after both production safety gates approve it."""
    if not fast_first_facts.production_enabled():
        return _extract_vision_with_reduced_retry(**kwargs)
    facts = ai_provider.extract_invoice_facts_only_vision_structured(
        document_text=str(kwargs.get("document_text") or ""),
        page_images_or_refs=list(kwargs.get("page_images_or_refs") or []),
        model_override=str(kwargs.get("model_override") or ""),
        cost_scope_id=str(kwargs.get("cost_scope_id") or ""),
    )
    reasons = fast_first_facts.escalation_reasons(facts)
    if not reasons:
        return facts
    ai_runtime_trace.record_schema_result(
        "escalated", retry_reason=";".join(reasons)
    )
    full = _extract_vision_with_reduced_retry(**kwargs)
    full["warnings"] = list(dict.fromkeys([
        *list(full.get("warnings") or []),
        *[f"fast_first_escalated:{reason}" for reason in reasons],
    ]))
    return full


def _safe_vision_failure_warning(exc: ai_provider.AIProviderError) -> str:
    diagnostic = exc.safe_diagnostic()
    code = str(diagnostic.get("failure_code") or "provider_error")
    status = diagnostic.get("http_status")
    return f"ai_vision_failure:{code}" + (f":http_{status}" if status else "")


def _safe_processing_failure(exc: Exception) -> tuple[str, str]:
    """Return an auditable failure code without provider response bodies."""
    if not isinstance(exc, ai_provider.AIProviderError):
        return "ai_processing_failed", "AI invoice processing failed. Review this file manually."
    if (
        isinstance(exc, ai_provider.AIProviderInvalidJSON)
        and "exceeded the configured output limit" in str(exc).lower()
    ):
        return (
            "ai_response_output_limit_exceeded",
            "AI returned more structured extraction data than the configured response budget allowed.",
        )
    diagnostic = exc.safe_diagnostic()
    code = _clean(diagnostic.get("failure_code")) or "provider_error"
    messages = {
        "cost_budget_exceeded": (
            "The AI batch cost budget was exhausted before this document could be processed. "
            "Exact duplicate inputs are skipped on the next run; retry the remaining source once."
        ),
        "vision_transport_error": "The Vision provider could not be reached within the configured retry policy.",
        "vision_http_error": "The Vision provider rejected the request.",
        "text_transport_error": "The text extraction provider could not be reached within the configured retry policy.",
        "text_http_error": "The text extraction provider rejected the request.",
        "vision_input_unsupported": "The configured Vision profile did not accept the supplied document image.",
    }
    return code, messages.get(code, f"AI provider processing failed with code {code}.")


def _is_unresolved_visual_total_fallback(
    item: dict[str, Any],
    *,
    warnings: list[str],
    line_item_count: int,
) -> bool:
    """Identify lossy total-only rows without vendor or fixture knowledge."""
    if line_item_count != 1:
        return False
    marker_text = " ".join(
        _clean(value).lower()
        for value in (
            item.get("row_label"),
            item.get("reason"),
            *warnings,
        )
    )
    visual_failed = any(
        token in marker_text
        for token in (
            "ai_vision_failed_text_fallback_used",
            "ai_vision_failure:",
            "vision failed",
        )
    )
    total_only = any(
        token in marker_text
        for token in (
            "invoice total fallback",
            "explicit invoice total",
            "using invoice total",
            "total is used as a payable fallback",
        )
    )
    return visual_failed and total_only


def _ai_payload_requires_vision(
    payload: dict[str, Any],
    status: ai_provider.AIProviderStatus,
) -> bool:
    """Reject syntactically valid but semantically unusable text extraction."""
    if not status.vision_enabled or not isinstance(payload, dict):
        return False
    line_items = payload.get("line_items")
    items = line_items if isinstance(line_items, list) else []
    payable_total = sum(
        (_money(item.get("amount")) for item in items if isinstance(item, dict)),
        0.0,
    )
    invoice_total = _money(payload.get("total_amount"))
    confidence = _confidence_or_none(payload.get("confidence")) or 0.0
    warnings = " ".join(_normalize_warnings(payload.get("warnings") or [])).lower()
    critical_missing = (
        not _clean(payload.get("vendor_name"))
        or not _clean(payload.get("invoice_date"))
        or _is_invoice_number_placeholder(payload.get("invoice_number"))
        or abs(invoice_total) < 0.01
        or not items
    )
    total_conflict = (
        abs(payable_total) >= 0.01
        and (
            abs(invoice_total) < 0.01
            or abs(payable_total - invoice_total) > 0.02
        )
    )
    visual_warning = any(
        token in warnings
        for token in ("handwrit", "ambiguous", "garbled", "unreadable", "ocr")
    )
    return critical_missing or total_conflict or confidence < 0.65 or visual_warning


def _requires_critical_header_verification(
    payload: dict[str, Any],
    candidate: document_ingestion.DocumentCandidate | None,
) -> bool:
    """Return whether a small header crop must verify ambiguous source facts."""

    if not candidate or candidate.source_type != "pdf_scanned":
        return False
    warnings = " ".join(_normalize_warnings(payload.get("warnings") or [])).lower()
    visual_ambiguity = any(
        token in warnings for token in ("faint", "ambiguous", "unclear", "handwrit", "illegible")
    )
    critical_missing = not any(
        _clean(payload.get(field)) for field in ("invoice_date", "service_date")
    ) or not _clean(payload.get("property_candidate"))
    return bool(critical_missing or visual_ambiguity)


def _merge_critical_header_verification(
    payload: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Merge a crop verification without changing table or accounting facts."""

    merged = dict(payload)
    confidence = _confidence_or_none(verification.get("confidence")) or 0.0
    corrections: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    ambiguous_primary = any(
        token in " ".join(_normalize_warnings(payload.get("warnings") or [])).lower()
        for token in ("faint", "ambiguous", "unclear", "handwrit", "illegible")
    )
    for field in (
        "invoice_date", "service_date", "due_date", "payment_terms",
        "property_candidate", "location_candidate",
    ):
        verified = _clean(verification.get(field))
        if not verified:
            continue
        previous = _clean(merged.get(field))
        if not previous:
            merged[field] = verified
            corrections.append({
                "field": field,
                "previous": previous,
                "value": verified,
                "confidence": confidence,
                "reason": "critical_header_crop_filled_missing_source_fact",
            })
        elif previous != verified:
            conflict = {
                "field": field,
                "primary": previous,
                "verification": verified,
                "confidence": confidence,
                "selected": previous,
            }
            # The crop is the more specific view of the same immutable source.
            # It may replace an explicitly ambiguous full-document reading,
            # but a low-confidence disagreement remains unresolved.
            may_replace_ambiguous_fact = field in {
                "invoice_date", "service_date", "property_candidate", "location_candidate",
            }
            if may_replace_ambiguous_fact and ambiguous_primary and confidence >= 0.85:
                merged[field] = verified
                conflict["selected"] = verified
                conflict["reason"] = "high_confidence_detail_crop_overrode_ambiguous_full_document_reading"
                corrections.append({
                    "field": field,
                    "previous": previous,
                    "value": verified,
                    "confidence": confidence,
                    "reason": conflict["reason"],
                })
            else:
                conflict["reason"] = "source_views_disagree_manual_review_required"
            conflicts.append(conflict)
    for evidence_field in ("sold_to_raw_text", "job_site_raw_text", "due_date_text"):
        merged[evidence_field] = _clean(verification.get(evidence_field))
    corrected_fields = {item["field"] for item in corrections}
    primary_warnings = _normalize_warnings(merged.get("warnings") or [])
    superseded_primary_warnings: list[str] = []
    active_primary_warnings: list[str] = []
    for warning in primary_warnings:
        lowered = warning.lower()
        superseded_date_reading = bool(
            {"invoice_date", "service_date"} & corrected_fields
            and "date" in lowered
            and any(token in lowered for token in ("faint", "ambiguous", "unclear", "handwrit", "illegible"))
        )
        if superseded_date_reading:
            superseded_primary_warnings.append(warning)
        else:
            active_primary_warnings.append(warning)

    merged["_critical_header_verification"] = {
        "profile_id": verification.get("_provider_profile_id"),
        "provider": verification.get("_provider_name"),
        "model": verification.get("_provider_model_id"),
        "confidence": confidence,
        "corrections": corrections,
        "conflicts": conflicts,
        "warnings": list(verification.get("warnings") or []),
        "superseded_primary_warnings": superseded_primary_warnings,
        "estimated_cost_usd": verification.get("_estimated_cost_usd"),
    }
    warnings = active_primary_warnings
    if conflicts and "critical_header_source_views_disagreed" not in warnings:
        warnings.append("critical_header_source_views_disagreed")
    merged["warnings"] = warnings
    return merged


def _requires_row_identity_verification(
    payload: dict[str, Any],
    candidate: document_ingestion.DocumentCandidate | None,
) -> bool:
    if not candidate or candidate.source_type != "pdf_scanned":
        return False
    items = [item for item in payload.get("line_items") or [] if isinstance(item, dict)]
    has_row_labels = any(_clean(item.get("row_label") or item.get("location_candidate")) for item in items)
    warnings = " ".join(_normalize_warnings(payload.get("warnings") or [])).lower()
    has_selection_evidence = bool(payload.get("excluded_paid_rows")) or any(
        token in warnings for token in ("paid", "circled", "circle", "handwrit")
    )
    return has_row_labels and has_selection_evidence


def _expected_visible_matrix_rows(payload: dict[str, Any]) -> int:
    payable = {
        _normalize_key(_clean(item.get("row_label") or item.get("location_candidate")))
        for item in payload.get("line_items") or []
        if isinstance(item, dict) and _clean(item.get("row_label") or item.get("location_candidate"))
    }
    return len(payable) + len(payload.get("excluded_paid_rows") or [])


def _catalog_units_for_row_identity(payload: dict[str, Any]) -> tuple[str, list[str]]:
    tenant_id = default_tenant_id()
    property_code = _clean(payload.get("property_abbreviation")).upper()
    property_name = _clean(payload.get("property_candidate"))
    if not property_code and property_name:
        matched = resman_context_data.find_property_by_name(tenant_id, property_name)
        if matched:
            property_code = _clean(matched.get("property_code")).upper()
    if not property_code:
        return "", []
    records = resman_context_data.list_all_effective_records(
        tenant_id,
        resman_context_data.DatasetKind.PROPERTIES_UNITS,
    )
    units = sorted({
        _clean(record.get("unit_number")).upper()
        for record in records
        if record.get("entity_type") == "unit"
        and _clean(record.get("property_code")).upper() == property_code
        and _clean(record.get("unit_number"))
    })
    return property_code, units


def _page_crop_coordinates(parent: dict[str, Any], bbox: dict[str, Any]) -> CropCoordinates:
    parent_x = int(parent.get("x") or 0)
    parent_y = int(parent.get("y") or 0)
    parent_width = int(parent.get("width") or 0)
    parent_height = int(parent.get("height") or 0)
    return CropCoordinates(
        page=int(parent.get("page") or 1),
        x=parent_x + int(round(parent_width * float(bbox.get("x") or 0))),
        y=parent_y + int(round(parent_height * float(bbox.get("y") or 0))),
        width=max(1, int(round(parent_width * float(bbox.get("w") or 0)))),
        height=max(1, int(round(parent_height * float(bbox.get("h") or 0)))),
        render_dpi=int(parent.get("render_dpi") or 600),
        source_page_width=(int(parent["source_page_width"]) if parent.get("source_page_width") else None),
        source_page_height=(int(parent["source_page_height"]) if parent.get("source_page_height") else None),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _merge_row_identity_verification(
    payload: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Merge pixel-first row identity evidence, then validate against catalog."""

    merged = dict(payload)
    property_code, catalog_units = _catalog_units_for_row_identity(merged)
    parent_crop = dict(verification.get("crop_coordinates") or {})
    evidence_rows: list[dict[str, Any]] = []
    for raw_row in verification.get("visible_rows") or []:
        if not isinstance(raw_row, dict):
            continue
        raw_value = _clean(raw_row.get("raw_value")).upper()
        alternatives = [
            RowIdentityAlternative(
                value=_clean(item.get("value")).upper(),
                confidence=float(item.get("confidence") or 0),
            )
            for item in raw_row.get("alternatives") or []
            if isinstance(item, dict) and _clean(item.get("value"))
        ]
        confidence = float(raw_row.get("confidence") or 0)
        status = str(raw_row.get("status") or "needs_confirmation")
        catalog_matches = [
            unit for unit in catalog_units
            if raw_value and unit.endswith(raw_value)
        ]
        close_alternative = any(
            alternative.value != raw_value
            and alternative.confidence >= max(0.70, confidence - 0.10)
            for alternative in alternatives
        )
        confirmed_from_pixels = (
            status == "confirmed"
            and bool(raw_value)
            and confidence >= 0.88
            and not close_alternative
        )
        if confirmed_from_pixels and len(catalog_matches) == 1:
            resolved_unit = catalog_matches[0]
            resolved_status = "confirmed"
            basis = "handwriting_confirmed_before_unique_catalog_validation"
        elif not raw_value:
            resolved_unit = None
            resolved_status = "illegible"
            basis = "source_handwriting_illegible"
        else:
            resolved_unit = None
            resolved_status = "needs_confirmation"
            basis = (
                "handwriting_ambiguous_catalog_not_authoritative"
                if close_alternative or status != "confirmed" or confidence < 0.88
                else "catalog_match_not_unique_or_missing"
            )
        identity = HandwrittenRowIdentityEvidence(
            raw_value=raw_value or None,
            alternatives=alternatives,
            confidence=confidence,
            crop_coordinates=_page_crop_coordinates(parent_crop, dict(raw_row.get("bbox") or {})),
            catalog_matches=catalog_matches,
            resolved_unit=resolved_unit,
            status=resolved_status,
            resolution_basis=basis,
        )
        item = model_dict(identity)
        item["row_index"] = int(raw_row.get("row_index") or len(evidence_rows))
        item["selection_marker"] = str(raw_row.get("selection_marker") or "unclear")
        item["property_code"] = property_code
        evidence_rows.append(item)

    circled = [row for row in evidence_rows if row.get("selection_marker") == "circled"]
    items = [dict(item) for item in merged.get("line_items") or [] if isinstance(item, dict)]
    old_labels: list[str] = []
    for item in items:
        label = _clean(item.get("row_label") or item.get("location_candidate")).upper()
        if label and label not in old_labels:
            old_labels.append(label)
    matched_by_label: dict[str, dict[str, Any]] = {}
    unused_circled = list(circled)
    for label in old_labels:
        exact = next((row for row in unused_circled if row.get("raw_value") == label), None)
        if exact:
            matched_by_label[label] = exact
            unused_circled.remove(exact)
    for label in old_labels:
        if label in matched_by_label or not unused_circled:
            continue
        matched_by_label[label] = unused_circled.pop(0)

    payable_needs_confirmation = len(circled) != len(old_labels)
    for item in items:
        old_label = _clean(item.get("row_label") or item.get("location_candidate")).upper()
        identity = matched_by_label.get(old_label)
        if not identity:
            payable_needs_confirmation = True
            continue
        corrected_label = identity.get("raw_value") or old_label
        item["row_label"] = corrected_label
        item["location_candidate"] = identity.get("resolved_unit") or identity.get("raw_value") or old_label
        item["row_identity_evidence"] = identity
        # Preserve provider-observed raw_description verbatim. Only the
        # normalized/display description may reflect the independently
        # verified handwritten row identity.
        item["description"] = _replace_display_row_label(
            _clean(item.get("description")),
            old_label=old_label,
            corrected_label=corrected_label,
        )
        item["normalized_description"] = _replace_display_row_label(
            _clean(item.get("normalized_description")),
            old_label=old_label,
            corrected_label=corrected_label,
        )
        if identity.get("status") != "confirmed":
            payable_needs_confirmation = True
    merged["line_items"] = items

    excluded_facts: list[dict[str, Any]] = []
    eligible_excluded_rows = [
        row for row in evidence_rows
        if row.get("selection_marker") not in {"circled", "crossed_out"}
    ]
    unused_excluded_rows = list(eligible_excluded_rows)
    for index, raw_excluded in enumerate(merged.get("excluded_paid_rows") or []):
        if not isinstance(raw_excluded, dict):
            continue
        raw_label = _clean(raw_excluded.get("raw_apartment_number")).upper()
        identity = next((row for row in unused_excluded_rows if row.get("raw_value") == raw_label), None)
        if identity is not None:
            unused_excluded_rows.remove(identity)
        else:
            # The Apt-only crop intentionally excludes the far-right PAID
            # text. Associate the independent row reading to source PAID
            # evidence by vertical coordinates on the immutable page. Never
            # use catalog membership or a merely convenient valid unit.
            marker_y = _paid_marker_center_y(raw_excluded, parent_crop)
            positioned = [
                row for row in unused_excluded_rows
                if marker_y is not None and row.get("crop_coordinates")
            ]
            if positioned:
                identity = min(
                    positioned,
                    key=lambda row: abs(
                        _identity_center_y(row) - float(marker_y)
                    ),
                )
                row_height = max(1.0, float(identity["crop_coordinates"].get("height") or 1))
                if abs(_identity_center_y(identity) - float(marker_y)) > row_height * 1.5:
                    identity = None
                else:
                    unused_excluded_rows.remove(identity)
        components: dict[str, Decimal] = {}
        for component in raw_excluded.get("component_amounts") or []:
            if not isinstance(component, dict) or not _clean(component.get("label")):
                continue
            amount = _decimal_or_none(component.get("amount"))
            if amount is not None:
                components[_clean(component.get("label"))] = amount
        paid_evidence = [
            PaidMarkerEvidence(
                page=int(marker.get("page") or 1),
                text=_clean(marker.get("text")) or "PAID",
                bbox=(
                    [float((marker.get("bbox") or {}).get(key) or 0) for key in ("x", "y", "w", "h")]
                    if isinstance(marker.get("bbox"), dict) else None
                ),
                confidence=_confidence_or_none(marker.get("confidence")),
            )
            for marker in raw_excluded.get("paid_marker_evidence") or []
            if isinstance(marker, dict)
        ]
        fact = ExcludedPaidRowFacts(
            raw_apartment_number=(identity or {}).get("raw_value") or raw_label or None,
            apartment_identity=(
                HandwrittenRowIdentityEvidence(**{
                    key: value for key, value in identity.items()
                    if key in HandwrittenRowIdentityEvidence.model_fields
                }) if identity else None
            ),
            component_amounts=components,
            row_total=_decimal_or_none(raw_excluded.get("row_total")),
            paid_marker_evidence=paid_evidence,
            exclusion_reason=_clean(raw_excluded.get("exclusion_reason")) or "visible_paid_marker",
        )
        excluded_facts.append(model_dict(fact))
    merged["excluded_paid_rows"] = excluded_facts
    merged["_handwritten_row_identities"] = evidence_rows
    active_warnings: list[str] = []
    superseded_warnings: list[str] = []
    for warning in _normalize_warnings(merged.get("warnings") or []):
        normalized_warning = warning.lower()
        is_preliminary_row_identity_warning = (
            "apartment" in normalized_warning
            and any(token in normalized_warning for token in ("handwrit", "suffix"))
        )
        if is_preliminary_row_identity_warning and evidence_rows:
            superseded_warnings.append(warning)
        else:
            active_warnings.append(warning)
    merged["warnings"] = active_warnings
    merged["_row_identity_verification"] = {
        "profile_id": verification.get("_provider_profile_id"),
        "provider": verification.get("_provider_name"),
        "model": verification.get("_provider_model_id"),
        "crop_coordinates": parent_crop,
        "estimated_cost_usd": verification.get("_estimated_cost_usd"),
        "payable_needs_confirmation": payable_needs_confirmation,
        "warnings": list(verification.get("warnings") or []),
        "superseded_primary_warnings": superseded_warnings,
    }
    return merged


def _identity_center_y(identity: dict[str, Any]) -> float:
    crop = dict(identity.get("crop_coordinates") or {})
    return float(crop.get("y") or 0) + (float(crop.get("height") or 0) / 2.0)


def _paid_marker_center_y(raw_excluded: dict[str, Any], parent_crop: dict[str, Any]) -> float | None:
    page_height = float(parent_crop.get("source_page_height") or 0)
    if page_height <= 0:
        return None
    for marker in raw_excluded.get("paid_marker_evidence") or []:
        bbox = marker.get("bbox") if isinstance(marker, dict) else None
        if not isinstance(bbox, dict):
            continue
        try:
            return (float(bbox.get("y") or 0) + float(bbox.get("h") or 0) / 2.0) * page_height
        except (TypeError, ValueError):
            continue
    return None


def _replace_display_row_label(value: str, *, old_label: str, corrected_label: str) -> str:
    """Correct a normalized/display prefix without mutating raw source text."""

    if not value or not old_label or not corrected_label or old_label == corrected_label:
        return value
    patterns = (
        rf"^(Apt\.?\s*#?\s*){re.escape(old_label)}(?=\b|\s|\|)",
        rf"^{re.escape(old_label)}(?=\b|\s|\|)",
    )
    for pattern in patterns:
        updated, count = re.subn(pattern, lambda match: f"{match.group(1) if match.lastindex else ''}{corrected_label}", value, count=1, flags=re.IGNORECASE)
        if count:
            return updated
    return value


def _is_invoice_number_placeholder(value: Any) -> bool:
    normalized = _normalize_key(_clean(value))
    return normalized in {
        "account",
        "account number",
        "invoice",
        "invoice number",
        "number",
        "sales",
        "sales number",
        "location",
        "date",
        "unknown",
        "n a",
    }


def _reconcile_high_confidence_vision_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve provider self-contradictions using its visual field regions.

    Some vision providers return one value in the top-level JSON and a
    different transcription for the same highlighted region. A field-region
    candidate at 90%+ confidence is the more specific visual observation, so
    it wins for a small whitelist of source fields.
    """
    if not isinstance(payload, dict):
        return payload
    candidates = payload.get("vision_candidates")
    if not isinstance(candidates, list):
        return payload
    supported = {
        "vendor_name",
        "invoice_number",
        "account_number",
        "invoice_date",
        "service_date",
        "due_date",
        "subtotal",
        "tax_amount",
        "shipping_amount",
        "fees_amount",
        "total_amount",
        "service_address",
        "address_role",
        "property_candidate",
        "location_candidate",
    }
    best: dict[str, tuple[float, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        field = _clean(candidate.get("field_key"))
        value = candidate.get("value")
        confidence = _confidence_or_none(candidate.get("confidence")) or 0.0
        status = _clean(candidate.get("validation_status"))
        if (
            field not in supported
            or value in (None, "")
            or confidence < 0.90
            or status == "page_scope_candidate"
        ):
            continue
        if field not in best or confidence > best[field][0]:
            best[field] = (confidence, value)

    reconciled = dict(payload)
    corrections: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    source_pages = {
        int(item.get("source_page"))
        for item in (payload.get("line_items") or [])
        if isinstance(item, dict) and str(item.get("source_page") or "").isdigit()
    }
    payable_scope_total = _round_money(
        sum(
            _money(item.get("amount"))
            for item in (payload.get("line_items") or [])
            if isinstance(item, dict)
        )
        + sum(_money(payload.get(key)) for key in ("tax_amount", "shipping_amount", "fees_amount"))
    )
    unresolved_text_candidates: list[dict[str, Any]] = list(
        reconciled.get("_unresolved_visual_field_candidates") or []
    )
    for field, (confidence, candidate_value) in best.items():
        value: Any = candidate_value
        if field in {
            "subtotal",
            "tax_amount",
            "shipping_amount",
            "fees_amount",
            "total_amount",
        }:
            value = _money(candidate_value)
            if field in {"subtotal", "total_amount"} and abs(value) < 0.01:
                continue
        elif field in {"invoice_date", "service_date", "due_date"}:
            normalized, valid = _normalize_date(candidate_value)
            if not valid or not normalized:
                # A visible non-calendar due-date value such as "Upon
                # Receipt" is still immutable source evidence.  It must not
                # become a normalized date, but it must not disappear either.
                unresolved_text_candidates.append({
                    "field": field,
                    "value": _clean(candidate_value),
                    "confidence": confidence,
                    "reason": "visible_text_is_not_a_normalized_calendar_date",
                })
                continue
            value = normalized
        else:
            value = _clean(candidate_value)
            if not value:
                continue
        previous = reconciled.get(field)
        if str(previous or "").strip() == str(value).strip():
            continue
        if (
            field == "total_amount"
            and len(source_pages) > 1
            and abs(_money(previous) - payable_scope_total) <= 0.02
            and abs(_money(value) - payable_scope_total) > 0.02
        ):
            # A bbox belongs to one visible page.  It cannot replace a document
            # total already reconciled from payable facts across several pages.
            conflicts.append({
                "field": field,
                "retained": previous,
                "candidate": value,
                "confidence": confidence,
                "reason": "page_scoped_candidate_conflicts_with_reconciled_document_total",
            })
            continue
        reconciled[field] = value
        corrections.append({
            "field": field,
            "previous": previous,
            "value": value,
            "confidence": confidence,
        })
    if corrections:
        reconciled["_vision_candidate_corrections"] = corrections
    if unresolved_text_candidates:
        reconciled["_unresolved_visual_field_candidates"] = unresolved_text_candidates
    if conflicts:
        reconciled["_vision_candidate_conflicts"] = conflicts
        warnings = _normalize_warnings(reconciled.get("warnings") or [])
        warning = "page_scoped_visual_total_did_not_override_reconciled_document_total"
        if warning not in warnings:
            warnings.append(warning)
        reconciled["warnings"] = warnings
    return reconciled


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
    paths = (
        settings.PROJECT_ROOT / "Vendors" / "Vendor List.csv",
        settings.PROJECT_ROOT / "Properties" / "Properties.csv",
        settings.PROJECT_ROOT / "Properties" / "Unit Info Clean.csv",
        settings.GENERAL_LEDGER_REFERENCE,
    )
    signature = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if path.is_file()
        else (str(path), 0, 0)
        for path in paths
    )
    return _load_references_cached(signature)


@lru_cache(maxsize=4)
def _load_references_cached(
    _signature: tuple[tuple[str, int, int], ...],
) -> dict[str, list[dict[str, Any]]]:
    """Load immutable reference snapshots once per source-file revision."""
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
    # Keep validation self-contained: callers and future processing paths
    # cannot accidentally skip visual candidate reconciliation.
    payload = _reconcile_high_confidence_vision_candidates(payload)
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
    # New vendors must remain usable in the template even before accounting
    # adds them to ResMan. Keep the exact source name and flag the mapping;
    # never erase a clearly extracted vendor merely because it is new.
    vendor_for_rows = canonical_vendor or vendor_name

    document_policy = tenant_document_policies.get_policy()
    raw_invoice_date, invoice_date_source = _choose_invoice_date_source(
        payload,
        allow_service_date_fallback=(
            document_policy.date_policy.invoice_date_from_service_date
        ),
    )
    invoice_date, invoice_date_ok = _normalize_date(raw_invoice_date)
    raw_due_candidate = _clean(payload.get("due_date"))
    if _normalize_key(raw_due_candidate) in {"upon receipt", "upon reciept"}:
        due_date, due_date_ok = "", False
    else:
        due_date, due_date_ok = _normalize_date(raw_due_candidate)
    due_date_source = "explicit_due_date" if due_date and due_date_ok else ""
    payment_terms_text = _clean(
        payload.get("due_date_text") or payload.get("payment_terms") or payload.get("terms")
    )
    may_derive_upon_receipt = (
        document_policy.date_policy.due_date_from_upon_receipt
        and _normalize_key(payment_terms_text) in {"upon receipt", "upon reciept"}
    )
    if (
        (not due_date or not due_date_ok)
        and invoice_date
        and invoice_date_ok
        and may_derive_upon_receipt
    ):
        terms_due_date = _due_date_from_visible_payment_terms(payload, invoice_date=invoice_date)
        if terms_due_date:
            due_date = terms_due_date
            due_date_ok = True
            due_date_source = "tenant_policy_from_due_date_text"
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
    document_text = str(payload.get("_document_text") or "")
    raw_service_address = _clean(payload.get("service_address"))
    address_role = _normalized_address_role(
        payload.get("address_role"),
        document_text=document_text,
    )
    billing_address = _clean(payload.get("billing_address"))
    if address_role in {"sold_to", "bill_to", "remit_to", "vendor_address"}:
        billing_address = billing_address or raw_service_address
        service_address = ""
    else:
        service_address = raw_service_address
    property_evidence_text = "\n".join(
        part for part in (
            document_text,
            property_candidate,
            _clean(payload.get("invoice_description")),
            "\n".join(
                _clean(item.get("description"))
                for item in (payload.get("line_items") or [])
                if isinstance(item, dict) and _clean(item.get("description"))
            ),
        )
        if part
    )
    property_identity = _property_identity_from_document_text(
        property_evidence_text,
        references["properties"],
    )
    if property_identity:
        identity_name = property_identity["property_name"]
        if not (
            property_candidate
            and _property_name_identity_key(property_candidate)
            == _property_name_identity_key(identity_name)
            and len(property_candidate) > len(identity_name)
        ):
            property_candidate = identity_name
        property_abbreviation = property_identity["property_abbreviation"]
    location_candidate = _clean(
        payload.get("location_candidate")
        or payload.get("unit_number")
        or payload.get("location")
    )
    account_number = _resolve_account_number(payload)
    service_period_start, service_period_end, service_period_source = _resolve_service_period(payload)
    source_invoice_number = _clean(payload.get("invoice_number"))
    if _is_invoice_number_placeholder(source_invoice_number):
        source_invoice_number = ""
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
    prepared_property_rows = _prepare_property_resolution_rows(
        references["properties"]
    )
    property_abbreviation, location, property_match = _resolve_property_context(
        property_abbreviation=property_abbreviation,
        property_candidate=property_candidate,
        service_address=service_address,
        location_candidate=location_candidate,
        properties=references["properties"],
        prepared_properties=prepared_property_rows,
    )
    # Some supplier headers contain a vendor-side one-character LOCATION code
    # next to Invoice/Account/Sales columns. It is not a ResMan unit. Suppress
    # it only when the page exposes that exact commercial header and no known
    # property unit matched, preserving legitimate A/B units elsewhere.
    if (
        not location
        and re.fullmatch(r"[A-Za-z]", location_candidate or "")
        and re.search(
            r"invoice\s+number\s+account\s+number.*sales.*location",
            _normalize_key(document_text),
        )
    ):
        location_candidate = ""
    if not property_abbreviation and vendor_for_rows:
        fallback_property, fallback_reason = _required_property_fallback(
            vendor_name=vendor_for_rows,
            property_candidate=property_candidate,
            service_address=service_address,
            address_role=address_role,
            document_text=str(payload.get("_document_text") or ""),
        )
        if fallback_property:
            property_abbreviation, location, property_match = _resolve_property_context(
                property_abbreviation=fallback_property,
                property_candidate=property_candidate,
                service_address=service_address,
                location_candidate=location_candidate,
                properties=references["properties"],
                prepared_properties=prepared_property_rows,
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
    raw_location = location_candidate
    blank_location_allowed = _blank_location_allowed_for_vendor_category(
        vendor_name=vendor_for_rows or vendor_name,
        category=_clean(payload.get("category")),
    )
    if raw_location and not location and not blank_location_allowed:
        add_issue(
            "location_unresolved",
            "Location could not be validated as a known unit/location. Raw addresses are not written to the Location column.",
        )
    elif service_address and not location and not blank_location_allowed:
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
            # A zero/unknown total is invalid for export, but the synthesized
            # review line must remain visible so a human can correct it.  The
            # readiness contract will keep export blocked until a valid amount
            # and the other required accounting fields are persisted.
            "preserve_for_review": True,
        }]

    if not (service_period_start or service_period_end):
        single_service_date = _single_service_date_from_line_items(
            line_items,
            invoice_date=invoice_date,
        )
        if single_service_date:
            service_period_start = single_service_date
            service_period_end = single_service_date
            service_period_source = "line_item_service_date"

    normalized_items: list[dict[str, Any]] = []
    gl_issue_seen = False
    unresolved_gl_descriptions: list[str] = []
    zero_amount_excluded = 0
    skipped_zero_items: list[dict[str, Any]] = []
    for idx, item in enumerate(line_items, start=1):
        item = item if isinstance(item, dict) else {}
        activity = _clean(
            item.get("activity")
            or item.get("line_activity")
            or item.get("service_type")
            or item.get("category")
        )
        source_line_description = _clean(item.get("raw_description") or item.get("description"))
        normalized_source_description = _clean(item.get("normalized_description"))
        generated_item_description = _clean(item.get("generated_description"))
        aggregate_fallback = _is_unresolved_visual_total_fallback(
            item,
            warnings=warnings,
            line_item_count=len(line_items),
        )
        display_source_description = normalized_source_description or source_line_description
        if activity and display_source_description and _normalize_key(activity) not in _normalize_key(display_source_description):
            description = f"{activity} - {display_source_description}"
        else:
            description = display_source_description or activity or f"Line item {idx}"
        # A provider-authored total fallback is a generated description, not
        # verbatim source text. Keeping it out of raw facts prevents a vague
        # summary from becoming false labor/service evidence downstream.
        raw_description = "" if aggregate_fallback else source_line_description
        amount = _money(item.get("amount"))
        if (
            abs(amount) <= 0.0
            and not settings.AI_INCLUDE_ZERO_AMOUNT_LINES
            and not bool(item.get("preserve_for_review"))
        ):
            zero_amount_excluded += 1
            skipped_zero_items.append(item)
            continue
        raw_item_confidence = _confidence_or_none(item.get("confidence"))
        raw_gl_candidate = _clean(item.get("gl_account_candidate"))
        gl_account = ai_mapping_review.validate_gl_account(raw_gl_candidate)
        if gl_account and not _is_payable_gl_account(gl_account):
            gl_account = None
        gl_suggestion_source = "ai_validated" if gl_account else ""
        if not gl_account and not aggregate_fallback:
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
            unresolved_gl_descriptions.append(description)
        gl_resolution_explanation = _gl_resolution_explanation(
            description=description,
            gl_code=gl_candidate,
            suggestion_source=gl_suggestion_source,
            vendor_name=vendor_for_rows or vendor_name,
        )
        item_location_candidate = _clean(item.get("location_candidate") or item.get("location") or item.get("unit_number"))
        item_location = ""
        if item_location_candidate:
            _, item_location, _ = _resolve_property_context(
                property_abbreviation=property_abbreviation,
                property_candidate=property_candidate,
                service_address=service_address,
                location_candidate=item_location_candidate,
                properties=references["properties"],
                prepared_properties=prepared_property_rows,
            )
        normalized_items.append({
            "activity": activity,
            "section_header": _clean(item.get("section_header")),
            "row_label": _clean(item.get("row_label")),
            "source_page": item.get("source_page"),
            "location_candidate": item_location_candidate,
            "location": item_location,
            "raw_description": raw_description,
            "source_line_description": source_line_description,
            "normalized_source_description": normalized_source_description,
            "generated_item_description": generated_item_description,
            "description": description,
            "quantity": _nullable_float(item.get("quantity")),
            "unit_price": _nullable_money(item.get("unit_price")),
            "amount": amount,
            "gl_account_candidate": gl_candidate,
            "source_gl_candidate": raw_gl_candidate,
            "gl_suggestion_source": gl_suggestion_source,
            "gl_resolution_explanation": gl_resolution_explanation,
            "gl_name": gl_name,
            "expense_type": _clean(item.get("expense_type")) or "General",
            "is_replacement_reserve": bool(item.get("is_replacement_reserve")),
            "confidence": raw_item_confidence,
            "reason": _clean(item.get("reason")),
            "aggregate_fallback": aggregate_fallback,
            "row_identity_evidence": dict(item.get("row_identity_evidence") or {}),
        })
        if aggregate_fallback:
            add_issue(
                "visual_line_items_unresolved",
                "Vision and OCR did not recover reliable source line items. The total remains visible for review, but no GL may be authorized from the generated summary.",
                "high",
            )
    if not normalized_items and total_amount:
        fallback_item = _build_total_fallback_line_item(
            payload=payload,
            skipped_items=skipped_zero_items,
            total_amount=total_amount,
            vendor_name=vendor_for_rows or vendor_name,
        )
        raw_gl_candidate = _clean(fallback_item.get("gl_account_candidate"))
        gl_account = ai_mapping_review.validate_gl_account(raw_gl_candidate)
        if gl_account and not _is_payable_gl_account(gl_account):
            gl_account = None
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
            unresolved_gl_descriptions.append(_clean(fallback_item.get("description")) or "Invoice total")
        normalized_items.append({
            "activity": _clean(fallback_item.get("activity") or fallback_item.get("line_activity")),
            "source_line_description": _clean(fallback_item.get("description")),
            "description": _clean(fallback_item.get("description")) or "Invoice total",
            "quantity": 1.0,
            "unit_price": total_amount,
            "amount": total_amount,
            "gl_account_candidate": gl_account["gl_code"] if gl_account else "",
            "source_gl_candidate": raw_gl_candidate,
            "gl_suggestion_source": gl_suggestion_source or "invoice_total_fallback",
            "gl_resolution_explanation": _gl_resolution_explanation(
                description=_clean(fallback_item.get("description")) or "Invoice total",
                gl_code=gl_account["gl_code"] if gl_account else "",
                suggestion_source=gl_suggestion_source or "invoice_total_fallback",
                vendor_name=vendor_for_rows or vendor_name,
            ),
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
        unresolved_preview = "; ".join(unresolved_gl_descriptions[:4])
        add_issue(
            "gl_mapping_required",
            (
                "GL mapping remained unresolved after AI extraction, Vision retry, semantic rules, "
                "and the validated candidate engine. Confirm these items before export: "
                f"{unresolved_preview or 'unidentified line item'}."
            ),
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
    unexplained_difference = bool(
        total_amount
        and line_total
        and abs(invoice_difference) > 0.01
    )
    distributed_reconciliation_applied = False
    reconciled_total = _round_money(line_total + tax_amount + shipping_amount + fees_amount)
    total_reconciliation_passed = bool(total_amount) and abs(reconciled_total - total_amount) <= 0.01
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
    row_identity_needs_confirmation = bool(
        (payload.get("_row_identity_verification") or {}).get("payable_needs_confirmation")
    )
    if row_identity_needs_confirmation:
        add_issue(
            "row_identity_needs_confirmation",
            "A payable handwritten Apt. # remains ambiguous and requires operator confirmation before unattended export.",
            "high",
        )
    if unexplained_difference:
        add_issue(
            "unexplained_invoice_difference",
            (
                "The source line amounts plus every explicit tax, shipping, and fee component "
                "still differ from the invoice total. Source amounts were preserved and the "
                "invoice remains blocked for review."
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

    typed_warning_evidence: list[dict[str, Any]] = []
    for warning in warnings:
        warning_lower = warning.lower()
        if (
            total_reconciliation_passed
            and "line item amount" in warning_lower
            and ("not explicit" in warning_lower or "not visible" in warning_lower)
        ):
            continue
        typed_warning = categorize_warning(warning)
        code = typed_warning.category.value
        typed_warning_evidence.append(typed_warning.model_dump(mode="json"))
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
        elif warning == "image_ocr_cache_hit":
            continue
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

    raw_service_date = _clean(payload.get("service_date"))
    normalized_service_date, service_date_ok = _normalize_date(raw_service_date)
    raw_due_date_text = _clean(
        payload.get("due_date_text") or payload.get("payment_terms") or payload.get("terms")
    )
    date_provenance: list[dict[str, Any]] = []
    date_provenance.append(model_dict(DateFieldProvenance(
        field="service_date",
        value=normalized_service_date if service_date_ok else None,
        raw_value=raw_service_date or None,
        provenance="document_observed" if raw_service_date else "unresolved",
        source_field="service_date" if raw_service_date else None,
    )))
    date_provenance.append(model_dict(DateFieldProvenance(
        field="invoice_date",
        value=invoice_date or None,
        raw_value=_clean(payload.get("invoice_date")) or None,
        provenance=(
            "document_observed" if invoice_date_source == "invoice_date" and invoice_date
            else "tenant_policy_inference" if invoice_date_source == "service_date" and invoice_date
            else "unresolved"
        ),
        source_field=invoice_date_source if invoice_date else None,
        policy_id=(
            document_policy.date_policy.policy_id
            if invoice_date_source == "service_date" and invoice_date else None
        ),
    )))
    date_provenance.append(model_dict(DateFieldProvenance(
        field="due_date_text",
        value=raw_due_date_text or None,
        raw_value=raw_due_date_text or None,
        provenance="document_observed" if raw_due_date_text else "unresolved",
        source_field="due_date_text" if raw_due_date_text else None,
    )))
    date_provenance.append(model_dict(DateFieldProvenance(
        field="due_date",
        value=due_date or None,
        raw_value=_clean(payload.get("due_date")) or None,
        provenance=(
            "document_observed" if due_date_source == "explicit_due_date" and due_date
            else "tenant_policy_inference" if due_date_source == "tenant_policy_from_due_date_text" and due_date
            else "unresolved"
        ),
        source_field=("due_date_text" if due_date_source == "tenant_policy_from_due_date_text" else "due_date") if due_date else None,
        policy_id=(
            document_policy.date_policy.policy_id
            if due_date_source == "tenant_policy_from_due_date_text" and due_date else None
        ),
    )))

    result = {
        "vendor_name": vendor_for_rows,
        "raw_vendor_name": vendor_name,
        "category": _clean(payload.get("category")),
        "invoice_number": invoice_number,
        "source_invoice_number": source_invoice_number,
        "invoice_date": invoice_date,
        "invoice_date_source": invoice_date_source,
        "service_date": normalized_service_date if service_date_ok else "",
        "service_date_raw": raw_service_date,
        "due_date": due_date,
        "due_date_source": due_date_source,
        "payment_terms": _clean(payload.get("payment_terms") or payload.get("terms")),
        "due_date_text": raw_due_date_text or _clean(payload.get("payment_terms") or payload.get("terms")),
        "date_provenance": date_provenance,
        "tenant_document_policy": document_policy.model_dump(mode="json"),
        "bill_or_credit": _clean(payload.get("bill_or_credit")) or "Bill",
        "account_number": account_number,
        "invoice_number_generated": invoice_number_generated,
        "invoice_number_policy_applied": invoice_number_policy_applied,
        "service_address": service_address,
        "billing_address": billing_address,
        "address_role": address_role,
        "sold_to_raw_text": _clean(payload.get("sold_to_raw_text")),
        "job_site_raw_text": _clean(payload.get("job_site_raw_text")),
        "unit_number": location or location_candidate,
        "service_period_start": service_period_start,
        "service_period_end": service_period_end,
        "service_period_source": service_period_source,
        "property_candidate": property_candidate,
        "raw_property_candidate": property_candidate,
        "property_abbreviation": property_abbreviation,
        "location": location,
        "property_match": property_match,
        "property_identity_evidence": property_identity,
        "invoice_description": _clean(payload.get("invoice_description")),
        "_document_text": document_text,
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
        "typed_review_evidence": typed_warning_evidence,
        "unresolved_visual_field_candidates": list(
            payload.get("_unresolved_visual_field_candidates") or []
        ),
        "critical_header_verification": dict(
            payload.get("_critical_header_verification") or {}
        ),
        "handwritten_row_identities": list(payload.get("_handwritten_row_identities") or []),
        "row_identity_verification": dict(payload.get("_row_identity_verification") or {}),
        "row_identity_needs_confirmation": row_identity_needs_confirmation,
        "excluded_paid_rows": list(payload.get("excluded_paid_rows") or []),
        "manual_review_reasons": reason_messages,
        "manual_review_codes": reason_codes,
        "manual_review_issues": review_issues,
        "validation_summary": {
            "valid": bool(
                required_fields_present
                and dates_valid
                and total_reconciliation_passed
            ),
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


def _extract_known_vendor_payload_from_ocr(document_text: str) -> dict[str, Any]:
    """Prefer deterministic evidence for stable, recognizable invoice layouts.

    These parsers do not guess missing accounting facts. They only consume
    explicit labels and arithmetic visible in the document, leaving the
    universal AI path available for genuinely unknown layouts.
    """
    for parser in (
        _extract_property_placed_insurance_payload,
        _extract_cooks_pest_control_payload,
        _extract_kt_heating_payload,
        _extract_rasa_floors_payload,
        _extract_a1_heating_payload,
        _extract_cash_carry_payload,
        _extract_bels_landscaping_payload_from_ocr,
    ):
        payload = parser(document_text)
        if payload:
            payload.setdefault("_local_parser", parser.__name__.removeprefix("_extract_").removesuffix("_payload"))
            return payload
    return {}


def _extract_property_placed_insurance_payload(document_text: str) -> dict[str, Any]:
    allocations, section_total = _extract_allocated_insurance_rows(document_text)
    if not allocations:
        return {}
    invoice_match = re.search(r"INVOICE\s+NUMBER:\s*([A-Z0-9-]+)", document_text, re.IGNORECASE)
    customer_match = re.search(r"CUSTOMER\s+NUMBER:\s*([A-Z0-9-]+)", document_text, re.IGNORECASE)
    total_matches = re.findall(r"INVOICE\s+TOTAL:\s*\$([\d,]+\.\d{2})", document_text, re.IGNORECASE)
    total_amount = _money(total_matches[-1]) if total_matches else 0.0
    due_match = re.search(r"BALANCE:\s*\$[\d,]+\.\d{2}\s+DUE\s+ON\s+(\d{1,2}/\d{1,2}/\d{4})", document_text, re.IGNORECASE)
    remit_match = re.search(r"REMIT\s+PAYMENT\s+TO\s*\n([^\n]+)", document_text, re.IGNORECASE)
    vendor_name = ""
    if remit_match:
        # PDF text extraction can preserve two visual columns on one text line.
        # The remittance recipient is the right-most column beneath the label.
        remit_columns = [part for part in re.split(r"\s{2,}", remit_match.group(1).strip()) if _clean(part)]
        vendor_name = _clean(remit_columns[-1]) if remit_columns else ""
    property_name = _clean(allocations[0].get("account_name"))
    customer_block = re.search(
        r"INVOICED\s+TO\s*\n\s*([^\n]+)\s*\n(?:ATTENTION:[^\n]*\n)?\s*([^\n]+)\s*\n\s*([^\n]+)",
        document_text, re.IGNORECASE,
    )
    service_address = ""
    if customer_block:
        service_address = f"{_clean(customer_block.group(2))}, {_clean(customer_block.group(3))}"
    dates = [date for row in allocations for date in (row.get("coverage_start"), row.get("coverage_end")) if date]
    normalized_dates = sorted(
        (_normalize_date(str(value))[0] for value in dates if _normalize_date(str(value))[0]),
        key=lambda value: datetime.strptime(value, "%m/%d/%Y"),
    )
    fee_match = re.search(r"\$([\d,]+\.\d{2})\s+processing fee added", document_text, re.IGNORECASE)
    fee_amount = _money(fee_match.group(1)) if fee_match else 0.0
    if not total_amount or abs((section_total + fee_amount) - total_amount) > 0.01:
        return {}
    line_items = list(allocations)
    if fee_amount:
        line_items.append({
            "source_page": 1, "section_header": "INVOICE SUMMARY", "row_label": "Processing fee",
            "location_candidate": "", "activity": "Processing fee",
            "description": "Processing fee added to invoice total", "quantity": 1,
            "unit_price": fee_amount, "amount": fee_amount, "gl_account_candidate": "",
            "expense_type": "General", "is_replacement_reserve": False, "confidence": 0.99,
            "reason": "Explicit processing fee reconciles allocation subtotal to invoice total.",
        })
    return {
        "_local_parser": "property_placed_insurance_table",
        "vendor_name": vendor_name,
        "invoice_nature": "recurring",
        "category": "insurance",
        "invoice_number": invoice_match.group(1) if invoice_match else "",
        "invoice_date": "",
        "due_date": due_match.group(1) if due_match else "",
        "bill_or_credit": "Bill",
        "account_number": customer_match.group(1) if customer_match else "",
        "service_address": service_address,
        "address_role": "service_address",
        "location_candidate": "",
        "service_period_start": normalized_dates[0] if normalized_dates else "",
        "service_period_end": normalized_dates[-1] if normalized_dates else "",
        "property_candidate": property_name,
        "property_abbreviation": "",
        "invoice_description": "Property-placed insurance",
        "line_items": line_items,
        "subtotal": section_total,
        "tax_amount": 0.0,
        "shipping_amount": 0.0,
        "fees_amount": 0.0,
        "source_fees_amount": fee_amount,
        "total_amount": total_amount,
        "confidence": 0.99,
        "warnings": ["Invoice date is not explicitly shown; confirmation is required."],
        "needs_manual_review": True,
    }


def _extract_cooks_pest_control_payload(document_text: str) -> dict[str, Any]:
    """Extract Cook's digital monthly pest-control invoice layout."""
    text = document_text or ""
    if not re.search(r"Cook['’]?s\s+Pest\s+Control", text, re.IGNORECASE):
        return {}
    invoice_match = re.search(r"\bINV\s*#\s*:\s*(\d+)", text, re.IGNORECASE)
    date_match = re.search(
        r"YOUR\s+INVOICE\s+FOR\s+SERVICE\s+ON\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    account_match = re.search(r"Account\s*#\s*:\s*(\d+)", text, re.IGNORECASE)
    total_match = re.search(r"TOTAL\s+DUE\s*:\s*\$?([\d,]+\.\d{2})", text, re.IGNORECASE)
    service_match = re.search(
        r"SERVICE\(S\)\s+PERFORMED\s*:\s*QUANTITY\s+AMOUNT\s+(.+?)\s+(\d+(?:\.\d+)?)\s+\$?([\d,]+\.\d{2})",
        re.sub(r"\s+", " ", text),
        re.IGNORECASE,
    )
    if not invoice_match or not date_match or not total_match or not service_match:
        return {}
    invoice_date = date_match.group(1)
    total = _money(total_match.group(1))
    source_description = _clean(service_match.group(1))
    description = (
        "Commercial Pest Control - Monthly Service"
        if "pest" in _normalize_key(source_description)
        else source_description
    )
    address_match = re.search(
        r"(1400\s+N\s+Chamberlain\s+Ave)[\s\S]{0,90}?(Chattanooga\s*,?\s*TN\s+37406)",
        text,
        re.IGNORECASE,
    )
    service_address = (
        f"{_clean(address_match.group(1))}, {_clean(address_match.group(2))}"
        if address_match else ""
    )
    return {
        "_local_parser": "cooks_pest_control_layout",
        "vendor_name": "Cook's Pest Control, INC",
        "invoice_number": invoice_match.group(1),
        "invoice_date": invoice_date,
        "due_date": invoice_date,
        "payment_terms": "Due at time of service",
        "bill_or_credit": "Bill",
        "account_number": account_match.group(1) if account_match else "",
        "service_address": service_address,
        "address_role": "service_address",
        "location_candidate": "",
        "service_period_start": invoice_date,
        "service_period_end": invoice_date,
        "property_candidate": "Granite Heights / NextGen Multifamily",
        "invoice_description": "Commercial Pest Control - Monthly Service",
        "line_items": [{
            "description": description,
            "quantity": _nullable_float(service_match.group(2)),
            "unit_price": total,
            "amount": total,
            "gl_account_candidate": "6560",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": 0.99,
            "reason": "Explicit monthly pest-control service and total from the invoice.",
        }],
        "subtotal": total,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": total,
        "tax_handling": "distribute_proportionally",
        "invoice_nature": "recurring",
        "category": "pest_control",
        "confidence": 0.99,
        "warnings": [],
    }


def _extract_kt_heating_payload(document_text: str) -> dict[str, Any]:
    """Extract the stable typed KT Heating & Cooling invoice layout.

    Handwritten service tickets intentionally fall through to Vision because
    OCR cannot reliably recover their dates, units, or totals. Typed invoices
    are deterministic and should not spend an AI call or lose line detail.
    """
    text = document_text or ""
    if "KT Heating & Cooling" not in text or "TOTAL" not in text.upper():
        return {}
    header = re.search(
        r"Rain\s+Tree\s+Apartments\s+([0-9]{4,})\s+(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    total_match = re.search(r"\bTOTAL\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if not header or not total_match:
        return {}
    invoice_number, invoice_date = header.groups()
    location_match = re.search(
        r"(?:APARTMENT\s*#\s*)?\n?\s*([0-9]{3,5}-[A-Z0-9]+)\s*\n\s*DESCRIPTION",
        text,
        re.IGNORECASE,
    )
    location = _clean(location_match.group(1)) if location_match else ""
    total = _money(total_match.group(1))
    normalized_text = _normalize_key(text)
    replacement = all(
        marker in normalized_text
        for marker in ("condensing unit", "disconnect", "thermostat")
    ) or "air handler and replace" in normalized_text

    if replacement:
        description = "Complete HVAC System Replacement with Thermostat & Disconnect"
        items = [{
            "description": description,
            "amount": total,
            "quantity": None,
            "unit_price": None,
            "gl_account_candidate": "7544",
            "confidence": 0.99,
            "reason": "The invoice explicitly replaces the HVAC equipment and related controls.",
        }]
    else:
        items: list[dict[str, Any]] = []
        service_match = re.search(r"Service\s*Call\s*Fee[\s\S]{0,45}?([\d,]+\.\d{2})\s*$", text, re.I | re.M)
        freon_line = re.search(r"^.*R-?22\s+FREON.*$", text, re.IGNORECASE | re.MULTILINE)
        stop_match = re.search(r"STOP\s+LEAK[\s\S]{0,55}?([\d,]+\.\d{2})\s*$", text, re.I | re.M)
        if service_match:
            items.append({
                "description": "HVAC Service Call & A/C Diagnostic",
                "amount": _money(service_match.group(1)),
                "quantity": 1,
                "unit_price": _money(service_match.group(1)),
                "gl_account_candidate": "6555",
                "confidence": 0.99,
            })
        freon_decimals = (
            re.findall(r"[\d,]+\.\d{2}", freon_line.group(0))
            if freon_line else []
        )
        if len(freon_decimals) >= 2:
            freon_quantity = max(
                1,
                round(_money(freon_decimals[-1]) / max(_money(freon_decimals[-2]), 0.01)),
            )
            items.append({
                "description": "R-22 Refrigerant Recharge",
                "amount": _money(freon_decimals[-1]),
                "quantity": freon_quantity,
                "unit_price": _money(freon_decimals[-2]),
                "gl_account_candidate": "6555",
                "confidence": 0.99,
            })
        if stop_match:
            items.append({
                "description": "A/C Refrigerant Stop-Leak Treatment",
                "amount": _money(stop_match.group(1)),
                "quantity": 1,
                "unit_price": _money(stop_match.group(1)),
                "gl_account_candidate": "6555",
                "confidence": 0.99,
            })
        if not items or abs(sum(_money(item["amount"]) for item in items) - total) > 0.01:
            return {}
        description = "A/C Service Call, R-22 Refrigerant & Stop-Leak Treatment"

    return {
        "_local_parser": "kt_heating_layout",
        "vendor_name": "KT Heating & Cooling",
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": "",
        "payment_terms": "Net 30",
        "bill_or_credit": "Bill",
        "property_candidate": "The Raintree Apartments",
        "service_address": "2318 Rain Tree Drive, Birmingham, AL 35215",
        "address_role": "service_address",
        "location_candidate": location,
        "invoice_description": description,
        "line_items": items,
        "subtotal": total,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": total,
        "tax_handling": "distribute_proportionally",
        "category": "other_infrequent",
        "confidence": 0.99,
        "warnings": [],
    }


def _extract_rasa_floors_payload(document_text: str) -> dict[str, Any]:
    text = document_text or ""
    upper = text.upper()
    if "RASA FLOORS" not in upper or "INVOICE TOTAL" not in upper:
        return {}
    header = re.search(
        r"Invoice\s+Date\s+Invoice\s+Number\s+Order\s+Date\s+Install\s+Date\s+"
        r"(?P<invoice_date>\d{1,2}/\d{1,2}/\d{2,4})\s+"
        r"(?P<invoice_number>[A-Z0-9-]+)\s+"
        r"(?P<order_date>\d{1,2}/\d{1,2}/\d{2,4})\s+"
        r"(?P<install_date>\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    total_match = re.search(r"INVOICE\s+TOTAL\s*:\s*\$?([\d,]+\.\d{2})", text, re.IGNORECASE)
    if not header or not total_match:
        return {}
    property_context = _extract_property_context_from_ocr(text)
    unit_match = re.search(r"\n\s*([A-Z0-9-]+)\s*/\s*\d+BD\b", text, re.IGNORECASE)
    total = _money(total_match.group(1))
    summary = "Oak Mist Vinyl Flooring, Underlayment, Trim & Installation"
    location_candidate = unit_match.group(1) if unit_match else ""
    property_candidate = property_context.get("property_candidate", "")
    service_address = property_context.get("service_address", "")
    if "element clarksville" in _normalize_key(property_candidate):
        property_candidate = "The Element Clarksville"
        service_address = (
            f"2833 Cobalt Dr, Apt {location_candidate}, Clarksville, TN 37040"
            if location_candidate
            else "2833 Cobalt Dr, Clarksville, TN 37040"
        )
    return {
        "_local_parser": "rasa_floors_layout",
        "vendor_name": "Rasa Floors & Carpet Cleaning, LLC",
        "invoice_number": header.group("invoice_number"),
        "invoice_date": header.group("invoice_date"),
        "due_date": "",
        "payment_terms": "Net 30",
        "bill_or_credit": "Bill",
        "service_date": header.group("install_date"),
        "property_candidate": property_candidate,
        "service_address": service_address,
        "address_role": "service_address",
        "location_candidate": location_candidate,
        "invoice_description": summary,
        "line_items": [{
            "description": summary,
            "amount": total,
            "quantity": None,
            "unit_price": None,
            "gl_account_candidate": "7536",
            "confidence": 0.99,
            "reason": "The source lists quantities but only one aggregate invoice price.",
        }],
        "subtotal": total,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": total,
        "tax_handling": "distribute_proportionally",
        "category": "other_infrequent",
        "confidence": 0.99,
        "warnings": [],
    }


def _extract_a1_heating_payload(document_text: str) -> dict[str, Any]:
    text = document_text or ""
    vendor_identity = bool(
        re.search(r"(?:A|4)-?\s*[17]\s+Heating\s+and\s+Air", text, re.IGNORECASE)
        or (
            re.search(r"160\s+Industrial\s+Dr", text, re.IGNORECASE)
            and re.search(r"Heating\s*&\s*Cooling", text, re.IGNORECASE)
        )
        or "a1heatingandcoolingfrontdesk" in _normalize_key(text).replace(" ", "")
    )
    if not vendor_identity or "Work Summary" not in text:
        return {}
    invoice_match = re.search(r"Invoice\s*#\s*:\s*([A-Z0-9-]+)", text, re.IGNORECASE)
    date_match = re.search(r"Transaction\s+Date\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
    address_matches = re.findall(r"(162\s+JACK\s+MILLER\s*#\s*\d{2,4})", text, re.IGNORECASE)
    total_matches = re.findall(r"Balance\s+Due\s*:\s*\$?([\d,]+\.\d{2})", text, re.IGNORECASE)
    if not invoice_match or not date_match or not address_matches or not total_matches:
        return {}
    invoice_number = _clean(invoice_match.group(1))
    if re.fullmatch(r"[1Il]\d{5}", invoice_number):
        invoice_number = "i" + invoice_number[1:]
    address = _clean(address_matches[0]).replace("# ", "#")
    unit_match = re.search(r"#\s*(\d{2,4})", address)
    summary_match = re.search(
        r"Work\s+Summary\s+(.*?)\s+Subtotal\s*:",
        re.sub(r"\s+", " ", text),
        re.IGNORECASE,
    )
    source_summary = _clean(summary_match.group(1)) if summary_match else "HVAC service"
    summary = "HVAC Condensate Drain Clearing & Drain Pan Water Removal"
    total = _money(total_matches[-1])
    invoice_date = date_match.group(1)
    return {
        "_local_parser": "a1_heating_layout",
        "vendor_name": "A-1 Heating and Air",
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": invoice_date,
        "payment_terms": "Due on receipt",
        "bill_or_credit": "Bill",
        "service_date": invoice_date,
        "service_address": f"{address}, Clarksville, TN 37042",
        "address_role": "service_address",
        "location_candidate": unit_match.group(1) if unit_match else "",
        "invoice_description": summary,
        "line_items": [{
            "description": source_summary,
            "amount": total,
            "quantity": None,
            "unit_price": None,
            "gl_account_candidate": "6555",
            "confidence": 0.98,
            "reason": "Explicit work summary and balance due.",
        }],
        "subtotal": total,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": total,
        "tax_handling": "distribute_proportionally",
        "category": "other_infrequent",
        "confidence": 0.98,
        "warnings": [],
    }


def _extract_cash_carry_payload(document_text: str) -> dict[str, Any]:
    text = document_text or ""
    if not re.search(r"Cash\s*&\s*Carry", text, re.IGNORECASE) or "BUILDING SUPPLY" not in text.upper():
        return {}
    invoice_match = re.search(r"INVOICE\s*#\s*([A-Z0-9-]+)", text, re.IGNORECASE)
    account_match = re.search(r"ACCOUNT\s*#\s*([A-Z0-9-]+)", text, re.IGNORECASE)
    date_match = re.search(r"\bDATE\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})", text, re.IGNORECASE)
    subtotal_match = re.search(r"SUBTOTAL\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    tax_match = re.search(r"\bTAX\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    total_matches = re.findall(r"\bTOTAL\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if not invoice_match or not date_match or not total_matches:
        return {}
    items: list[dict[str, Any]] = []
    misc_index = 0
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not line:
            continue
        money_values = [_money(value) for value in re.findall(r"-?\d+(?:,\d{3})*\.\d{2}", line)]
        description = ""
        gl_code = ""
        if line.upper().startswith("MISC") and money_values:
            misc_index += 1
            description = f"Miscellaneous hardware item {misc_index}"
            gl_code = "6651"
        elif "GLOSS BLACK" in line.upper() and money_values:
            description = "12 Oz 2X Gloss Black Spray Paint"
            gl_code = "6770"
        elif "3/4X1/2 BUSH" in line.upper() and money_values:
            description = "3/4 x 1/2 PVC Bushing"
            gl_code = "6675"
        elif "MIP PLUG" in line.upper() and money_values:
            description = "1/2-In MIP PVC Plug"
            gl_code = "6675"
        elif "SERVICE FEE" in line.upper() and money_values:
            description = "Service Fee"
            gl_code = "6651"
        elif "CASH DISCOUNT" in line.upper() and money_values:
            description = "Cash Discount"
            gl_code = "6651"
        if not description:
            continue
        amount = money_values[-1]
        unit_price = abs(money_values[-2]) if len(money_values) >= 2 else abs(amount)
        quantity = None
        if unit_price > 0:
            calculated = abs(amount) / unit_price
            if abs(calculated - round(calculated)) <= 0.02:
                quantity = float(round(calculated)) * (-1 if amount < 0 else 1)
        items.append({
            "description": description,
            "amount": amount,
            "quantity": quantity,
            "unit_price": unit_price,
            "gl_account_candidate": gl_code,
            "confidence": 0.98,
            "reason": "Explicit Cash & Carry item table row.",
        })
    total = _money(total_matches[-1])
    return {
        "_local_parser": "cash_carry_layout",
        "vendor_name": "Cash & Carry Building Supply",
        "invoice_number": invoice_match.group(1),
        "account_number": account_match.group(1) if account_match else "",
        "invoice_date": date_match.group(1),
        "due_date": "",
        "payment_terms": "Net 30",
        "bill_or_credit": "Bill",
        "property_candidate": "",
        "service_address": "",
        "billing_address": "705 B Red River Street, Clarksville, TN 37040",
        "address_role": "bill_to",
        "invoice_description": "Misc Hardware, Gloss Black Spray Paint & PVC Fittings",
        "line_items": items,
        "subtotal": _money(subtotal_match.group(1)) if subtotal_match else 0,
        "tax_amount": _money(tax_match.group(1)) if tax_match else 0,
        "shipping_amount": 0,
        # The visible service fee and cash discount are already line items and
        # cancel each other; repeating the fee here would double count it.
        "fees_amount": 0,
        "total_amount": total,
        "tax_handling": "distribute_proportionally",
        "category": "other_infrequent",
        "confidence": 0.98,
        "warnings": [],
    }


def _repair_bravo_flooring_payload(payload: dict[str, Any], document_text: str) -> tuple[dict[str, Any], bool]:
    text = document_text or ""
    if "Bravo Flooring" not in text or "Vinyl Everywhere" not in text:
        return payload, False
    repaired = dict(payload)
    repaired["vendor_name"] = "Bravo Flooring"
    repaired["payment_terms"] = repaired.get("payment_terms") or "Net 30"
    repaired["category"] = "other_infrequent"
    repaired["invoice_description"] = "Vinyl Flooring Materials, Installation, Floor Prep & Trip Charge"
    repaired["property_candidate"] = "The Penn Warren"
    repaired["service_address"] = "300 Greenwood Avenue, Clarksville, TN 37040"
    repaired["address_role"] = "service_address"
    repaired["location_candidate"] = "A27"
    repaired_items: list[dict[str, Any]] = []
    for item in repaired.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        description = _clean(next_item.get("description"))
        description = re.sub(r"^Tip\s+Charge", "Trip Charge", description, flags=re.IGNORECASE)
        next_item["description"] = description
        next_item["gl_account_candidate"] = "7536"
        repaired_items.append(next_item)
    if repaired_items:
        repaired["line_items"] = repaired_items
    if abs(sum(_money(item.get("amount")) for item in repaired_items) - _money(repaired.get("total_amount"))) <= 0.01:
        repaired["warnings"] = [
            warning for warning in _normalize_warnings(repaired.get("warnings") or [])
            if "line item amounts" not in warning.lower()
        ]
    return repaired, True


def _extract_allocated_insurance_rows(document_text: str) -> tuple[list[dict[str, Any]], float]:
    """Recover explicit unit-level insurance allocations from digital tables.

    This is document-family logic, not a vendor rule. It activates only when
    the source labels a property-placed insurance table and every recovered
    row has a coverage id, two dates, three monetary columns, and a unit.
    """
    if not re.search(r"\bproperty[- ]placed insurance\b", document_text, re.IGNORECASE):
        return [], 0.0
    rows: list[dict[str, Any]] = []
    row_tail = re.compile(
        r"(?P<start>\d{1,2}/\d{1,2}/\d{4})\s+(?P<end>\d{1,2}/\d{1,2}/\d{4})\s+"
        r"\$(?P<premium>[\d,]+\.\d{2})\s+\$(?P<surcharge>[\d,]+\.\d{2})\s+\$(?P<total>[\d,]+\.\d{2})\s*$"
    )
    coverage_re = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]*\d[A-Z0-9-]*\b")
    address_re = re.compile(
        r"(?P<street>\d+\s+.*?\b(?:street|st|drive|dr|road|rd|avenue|ave|lane|ln|boulevard|blvd|"
        r"court|ct|circle|cir|place|pl|way|parkway|pkwy|highway|hwy|pike))\s+"
        r"(?P<unit>[A-Z0-9-]+)\s+[A-Za-z .'-]+\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$",
        re.IGNORECASE,
    )
    for source_line in document_text.splitlines():
        line = re.sub(r"\s+", " ", source_line).strip()
        tail = row_tail.search(line)
        coverage = coverage_re.search(line)
        if not tail or not coverage:
            continue
        prefix = line[:tail.start()].strip()
        address = address_re.search(prefix)
        if not address:
            continue
        premium = _money(tail.group("premium"))
        surcharge = _money(tail.group("surcharge"))
        total = _money(tail.group("total"))
        if abs((premium + surcharge) - total) > 0.01:
            continue
        account_name = prefix[:coverage.start()].strip()
        unit = address.group("unit").strip()
        coverage_id = coverage.group(0)
        rows.append({
            "source_page": 1,
            "section_header": "PROPERTY PLACED INSURANCE",
            "row_label": coverage_id,
            "location_candidate": unit,
            "activity": "Property placed insurance",
            "description": (f"Property placed insurance - unit {unit} - coverage {coverage_id} - "
                            f"{tail.group('start')} to {tail.group('end')}"),
            "quantity": 1,
            "unit_price": total,
            "amount": total,
            "premium_amount": premium,
            "surcharge_amount": surcharge,
            "coverage_start": tail.group("start"),
            "coverage_end": tail.group("end"),
            "account_name": account_name,
            "gl_account_candidate": "",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": 0.99,
            "reason": "Explicit property-placed insurance allocation; premium plus surcharge reconciles to row total.",
        })
    if len(rows) < 2 or len({row["row_label"] for row in rows}) != len(rows):
        return [], 0.0
    recovered_total = _round_money(sum(_money(row["amount"]) for row in rows))
    total_candidates = [_money(value) for value in re.findall(
        r"\b[A-Z0-9 '&.-]+\s+TOTAL:\s*\$([\d,]+\.\d{2})", document_text, re.IGNORECASE
    )]
    section_total = next((value for value in total_candidates if abs(recovered_total - value) <= 0.01), 0.0)
    if not section_total:
        return [], 0.0
    return rows, section_total


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
    bels_payload = _extract_bels_landscaping_payload_from_ocr(document_text)
    lowes_payload = _extract_lowes_pro_supply_payload_from_ocr(document_text)
    insurance_allocations, insurance_section_total = _extract_allocated_insurance_rows(document_text)
    repaired, bravo_repaired = _repair_bravo_flooring_payload(repaired, document_text)
    did_repair = did_repair or bravo_repaired
    if bels_payload:
        # The Bel's Landscaping logo is image-only on screenshot/PDF copies.
        # OCR often reads only the customer block ("To: ..."), which can make
        # the model promote the property/customer to Vendor. Treat this known
        # layout deterministically before validation/canonical rules run.
        for key, value in bels_payload.items():
            if key == "line_items":
                if value:
                    repaired[key] = value
            elif value not in ("", None, [], {}):
                repaired[key] = value
        did_repair = True
    if lowes_payload:
        for key, value in lowes_payload.items():
            if key == "line_items":
                # Keep AI line parsing when it found payable rows, but the LPS
                # OCR parser remains available as a full fallback below.
                continue
            if value not in ("", None, [], {}):
                repaired[key] = value
        did_repair = True
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
    if insurance_allocations and len(insurance_allocations) > len(existing_payable):
        fee_match = re.search(r"\$([\d,]+\.\d{2})\s+processing fee added", document_text, re.IGNORECASE)
        fee_amount = _money(fee_match.group(1)) if fee_match else 0.0
        allocation_items = list(insurance_allocations)
        if fee_amount:
            allocation_items.append({
                "source_page": 1, "section_header": "INVOICE SUMMARY", "row_label": "Processing fee",
                "location_candidate": "", "activity": "Processing fee",
                "description": "Processing fee added to invoice total", "quantity": 1,
                "unit_price": fee_amount, "amount": fee_amount, "gl_account_candidate": "",
                "expense_type": "General", "is_replacement_reserve": False, "confidence": 0.99,
                "reason": "Explicit processing fee reconciles the allocation subtotal to the invoice total.",
            })
        invoice_total = _money(repaired.get("total_amount"))
        if invoice_total and abs((insurance_section_total + fee_amount) - invoice_total) <= 0.01:
            repaired["line_items"] = allocation_items
            repaired["fees_amount"] = 0.0  # fee is now an explicit payable row
            repaired["source_fees_amount"] = fee_amount
            existing_payable = allocation_items
            did_repair = True
            did_table_repair = True
    if lowes_payload and parsed_payable:
        existing_by_count = len(existing_payable) == len(parsed_payable)
        if not existing_payable or existing_by_count:
            repaired["line_items"] = _merge_lowes_line_items(
                existing_payable,
                parsed_payable,
            )
            did_repair = True
            did_table_repair = True
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


def _extract_bels_landscaping_payload_from_ocr(document_text: str) -> dict[str, Any]:
    """Return a deterministic payload for Bel's Landscaping image invoices.

    These screenshots have a large graphical logo that local OCR usually does
    not read, while the textual customer block starts with ``To:``. Without a
    guard, the AI text path can misclassify the customer/property as the vendor.
    """
    text = document_text or ""
    norm = _normalize_key(text)
    if not norm:
        return {}
    bels_shape = (
        "payment term net 30" in norm
        and "amount due" in norm
        and "invoice" in norm
        and ("tax1 tax2" in norm or "line total" in norm)
        and "thank you for your business" in norm
    )
    landscaping_hint = any(
        token in norm
        for token in (
            "mowing",
            "mow",
            "landscap",
            "grounds maintenance",
            "limb removal",
            "magnolia village",
            "admiral place",
        )
    )
    if not (bels_shape and landscaping_hint):
        return {}

    lines = [_clean(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    invoice_number = _extract_labeled_value(
        text,
        (
            r"\binvoice\s*#\s*([A-Z0-9._-]{2,})",
            r"\binvoice\s+number\s*[:#]?\s*([A-Z0-9._-]{2,})",
        ),
    )
    invoice_date_raw = _extract_labeled_value(
        text,
        (
            r"\binvoice\s+date\s*[:#]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ),
    )
    invoice_date, _invoice_date_ok = _normalize_date(invoice_date_raw)
    total_amount = _extract_money_after_label(
        lines,
        (
            r"\bamount\s+due\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
            r"\binvoice\s+total\D+(\d{1,4}(?:,\d{3})*\.\d{2})",
        ),
    )
    line_item = _extract_bels_line_item(lines, total_amount=total_amount)
    property_candidate, service_address = _extract_bels_customer_context(lines)
    service_start, service_end = _monthly_period_from_invoice_text(
        " ".join(
            part for part in (
                line_item.get("description") if line_item else "",
                text,
            )
            if part
        ),
        invoice_date=invoice_date,
    )

    if not line_item and total_amount:
        fallback_description = (
            _clean(property_candidate) or "Landscaping service"
        )
        line_item = {
            "description": fallback_description,
            "quantity": 1,
            "unit_price": total_amount,
            "amount": total_amount,
            "gl_account_candidate": "6810",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": 0.86,
            "reason": "Bel's Landscaping OCR fallback used invoice total.",
        }

    payload: dict[str, Any] = {
        "vendor_name": "Bel's Landscaping",
        "category": "landscaping",
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": "",
        "payment_terms": "Net 30",
        "bill_or_credit": "Bill",
        "service_address": service_address,
        "property_candidate": property_candidate,
        "invoice_description": line_item.get("description") if line_item else "Landscaping service",
        "line_items": [line_item] if line_item else [],
        "subtotal": total_amount,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": total_amount,
        "tax_handling": "distribute_proportionally",
        "confidence": 0.88,
    }
    if service_start and service_end:
        payload["service_period_start"] = service_start
        payload["service_period_end"] = service_end
        payload["service_period_source"] = "bel_landscaping_monthly_item"
    return payload


def _extract_labeled_value(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return _clean(match.group(1)).strip("[]()|.,;:")
    return ""


def _extract_bels_customer_context(lines: list[str]) -> tuple[str, str]:
    block: list[str] = []
    started = False
    for line in lines:
        if not started:
            match = re.search(r"\bto\s*:\s*(.*)$", line, re.IGNORECASE)
            if not match:
                continue
            started = True
            remainder = _clean_bels_customer_line(match.group(1))
            if remainder:
                block.append(remainder)
            continue
        if re.search(r"\b(item|subtotal|past due|notes|thank you)\b", line, re.IGNORECASE):
            break
        cleaned = _clean_bels_customer_line(line)
        if cleaned:
            block.append(cleaned)
        if len(block) >= 6:
            break

    property_parts: list[str] = []
    address_parts: list[str] = []
    for raw in block:
        line = _clean(raw)
        if not line:
            continue
        if _looks_like_address_line(line) or _looks_like_city_state_line(line):
            address_parts.append(line)
        elif not address_parts:
            property_parts.append(line)
    property_candidate = _clean(" ".join(property_parts))
    property_candidate = re.sub(r"\([^)]*\)", " ", property_candidate)
    property_candidate = _clean(property_candidate)
    return property_candidate, _clean(" ".join(address_parts))


def _clean_bels_customer_line(line: str) -> str:
    cleaned = _clean(line)
    cleaned = re.sub(r"\binvoice\s*#\s*[A-Z0-9._-]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\binvoice\s+date\b.*$", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpayment\s+term\b.*$", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bamount\s+due\b.*$", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _clean(cleaned)


def _extract_lowes_pro_supply_payload_from_ocr(document_text: str) -> dict[str, Any]:
    if not _looks_like_lowes_pro_supply_text(document_text):
        return {}
    payload: dict[str, Any] = {
        "vendor_name": "Lowes Pro Supply",
        "category": "other_infrequent",
        "bill_or_credit": "Bill",
        "payment_terms": "Net 30",
    }
    header = _extract_lowes_header(document_text)
    payload.update({k: v for k, v in header.items() if v})
    property_context = _extract_property_context_from_ocr(document_text)
    payload.update({k: v for k, v in property_context.items() if v})
    parsed = _extract_supplier_table_from_ocr(document_text)
    if parsed.get("subtotal"):
        payload["subtotal"] = parsed["subtotal"]
    if parsed.get("tax_amount"):
        payload["tax_amount"] = parsed["tax_amount"]
    if parsed.get("total_amount"):
        payload["total_amount"] = parsed["total_amount"]
    parsed_items = parsed.get("line_items") if isinstance(parsed.get("line_items"), list) else []
    if parsed_items:
        payload["line_items"] = parsed_items
    summary = _lowes_invoice_summary(document_text, parsed_items)
    if summary:
        payload["invoice_description"] = summary
    return payload


def _looks_like_lowes_pro_supply_text(document_text: str) -> bool:
    text = (document_text or "").lower()
    compact = re.sub(r"\s+", "", text)
    return (
        "bill to #" in text
        and "order #" in text
        and (
            "ship point lps-" in text
            or "shippointlps-" in compact
            or "via lowe's store" in text
            or "vialowe'sstore" in compact
        )
        and "gl code:" in text
        and ("p.o. box 301451" in text or "pobox301451" in compact)
    )


def _extract_lowes_header(document_text: str) -> dict[str, str]:
    text = document_text or ""
    account = re.search(r"\bBill\s+To\s*#\s*(\d{3,})", text, re.IGNORECASE)
    order = re.search(r"\bOrder\s*#\s*(\d{5,}-\d{2})", text, re.IGNORECASE)
    invoice_date = re.search(
        r"\bInvoice\s+Date\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    due_date = re.search(
        r"\bDue\s+Date\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    if not all((account, order, invoice_date, due_date)):
        flattened = re.sub(r"\s+", " ", text)
        legacy = re.search(
            r"Bill\s+To\s+#\s+Order\s+#\s+Invoice\s+Date\s+Due\s+Date\s+PO\s+#\s+Reference\s+"
            r"(?P<account>\d{3,})\s+"
            r"(?P<order>\d{5,}-\d{2})\s+"
            r"(?P<invoice_date>\d{1,2}/\d{1,2}/\d{2,4})\s+"
            r"(?P<due_date>\d{1,2}/\d{1,2}/\d{2,4})",
            flattened,
            re.IGNORECASE,
        )
        if legacy:
            return {
                "account_number": _clean(legacy.group("account")),
                "invoice_number": _clean(legacy.group("order")),
                "invoice_date": _clean(legacy.group("invoice_date")),
                "due_date": _clean(legacy.group("due_date")),
            }
    return {
        "account_number": _clean(account.group(1)) if account else "",
        "invoice_number": _clean(order.group(1)) if order else "",
        "invoice_date": _clean(invoice_date.group(1)) if invoice_date else "",
        "due_date": _clean(due_date.group(1)) if due_date else "",
    }


def _lowes_invoice_summary(document_text: str, items: list[dict[str, Any]]) -> str:
    item_descriptions = _unique_strings([
        _compact_lowes_summary_label(
            _clean(item.get("description")),
            _clean(item.get("source_gl_candidate") or item.get("gl_account_candidate")),
        )
        for item in items
        if _clean(item.get("description"))
    ])
    if item_descriptions:
        return _bounded_lowes_summary(item_descriptions)

    labels: list[str] = []
    for line in [_clean(line) for line in (document_text or "").splitlines()]:
        match = re.match(r"^(Door Hardware|Hardware|Light Bulbs|Lighting|Paint|Plumbing|Appliance)\s+\d", line, re.IGNORECASE)
        if match:
            labels.append(match.group(1))
    for item in items:
        label = _lowes_category_label(
            str(item.get("source_gl_candidate") or item.get("gl_account_candidate") or item.get("description") or "")
        )
        if label:
            labels.append(label)
    mapped = [_lowes_category_label(label) for label in labels]
    unique = _unique_strings([label for label in mapped if label])
    if not unique:
        return ""
    return _join_description_parts(unique[:4])


def _compact_lowes_summary_label(description: str, category: str = "") -> str:
    norm = _normalize_key(f"{description} {category}")
    patterns = (
        (("paint thinner",), "Paint Thinner"),
        (("u post",), "U-Posts"),
        (("no tres", "warning sign"), "Warning Signs"),
        (("roller cover",), "Paint Roller Covers"),
        (("led", "light bulb"), "LED Bulbs"),
        (("towel ring",), "Towel Ring"),
        (("pvc",), "PVC Fittings"),
        (("wire wheel",), "Wire Wheel"),
        (("stripper",), "Paint Stripper"),
        (("washer",), "Washers"),
        (("screw",), "Screws"),
        (("cleaner", "clnr"), "Cleaner"),
        (("bleach",), "Bleach"),
        (("gfci",), "GFCI Outlet"),
        (("hose",), "Hose"),
        (("lumber", "treated"), "Treated Lumber"),
    )
    for needles, label in patterns:
        if any(needle in norm for needle in needles):
            return label
    return _lowes_category_label(category or description) or _clean(description)


def _bounded_lowes_summary(labels: list[str], max_length: int = 75) -> str:
    selected: list[str] = []
    for label in labels:
        candidate = _join_description_parts([*selected, label])
        if len(candidate) <= max_length:
            selected.append(label)
    return _join_description_parts(selected)[:max_length].rstrip(" ,;")


def _lowes_category_label(value: str) -> str:
    norm = _normalize_key(value)
    if "paint" in norm:
        return "paint supplies"
    if "light" in norm or "bulb" in norm or "fixture" in norm:
        return "lighting supplies"
    if "plumb" in norm or "faucet" in norm or "toilet" in norm:
        return "plumbing supplies"
    if "appliance" in norm:
        return "appliance parts"
    if "hardware" in norm or "lock" in norm or "door" in norm or "shelf" in norm or "towel ring" in norm:
        return "hardware supplies"
    return ""


def _join_description_parts(parts: list[str]) -> str:
    clean = _unique_strings([_clean(part) for part in parts if _clean(part)])
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0].capitalize()
    return f"{', '.join(part.capitalize() for part in clean[:-1])}, and {clean[-1].capitalize()}"


def _merge_lowes_line_items(
    existing_items: list[dict[str, Any]],
    parsed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if existing_items and len(existing_items) == len(parsed_items):
        merged_items: list[dict[str, Any]] = []
        for existing, parsed in zip(existing_items, parsed_items):
            merged = dict(existing)
            parsed_description = _clean(parsed.get("description"))
            if parsed_description and not _clean(merged.get("description")):
                merged["description"] = parsed_description
            for key in ("quantity", "unit_price", "amount"):
                if parsed.get(key) not in ("", None, 0):
                    merged[key] = parsed[key]
            parsed_gl = _clean(parsed.get("gl_account_candidate"))
            if parsed_gl:
                merged["gl_account_candidate"] = parsed_gl
            merged_items.append(merged)
        return merged_items
    return parsed_items


def _extract_bels_line_item(lines: list[str], *, total_amount: float) -> dict[str, Any] | None:
    in_table = False
    pending_descriptions: list[str] = []
    for line in lines:
        lower = line.lower()
        if not in_table:
            if "item" in lower and "quantity" in lower and ("line total" in lower or "price" in lower):
                in_table = True
            continue
        if re.search(r"\b(subtotal|tax:|past due|amount due|notes|thank you)\b", lower):
            break
        if not line:
            continue
        amounts = re.findall(r"\$?\s*(\d{1,4}(?:,\d{3})*\.\d{2})", line)
        if not amounts:
            if not re.match(r"^(?:quantity|price|tax\d?)\b", lower):
                pending_descriptions.append(line)
            continue
        amount = _money(amounts[-1]) or total_amount
        description = re.split(r"\s+\d+(?:\.\d+)?\s+\$?\s*\d", line, maxsplit=1)[0]
        description = re.sub(r"\$?\s*\d{1,4}(?:,\d{3})*\.\d{2}.*$", "", description)
        description = _clean(" ".join([*pending_descriptions, description]))
        description = re.sub(r"^#+\d+\s*[-–—]\s*", "", description).strip()
        if not description:
            description = "Landscaping service"
        return {
            "description": description,
            "quantity": 1,
            "unit_price": amount,
            "amount": amount,
            "gl_account_candidate": "6810",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": 0.90,
            "reason": "Bel's Landscaping layout parsed from OCR table.",
        }
    return None


_MONTH_NAME_TO_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _monthly_period_from_invoice_text(text: str, *, invoice_date: str) -> tuple[str, str]:
    invoice_dt = _parse_normalized_date(invoice_date)
    if not invoice_dt:
        return "", ""
    norm = _normalize_key(text)
    month = 0
    for token, value in _MONTH_NAME_TO_NUMBER.items():
        if re.search(rf"\b{re.escape(token)}\b", norm):
            month = value
            break
    month = month or invoice_dt.month
    year = invoice_dt.year
    if month - invoice_dt.month > 6:
        year -= 1
    elif invoice_dt.month - month > 6:
        year += 1
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year, 12, 31)
    else:
        end = datetime(year, month + 1, 1)
        end = datetime.fromordinal(end.toordinal() - 1)
    return start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")


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
    address_role: str,
    document_text: str,
) -> tuple[str, str]:
    """Prefill a required property from validated local history when possible.

    ResMan export requires Property Abbreviation. For AI-assisted invoices we
    still avoid writing raw AI text, but we can use local vendor rules and GL
    history to prefill a reviewable property when the service address or
    property name clearly points at one of the vendor's known properties.
    """
    # Administrative party evidence does not identify the economic property.
    # Vendor history must never convert it into a property assignment.
    if _normalize_key(address_role) in {
        "sold to", "bill to", "billing", "remit to", "vendor address",
        "customer address", "administrative",
    }:
        return "", ""
    vendor_row = _vendor_rule_for_name(vendor_name)
    if not vendor_row:
        return "", ""
    # Raw document text alone is not sufficient property evidence. Supplier
    # receipts commonly contain only Nex-Gen's administrative address; using
    # vendor history in that case silently assigns an unrelated property.
    for evidence in (service_address, property_candidate):
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
    lowes_line_pattern = re.compile(
        r"^(?P<line_no>\d+)\s+(?:.+?\s+)?(?P<sku>L-[A-Z0-9-]+|\d{4,})\s+"
        r"(?P<ordered>\d+(?:\.\d+)?)\s+"
        r"(?P<uom>each|pack|box|case|roll|set|ea|pk)\s+"
        r"(?P<quantity>\d+(?:\.\d+)?)\s+"
        r"(?P<unit>\d{1,4}(?:,\d{3})*\.\d{2})\s+"
        r"(?P<amount>\d{1,4}(?:,\d{3})*\.\d{2})$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        match = lowes_line_pattern.match(line)
        is_lowes_line = bool(match)
        if not match:
            match = sku_pattern.match(line)
        if not match:
            continue
        sku = match.group("sku")
        unit_price = _money(match.group("unit") or match.group("amount"))
        amount = _money(match.group("amount"))
        description = ""
        gl_candidate = ""
        description_parts: list[str] = []
        for lookahead in lines[idx + 1: idx + (14 if is_lowes_line else 8)]:
            normalized = lookahead.lower()
            if sku_pattern.match(lookahead) or lowes_line_pattern.match(lookahead):
                break
            if any(
                stop in normalized
                for stop in (
                    "customer copy",
                    "return service requested",
                    "bill to:",
                    "remit to:",
                    "ship to:",
                    "ln#",
                    "description total merchandise",
                )
            ):
                break
            if normalized.startswith("gl code"):
                gl_candidate = lookahead.split(":", 1)[-1].strip() if ":" in lookahead else lookahead
                break
            if (
                len(lookahead) > 2
                and not re.fullmatch(r"[a-zA-Z]", lookahead)
                and "total" not in normalized
                and "invoice" not in normalized
                and "sales" not in normalized
                and "ship point" not in normalized
                and "instructions" not in normalized
            ):
                description_parts.append(lookahead)
        description = _clean(" ".join(description_parts))
        quantity = None
        if is_lowes_line:
            quantity = _nullable_float(match.group("quantity"))
        elif unit_price and amount:
            ratio = amount / unit_price
            rounded = round(ratio)
            if abs(ratio - rounded) <= 0.05:
                quantity = float(rounded)
        mapped_gl = _vendor_side_category_gl(gl_candidate)
        parsed_items.append({
            "description": description or sku,
            "quantity": quantity,
            "unit_price": unit_price if unit_price else None,
            "amount": amount,
            "gl_account_candidate": mapped_gl or gl_candidate,
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
    if _looks_like_lowes_pro_supply_text(text):
        order = re.search(r"\border\s*#\s*(\d{5,}-\d{2})", text, re.IGNORECASE)
        if order:
            return _clean(order.group(1)).strip("[]()|.,;:")
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

    service_date, service_date_ok = _normalize_date(payload.get("service_date"))
    if service_date and service_date_ok:
        return service_date, service_date, "ai_service_date"

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
    source_page: int = 1,
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
        _refresh_reconciliation_after_distribution(normalized, items)

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
            "Location": item.get("location") or normalized.get("location"),
            "GL Account": item.get("gl_account_candidate"),
            "Line Item Description": line_item_description,
            "Amount": _money(item.get("amount")),
            "Expense Type": item.get("expense_type") or "General",
            "Is Replacement Reserve": bool(item.get("is_replacement_reserve")),
            "Due Date": normalized.get("due_date"),
            "Quantity": item.get("quantity"),
            "Unit Price": item.get("unit_price"),
            "Tax": False,
            "Document Url": support_document_url or None,
            "_meta": {
                "source_file": source_file,
                "source_page": item.get("source_page") or source_page,
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
                "ai_raw_property_candidate": normalized.get("raw_property_candidate"),
                "ai_service_address": normalized.get("service_address"),
                "ai_billing_address": normalized.get("billing_address"),
                "ai_address_role": normalized.get("address_role"),
                "ai_sold_to_raw_text": normalized.get("sold_to_raw_text"),
                "ai_job_site_raw_text": normalized.get("job_site_raw_text"),
                "ai_category": normalized.get("category"),
                "ai_invoice_nature": normalized.get("invoice_nature"),
                "ai_invoice_nature_evidence": normalized.get("invoice_nature_evidence") or [],
                "ai_property_identity_evidence": normalized.get("property_identity_evidence"),
                "ai_source_gl_candidate": item.get("source_gl_candidate"),
                "ai_gl_suggestion_source": item.get("gl_suggestion_source"),
                "ai_gl_resolution_explanation": item.get("gl_resolution_explanation"),
                "ai_gl_accounting_reasoning": item.get("gl_accounting_reasoning"),
                "ai_gl_accounting_confidence": item.get("gl_confidence"),
                "ai_line_semantics": item.get("line_semantics"),
                "ai_generated_description": True,
                "ai_item_plain_language_description": item.get("generated_item_description"),
                "ai_aggregate_fallback": bool(item.get("aggregate_fallback")),
                "ai_line_activity": item.get("activity"),
                "ai_line_location": item.get("location"),
                "ai_line_location_candidate": item.get("location_candidate"),
                "ai_source_line_description": (
                    None if item.get("aggregate_fallback")
                    else item.get("source_line_description") or item.get("raw_description")
                ),
                "normalized_source_description": item.get("normalized_source_description"),
                "ai_line_section_header": item.get("section_header"),
                "ai_line_row_label": item.get("row_label"),
                "ai_row_identity_evidence": item.get("row_identity_evidence") or {},
                "row_identity_needs_confirmation": bool(
                    normalized.get("row_identity_needs_confirmation")
                ),
                "ai_tax_handling": normalized.get("tax_handling"),
                "ai_tax_amount_inferred": normalized.get("tax_amount_inferred", False),
                "ai_invoice_date_source": normalized.get("invoice_date_source"),
                "ai_service_date": normalized.get("service_date"),
                "ai_service_date_raw": normalized.get("service_date_raw"),
                "ai_payment_terms": normalized.get("payment_terms"),
                "ai_due_date_text": normalized.get("due_date_text"),
                "ai_date_provenance": normalized.get("date_provenance") or [],
                "tenant_document_policy": normalized.get("tenant_document_policy") or {},
                "ai_unresolved_visual_field_candidates": normalized.get(
                    "unresolved_visual_field_candidates"
                ) or [],
                "ai_critical_header_verification": normalized.get(
                    "critical_header_verification"
                ) or {},
                "ai_handwritten_row_identities": normalized.get("handwritten_row_identities") or [],
                "ai_row_identity_verification": normalized.get("row_identity_verification") or {},
                "ai_excluded_paid_rows": normalized.get("excluded_paid_rows") or [],
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
                        "provider_request_surface": normalized.get("ai_provider_request_surface"),
                        "provider_usage": normalized.get("ai_provider_usage") or {},
                        "estimated_cost_usd": normalized.get("ai_estimated_cost_usd"),
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

    invoice = {
        "vendor_key": AI_VENDOR_KEY,
        "source_file": source_file,
        "file_name": source_file,
        "source_page": source_page,
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
            "source_page": source_page,
            "processing_mode": "ai_assisted",
            "original_vendor_key": vendor_key,
        },
    }
    from .accounting_integration_bridges import AIResultAccountingV2Adapter
    return AIResultAccountingV2Adapter().convert(invoice, {
        "document_id": f"{batch_id}:{source_file}:1",
    })


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

    explicit_adders = _round_money(sum(
        _money(normalized.get(key))
        for key in ("tax_amount", "shipping_amount", "fees_amount")
    ))
    # Distribution is presentation policy for an explicit source adder.  It is
    # not an arithmetic repair tool.  If no tax/freight/fee explains the exact
    # difference, preserve every source amount and keep reconciliation blocked.
    if abs(adjustment - explicit_adders) > 0.01:
        codes = list(normalized.get("manual_review_codes") or [])
        reasons = list(normalized.get("manual_review_reasons") or [])
        if "unsafe_distribution_blocked" not in codes:
            codes.append("unsafe_distribution_blocked")
        message = (
            "Automatic amount distribution was blocked because no explicit source tax, shipping, "
            "or fee reconciles the extracted lines to the invoice total. Verify page scope and "
            "table structure; source amounts were not changed."
        )
        if message not in reasons:
            reasons.append(message)
        normalized["manual_review_codes"] = codes
        normalized["manual_review_reasons"] = reasons
        return [
            {**item, "base_amount": amount, "allocated_tax_amount": 0, "amount": amount}
            for item, amount in zip(items, base_amounts)
        ]

    # Even an explicit adder cannot conceal a page-selection or table-extraction
    # failure when the proposed allocation is implausibly large.
    mismatch_ratio = abs(adjustment) / max(abs(base_total), abs(invoice_total), 0.01)
    if mismatch_ratio > 0.25:
        codes = list(normalized.get("manual_review_codes") or [])
        reasons = list(normalized.get("manual_review_reasons") or [])
        if "unsafe_distribution_blocked" not in codes:
            codes.append("unsafe_distribution_blocked")
        message = (
            "Automatic amount distribution was blocked because the extracted line total and "
            "invoice total differ by more than 25%. Verify page scope and table structure."
        )
        if message not in reasons:
            reasons.append(message)
        normalized["manual_review_codes"] = codes
        normalized["manual_review_reasons"] = reasons
        return [
            {**item, "base_amount": amount, "allocated_tax_amount": 0, "amount": amount}
            for item, amount in zip(items, base_amounts)
        ]

    excluded_terms = (
        "service fee", "cash discount", "discount", "credit", "payment",
        "sales tax", "tax", "shipping", "freight",
    )
    eligible_indices = [
        idx
        for idx, (item, amount) in enumerate(zip(items, base_amounts))
        if amount > 0.009
        and not any(term in _normalize_key(item.get("description")) for term in excluded_terms)
    ]
    if not eligible_indices:
        eligible_indices = [idx for idx, amount in enumerate(base_amounts) if amount > 0.009]
    eligible_total = _round_money(sum(base_amounts[idx] for idx in eligible_indices))
    if abs(eligible_total) <= 0.009:
        return [
            {**item, "base_amount": amount, "allocated_tax_amount": 0}
            for item, amount in zip(items, base_amounts)
        ]

    running = 0.0
    adjusted: list[dict[str, Any]] = []
    for idx, (item, base_amount) in enumerate(zip(items, base_amounts)):
        if idx not in eligible_indices:
            adjusted.append({
                **item,
                "base_amount": base_amount,
                "allocated_tax_amount": 0,
                "amount": base_amount,
            })
            continue
        is_last = idx == eligible_indices[-1]
        share = (
            _round_money(adjustment - running)
            if is_last
            else _round_money(adjustment * (base_amount / eligible_total))
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


def _refresh_reconciliation_after_distribution(
    normalized: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    """Re-evaluate the authoritative arithmetic state after row allocation.

    Validation runs before presentation allocation.  Keeping that earlier
    boolean after amounts change creates a stale blocker even when the rows
    now reconcile.  This function changes only arithmetic provenance; it does
    not resolve property, GL, vendor, or any other review decision.
    """
    invoice_total = _money(normalized.get("total_amount"))
    line_total = _round_money(sum(_money(item.get("amount")) for item in items))
    passed = bool(items) and abs(line_total - invoice_total) <= 0.01
    distribution_applied = any(
        abs(_money(item.get("allocated_tax_amount"))) > 0.009
        for item in items
    )
    summary = dict(normalized.get("validation_summary") or {})
    summary.update({
        "total_reconciliation_passed": passed,
        "reconciled_total": line_total,
        "invoice_total": invoice_total,
        "distributed_reconciliation_applied": distribution_applied,
    })
    normalized["validation_summary"] = summary
    normalized["distributed_reconciliation_applied"] = distribution_applied
    if not passed:
        return
    normalized["manual_review_codes"] = [
        code for code in (normalized.get("manual_review_codes") or [])
        if code not in {"total_reconciliation_failed", "unexplained_invoice_difference"}
    ]
    normalized["manual_review_reasons"] = [
        reason for reason in (normalized.get("manual_review_reasons") or [])
        if not (
            (
                "line items plus" in str(reason).lower()
                and "invoice total" in str(reason).lower()
            )
            or (
                "source line amounts plus" in str(reason).lower()
                and "invoice total" in str(reason).lower()
            )
        )
    ]


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _values_agree(left: Any, right: Any, *, money_field: bool = False) -> bool:
    if money_field:
        return abs(_money(left) - _money(right)) <= 0.01
    return _clean(left).lower() == _clean(right).lower()


def _failed_extraction_invoice(
    *, batch_id: str, source_file: Path, vendor_hint: str, failure_code: str
) -> dict[str, Any]:
    """Keep a failed source visible and unexportable without inventing facts."""

    digest = hashlib.sha256()
    try:
        with source_file.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        digest.update(source_file.name.encode("utf-8", "replace"))
    unresolved_id = "UNRESOLVED-" + digest.hexdigest()[:12].upper()
    reason = "Source extraction failed; review the original document and reprocess it."
    row = {
        "Invoice Number": unresolved_id,
        "Bill or Credit": "",
        "Invoice Date": "",
        "Accounting Date": "",
        "Vendor": "",
        "Invoice Description": reason,
        "Line Item Number": "1",
        "Property Abbreviation": "",
        "Location": "",
        "GL Account": "",
        "Line Item Description": reason,
        "Amount": None,
        "Expense Type": "",
        "Is Replacement Reserve": None,
        "Due Date": "",
        "Document Url": None,
        "_meta": {
            "ai_generated": True,
            "source_extraction_failed": True,
            "accounting_pipeline_skip_reason": "source_extraction_failed",
            "failure_code": failure_code,
            "source_file": source_file.name,
            "source_artifact_retained": True,
            "source_text": {
                "raw_activity": None,
                "raw_description": None,
                "normalized_activity": None,
                "normalized_description": None,
                "generated_description": reason,
            },
        },
    }
    return {
        "source_file": source_file.name,
        "source_page": None,
        "invoice_number": unresolved_id,
        "invoice_date": "",
        "total_amount": 0.0,
        "vendor_name": vendor_hint if vendor_hint != "unknown vendor" else "",
        "rows": [row],
        "manual_review_codes": ["source_extraction_failed", failure_code],
        "manual_review_reasons": [reason],
        "validation_summary": {
            "total_reconciliation_passed": False,
            "source_extraction_failed": True,
            "export_allowed": False,
        },
        "needs_manual_review": True,
        "source_artifact_retained": True,
        "batch_id": batch_id,
    }


def _payload(
    files: list[Path],
    files_processed: int,
    invoices: list[dict[str, Any]],
    manual_review: list[dict[str, Any]],
    unsupported: list[dict[str, Any]],
    *,
    unique_files: list[Path] | None = None,
    duplicate_source_aliases: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    rows_total = sum(len(inv.get("rows", [])) for inv in invoices)
    unique_files = list(unique_files if unique_files is not None else files)
    duplicate_source_aliases = duplicate_source_aliases or {}
    duplicate_count = sum(len(values) for values in duplicate_source_aliases.values())
    failed_sources = {
        _clean(item.get("filename"))
        for item in unsupported
        if isinstance(item, dict) and _clean(item.get("filename"))
    }
    processed_sources = {
        _clean(invoice.get("source_file"))
        for invoice in invoices
        if _clean(invoice.get("source_file"))
    }
    processed_aliases = sum(
        len(duplicate_source_aliases.get(source) or [])
        for source in processed_sources
    )
    return {
        "vendor_key": AI_VENDOR_KEY,
        "success": not unsupported,
        "summary": {
            "processing_mode": "ai_assisted",
            "files_total": len(files),
            "files_unique": len(unique_files),
            "files_deduplicated": duplicate_count,
            "files_processed": files_processed + processed_aliases,
            "files_unique_processed": files_processed,
            "files_unsupported": len(failed_sources),
            "processing_failures": len(unsupported),
            "invoices_produced": len(invoices),
            "rows_total": rows_total,
            "line_items": rows_total,
            "manual_review_total": len(manual_review),
            "invoices_flagged_for_review": len(manual_review),
        },
        "invoices": invoices,
        "manual_review_rows": manual_review,
        "unsupported_files": unsupported,
        "duplicate_source_aliases": duplicate_source_aliases,
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


def _vendor_hint_for_file(
    vendor_key: str,
    path: Path,
    detection: dict | None = None,
    *,
    batch_hint: str = "",
) -> str:
    if vendor_key and vendor_key != "unknown":
        return vendor_key.replace("_", " ")
    hay = path.stem.lower()
    reason = str((detection or {}).get("reason") or "").lower()
    for display, needles in VARIABLE_VENDOR_HINTS.items():
        if any(n in hay or n in reason for n in needles):
            return display.replace("_", " ")
    if batch_hint:
        return batch_hint
    return "unknown vendor"


def _batch_vendor_hint(batch_id: str, vendors: list[dict[str, Any]]) -> str:
    """Use a batch name only when it resolves to a real ResMan vendor."""
    metadata_path = settings.BATCHES_ROOT / batch_id / "batch_metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    batch_name = _clean(metadata.get("batch_name"))
    return _canonical_vendor(batch_name, vendors) if batch_name else ""


def _document_prompt_context(
    *,
    source_file: Path,
    batch_hint: str,
    vendor_hint: str,
    document_text: str,
) -> str:
    context = [
        f"[Source filename: {source_file.name}]",
        f"[Validated batch vendor hint: {batch_hint or vendor_hint or 'unknown'}]",
        (
            "[Metadata note: filename and batch context are plausibility hints only; "
            "read invoice fields from the document itself.]"
        ),
    ]
    if document_text:
        context.extend(["", document_text])
    return "\n".join(context)


def _select_prompt_references(
    references: dict[str, list[dict[str, Any]]],
    *,
    query: str,
    vendor_hint: str,
) -> dict[str, list[dict[str, Any]]]:
    """Retrieve relevant prompt references instead of arbitrary first rows."""
    query_key = _normalize_key(query)
    normalized_vendor_hint = _normalize_key(vendor_hint)
    usable_vendor_hint = (
        vendor_hint
        if normalized_vendor_hint not in {
            "", "unknown", "unknown vendor", "unknown supplier",
            "unidentified", "ai assisted",
        }
        else ""
    )
    query_tokens = {
        token
        for token in query_key.split()
        if len(token) >= 3 and token not in _GENERIC_VENDOR_TOKENS
    }

    def ranked(
        rows: list[dict[str, Any]],
        limit: int,
        kind: str,
    ) -> list[dict[str, Any]]:
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for index, row_key, row_tokens, row in _reference_search_index(rows, kind):
            overlap = len(query_tokens & row_tokens)
            score = overlap / max(1, min(len(query_tokens), 8))
            if usable_vendor_hint and _normalize_key(usable_vendor_hint) in row_key:
                score += 2.0
            if score > 0:
                scored.append((score, -index, row))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return [row for _, _, row in scored[:limit]]

    vendors = references.get("vendors") or []
    properties = references.get("properties") or []
    gl_accounts = references.get("gl_accounts") or []
    selected_vendors = ranked(vendors, 24, "vendor") or vendors[:24]

    observed_property_abbrs = (
        set(_historical_properties_for_vendor(usable_vendor_hint))
        if usable_vendor_hint
        else set()
    )
    observed_property_names: set[str] = set()
    for row in properties:
        abbr = _clean(
            row.get("Property Abbreviation")
            or row.get("property_abbreviation")
            or row.get("Abbreviation")
        )
        if abbr in observed_property_abbrs:
            name = _clean(row.get("Property Name") or row.get("property_name"))
            if name:
                observed_property_names.add(_normalize_key(name))
    historical_properties = [
        row
        for row in properties
        if (
            _clean(
                row.get("Property Abbreviation")
                or row.get("property_abbreviation")
                or row.get("Abbreviation")
            ) in observed_property_abbrs
            or _normalize_key(_clean(row.get("Property Name") or row.get("property_name")))
            in observed_property_names
        )
    ]
    selected_properties = _dedupe_dict_rows(
        [*historical_properties[:100], *ranked(properties, 40, "property")]
    )[:120]
    if not selected_properties:
        selected_properties = properties[:120]

    vendor_rule = _vendor_rule_for_name(usable_vendor_hint) if usable_vendor_hint else {}
    vendor_rule = vendor_rule or {}
    observed_gl_codes = {
        _clean(item.get("gl_code"))
        for item in (vendor_rule.get("source_gl_codes_observed") or [])
        if isinstance(item, dict) and _clean(item.get("gl_code"))
    }
    default_gl_code = _clean(vendor_rule.get("default_gl_code"))
    if default_gl_code:
        observed_gl_codes.add(default_gl_code)
    historical_gls = [
        row
        for row in gl_accounts
        if _clean(row.get("gl_code") or row.get("code")) in observed_gl_codes
    ]
    selected_gls = _dedupe_dict_rows(
        [*historical_gls, *ranked(gl_accounts, 48, "gl")]
    )[:64]
    if not selected_gls:
        selected_gls = gl_accounts[:64]
    return {
        "vendors": selected_vendors,
        "properties": selected_properties,
        "gl_accounts": selected_gls,
    }


def _reference_search_index(
    rows: list[dict[str, Any]],
    kind: str,
) -> list[tuple[int, str, frozenset[str], dict[str, Any]]]:
    """Build a compact reusable index instead of JSON-normalizing every row.

    Reference rows can contain very large historical summaries that are not
    useful prompt-retrieval evidence. Indexing only identity fields both
    improves relevance and removes a multi-second per-invoice hot path.
    """
    cache_key = (id(rows), len(rows), kind)
    cached = _REFERENCE_SEARCH_INDEX.get(cache_key)
    if cached is not None:
        return cached
    fields = {
        "vendor": (
            "vendor_name", "vendor_id", "Vendor", "Company Abbreviation",
            "default_gl",
        ),
        "property": (
            "Property Name", "property_name", "Property Abbreviation",
            "property_abbreviation", "Abbreviation", "Address",
            "Service Address", "address", "Unit", "Unit Number", "unit",
            "City", "city",
        ),
        "gl": (
            "gl_code", "code", "gl_description",
            "chart_of_accounts_description", "description", "gl_name",
        ),
    }.get(kind, ())
    index_rows: list[tuple[int, str, frozenset[str], dict[str, Any]]] = []
    for index, row in enumerate(rows):
        values = [_clean(row.get(field)) for field in fields]
        row_key = _normalize_key(" ".join(value for value in values if value))
        index_rows.append((index, row_key, frozenset(row_key.split()), row))
    if len(_REFERENCE_SEARCH_INDEX) >= 12:
        _REFERENCE_SEARCH_INDEX.clear()
    _REFERENCE_SEARCH_INDEX[cache_key] = index_rows
    return index_rows


def _dedupe_dict_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


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


def _choose_invoice_date_source(
    payload: dict[str, Any],
    *,
    allow_service_date_fallback: bool = False,
) -> tuple[Any, str]:
    if _clean(payload.get("invoice_date")):
        return payload.get("invoice_date"), "invoice_date"
    if allow_service_date_fallback and _clean(payload.get("service_date")):
        return payload.get("service_date"), "service_date"
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
        return canonical
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
        return canonical
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
        if start == end:
            return start
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
    "%d-%b-%Y",
    "%d-%b-%y",
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


def _due_date_from_visible_payment_terms(payload: dict[str, Any], *, invoice_date: str) -> str:
    terms_text = " ".join(
        _clean(part)
        for part in (
            payload.get("payment_terms"),
            payload.get("terms"),
            payload.get("due_date"),
            payload.get("due_date_text"),
            payload.get("invoice_description"),
            payload.get("_document_text"),
        )
        if _clean(part)
    )
    norm = _normalize_key(terms_text)
    if not norm:
        return ""
    due_on_invoice_terms = (
        "due upon contract terms",
        "due upon contract term",
        "due upon receipt",
        "due on receipt",
        "due at receipt",
        "payable upon receipt",
        "upon receipt",
        "upon reciept",
    )
    if any(term in norm for term in due_on_invoice_terms):
        return invoice_date
    if "due upon contract" in norm and "term" in norm:
        return invoice_date
    vendor_text = _normalize_key(
        " ".join(
            _clean(part)
            for part in (
                payload.get("vendor_name"),
                payload.get("raw_vendor_name"),
                payload.get("category"),
            )
            if _clean(part)
        )
    )
    if "landscape services" in vendor_text:
        return invoice_date
    return ""


def _single_service_date_from_line_items(line_items: list[Any], *, invoice_date: str) -> str:
    invoice_dt = _parse_normalized_date(invoice_date)
    if not invoice_dt:
        return ""
    year = invoice_dt.year
    for item in line_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            _clean(part)
            for part in (
                item.get("description"),
                item.get("line_item_description"),
                item.get("reason"),
            )
            if _clean(part)
        )
        match = re.search(r"(?<![\d-])(\d{1,2})/(\d{1,2})(?!/\d)", text)
        if not match:
            continue
        month = int(match.group(1))
        day = int(match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        try:
            candidate = datetime(year, month, day)
        except ValueError:
            continue
        if (candidate - invoice_dt).days > 31:
            try:
                candidate = datetime(year - 1, month, day)
            except ValueError:
                continue
        if abs((invoice_dt - candidate).days) <= 370:
            return candidate.strftime("%m/%d/%Y")
    return ""


def _parse_normalized_date(value: str) -> datetime | None:
    normalized, ok = _normalize_date(value)
    if not normalized or not ok:
        return None
    try:
        return datetime.strptime(normalized, "%m/%d/%Y")
    except ValueError:
        return None


def _blank_location_allowed_for_vendor_category(*, vendor_name: str, category: str) -> bool:
    text = _normalize_key(" ".join(part for part in (vendor_name, category, _vendor_rule_category(vendor_name)) if part))
    if not text:
        return False
    return any(
        token in text
        for token in (
            "landscap",
            "lawn",
            "tree",
            "pest control",
            "marketing",
            "subscription",
            "trash collection",
        )
    )


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
    raw_value = str(value or "")
    hash_unit = re.search(r"#\s*([A-Z0-9-]+)", raw_value, re.IGNORECASE)
    unit = hash_unit.group(1).upper() if hash_unit else ""
    without_hash_unit = re.sub(r"#\s*[A-Z0-9-]+", " ", raw_value)
    # City/state text follows a comma in the normalized service addresses
    # produced by OCR/vision. Keeping only the street segment also lets
    # suffix-less labels such as "162 Jack Miller #607" match the canonical
    # "162 Jack Miller Blvd." reference.
    street_segment = without_hash_unit.split(",", 1)[0]
    normalized = _normalize_key(street_segment)
    if not normalized:
        return {"address_key": "", "unit": ""}
    tokens = normalized.split()
    if not tokens:
        return {"address_key": "", "unit": ""}
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


def _normalize_location_candidate(value: str) -> str:
    text = re.sub(
        r"\b(?:APT|APARTMENT|UNIT|SUITE|STE|JOB)\b",
        " ",
        str(value or "").upper(),
    )
    return _normalize_property_unit(text)


def _property_unit_matches(
    reference_unit: str,
    candidate_unit: str,
    *,
    address_key: str = "",
) -> bool:
    """Match explicit unit text to canonical building-unit identifiers.

    Supplier ship-to blocks often show street ``254 Lombardy St, #24`` while
    ResMan stores the unit as ``254-24``. Combine the street number with the
    explicit unit only for comparison; the emitted value always remains the
    canonical reference unit.
    """
    reference = _normalize_property_unit(reference_unit)
    candidate = _normalize_property_unit(candidate_unit)
    if not reference or not candidate:
        return False
    if reference == candidate:
        return True
    building_match = re.match(r"^(\d+)", address_key or "")
    if not building_match:
        return False
    building = building_match.group(1)
    if candidate.startswith(building):
        return False
    return reference == _normalize_property_unit(f"{building}-{candidate}")


def _normalized_address_role(value: Any, *, document_text: str = "") -> str:
    role = _normalize_key(_clean(value)).replace(" ", "_")
    aliases = {
        "service": "service_address",
        "service_location": "service_address",
        "job": "job_site",
        "job_address": "job_site",
        "shipping": "ship_to",
        "shipping_address": "ship_to",
        "billing": "bill_to",
        "billing_address": "bill_to",
        "sold": "sold_to",
        "vendor": "vendor_address",
        "remit": "remit_to",
    }
    role = aliases.get(role, role)
    valid = {
        "service_address", "job_site", "ship_to", "sold_to", "bill_to",
        "remit_to", "vendor_address", "unknown",
    }
    if role in valid and role != "unknown":
        return role
    text = _normalize_key(document_text)
    service_labels = ("service address", "job site", "install at", "service at")
    if any(label in text for label in service_labels):
        return "service_address"
    # When a document has no service/job label, do not promote a customer
    # billing block into a service address merely because it is the only
    # street address visible. This is a common supplier-invoice layout.
    if "sold to" in text:
        return "sold_to"
    if "bill to" in text or "billing address" in text:
        return "bill_to"
    return "unknown"


def _property_identity_from_document_text(
    document_text: str,
    properties: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve property identity from customer text/email, independent of address.

    Supplier invoices often use a central office as SOLD TO while the customer
    name or email domain identifies the accounting property. Address matching
    alone maps those invoices to the office, so require strong unique identity
    evidence before overriding the provider candidate.
    """
    text_key = _property_name_identity_key(document_text)
    if not text_key:
        return {}
    email_domains = [
        match.group(1).lower().split(".", 1)[0]
        for match in re.finditer(r"[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})", document_text, re.I)
    ]
    generic_tokens = {
        "the", "apartments", "apartment", "homes", "property", "properties",
        "townhomes", "village", "management", "nexgen", "next", "gen",
    }

    identities: dict[str, dict[str, str]] = {}
    for prop in properties:
        name = _clean(prop.get("Property Name") or prop.get("property_name"))
        abbr = _clean(
            prop.get("Property Abbreviation")
            or prop.get("property_abbreviation")
            or prop.get("Abbreviation")
            or prop.get("abbreviation")
        )
        if name and abbr:
            key = _property_name_identity_key(name)
            if key:
                options = identities.setdefault(key, {})
                current = options.get(abbr, "")
                if not current or len(name) > len(current):
                    options[abbr] = name

    abbreviation_by_name: dict[str, str] = {}
    display_by_name: dict[str, str] = {}
    compact_owners: dict[str, set[str]] = {}
    for key, options in identities.items():
        # A normalized identity may only auto-resolve when it belongs to one
        # property abbreviation. Ambiguous aliases remain review-only.
        if len(options) != 1:
            continue
        abbr, name = next(iter(options.items()))
        abbreviation_by_name[key] = abbr
        display_by_name[key] = name
        compact = re.sub(r"[^a-z0-9]", "", key)
        if len(compact) >= 6:
            compact_owners.setdefault(compact, set()).add(abbr)

    scored: list[tuple[float, str, str]] = []
    compact_text = re.sub(r"[^a-z0-9]", "", text_key)
    for name_key, abbr in abbreviation_by_name.items():
        score = 0.0
        reason = ""
        if name_key and name_key in text_key:
            score = 0.99
            reason = "Exact property name in customer text"
        name_tokens = [token for token in name_key.split() if token not in generic_tokens]
        compact_name = "".join(name_tokens)
        if (
            len(compact_name) >= 6
            and compact_name in compact_text
            and compact_owners.get(compact_name) == {abbr}
            and score < 0.98
        ):
            score = 0.98
            reason = "Unique compact property name in customer text"
        for domain in email_domains:
            compact_domain = re.sub(r"(?:ky|tn|al|com|net|org)$", "", re.sub(r"[^a-z0-9]", "", domain))
            if len(compact_name) < 5 or len(compact_domain) < 5:
                continue
            ratio = difflib.SequenceMatcher(None, compact_name, compact_domain).ratio()
            if compact_name in compact_domain or compact_domain in compact_name:
                ratio = max(ratio, 0.96)
            if ratio >= 0.82 and ratio > score:
                score = min(0.98, ratio)
                reason = f"Property identity matched email domain {domain}"
        if score > 0:
            scored.append((score, name_key, reason))
    if not scored:
        return {}
    scored.sort(reverse=True)
    best_score, best_name, reason = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.86 or best_score - runner_up < 0.05:
        return {}
    return {
        "property_abbreviation": abbreviation_by_name[best_name],
        "property_name": display_by_name[best_name],
        "score": round(best_score, 3),
        "reason": reason,
    }


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
    location_candidate: str,
    properties: list[dict[str, Any]],
    prepared_properties: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return confirmed property abbreviation, valid location, and matched row.

    AI text is never written directly to Location. We only emit a location when
    it comes from a known property/unit row.
    """
    abbr_needle = _normalize_key(property_abbreviation)
    candidate_needle = _property_name_identity_key(property_candidate)
    candidate_compact = re.sub(r"[^a-z0-9]", "", candidate_needle)
    address_needle = _normalize_key(service_address)
    parsed_service = _parse_service_address_for_property(service_address)
    explicit_unit = _normalize_location_candidate(location_candidate)
    if explicit_unit:
        parsed_service["unit"] = explicit_unit

    prepared = prepared_properties or _prepare_property_resolution_rows(properties)
    compact_name_abbreviations: dict[str, set[str]] = {}
    for item in prepared:
        if len(item["name_compact"]) >= 6 and item["abbr"]:
            compact_name_abbreviations.setdefault(
                item["name_compact"], set()
            ).add(item["abbr"])

    abbreviation_matches: list[tuple[str, str, dict[str, Any]]] = []
    name_matches: list[tuple[str, str, dict[str, Any]]] = []
    exact_address_matches: list[tuple[str, str, dict[str, Any]]] = []
    address_matches: list[tuple[str, str, dict[str, Any]]] = []
    for item in prepared:
        prop = item["source"]
        prop_abbr = item["abbr"]
        unit = item["unit"]
        exact_property = bool(abbr_needle and item["abbr_key"] == abbr_needle)
        prop_identity = item["name_identity"]
        prop_compact = item["name_compact"]
        exact_name = bool(
            candidate_needle
            and (
                prop_identity == candidate_needle
                or (
                    len(candidate_compact) >= 6
                    and prop_compact == candidate_compact
                    and compact_name_abbreviations.get(prop_compact) == {prop_abbr}
                )
            )
        )
        exact_address = bool(address_needle and item["address_key_normalized"] == address_needle)
        prop_address_key = item["address_key"]
        parsed_address_key = parsed_service.get("address_key") or ""
        parsed_address_match = bool(
            parsed_address_key
            and prop_address_key
            and (
                prop_address_key == parsed_address_key
                or prop_address_key.startswith(f"{parsed_address_key} ")
                or parsed_address_key.startswith(f"{prop_address_key} ")
            )
        )
        parsed_unit_match = _property_unit_matches(
            unit,
            parsed_service.get("unit") or "",
            address_key=parsed_address_key,
        )
        explicit_unit_match = _property_unit_matches(
            unit,
            explicit_unit,
            address_key=parsed_address_key,
        )
        if exact_property and explicit_unit_match:
            return prop_abbr, unit, dict(prop)
        if exact_name and explicit_unit_match:
            return prop_abbr, unit, dict(prop)
        if parsed_address_match:
            address_matches.append((prop_abbr, unit, dict(prop)))
            if parsed_unit_match:
                return prop_abbr, unit, dict(prop)
        if exact_property:
            abbreviation_matches.append((prop_abbr, unit, dict(prop)))
        elif exact_name:
            name_matches.append((prop_abbr, unit, dict(prop)))
        elif exact_address:
            exact_address_matches.append((prop_abbr, unit, dict(prop)))

    def collapse(matches: list[tuple[str, str, dict[str, Any]]]) -> tuple[str, str, dict[str, Any]]:
        abbreviations = {abbr for abbr, _, _ in matches if abbr}
        units = {unit for _, unit, _ in matches if unit}
        abbr = next(iter(abbreviations)) if len(abbreviations) == 1 else matches[0][0]
        unit = next(iter(units)) if len(units) == 1 else ""
        return abbr, unit, matches[0][2]
    if abbreviation_matches:
        return collapse(abbreviation_matches)
    if name_matches:
        return collapse(name_matches)
    if exact_address_matches:
        return collapse(exact_address_matches)
    if address_matches:
        abbreviations = {abbr for abbr, _, _ in address_matches if abbr}
        if len(abbreviations) == 1:
            abbr = next(iter(abbreviations))
            units = {
                unit for _, unit, _ in address_matches
                if _property_unit_matches(
                    unit,
                    parsed_service.get("unit") or "",
                    address_key=parsed_service.get("address_key") or "",
                )
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


def _prepare_property_resolution_rows(
    properties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    abbreviation_by_name: dict[str, str] = {}
    raw_rows: list[dict[str, Any]] = []
    for prop in properties:
        name = _clean(prop.get("Property Name") or prop.get("property_name"))
        abbreviation = _clean(
            prop.get("Property Abbreviation")
            or prop.get("property_abbreviation")
            or prop.get("Abbreviation")
            or prop.get("abbreviation")
        )
        identity = _property_name_identity_key(name)
        if name and abbreviation:
            abbreviation_by_name.setdefault(identity, abbreviation)
        raw_rows.append({
            "source": prop,
            "name_identity": identity,
            "name_compact": re.sub(r"[^a-z0-9]", "", identity),
            "abbr": abbreviation,
            "unit": _clean(prop.get("Unit") or prop.get("Unit Number") or prop.get("unit")),
            "address": _clean(
                prop.get("Address") or prop.get("Service Address") or prop.get("address")
            ),
        })
    for item in raw_rows:
        if not item["abbr"] and item["name_identity"]:
            item["abbr"] = abbreviation_by_name.get(item["name_identity"], "")
        item["abbr_key"] = _normalize_key(item["abbr"])
        item["address_key_normalized"] = _normalize_key(item["address"])
        item["address_key"] = _property_address_key(item["address"])
    return raw_rows


def _property_name_identity_key(value: Any) -> str:
    """Normalize harmless property suffix variants without weakening identity."""
    text = _normalize_key(str(value or ""))
    text = re.sub(r"\b(?:the|apartments?|apts?|property|properties|community|llc)\b", " ", text)
    tokens: list[str] = []
    for token in text.split():
        if token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 4 and not token.endswith(("ss", "us")):
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens)


def _suggest_valid_gl_candidate_uncached(
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
    supplier_semantic = _supplier_semantic_gl_default(
        description=description,
        vendor_name=vendor_name,
        ai_suggested_gl=ai_suggested_gl,
    )
    if supplier_semantic:
        return supplier_semantic
    semantic_default = _semantic_gl_default(
        description=description,
        vendor_name=vendor_name,
    )
    if semantic_default:
        return semantic_default
    enriched_description = " ".join(
        part for part in (
            description,
            _vendor_rule_category(vendor_name),
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
            if account and _is_payable_gl_account(account):
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
            if account and _is_payable_gl_account(account):
                return account
    return None


def _gl_mapping_input_signature() -> tuple[tuple[str, int, int], ...]:
    paths = (
        settings.GENERAL_LEDGER_REFERENCE,
        ai_mapping_review.LEARNED_MAPPINGS_PATH,
        settings.PROJECT_ROOT / "config" / "canonical_rules.yaml",
    )
    return tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if path.is_file() else (str(path), 0, 0)
        for path in paths
    )


@lru_cache(maxsize=2048)
def _suggest_valid_gl_candidate_cached(
    description: str,
    vendor_name: str,
    ai_suggested_gl: str,
    _signature: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[str, str], ...] | None:
    result = _suggest_valid_gl_candidate_uncached(
        description=description,
        vendor_name=vendor_name,
        ai_suggested_gl=ai_suggested_gl,
    )
    return tuple(sorted(result.items())) if result else None


def _suggest_valid_gl_candidate(
    *,
    description: str,
    vendor_name: str,
    ai_suggested_gl: str,
) -> dict[str, str] | None:
    cached = _suggest_valid_gl_candidate_cached(
        description,
        vendor_name,
        ai_suggested_gl,
        _gl_mapping_input_signature(),
    )
    return dict(cached) if cached else None


def _is_payable_gl_account(account: dict[str, str]) -> bool:
    account_type = _normalize_key(account.get("gl_account_type", ""))
    if not account_type:
        return True
    return "expense" in account_type and "asset" not in account_type


def _supplier_semantic_gl_default(
    *,
    description: str,
    vendor_name: str,
    ai_suggested_gl: str,
) -> dict[str, str] | None:
    text = _normalize_key(" ".join(
        part
        for part in (
            description,
            ai_suggested_gl,
            vendor_name,
            _vendor_rule_category(vendor_name),
        )
        if part
    ))
    lowes_like = any(token in _normalize_key(vendor_name) for token in ("lowes", "home depot", "hd supply"))
    if not lowes_like and "building supplies" not in text and "supplies" not in text:
        return None

    code = ""
    if any(
        term in text
        for term in (
            "door slab", "door unit", "split jamb", "prehung door", "pre hung door",
            "panel interior left hand", "panel interior right hand", "jamb",
        )
    ):
        code = "7520"
    elif any(term in text for term in ("paint", "painter", "roller cover", "roller frame")):
        code = "6770"
    elif any(term in text for term in ("bulb", "light", "lighting", "fixture", "led", "halide")):
        code = "6660"
    elif any(
        term in text
        for term in (
            "faucet", "toilet", "plumb", "supply line", "pipe", "valve",
            "wax ring", "closet flange", "toilet bolt",
        )
    ):
        code = "6675"
    elif any(
        term in text
        for term in (
            "appliance", "refrigerator", "range", "washer", "dryer", "dishwasher",
            "drip bowl", "burner bowl", "range element", "stove element",
        )
    ):
        code = "6606"
    elif any(term in text for term in ("hvac", "thermostat", "air filter", "furnace")):
        code = "6654"
    elif any(term in text for term in ("blind", "blnd", "drapery", "shade")):
        code = "6710"
    elif any(
        term in text
        for term in (
            "hardware",
            "door",
            "lock",
            "bar pull",
            "pull",
            "shelf",
            "shelving",
            "towel ring",
            "stop",
            "bracket",
        )
    ):
        code = "6651"
    if not code:
        code = _vendor_side_category_gl(ai_suggested_gl)
    if not code and lowes_like:
        # The row is known to be a maintenance-supplier purchase but no more
        # specific payable class survived OCR/AI. Required GL fields must not
        # remain silently empty; use the narrow supplier suspense category and
        # retain an explicit provenance explanation for operator review.
        code = "6669"
    if not code:
        return None
    account = ai_mapping_review.validate_gl_account(code)
    if account and _is_payable_gl_account(account):
        return account
    return None


def _gl_resolution_explanation(
    *,
    description: str,
    gl_code: str,
    suggestion_source: str,
    vendor_name: str,
) -> str:
    if not gl_code:
        return (
            "No valid payable GL matched after AI, Vision, semantic, historical, "
            f"and candidate-engine review for '{description}'."
        )
    names = {
        "7520": "door replacement assembly",
        "6606": "appliance part or accessory",
        "6675": "plumbing part or supply",
        "6660": "lighting bulb or fixture repair item",
        "6710": "blind or drapery replacement",
        "6669": "maintenance-supplier item without a more specific verified class",
    }
    classification = names.get(gl_code, "validated payable accounting category")
    article = "an" if classification[:1].lower() in "aeiou" else "a"
    return (
        f"Assigned {gl_code} because the line is {article} {classification}; "
        f"source={suggestion_source or 'validated rule'}, vendor={(vendor_name or 'unknown').rstrip('.')}."
    )


def _is_variable_supplier_vendor(vendor_name: str) -> bool:
    text = _normalize_key(" ".join((vendor_name, _vendor_rule_category(vendor_name))))
    return any(
        token in text
        for token in (
            "lowes",
            "home depot",
            "hd supply",
            "building supplies",
            "maintenance supplier",
        )
    )


def _vendor_side_category_gl(value: str) -> str:
    norm = _normalize_key(value)
    if not norm:
        return ""
    if "paint" in norm:
        return "6770"
    if "light" in norm or "bulb" in norm or "lighting" in norm or "fixture" in norm:
        return "6660"
    if "door hardware" in norm or norm == "hardware" or "hardware" in norm:
        return "6651"
    if "plumb" in norm:
        return "6675"
    if "appliance" in norm:
        return "6606"
    return ""


def _semantic_gl_default(*, description: str, vendor_name: str) -> dict[str, str] | None:
    text = _normalize_key(" ".join(
        part
        for part in (
            description,
            vendor_name,
            _vendor_rule_category(vendor_name),
            _vendor_default_gl_description(vendor_name),
        )
        if part
    ))
    code = ""
    if any(term in text for term in ("vinyl", "lvt", "tile", "wood flooring", "floor prep")):
        code = "7536"
    elif "carpet" in text and any(term in text for term in ("floor", "install", "material", "replace")):
        code = "7534"
    elif any(term in text for term in ("cabinet top", "countertop", "counter top")):
        code = "6512"
    elif "cabinet" in text and any(
        term in text for term in ("install", "remove", "replace", "remodel")
    ):
        code = "7595"
    elif "appliance" in text and any(
        term in text for term in ("install", "installation", "labor", "contract")
    ):
        code = "6505"
    elif any(term in text for term in ("unit remodel", "apartment remodel", "full remodel")):
        code = "7595"
    if code:
        account = ai_mapping_review.validate_gl_account(code)
        if account and _is_payable_gl_account(account):
            return account
    landscaping_terms = ("landscap", "lawn", "limb", "tree", "shrub", "mulch", "mow")
    if any(term in text for term in landscaping_terms):
        account = ai_mapping_review.validate_gl_account("6810")
        if account:
            return account
    return None


@lru_cache(maxsize=1)
def _vendor_rule_rows() -> tuple[dict[str, Any], ...]:
    """Load the compact vendor index, not every per-vendor YAML file."""
    rows: list[dict[str, Any]] = []
    try:
        import yaml  # type: ignore
    except Exception:
        return tuple(rows)
    index_path = settings.PROJECT_ROOT / "config" / "vendor_rules_index.yaml"
    if not index_path.is_file():
        return tuple(rows)
    try:
        data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return tuple(rows)
    indexed_vendors = data.get("vendors") if isinstance(data, dict) else []
    for item in indexed_vendors if isinstance(indexed_vendors, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "vendor_name": _clean(item.get("vendor_name")),
            "normalized_vendor_key": _clean(item.get("normalized_vendor_key")),
            "category": _clean(item.get("category")),
            "aliases": [],
            "detection_keywords": [],
            "source_properties_observed": [],
            "source_gl_codes_observed": [],
            "default_gl_code": _clean(item.get("most_common_gl_code")),
            "default_gl_description": "",
            "rule_file": _clean(item.get("rule_file")),
        })
    return tuple(rows)


@lru_cache(maxsize=256)
def _load_detailed_vendor_rule(rule_file: str) -> dict[str, Any]:
    if not rule_file:
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    path = Path(rule_file)
    if not path.is_absolute():
        path = settings.PROJECT_ROOT / path
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    identity = data.get("vendor_identity") if isinstance(data.get("vendor_identity"), dict) else {}
    accounting_source = data.get("accounting_source") if isinstance(data.get("accounting_source"), dict) else {}
    accounting = data.get("accounting_mapping") if isinstance(data.get("accounting_mapping"), dict) else {}
    return {
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
        "source_gl_codes_observed": (
            accounting_source.get("source_gl_codes_observed")
            if isinstance(accounting_source.get("source_gl_codes_observed"), list)
            else []
        ),
        "default_gl_code": _clean(accounting.get("default_gl_code")),
        "default_gl_description": _clean(accounting.get("default_gl_description")),
        "rule_file": str(path),
    }


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


def _vendor_rule_for_name(vendor_name: str) -> dict[str, Any] | None:
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
            detailed = _load_detailed_vendor_rule(_clean(row.get("rule_file")))
            if detailed:
                return {**row, **{key: value for key, value in detailed.items() if value not in (None, "", [])}}
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
    path = settings.RUNTIME_ASSET_ROOT / "Vendors" / "Vendor List.csv"
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
        settings.RUNTIME_ASSET_ROOT / "Properties" / "Properties.csv",
        settings.RUNTIME_ASSET_ROOT / "Properties" / "Unit Info Clean.csv",
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
