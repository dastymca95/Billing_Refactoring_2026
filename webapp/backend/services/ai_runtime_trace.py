"""Safe request tracing and bounded provider concurrency for AI fallback work.

The trace deliberately stores no prompts, response bodies, headers, endpoints,
credentials, filenames, or source text.  It is batch-local runtime evidence.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import settings


TRACE_SCHEMA_VERSION = "ai-request-trace/1.0"


@dataclass(frozen=True)
class RequestContext:
    batch_id: str = ""
    stage: str = "unknown"
    provider: str = ""
    model: str = ""
    profile_id: str = ""
    cache_key: str = ""
    cache_status: str = "not_checked"
    media_bytes: int = 0
    media_pixels: int = 0
    estimated_cost_usd: float = 0.0
    input_cost_usd_per_million: float = 0.0
    output_cost_usd_per_million: float = 0.0
    fixed_request_cost_usd: float = 0.0
    pricing_version: str = ""
    experiment_id: str = ""
    experiment_phase: str = ""


_CONTEXT: contextvars.ContextVar[RequestContext] = contextvars.ContextVar(
    "innerview_ai_request_context", default=RequestContext()
)
_LAST_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "innerview_ai_last_request_id", default=""
)
_TRACE_LOCK = threading.Lock()
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_ACTIVE: dict[str, int] = {}
_PEAK: dict[str, int] = {}

_PROVIDER_CATEGORIES = {
    "anthropic", "claude", "deepseek", "gemini", "local", "local_ollama",
    "local_openai_compatible", "none", "openai", "unknown",
}
_STAGE_CATEGORIES = {
    "accounting_pipeline", "accounting_pipeline_manifest_hit",
    "accounting_semantic_reasoning", "controlled_gemini_supplementary",
    "document_facts_construction", "exact_document_facts_manifest_load",
    "exact_document_facts_manifest_total", "facts_validation", "initialization",
    "local_observation_merge", "manifest_facts_validation", "mapping_review",
    "normalization", "observed_facts_persistence", "persistence",
    "reconciliation", "responses_native_pdf", "segmented_processing_failure",
    "single_invoice_processing_failure", "supplementary_planning",
    "support_document_link", "text", "unknown", "vision",
    "vision_candidate_reconciliation",
}
_FAILURE_CODES = {
    "ai_processing_failed", "ai_response_invalid_json",
    "ai_response_output_limit_exceeded", "controlled_external_context_required",
    "controlled_external_controller_missing", "controlled_external_cost_estimate_missing",
    "controlled_external_document_scope_missing", "controlled_external_mode_not_enabled",
    "controlled_external_pricing_indeterminate", "controlled_external_provider_not_allowed",
    "controlled_provider_route_blocked", "controlled_supplementary_gemini_profile_required",
    "controlled_supplementary_target_not_allowed", "experiment_cost_estimate_missing",
    "experiment_spend_gate_missing", "gemini_internal_contract_invalid",
    "gemini_transport_invalid_schema", "initial_structured_response_invalid",
    "local_only_network_block", "processor_failure", "provider_invalid_json",
    "provider_invalid_schema", "provider_permanent_failure",
    "provider_usage_or_pricing_indeterminate", "request_construction_failed",
    "source_extraction_failed", "supplementary_context_thumbnail_missing",
    "supplementary_crop_anchor_mismatch", "supplementary_crop_renderer_unavailable",
    "supplementary_crop_reference_required", "supplementary_enum_invalid",
    "supplementary_evidence_document_scope_mismatch",
    "supplementary_evidence_localization_unavailable",
    "supplementary_evidence_packet_invalid", "supplementary_evidence_plan_packet_mismatch",
    "supplementary_evidence_privacy_validation_failed",
    "supplementary_evidence_reference_invalid",
    "supplementary_evidence_source_page_unavailable",
    "supplementary_evidence_target_mismatch", "supplementary_field_type_invalid",
    "supplementary_internal_contract_invalid", "supplementary_invalid_json",
    "supplementary_primary_crop_missing", "supplementary_request_limit_reached",
    "supplementary_required_field_missing", "supplementary_second_plan_not_justified",
    "supplementary_target_label_not_localized", "supplementary_transport_version_invalid",
    "supplementary_unexpected_field", "supplementary_unplanned_crop_reference",
    "supplementary_visual_evidence_contradiction",
    "supplementary_visual_evidence_unresolved", "visual_evidence_unavailable",
}
_SCHEMA_RESULTS = {
    "escalated", "invalid", "supplementary_slot_2_justified", "unknown", "valid",
}
_RECONCILIATION_STATUSES = {
    "inconclusive", "mismatch", "not_run", "reconciled", "unavailable",
    "unavailable_due_to_missing_facts", "unknown", "unreconciled",
}
_TARGET_CATEGORIES = {
    "date_ambiguity", "duplicate_row_suspicion", "invoice_number_ambiguity",
    "missing_line_item", "missing_tax_or_fee", "page_continuation",
    "paid_crossed_out_row_status", "quantity_unit_price_mismatch", "subtotal_mismatch",
    "total_mismatch", "unknown", "vendor_name_ambiguity",
}
_TARGET_SUBTYPES = {
    "ambiguous_total_label", "date_identity", "duplicate_row", "invoice_identity",
    "missing_discount_or_credit", "missing_tax_or_fee", "omitted_line_item",
    "page_continuation", "paid_or_crossed_row", "payment_or_deposit", "previous_balance",
    "quantity_price", "statement_vs_invoice", "unknown", "unknown_total_composition",
    "vendor_identity",
}
_PROCESSING_STAGES = {
    "accounting_pipeline", "document_facts_construction", "initialization",
    "local_observation_merge", "mapping_review", "normalization",
    "observed_facts_persistence", "persistence", "reconciliation",
    "support_document_link", "unknown", "vision_candidate_reconciliation",
}
_EXCEPTION_TYPES = {
    "AIProviderError", "AIProviderInvalidJSON", "AIProviderInvalidSchema",
    "IntermediateObservationReviewRequired", "RuntimeError", "ValidationError",
    "ValueError", "unexpected_exception", "unknown",
}
_DISPOSITION_TRANSITIONS = {
    "processing->blocked", "processing->processing_failure",
    "processing->review_required", "processing->unsupported", "unknown",
}
_SECOND_SLOT_REASONS = {"distinct_deterministic_target", "not_applicable", "unknown"}
_CIRCUIT_ENDPOINTS = {"chat_completions", "native_gemini", "responses_native_pdf", "unknown"}
_CIRCUIT_CAPABILITIES = {"structured_output", "text", "vision", "unknown"}
_CIRCUIT_ACTIONS = {"blocked", "opened", "rejected", "unknown"}
_CACHE_LAYERS = {
    "exact_document_facts", "exact_page_facts", "provider_request",
    "provider_request_disabled", "semantic_candidate", "unknown",
}
_CHARACTER_CLASSES = {
    "array_boundary", "digit", "letter", "markdown_fence", "none",
    "object_boundary", "other", "quote", "unknown",
}
_FINISH_REASONS = {
    "content_filter", "length", "max_output_tokens", "max_token",
    "max_tokens", "other", "safety", "stop", "unknown",
}
_JSON_PARSER_ERRORS = {
    "CompetingOrStructuredPrefix", "EmptyResponseContent", "JSONDecodeError",
    "OutputTokenLimitReached", "ResponseCharacterLimitExceeded",
    "SchemaValidationError", "StrictInternalContractValidationError",
    "TrailingStructuredData", "TruncatedJSON", "UnexpectedField",
    "UnexpectedProviderResponseShape", "unknown_parser_error",
}
_SCHEMA_VALIDATION_ERROR_TYPES = {
    "bool_parsing", "bool_type", "date_from_datetime_parsing", "date_parsing",
    "decimal_parsing", "dict_type", "extra_forbidden", "finite_number",
    "float_parsing", "float_type", "greater_than_equal", "int_from_float",
    "int_parsing", "int_type", "less_than_equal", "list_type", "missing",
    "missing_argument", "model_type", "none_required", "string_too_long",
    "string_too_short", "string_type", "unknown_validation_error",
    "value_error",
}
_SCHEMA_FAILURE_CATEGORIES = {
    "additional_unsupported_field", "incorrect_field_type",
    "internal_normalization_failure", "missing_required_field",
    "multiple_json_objects", "output_limit_exhaustion", "raw_json_parser_failure",
    "transport_schema_validation_failure", "truncation", "unclassified",
}
_TRANSPORT_SCHEMA_VERSIONS = {
    "gemini-facts-transport/1.0", "supplementary-transport/2.0", "unknown",
}


def _canonical_category(value: object, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else default


def _safe_stage(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in _STAGE_CATEGORIES:
        return normalized
    for prefix in (
        "controlled_gemini_supplementary:", "segmented_processing_failure:",
    ):
        if normalized.startswith(prefix):
            return prefix[:-1]
    return "unknown"


def _safe_failure_code(value: object, *, default: str = "unspecified_failure") -> str:
    return _canonical_category(value, _FAILURE_CODES, default)


def _safe_identifier(value: object, *, default: str = "") -> str:
    candidate = str(value or "").strip()
    lowered = candidate.casefold()
    if (
        not candidate
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", candidate)
        or any(token in lowered for token in ("authorization", "bearer", "api_key", "secret"))
        or re.search(r"\d{7,}", candidate)
    ):
        return default
    return candidate


def _safe_nonnegative_int(value: object) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_sha256(value: object) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if re.fullmatch(r"[a-f0-9]{64}", candidate) else ""


def _safe_hash_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({
        candidate
        for item in value
        if re.fullmatch(
            r"[a-f0-9]{12}|[a-f0-9]{16}|[a-f0-9]{24}|[a-f0-9]{32}|[a-f0-9]{64}",
            candidate := str(item or "").casefold(),
        )
        and not candidate.isdigit()
    })


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_batch_id(value: str) -> str:
    value = str(value or "").strip()
    return value if value.startswith("batch_") and value.replace("_", "").isalnum() else ""


def _trace_path(batch_id: str) -> Path | None:
    safe = _safe_batch_id(batch_id)
    if not safe:
        return None
    return settings.BATCHES_ROOT / safe / "audit" / "ai_request_trace.jsonl"


def _write_event(event: dict[str, Any]) -> None:
    path = _trace_path(str(event.get("batch_id") or ""))
    if path is None:
        return
    payload = {"schema": TRACE_SCHEMA_VERSION, **event}
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with _TRACE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def current_context() -> RequestContext:
    return _CONTEXT.get()


@contextlib.contextmanager
def operation(
    *,
    batch_id: str,
    stage: str,
    provider: str = "",
    model: str = "",
    profile_id: str = "",
    media_bytes: int = 0,
    media_pixels: int = 0,
) -> Iterator[None]:
    token = _CONTEXT.set(RequestContext(
        batch_id=_safe_batch_id(batch_id),
        stage=_safe_stage(stage),
        provider=_canonical_category(provider, _PROVIDER_CATEGORIES, "unknown"),
        model=_safe_identifier(model),
        profile_id=_safe_identifier(profile_id),
        media_bytes=max(0, int(media_bytes or 0)),
        media_pixels=max(0, int(media_pixels or 0)),
    ))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def update_context(**changes: Any) -> None:
    allowed = {key: value for key, value in changes.items() if hasattr(current_context(), key)}
    if "stage" in allowed:
        allowed["stage"] = _safe_stage(allowed["stage"])
    if "provider" in allowed:
        allowed["provider"] = _canonical_category(
            allowed["provider"], _PROVIDER_CATEGORIES, "unknown",
        )
    for key in ("model", "profile_id"):
        if key in allowed:
            allowed[key] = _safe_identifier(allowed[key])
    if allowed:
        _CONTEXT.set(replace(current_context(), **allowed))


def record_cache(cache_key: str, *, hit: bool, layer: str) -> None:
    context = current_context()
    update_context(cache_key=cache_key, cache_status="hit" if hit else "miss")
    _write_event({
        "event": "cache",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": _safe_stage(context.stage),
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "cache_layer": _canonical_category(layer, _CACHE_LAYERS, "unknown"),
        "cache_key": (
            str(cache_key).casefold()
            if re.fullmatch(r"[a-f0-9]{16,128}", str(cache_key or "").casefold())
            else ""
        ),
        "cache_status": "hit" if hit else "miss",
        "at": _utc_now(),
    })


def _provider_limit(provider: str) -> int:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in provider.upper())
    raw = os.environ.get(f"AI_{normalized}_MAX_CONCURRENCY") or os.environ.get(
        "AI_PROVIDER_MAX_CONCURRENCY", "4"
    )
    try:
        return min(32, max(1, int(raw)))
    except (TypeError, ValueError):
        return 4


def _semaphore(provider: str) -> tuple[threading.BoundedSemaphore, int]:
    key_provider = str(provider or "unknown").strip().lower() or "unknown"
    limit = _provider_limit(key_provider)
    key = (key_provider, limit)
    with _SEMAPHORE_LOCK:
        return _SEMAPHORES.setdefault(key, threading.BoundedSemaphore(limit)), limit


@contextlib.contextmanager
def provider_attempt(provider: str, attempt: int) -> Iterator[str]:
    """Acquire a provider slot and persist one safe transport-attempt event."""

    context = current_context()
    semaphore, limit = _semaphore(provider)
    wait_started = time.perf_counter()
    semaphore.acquire()
    wait_ms = round((time.perf_counter() - wait_started) * 1000, 3)
    normalized = _canonical_category(provider, _PROVIDER_CATEGORIES, "unknown")
    with _SEMAPHORE_LOCK:
        active = _ACTIVE.get(normalized, 0) + 1
        _ACTIVE[normalized] = active
        _PEAK[normalized] = max(_PEAK.get(normalized, 0), active)
        peak = _PEAK[normalized]
    request_id = uuid.uuid4().hex
    _LAST_REQUEST_ID.set(request_id)
    started_at = _utc_now()
    started = time.perf_counter()
    outcome = "response_received"
    failure_code = ""
    try:
        yield request_id
    except Exception as exc:
        outcome = "transport_error"
        failure_code = _safe_failure_code(
            getattr(exc, "failure_code", ""), default="provider_transport_error",
        )
        raise
    finally:
        ended_at = _utc_now()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        with _SEMAPHORE_LOCK:
            _ACTIVE[normalized] = max(0, _ACTIVE.get(normalized, 1) - 1)
        semaphore.release()
        latest = current_context()
        _write_event({
            "event": "provider_attempt",
            "batch_id": latest.batch_id or context.batch_id,
            "request_id": request_id,
            "stage": _safe_stage(latest.stage or context.stage),
            "provider": normalized,
            "model": latest.model or context.model,
            "profile_id": latest.profile_id or context.profile_id,
            "cache_key": latest.cache_key or context.cache_key,
            "cache_status": latest.cache_status or context.cache_status,
            "attempt": max(1, int(attempt)),
            "started_at": started_at,
            "ended_at": ended_at,
            "elapsed_ms": elapsed_ms,
            "provider_semaphore_wait_ms": wait_ms,
            "provider_concurrency_limit": limit,
            "provider_active_at_start": active,
            "provider_peak_concurrency": peak,
            "media_bytes": latest.media_bytes or context.media_bytes,
            "media_pixels": latest.media_pixels or context.media_pixels,
            "estimated_cost_usd": round(
                float(latest.estimated_cost_usd or context.estimated_cost_usd or 0.0), 6
            ),
            "experiment_id": latest.experiment_id or context.experiment_id,
            "experiment_phase": latest.experiment_phase or context.experiment_phase,
            "pricing_version": latest.pricing_version or context.pricing_version,
            "outcome": outcome,
            "failure_code": failure_code,
        })


def record_schema_result(result: str, *, retry_reason: str = "") -> None:
    context = current_context()
    _write_event({
        "event": "schema_validation",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "stage": _safe_stage(context.stage),
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "cache_key": context.cache_key,
        "cache_status": context.cache_status,
        "schema_result": _canonical_category(result, _SCHEMA_RESULTS, "unknown"),
        "retry_reason": _safe_failure_code(retry_reason, default="unspecified_retry_reason")
        if retry_reason else "",
        "at": _utc_now(),
    })


def record_supplementary_verification(
    *, target_category: str, request_count: int, schema_valid: bool,
    reconciliation_before: str, reconciliation_after: str,
    resolved: bool, evidence_reference_count: int, failure_code: str = "",
) -> None:
    """Persist only categorical/result metadata for targeted verification."""

    context = current_context()
    _write_event({
        "event": "supplementary_verification",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "stage": _safe_stage(context.stage),
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "target_category": _canonical_category(
            target_category, _TARGET_CATEGORIES, "unknown",
        ),
        "request_count": max(0, int(request_count or 0)),
        "schema_valid": bool(schema_valid),
        "reconciliation_before": _canonical_category(
            reconciliation_before, _RECONCILIATION_STATUSES, "unknown",
        ),
        "reconciliation_after": _canonical_category(
            reconciliation_after, _RECONCILIATION_STATUSES, "unknown",
        ),
        "resolved": bool(resolved),
        "evidence_reference_count": max(0, int(evidence_reference_count or 0)),
        "failure_code": _safe_failure_code(failure_code, default="") if failure_code else "",
        "at": _utc_now(),
    })


_SUPPLEMENTARY_PLAN_ROLES = {
    "primary_target", "context_thumbnail", "related_evidence", "continuation",
}
_SUPPLEMENTARY_PLAN_OUTCOMES = {
    "packet_validated", "packet_rejected_locally", "plan_rejected_locally",
}


def record_supplementary_evidence_plan(
    *, target_category: str, target_subtype: str, outcome: str,
    crop_count: int = 0, crop_roles: list[str] | None = None,
    combined_pixels: int = 0, plan_id: str = "", failure_code: str = "",
    second_slot_reason: str = "",
) -> None:
    """Persist safe, categorical plan/packet observability only.

    Crop coordinates, image hashes, source text, document identity and response
    content are intentionally excluded.  ``plan_id`` is an opaque local
    fingerprint and cannot authorize provider dispatch by itself.
    """

    context = current_context()
    safe_outcome = str(outcome or "")
    if safe_outcome not in _SUPPLEMENTARY_PLAN_OUTCOMES:
        safe_outcome = "plan_rejected_locally"
    safe_roles = sorted({
        str(value) for value in (crop_roles or [])
        if str(value) in _SUPPLEMENTARY_PLAN_ROLES
    })
    _write_event({
        "event": "supplementary_evidence_plan",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "stage": _safe_stage(context.stage),
        "target_category": _canonical_category(
            target_category, _TARGET_CATEGORIES, "unknown",
        ),
        "target_subtype": _canonical_category(
            target_subtype, _TARGET_SUBTYPES, "unknown",
        ),
        "outcome": safe_outcome,
        "crop_count": max(0, int(crop_count or 0)),
        "crop_roles": safe_roles,
        "combined_pixels": max(0, int(combined_pixels or 0)),
        "plan_id": re.sub(r"[^a-f0-9]", "", str(plan_id or "").lower())[:24],
        "failure_code": _safe_failure_code(failure_code, default="") if failure_code else "",
        "second_slot_reason": _canonical_category(
            second_slot_reason, _SECOND_SLOT_REASONS, "unknown",
        ) if second_slot_reason else "",
        "at": _utc_now(),
    })


_VISUAL_EVIDENCE_SOURCE_KINDS = {
    "gemini_facts_transport",
    "gemini_supplementary_verification",
    "other",
}
_VISUAL_EVIDENCE_MISSING_FIELDS = {
    "source_type",
    "extraction_method",
    "visual_anchor",
    "canonical_evidence",
}
_VISUAL_EVIDENCE_MERGE_OUTCOMES = {
    "initial_only",
    "supplementary_merged",
    "not_available",
}
_VISUAL_EVIDENCE_VALIDATION_OUTCOMES = {
    "valid",
    "missing_required_evidence",
}
_VISUAL_EVIDENCE_BRANCHES = {
    "not_raised",
    "single_visual_source_absent",
    "single_visual_payload_insufficient",
    "segmented_visual_source_absent",
    "segmented_visual_payload_insufficient",
}


def record_visual_evidence_contract(
    *, evidence_object_count: int, initial_evidence_count: int,
    supplementary_evidence_count: int, evidence_reference_count: int,
    page_reference_count: int, bounding_region_present_count: int,
    observed_text_present_count: int, source_kind_categories: list[str],
    missing_required_evidence_fields: list[str], merge_stage_outcome: str,
    evidence_validation_outcome: str, raise_branch: str,
) -> None:
    """Persist only categorical evidence-contract diagnostics.

    The current batch is used only to select the private trace file. No batch,
    request, provider, filename, path, observed value, source text, or timestamp
    is serialized into this event.
    """

    context = current_context()
    path = _trace_path(context.batch_id)
    if path is None:
        return
    payload = {
        "schema": TRACE_SCHEMA_VERSION,
        "event": "visual_evidence_contract",
        "evidence_object_count": max(0, int(evidence_object_count or 0)),
        "initial_evidence_count": max(0, int(initial_evidence_count or 0)),
        "supplementary_evidence_count": max(
            0, int(supplementary_evidence_count or 0),
        ),
        "evidence_reference_count": max(0, int(evidence_reference_count or 0)),
        "page_reference_count": max(0, int(page_reference_count or 0)),
        "bounding_region_present_count": max(
            0, int(bounding_region_present_count or 0),
        ),
        "observed_text_present_count": max(
            0, int(observed_text_present_count or 0),
        ),
        "source_kind_categories": sorted({
            value if value in _VISUAL_EVIDENCE_SOURCE_KINDS else "other"
            for value in source_kind_categories
        }),
        "missing_required_evidence_fields": sorted({
            value for value in missing_required_evidence_fields
            if value in _VISUAL_EVIDENCE_MISSING_FIELDS
        }),
        "merge_stage_outcome": (
            merge_stage_outcome
            if merge_stage_outcome in _VISUAL_EVIDENCE_MERGE_OUTCOMES
            else "not_available"
        ),
        "evidence_validation_outcome": (
            evidence_validation_outcome
            if evidence_validation_outcome in _VISUAL_EVIDENCE_VALIDATION_OUTCOMES
            else "missing_required_evidence"
        ),
        "raise_branch": (
            raise_branch
            if raise_branch in _VISUAL_EVIDENCE_BRANCHES
            else "single_visual_payload_insufficient"
        ),
    }
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with _TRACE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def record_processing_stage_failure(
    *,
    processing_stage: str,
    exception_type: str,
    failure_code: str = "",
    disposition_transition: str = "processing->processing_failure",
    document_facts_created: bool = False,
    reconciliation_completed: bool = False,
    provenance_attached: bool = False,
    persistence_attempted: bool = False,
    final_disposition_written: bool = False,
) -> None:
    """Record the strictly value-free downstream diagnostic authorized for Gate 1.

    The batch identifier is used only to select the private trace file.  It is
    deliberately omitted from the serialized event along with timestamps,
    request/provider/model metadata and all document-derived values.
    """

    context = current_context()
    path = _trace_path(context.batch_id)
    if path is None:
        return
    payload = {
        "schema": TRACE_SCHEMA_VERSION,
        "event": "processing_stage_failure",
        "local_processing_stage": _canonical_category(
            processing_stage, _PROCESSING_STAGES, "unknown",
        ),
        "safe_exception_type": (
            str(exception_type)
            if str(exception_type) in _EXCEPTION_TYPES
            else "unexpected_exception"
        ),
        "sanitized_failure_code": _safe_failure_code(failure_code),
        "disposition_transition": _canonical_category(
            disposition_transition, _DISPOSITION_TRANSITIONS, "unknown",
        ),
        "document_facts_created": bool(document_facts_created),
        "reconciliation_completed": bool(reconciliation_completed),
        "provenance_attached": bool(provenance_attached),
        "persistence_attempted": bool(persistence_attempted),
        "final_disposition_written": bool(final_disposition_written),
    }
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with _TRACE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def record_structured_response_failure(diagnostic: dict[str, Any]) -> None:
    """Persist only allow-listed response-shape metadata.

    The caller has already reduced the provider response to lengths, hashes,
    character classes, token counts, field-name hashes, and validation error
    locations.  This second allow-list prevents a future caller from adding
    response text, invoice values, paths, or credentials to the trace.
    """

    safe: dict[str, Any] = {
        "provider": _canonical_category(
            diagnostic.get("provider"), _PROVIDER_CATEGORIES, "unknown",
        ),
        "model": _safe_identifier(diagnostic.get("model")),
        "request_profile": _safe_identifier(diagnostic.get("request_profile")),
        "response_byte_length": _safe_nonnegative_int(
            diagnostic.get("response_byte_length"),
        ),
        "response_character_length": _safe_nonnegative_int(
            diagnostic.get("response_character_length"),
        ),
        "response_sha256": _safe_sha256(diagnostic.get("response_sha256")),
        "first_non_whitespace_character_class": _canonical_category(
            diagnostic.get("first_non_whitespace_character_class"),
            _CHARACTER_CLASSES, "unknown",
        ),
        "last_non_whitespace_character_class": _canonical_category(
            diagnostic.get("last_non_whitespace_character_class"),
            _CHARACTER_CLASSES, "unknown",
        ),
        "markdown_code_fence_present": bool(
            diagnostic.get("markdown_code_fence_present"),
        ),
        "json_object_boundary_detectable": bool(
            diagnostic.get("json_object_boundary_detectable"),
        ),
        "json_array_boundary_detectable": bool(
            diagnostic.get("json_array_boundary_detectable"),
        ),
        "json_object_boundary_count": _safe_nonnegative_int(
            diagnostic.get("json_object_boundary_count"),
        ),
        "json_array_boundary_count": _safe_nonnegative_int(
            diagnostic.get("json_array_boundary_count"),
        ),
        "finish_reason": _canonical_category(
            diagnostic.get("finish_reason"), _FINISH_REASONS, "unknown",
        ),
        "prompt_token_count": _safe_nonnegative_int(
            diagnostic.get("prompt_token_count"),
        ),
        "output_token_count": _safe_nonnegative_int(
            diagnostic.get("output_token_count"),
        ),
        "output_token_limit_reached": bool(
            diagnostic.get("output_token_limit_reached"),
        ),
        "json_parser_error_type": _canonical_category(
            diagnostic.get("json_parser_error_type"),
            _JSON_PARSER_ERRORS, "unknown_parser_error",
        ),
        "json_parser_error_character_offset": _safe_nonnegative_int(
            diagnostic.get("json_parser_error_character_offset"),
        ),
        "schema_validation_error_type": _canonical_category(
            diagnostic.get("schema_validation_error_type"),
            _SCHEMA_VALIDATION_ERROR_TYPES, "unknown_validation_error",
        ),
        "schema_failure_category": _canonical_category(
            diagnostic.get("schema_failure_category"),
            _SCHEMA_FAILURE_CATEGORIES, "unclassified",
        ),
        "unexpected_field_name_hashes": _safe_hash_list(
            diagnostic.get("unexpected_field_name_hashes"),
        ),
        "unknown_field_count": _safe_nonnegative_int(
            diagnostic.get("unknown_field_count"),
        ),
        "missing_required_field_count": _safe_nonnegative_int(
            diagnostic.get("missing_required_field_count"),
        ),
        "transport_schema_version": _canonical_category(
            diagnostic.get("transport_schema_version"),
            _TRANSPORT_SCHEMA_VERSIONS, "unknown",
        ),
    }
    for key in ("received_top_level_field_names", "missing_required_field_names"):
        value = diagnostic.get(key)
        safe[key] = sorted({
            hashlib.sha256(str(item).encode("utf-8")).hexdigest()[:16]
            for item in value
        }) if isinstance(value, (list, tuple, set)) else []
    path = diagnostic.get("schema_validation_error_path")
    safe["schema_validation_error_path"] = (
        hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        if path else ""
    )
    value_types = diagnostic.get("received_top_level_value_types")
    safe["received_top_level_value_types"] = sorted({
        str(value) for value in value_types.values()
        if str(value) in {"array", "boolean", "null", "number", "object", "string"}
    }) if isinstance(value_types, dict) else []
    context = current_context()
    _write_event({
        "event": "structured_response_failure",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "stage": _safe_stage(context.stage),
        "profile_id": context.profile_id,
        **safe,
        "at": _utc_now(),
    })


def record_blocked_network_attempt(
    *, provider: str, stage: str, failure_code: str,
) -> None:
    """Persist a secret-free event for a dispatch rejected before transport."""

    context = current_context()
    _write_event({
        "event": "network_dispatch_blocked",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": _safe_stage(stage or context.stage),
        "provider": _canonical_category(provider, _PROVIDER_CATEGORIES, "unknown"),
        "model": context.model,
        "profile_id": context.profile_id,
        "experiment_id": context.experiment_id,
        "experiment_phase": context.experiment_phase,
        "failure_code": _safe_failure_code(
            failure_code, default="local_only_network_block",
        ),
        "at": _utc_now(),
    })


def record_controlled_provider_route_blocked(
    *, provider: str, stage: str, failure_code: str,
) -> None:
    """Record a fail-closed route rejection without request or document content."""

    context = current_context()
    _write_event({
        "event": "controlled_provider_route_blocked",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": _safe_stage(stage or context.stage),
        "provider": _canonical_category(provider, _PROVIDER_CATEGORIES, "unknown"),
        "model": context.model,
        "profile_id": context.profile_id,
        "experiment_id": context.experiment_id,
        "experiment_phase": context.experiment_phase,
        "failure_code": _safe_failure_code(
            failure_code, default="controlled_provider_route_blocked",
        ),
        "at": _utc_now(),
    })


def record_provider_usage(
    *, reservation_id: str, usage: dict[str, int | float | str],
    actual_cost_usd: float | None, provider_reported: bool,
    failure_code: str = "",
) -> None:
    """Persist normalized request usage without private request/response data."""
    context = current_context()
    allowed_keys = {
        "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
        "images", "pages", "bytes", "pixels",
    }
    safe_usage = {
        key: value for key, value in usage.items()
        if key in allowed_keys and isinstance(value, (int, float))
    }
    _write_event({
        "event": "provider_usage",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "reservation_id": (
            str(reservation_id)
            if re.fullmatch(r"[A-Fa-f0-9-]{8,80}", str(reservation_id or ""))
            else ""
        ),
        "stage": _safe_stage(context.stage),
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "experiment_id": context.experiment_id,
        "experiment_phase": context.experiment_phase,
        "pricing_version": context.pricing_version,
        "usage": safe_usage,
        "provider_reported_usage": bool(provider_reported),
        "actual_cost_usd": (
            round(max(0.0, float(actual_cost_usd)), 6)
            if actual_cost_usd is not None else None
        ),
        "failure_code": _safe_failure_code(failure_code, default="") if failure_code else "",
        "at": _utc_now(),
    })


def record_circuit_breaker(
    *,
    provider: str,
    model: str,
    endpoint_surface: str,
    capability: str,
    action: str,
    http_status: int | None = None,
    failure_code: str = "",
) -> None:
    """Persist a secret-safe permanent-provider-failure transition."""

    context = current_context()
    _write_event({
        "event": "provider_circuit_breaker",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": _safe_stage(context.stage),
        "provider": _canonical_category(provider, _PROVIDER_CATEGORIES, "unknown"),
        "model": _safe_identifier(model),
        "profile_id": context.profile_id,
        "endpoint_surface": _canonical_category(
            endpoint_surface, _CIRCUIT_ENDPOINTS, "unknown",
        ),
        "capability": _canonical_category(
            capability, _CIRCUIT_CAPABILITIES, "unknown",
        ),
        "action": _canonical_category(action, _CIRCUIT_ACTIONS, "unknown"),
        "http_status": http_status,
        "failure_code": _safe_failure_code(failure_code, default="") if failure_code else "",
        "at": _utc_now(),
    })


def record_stage_timing(stage: str, elapsed_ms: float) -> None:
    context = current_context()
    _write_event({
        "event": "stage_timing",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": _safe_stage(stage or context.stage),
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "elapsed_ms": round(max(0.0, float(elapsed_ms or 0.0)), 3),
        "at": _utc_now(),
    })


def media_stats(data_urls: list[str] | tuple[str, ...]) -> tuple[int, int]:
    """Return decoded media bytes and pixels without retaining any media."""

    total_bytes = 0
    total_pixels = 0
    for value in data_urls:
        if not isinstance(value, str) or ";base64," not in value:
            continue
        try:
            raw = base64.b64decode(value.split(",", 1)[1], validate=False)
        except (ValueError, TypeError):
            continue
        total_bytes += len(raw)
        try:
            import io
            from PIL import Image  # type: ignore

            with Image.open(io.BytesIO(raw)) as image:
                total_pixels += int(image.width) * int(image.height)
        except Exception:
            pass
    return total_bytes, total_pixels


def reset_for_tests() -> None:
    with _SEMAPHORE_LOCK:
        _SEMAPHORES.clear()
        _ACTIVE.clear()
        _PEAK.clear()


__all__ = [
    "RequestContext",
    "current_context",
    "media_stats",
    "operation",
    "provider_attempt",
    "record_cache",
    "record_controlled_provider_route_blocked",
    "record_provider_usage",
    "record_processing_stage_failure",
    "record_schema_result",
    "record_supplementary_evidence_plan",
    "record_supplementary_verification",
    "record_structured_response_failure",
    "record_stage_timing",
    "reset_for_tests",
    "update_context",
]
