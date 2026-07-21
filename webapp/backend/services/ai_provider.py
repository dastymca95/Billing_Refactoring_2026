"""Provider-agnostic AI invoice extraction client.

Phase AI-1 intentionally keeps the provider surface narrow and disabled by
default. The rest of the backend calls this module through
``extract_invoice_structured`` and receives a parsed JSON object; API keys
never leave the process.
"""

from __future__ import annotations

import copy
import json
import hashlib
import itertools
import logging
import os
import re
import contextvars
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .. import settings
from . import ai_runtime_trace, canonical_rules
from .experiment_spend_controller import (
    SpendAuthorizationError,
    current_experiment_spend_gate,
)
from .native_pdf_evidence import NativePdfEvidence
from .local_inference_guard import (
    LOCAL_PROVIDER_NAMES,
    LocalInferenceNetworkBlocked,
    assert_dispatch_allowed,
)
from .controlled_external_experiment import (
    ControlledCallPermit,
    ControlledCallPermitLifecycle,
    ControlledCallPurpose,
    ControlledExternalBlocked,
    ControlledExternalGateTerminated,
    ExperimentProviderContext,
    assert_controlled_external_dispatch_allowed,
    controlled_external_active,
    controlled_urlopen,
    current_document_scope,
    current_experiment_provider_context,
    preflight_controlled_provider_route,
    require_experiment_provider_context,
    spend_document_context,
)
from .gemini_facts_transport import (
    GeminiTransportError,
    TRANSPORT_PROMPT_VERSION,
    TRANSPORT_SCHEMA_VERSION,
    build_gemini_facts_prompt,
    build_safe_diagnostic as build_gemini_safe_diagnostic,
    extract_single_json_object as extract_single_gemini_json_object,
    gemini_response_format,
    parse_and_normalize_gemini_facts,
)
from .gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    SUPPLEMENTARY_PROMPT_VERSION,
    SUPPLEMENTARY_SCHEMA_VERSION,
    SupplementaryTarget,
    SupplementaryVerificationError,
    build_minimized_initial_summary,
    build_supplementary_prompt,
    parse_supplementary_response,
    reconciliation_snapshot,
    supplementary_response_format,
    validate_observation_crop_references,
)
from .intermediate_invoice_observation import (
    InitialNormalizationCategory,
    InitialNormalizationOutcome,
    build_unreconciled_observation,
)
from .supplementary_evidence_planner import (
    EvidenceLocalizationError,
    SupplementaryEvidencePacket,
    SupplementaryEvidencePlan,
    validate_evidence_packet,
)


_LOG = logging.getLogger(__name__)
_EXTRACTION_CACHE_VERSION = 8
_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai", "openai_compatible", "gemini", "google_gemini",
    "deepseek", "anthropic", "claude",
}
_PROVIDER_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "google_gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com/v1",
    "claude": "https://api.anthropic.com/v1",
}
_COST_BUDGET_LOCK = threading.Lock()
_COST_BUDGET_RESERVED_USD: dict[str, float] = {}
_NATIVE_PDF_SURFACE_LOCK = threading.Lock()
_NATIVE_PDF_UNAVAILABLE_MODELS: set[str] = set()
_PROVIDER_CIRCUIT_LOCK = threading.Lock()
_PROVIDER_PERMANENT_FAILURES: dict[tuple[str, str, str, str], dict[str, Any]] = {}
_PERMANENT_PROVIDER_STATUSES = {401, 403, 404}
_LAST_PROVIDER_RESPONSE_METADATA: contextvars.ContextVar[dict[str, Any]] = (
    contextvars.ContextVar("innerview_last_provider_response_metadata", default={})
)


def _provider_circuit_key(
    *, provider: str, model: str | None, endpoint_surface: str, capability: str
) -> tuple[str, str, str, str]:
    return (
        str(provider or "unknown").strip().lower() or "unknown",
        str(model or "").strip().lower(),
        str(endpoint_surface or "unknown").strip().lower() or "unknown",
        str(capability or "unknown").strip().lower() or "unknown",
    )


def _open_provider_circuit(
    *,
    provider: str,
    model: str | None,
    endpoint_surface: str,
    capability: str,
    http_status: int,
    failure_code: str,
) -> None:
    key = _provider_circuit_key(
        provider=provider,
        model=model,
        endpoint_surface=endpoint_surface,
        capability=capability,
    )
    with _PROVIDER_CIRCUIT_LOCK:
        _PROVIDER_PERMANENT_FAILURES[key] = {
            "http_status": int(http_status),
            "failure_code": str(failure_code or "provider_permanent_failure"),
        }
    ai_runtime_trace.record_circuit_breaker(
        provider=key[0],
        model=key[1],
        endpoint_surface=key[2],
        capability=key[3],
        action="opened",
        http_status=int(http_status),
        failure_code=failure_code,
    )


def _assert_provider_circuit_closed(
    *, provider: str, model: str | None, endpoint_surface: str, capability: str
) -> None:
    key = _provider_circuit_key(
        provider=provider,
        model=model,
        endpoint_surface=endpoint_surface,
        capability=capability,
    )
    with _PROVIDER_CIRCUIT_LOCK:
        failure = dict(_PROVIDER_PERMANENT_FAILURES.get(key) or {})
    if not failure:
        return
    ai_runtime_trace.record_circuit_breaker(
        provider=key[0],
        model=key[1],
        endpoint_surface=key[2],
        capability=key[3],
        action="blocked",
        http_status=failure.get("http_status"),
        failure_code=failure.get("failure_code") or "provider_permanent_failure",
    )
    raise AIProviderUnavailable(
        "Provider route is unavailable for this process after a permanent capability failure.",
        failure_code="provider_circuit_open",
        http_status=failure.get("http_status"),
    )


def provider_circuit_report() -> list[dict[str, Any]]:
    """Return only non-sensitive route identities and permanent failure codes."""

    with _PROVIDER_CIRCUIT_LOCK:
        rows = list(_PROVIDER_PERMANENT_FAILURES.items())
    return [
        {
            "provider": key[0],
            "model": key[1],
            "endpoint_surface": key[2],
            "capability": key[3],
            "http_status": value.get("http_status"),
            "failure_code": value.get("failure_code"),
        }
        for key, value in sorted(rows)
    ]


def _reset_provider_circuits_for_tests() -> None:
    with _PROVIDER_CIRCUIT_LOCK:
        _PROVIDER_PERMANENT_FAILURES.clear()


def native_pdf_surface_available(model: str | None) -> bool:
    key = str(model or "").strip().lower()
    with _NATIVE_PDF_SURFACE_LOCK:
        legacy_available = key not in _NATIVE_PDF_UNAVAILABLE_MODELS
    circuit_key = _provider_circuit_key(
        provider="openai",
        model=key,
        endpoint_surface="responses_native_pdf",
        capability="native_pdf",
    )
    with _PROVIDER_CIRCUIT_LOCK:
        return legacy_available and circuit_key not in _PROVIDER_PERMANENT_FAILURES


def _mark_native_pdf_surface_unavailable(model: str | None) -> None:
    key = str(model or "").strip().lower()
    if key:
        with _NATIVE_PDF_SURFACE_LOCK:
            _NATIVE_PDF_UNAVAILABLE_MODELS.add(key)


def _reset_native_pdf_surface_for_tests() -> None:
    with _NATIVE_PDF_SURFACE_LOCK:
        _NATIVE_PDF_UNAVAILABLE_MODELS.clear()
    _reset_provider_circuits_for_tests()


def _estimated_profile_request_cost(profile, payload: dict[str, Any], *, vision: bool) -> float:
    if profile is None:
        return 0.0
    input_rate = profile.input_cost_usd_per_million
    output_rate = profile.output_cost_usd_per_million
    if input_rate is None or output_rate is None:
        return 0.0
    # Do not count base64 bytes as text tokens. Image-token pricing is covered
    # by the provider's multimodal input rate and a small conservative floor.
    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
            return "<image-evidence>"
        return value
    input_tokens = len(json.dumps(scrub(payload), default=str)) / 4
    output_tokens = int(
        payload.get("max_output_tokens")
        or payload.get("max_completion_tokens")
        or payload.get("max_tokens")
        or 4096
    )
    estimate = input_tokens * input_rate / 1_000_000 + output_tokens * output_rate / 1_000_000
    if vision:
        estimate += float(os.environ.get("AI_ESTIMATED_VISION_IMAGE_COST_USD", "0.002") or 0.002)
    return round(estimate, 6)


def _update_profile_cost_context(profile, estimated_cost: float, *, vision: bool) -> None:
    """Expose a private rate card to the experiment transport without secrets."""
    ai_runtime_trace.update_context(
        provider=(profile.provider if profile is not None else None),
        model=(profile.model_id if profile is not None else None),
        profile_id=(profile.profile_id if profile is not None else None),
        estimated_cost_usd=max(0.0, float(estimated_cost or 0.0)),
        input_cost_usd_per_million=(
            float(profile.input_cost_usd_per_million or 0.0) if profile is not None else 0.0
        ),
        output_cost_usd_per_million=(
            float(profile.output_cost_usd_per_million or 0.0) if profile is not None else 0.0
        ),
        fixed_request_cost_usd=(
            max(0.0, float(os.environ.get("AI_ESTIMATED_VISION_IMAGE_COST_USD", "0.002") or 0.002))
            if vision else 0.0
        ),
    )


def _normalized_provider_usage(
    envelope: dict[str, Any], *, native_anthropic: bool = False,
) -> dict[str, int]:
    raw = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    input_value = raw.get("input_tokens") if native_anthropic else raw.get("prompt_tokens")
    output_value = raw.get("output_tokens") if native_anthropic else raw.get("completion_tokens")
    if input_value is None:
        input_value = raw.get("input_tokens")
    if output_value is None:
        output_value = raw.get("output_tokens")
    total_value = raw.get("total_tokens")
    result: dict[str, int] = {}
    for key, value in (
        ("input_tokens", input_value),
        ("output_tokens", output_value),
        ("total_tokens", total_value),
    ):
        try:
            result[key] = max(0, int(value or 0))
        except (TypeError, ValueError):
            result[key] = 0
    if not result["total_tokens"]:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        try:
            result["cached_input_tokens"] = max(0, int(details.get("cached_tokens") or 0))
        except (TypeError, ValueError):
            result["cached_input_tokens"] = 0
    return result


def _capture_provider_response_metadata(
    envelope: dict[str, Any], *, payload: dict[str, Any], native_anthropic: bool,
) -> dict[str, Any]:
    """Retain only non-content response metadata in the current request context."""

    usage = _normalized_provider_usage(envelope, native_anthropic=native_anthropic)
    finish_reason = ""
    if native_anthropic:
        finish_reason = str(envelope.get("stop_reason") or "")
    else:
        choices = envelope.get("choices") if isinstance(envelope.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        finish_reason = str(first.get("finish_reason") or "")
    requested_limit = int(
        payload.get("max_output_tokens")
        or payload.get("max_completion_tokens")
        or payload.get("max_tokens")
        or 0
    )
    normalized_finish = finish_reason.strip().casefold()
    metadata = {
        "finish_reason": finish_reason[:80],
        "prompt_token_count": usage.get("input_tokens", 0),
        "output_token_count": usage.get("output_tokens", 0),
        "output_token_limit_reached": (
            normalized_finish in {"length", "max_tokens", "max_token", "max_output_tokens"}
            or bool(requested_limit and usage.get("output_tokens", 0) >= requested_limit)
        ),
    }
    _LAST_PROVIDER_RESPONSE_METADATA.set(metadata)
    return metadata


def _record_gemini_structured_failure(
    raw_response: str,
    *,
    provider: str,
    model: str,
    request_profile: str,
    parser_error_type: str,
    parser_error_offset: int | None = None,
) -> dict[str, Any]:
    diagnostic = build_gemini_safe_diagnostic(
        raw_response,
        provider=provider,
        model=model,
        request_profile=request_profile,
        response_metadata=_LAST_PROVIDER_RESPONSE_METADATA.get(),
        parser_error_type=parser_error_type,
        parser_error_offset=parser_error_offset,
    )
    ai_runtime_trace.record_structured_response_failure(diagnostic)
    return diagnostic


def _experiment_reserve_attempt(*, provider: str, model: str):
    gate = current_experiment_spend_gate()
    if gate is None:
        if str(os.environ.get("INNER_VIEW_EXPERIMENT_MODE") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            if controlled_external_active():
                raise ControlledExternalGateTerminated("experiment_spend_gate_missing")
            raise AIProviderUnavailable(
                "Experiment provider dispatch is blocked because no spend authority "
                "is active in the current execution context.",
                failure_code="experiment_spend_gate_missing",
            )
        return None
    context = ai_runtime_trace.current_context()
    ai_runtime_trace.update_context(
        experiment_id=gate.controller.experiment_id,
        experiment_phase=gate.phase.value,
        pricing_version=gate.pricing_version,
    )
    if float(context.estimated_cost_usd or 0.0) <= 0:
        if controlled_external_active():
            raise ControlledExternalGateTerminated(
                "controlled_external_cost_estimate_missing"
            )
        raise AIProviderUnavailable(
            "Experiment provider dispatch is blocked because its cost cannot be estimated.",
            failure_code="experiment_cost_estimate_missing",
        )
    try:
        document_sha256, purpose = spend_document_context()
        return gate.controller.reserve(
            phase=gate.phase,
            estimated_cost_usd=context.estimated_cost_usd,
            provider=provider,
            model_id=model,
            profile_id=context.profile_id or "unscoped-profile",
            stage=context.stage or "unknown",
            document_sha256=document_sha256,
            purpose=purpose,
        )
    except SpendAuthorizationError as exc:
        if controlled_external_active():
            raise ControlledExternalGateTerminated(str(exc)) from exc
        raise AIProviderUnavailable(
            "Experiment provider dispatch was denied by the spend authority.",
            failure_code=str(exc),
        ) from exc


def _experiment_mark_dispatched(reservation):
    gate = current_experiment_spend_gate()
    if gate is not None and reservation is not None:
        return gate.controller.mark_dispatched(reservation.reservation_id)
    return reservation


def _experiment_abort_before_dispatch(reservation, *, reason: str) -> None:
    gate = current_experiment_spend_gate()
    if gate is not None and reservation is not None:
        gate.controller.release_reserved(reservation.reservation_id, reason=reason)


def _experiment_settle_attempt(
    reservation, *, envelope: dict[str, Any] | None,
    native_anthropic: bool = False, failure_code: str = "",
) -> None:
    gate = current_experiment_spend_gate()
    if gate is None or reservation is None:
        return
    if getattr(reservation, "status", "") == "reserved":
        gate.controller.release_reserved(
            reservation.reservation_id, reason=failure_code or "dispatch_not_started",
        )
        return
    usage = (
        _normalized_provider_usage(envelope, native_anthropic=native_anthropic)
        if isinstance(envelope, dict) else {}
    )
    provider_reported = any(usage.values())
    context = ai_runtime_trace.current_context()
    if controlled_external_active() and (
        not provider_reported
        or context.input_cost_usd_per_million <= 0
        or context.output_cost_usd_per_million <= 0
    ):
        settled = gate.controller.settle(
            reservation.reservation_id,
            actual_cost_usd=None,
            usage=usage,
            provider_reported_usage=provider_reported,
            failure_code=failure_code or "provider_usage_or_pricing_indeterminate",
        )
        ai_runtime_trace.record_provider_usage(
            reservation_id=reservation.reservation_id,
            usage=usage,
            actual_cost_usd=(
                float(settled.actual_cost_usd) if settled.actual_cost_usd else None
            ),
            provider_reported=provider_reported,
            failure_code=failure_code or "provider_usage_or_pricing_indeterminate",
        )
        gate.controller.cancel_outstanding(
            reason="provider_usage_or_pricing_indeterminate"
        )
        raise ControlledExternalGateTerminated(
            "provider_usage_or_pricing_indeterminate"
        )
    actual: float | None = None
    if provider_reported and (
        context.input_cost_usd_per_million > 0 or context.output_cost_usd_per_million > 0
    ):
        actual = (
            usage.get("input_tokens", 0) * context.input_cost_usd_per_million / 1_000_000
            + usage.get("output_tokens", 0) * context.output_cost_usd_per_million / 1_000_000
            + max(0.0, float(context.fixed_request_cost_usd or 0.0))
        )
    settled = gate.controller.settle(
        reservation.reservation_id,
        actual_cost_usd=actual,
        usage=usage,
        provider_reported_usage=provider_reported,
        failure_code=failure_code or None,
    )
    ai_runtime_trace.record_provider_usage(
        reservation_id=reservation.reservation_id,
        usage=usage,
        actual_cost_usd=(float(settled.actual_cost_usd) if settled.actual_cost_usd else None),
        provider_reported=provider_reported,
        failure_code=failure_code,
    )


def _reserve_cost_budget(scope_id: str, estimated_cost_usd: float) -> None:
    if estimated_cost_usd <= 0:
        return
    try:
        limit = max(0.0, float(os.environ.get("AI_MAX_COST_PER_BATCH_USD", "0.50")))
    except ValueError:
        limit = 0.50
    if limit <= 0:
        raise AIProviderUnavailable(
            "AI cost budget is disabled for this batch.", failure_code="cost_budget_exceeded",
        )
    with _COST_BUDGET_LOCK:
        current = _COST_BUDGET_RESERVED_USD.get(scope_id, 0.0)
        if current + estimated_cost_usd > limit:
            raise AIProviderUnavailable(
                "AI cost budget would be exceeded for this batch.",
                failure_code="cost_budget_exceeded",
            )
        _COST_BUDGET_RESERVED_USD[scope_id] = current + estimated_cost_usd


def reset_cost_budget(scope_id: str) -> None:
    """Start a fresh bounded accounting run without retaining prior spend.

    The ledger is process-local and contains estimates only.  Resetting occurs
    once at the explicit batch entry point; retries inside that run continue to
    accumulate so a provider/schema failure cannot bypass the cap.
    """
    if not scope_id:
        return
    with _COST_BUDGET_LOCK:
        _COST_BUDGET_RESERVED_USD.pop(scope_id, None)


def _verified_cost_routing_profile_ids() -> set[str]:
    """Return only profile IDs backed by a successful private capability probe."""
    ids = {
        value.strip()
        for value in os.environ.get("AI_COST_ROUTING_VERIFIED_PROFILE_IDS", "").split(",")
        if value.strip()
    }
    report_path = os.environ.get("AI_PROVIDER_CAPABILITY_REPORT", "").strip()
    if report_path:
        try:
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            ids.update(
                str(row.get("profile_id"))
                for row in report.get("profiles", [])
                if row.get("health_status") == "healthy" and row.get("profile_id")
            )
        except (OSError, TypeError, ValueError):
            pass
    return ids


def _select_cost_routing_profile(
    role_value: str, *, experiment_context: ExperimentProviderContext | None = None,
):
    """Choose the cheapest healthy profile for a role without guessing models.

    The four legacy ``runtime-*`` profiles remain eligible for backward
    compatibility. Additional providers must first appear in a successful
    capability report (or its explicit private list) before receiving traffic.
    """
    try:
        from .provider_capabilities import ModelProfileRole, ProfileLoader
        role = ModelProfileRole(role_value)
        profiles = [
            profile for profile in ProfileLoader().load()
            if profile.role is role and profile.enabled and profile.credentials_present
        ]
    except (ImportError, ValueError):
        return None
    if controlled_external_active():
        context = require_experiment_provider_context(
            experiment_context or current_experiment_provider_context()
        )
        exact = next((
            profile for profile in profiles
            if profile.profile_id == context.authorized_profile_id
            and str(profile.provider or "").strip().casefold() in {
                "gemini", "google_gemini",
            }
            and str(profile.model_id or "").strip() == context.authorized_model
            and str(profile.base_url or "").rstrip("/")
            in {
                context.allowed_endpoint.rsplit("/chat/completions", 1)[0],
                context.allowed_endpoint,
            }
        ), None)
        if exact is None:
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            )
        return exact
    verified = _verified_cost_routing_profile_ids()
    eligible = [
        profile for profile in profiles
        if profile.profile_id.startswith("runtime-") or profile.profile_id in verified
    ]
    if not eligible:
        return None
    explicit = os.environ.get(
        "AI_VISION_ROUTING_PROFILE_ID" if role_value == "multimodal_extraction"
        else "AI_TEXT_ROUTING_PROFILE_ID",
        "",
    ).strip()
    if explicit:
        return next((profile for profile in eligible if profile.profile_id == explicit), None)
    return min(eligible, key=lambda profile: (
        profile.input_cost_usd_per_million is None
        or profile.output_cost_usd_per_million is None,
        (profile.input_cost_usd_per_million or 0)
        + (profile.output_cost_usd_per_million or 0),
        profile.routing_priority,
        profile.profile_id,
    ))


def extraction_profile_identity(
    *, vision: bool, model_override: str = "", force_model_override: bool = False,
    experiment_context: ExperimentProviderContext | None = None,
) -> tuple[str, str, str]:
    """Return the exact configured request identity without calling a provider.

    The page-facts cache needs the same routing decision as extraction before
    it renders high-resolution evidence.  This helper intentionally exposes
    only provider/profile/model identifiers; credentials and endpoints never
    leave the provider module.
    """
    status = (
        _require_vision_configured(experiment_context)
        if vision else _require_configured(experiment_context)
    )
    role = "multimodal_extraction" if vision else "text_extraction"
    if controlled_external_active() and (not vision or force_model_override):
        raise ControlledExternalGateTerminated("controlled_provider_route_blocked")
    profile = (
        None if force_model_override
        else _select_cost_routing_profile(
            role, experiment_context=experiment_context,
        )
    )
    configured_model = (
        (status.vision_model or status.model or "") if vision else (status.model or "")
    ).strip()
    if model_override and (force_model_override or model_override != configured_model):
        profile = None
    provider = (
        profile.provider
        if profile is not None
        else ((status.vision_provider or status.provider or "") if vision else (status.provider or ""))
    )
    model = (profile.model_id if profile is not None else (model_override or configured_model)).strip()
    profile_id = (
        profile.profile_id
        if profile is not None
        else (
            "runtime-vision-forced"
            if vision and force_model_override
            else ("runtime-vision" if vision else "runtime-text")
        )
    )
    return provider, profile_id, model


def _extraction_cache_key(payload: dict[str, Any], *, vision: bool) -> str:
    encoded = json.dumps(
        {
            "version": _EXTRACTION_CACHE_VERSION,
            "vision": vision,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _frozen_cache_payload(profile_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Snapshot request identity before repair retries mutate their working payload."""

    def fingerprint_binary(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: fingerprint_binary(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fingerprint_binary(item) for item in value]
        if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
            return {
                "data_url_sha256": hashlib.sha256(value.encode("ascii")).hexdigest(),
                "encoded_length": len(value),
            }
        return value

    return {
        "routing_profile_id": profile_id,
        "request": fingerprint_binary(json.loads(json.dumps(request))),
    }


def _load_extraction_cache(payload: dict[str, Any], *, vision: bool) -> dict[str, Any] | None:
    key = _extraction_cache_key(payload, vision=vision)
    if controlled_external_active():
        # The operator authorized one-shot Gemini facts extraction, not an
        # explicit request/response cache. Observed facts are persisted by the
        # private experiment result contract after validation instead.
        ai_runtime_trace.record_cache(key, hit=False, layer="provider_request_disabled")
        return None
    path = settings.WEBAPP_DATA_ROOT / "cache" / "ai_invoice" / f"{key}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        ai_runtime_trace.record_cache(key, hit=False, layer="provider_request")
        return None
    if not isinstance(cached, dict):
        ai_runtime_trace.record_cache(key, hit=False, layer="provider_request")
        return None
    ai_runtime_trace.record_cache(key, hit=True, layer="provider_request")
    result = json.loads(json.dumps(cached))
    result["_provider_cache_hit"] = True
    return result


def _save_extraction_cache(payload: dict[str, Any], result: dict[str, Any], *, vision: bool) -> None:
    if controlled_external_active():
        return
    path = settings.WEBAPP_DATA_ROOT / "cache" / "ai_invoice" / f"{_extraction_cache_key(payload, vision=vision)}.json"
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class AIProviderError(RuntimeError):
    """Base exception for configured provider failures."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str = "provider_error",
        http_status: int | None = None,
        provider_error_type: str = "",
        provider_error_code: str = "",
        provider_error_param: str = "",
        structured_diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.http_status = http_status
        self.provider_error_type = provider_error_type
        self.provider_error_code = provider_error_code
        self.provider_error_param = provider_error_param
        self.structured_diagnostic = dict(structured_diagnostic or {})

    def safe_diagnostic(self) -> dict[str, Any]:
        """Return allow-listed diagnostics that can be persisted without secrets."""
        safe = {
            "failure_code": self.failure_code,
            "http_status": self.http_status,
            "provider_error_type": self.provider_error_type or None,
            "provider_error_code": self.provider_error_code or None,
            "provider_error_param": self.provider_error_param or None,
        }
        if self.structured_diagnostic:
            safe["structured_response"] = dict(self.structured_diagnostic)
        return safe


class AIProviderNotConfigured(AIProviderError):
    """Raised when AI invoice processing is disabled or incomplete."""


class AIProviderInvalidJSON(AIProviderError):
    """Raised when provider text cannot be parsed as strict JSON."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("failure_code", "provider_invalid_json")
        super().__init__(message, **kwargs)


class AIProviderInvalidSchema(AIProviderError):
    """Raised when parsed provider JSON does not match the invoice schema."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("failure_code", "provider_invalid_schema")
        super().__init__(message, **kwargs)


class AIProviderUnavailable(AIProviderError):
    """Raised when the configured provider cannot be reached."""


@dataclass(frozen=True)
class AIProviderStatus:
    enabled: bool
    provider: str | None
    model: str | None
    configured: bool
    supports_vision: bool
    vision_enabled: bool
    vision_provider: str | None
    vision_model: str | None
    vision_mode: str
    message: str


def provider_status(
    experiment_context: ExperimentProviderContext | None = None,
) -> AIProviderStatus:
    if controlled_external_active():
        context = require_experiment_provider_context(
            experiment_context or current_experiment_provider_context()
        )
        return AIProviderStatus(
            enabled=True,
            provider=context.authorized_provider,
            model=context.authorized_model,
            configured=True,
            supports_vision=True,
            vision_enabled=True,
            vision_provider=context.authorized_provider,
            vision_model=context.authorized_model,
            vision_mode="always",
            message="Controlled external Gemini facts-only profile is configured.",
        )
    provider = (settings.AI_PROVIDER or "").strip().lower()
    model = (settings.AI_MODEL or "").strip()
    key = (settings.AI_API_KEY or "").strip()
    base_url = (settings.AI_BASE_URL or "").strip()
    vision_requested = bool(getattr(settings, "AI_VISION_ENABLED", False))
    configured_vision_model = (getattr(settings, "AI_VISION_MODEL", "") or "").strip()
    vision_model = configured_vision_model
    vision_provider = (getattr(settings, "AI_VISION_PROVIDER", "") or provider).strip().lower()
    vision_key = (getattr(settings, "AI_VISION_API_KEY", "") or key).strip()
    vision_base_url = (getattr(settings, "AI_VISION_BASE_URL", "") or base_url).strip()
    vision_mode = (getattr(settings, "AI_VISION_MODE", "fallback_only") or "fallback_only").strip()
    enabled = bool(settings.AI_ASSIST_ENABLED)

    if not enabled:
        return AIProviderStatus(
            enabled=False,
            provider=None,
            model=None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=None,
            vision_model=None,
            vision_mode=vision_mode,
            message="AI is not configured.",
        )
    if provider == "mock":
        vision_enabled = bool(getattr(settings, "AI_VISION_ENABLED", False))
        return AIProviderStatus(
            enabled=True,
            provider="mock",
            model=model or "mock-invoice-v1",
            configured=True,
            supports_vision=vision_enabled,
            vision_enabled=vision_enabled,
            vision_provider="mock" if vision_enabled else None,
            vision_model=vision_model or ("mock-vision-v1" if vision_enabled else None),
            vision_mode=vision_mode,
            message=(
                "AI invoice processing is configured with the mock provider."
                if not vision_enabled
                else "AI invoice processing and mock vision assist are configured."
            ),
        )
    if provider in LOCAL_PROVIDER_NAMES:
        local_base = base_url or "http://127.0.0.1:11434"
        configured = bool(model and local_base)
        vision_enabled = bool(vision_requested and configured_vision_model and configured)
        return AIProviderStatus(
            enabled=True,
            provider="local_ollama",
            model=model or None,
            configured=configured,
            supports_vision=bool(configured_vision_model and configured),
            vision_enabled=vision_enabled,
            vision_provider="local_ollama" if configured_vision_model else None,
            vision_model=configured_vision_model or None,
            vision_mode=vision_mode,
            message=(
                "Local-only AI invoice processing is configured."
                if configured and (not vision_requested or vision_enabled)
                else "Local-only AI is enabled but the local model/profile is incomplete."
            ),
        )
    missing: list[str] = []
    if not provider:
        missing.append("AI_PROVIDER")
    if not model:
        missing.append("AI_MODEL")
    if not key:
        missing.append("AI_API_KEY")
    if provider == "openai_compatible" and not base_url:
        missing.append("AI_BASE_URL")
    elif provider not in _OPENAI_COMPATIBLE_PROVIDERS and not base_url:
        missing.append("AI_BASE_URL")
    if missing:
        return AIProviderStatus(
            enabled=True,
            provider=provider or None,
            model=model or None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=vision_provider or None,
            vision_model=vision_model or None,
            vision_mode=vision_mode,
            message="AI is enabled but missing: " + ", ".join(missing),
        )
    if provider not in _OPENAI_COMPATIBLE_PROVIDERS:
        return AIProviderStatus(
            enabled=True,
            provider=provider or None,
            model=model or None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=vision_provider or None,
            vision_model=vision_model or None,
            vision_mode=vision_mode,
            message=(
                "Unsupported AI_PROVIDER. Configure an OpenAI-compatible "
                "OpenAI, Gemini, DeepSeek, or Anthropic profile."
            ),
        )
    vision_missing: list[str] = []
    if vision_requested:
        if not vision_model:
            vision_missing.append("AI_VISION_MODEL")
        if not vision_key:
            vision_missing.append("AI_VISION_API_KEY or AI_API_KEY")
        if vision_provider == "openai_compatible" and not vision_base_url:
            vision_missing.append("AI_VISION_BASE_URL or AI_BASE_URL")
        elif vision_provider not in (_OPENAI_COMPATIBLE_PROVIDERS | {"mock"}) and not vision_base_url:
            vision_missing.append("AI_VISION_BASE_URL or AI_BASE_URL")
    supports_vision = bool(
        vision_model and vision_key
        and (vision_base_url or vision_provider in _PROVIDER_DEFAULT_BASE_URLS)
    )
    vision_enabled = bool(vision_requested and supports_vision)
    return AIProviderStatus(
        enabled=True,
        provider=provider,
        model=model,
        configured=True,
        supports_vision=supports_vision,
        vision_enabled=vision_enabled,
        vision_provider=vision_provider or None,
        vision_model=vision_model or None,
        vision_mode=vision_mode,
        message=(
            "AI invoice processing is configured."
            if not vision_requested
            else (
                "AI invoice processing and vision assist are configured."
                if vision_enabled
                else "AI vision is enabled but missing: " + ", ".join(vision_missing)
            )
        ),
    )


def status_payload() -> dict[str, Any]:
    status = provider_status()
    return {
        "enabled": status.enabled,
        "provider": status.provider,
        "model": status.model,
        "configured": status.configured,
        "supports_vision": status.supports_vision,
        "vision_enabled": status.vision_enabled,
        "vision_provider": status.vision_provider,
        "vision_model": status.vision_model,
        "vision_mode": status.vision_mode,
        "message": status.message,
    }


def _require_configured(
    experiment_context: ExperimentProviderContext | None = None,
) -> AIProviderStatus:
    status = provider_status(experiment_context)
    if not status.enabled or not status.configured:
        raise AIProviderNotConfigured(status.message)
    return status


def _require_vision_configured(
    experiment_context: ExperimentProviderContext | None = None,
) -> AIProviderStatus:
    status = _require_configured(experiment_context)
    if not getattr(settings, "AI_VISION_ENABLED", False):
        raise AIProviderNotConfigured(
            "Vision assist is not enabled. Configure AI_VISION_ENABLED and a vision-capable model."
        )
    if not status.supports_vision or not status.vision_enabled:
        raise AIProviderNotConfigured(
            "Vision assist is not available for this provider. Configure AI_VISION_MODEL with a vision-capable model."
        )
    return status


def _chat_completions_url(provider: str, base_url: str) -> str:
    provider = (provider or "").strip().lower()
    base = (base_url or "").rstrip("/")
    if base.startswith("//"):
        base = "https:" + base
    elif base and "://" not in base:
        base = "https://" + base
    if not base:
        base = _PROVIDER_DEFAULT_BASE_URLS.get(provider, "")
    if provider == "openai_compatible" and not base:
        raise AIProviderNotConfigured("AI_BASE_URL is required for openai_compatible.")
    if not base:
        raise AIProviderNotConfigured("AI_BASE_URL is required for this provider.")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _responses_url(base_url: str) -> str:
    base = (base_url or _PROVIDER_DEFAULT_BASE_URLS["openai"]).rstrip("/")
    if base.startswith("//"):
        base = "https:" + base
    elif base and "://" not in base:
        base = "https://" + base
    return base if base.endswith("/responses") else f"{base}/responses"


def _anthropic_messages_url(base_url: str) -> str:
    base = (base_url or _PROVIDER_DEFAULT_BASE_URLS["anthropic"]).rstrip("/")
    return base if base.endswith("/messages") else f"{base}/messages"


def _anthropic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt the internal request to Claude's native Messages API."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            system_parts.append(str(content or ""))
            continue
        if isinstance(content, list):
            native_content: list[dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    native_content.append({"type": "text", "text": str(item)})
                elif item.get("type") == "text":
                    native_content.append({"type": "text", "text": str(item.get("text") or "")})
                elif item.get("type") == "image_url":
                    url = str((item.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:") and ";base64," in url:
                        header, data = url.split(",", 1)
                        native_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": header[5:].split(";", 1)[0],
                                "data": data,
                            },
                        })
            content = native_content
        messages.append({
            "role": role if role in {"user", "assistant"} else "user",
            "content": content,
        })
    result: dict[str, Any] = {
        "model": payload.get("model"),
        "max_tokens": int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 4096),
        "messages": messages,
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    return result


def _completion_controls(provider: str, max_output_tokens: int) -> dict[str, Any]:
    """Return provider-compatible generation controls.

    Current OpenAI reasoning-capable chat models reject the legacy
    ``max_tokens`` parameter (and do not accept a forced temperature).  Keep
    the legacy controls for other OpenAI-compatible providers, whose APIs may
    not implement the newer OpenAI parameter names.
    """
    token_limit = min(max(512, int(max_output_tokens or 4096)), 8192)
    normalized = (provider or "").strip().lower()
    if normalized == "openai":
        return {
            "max_completion_tokens": token_limit,
            "reasoning_effort": "low",
        }
    if normalized in {"anthropic", "claude"}:
        # Claude's compatibility endpoint accepts max tokens but newer models
        # may reject non-default sampling controls.
        return {"max_completion_tokens": token_limit}
    return {"max_tokens": token_limit, "temperature": 0}


def _response_char_limit(payload: dict[str, Any]) -> int:
    """Keep the transport cap consistent with the requested output budget."""
    requested_tokens = int(
        payload.get("max_output_tokens")
        or payload.get("max_completion_tokens")
        or payload.get("max_tokens")
        or 4096
    )
    # JSON, escaped Unicode, and long numeric arrays can exceed four characters
    # per token.  Keep a hard ceiling while never rejecting a response that is
    # still within the generation limit we explicitly asked the provider for.
    return min(
        200_000,
        max(int(settings.AI_MAX_OUTPUT_CHARS or 20_000), requested_tokens * 8),
    )


def _safe_http_error_fields(raw_body: str) -> tuple[str, str, str]:
    """Extract non-secret provider error identifiers without retaining text."""
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            return "", "", ""
        error = payload.get("error") or {}
        if not isinstance(error, dict):
            return "", "", ""
        return tuple(
            str(error.get(field) or "")[:120]
            for field in ("type", "code", "param")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", "", ""


def _send_chat_completion(
    *,
    provider: str,
    payload: dict[str, Any],
    vision: bool = False,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    timeout_seconds_override: int | None = None,
    max_attempts_override: int | None = None,
    endpoint_surface_override: str | None = None,
    capability_override: str | None = None,
    experiment_context: ExperimentProviderContext | None = None,
    controlled_call_purpose: ControlledCallPurpose = ControlledCallPurpose.OTHER_VISUAL,
    request_profile_id: str = "",
    controlled_call_permit: ControlledCallPermit | None = None,
) -> str:
    _LAST_PROVIDER_RESPONSE_METADATA.set({})
    controlled_context = None
    if controlled_external_active():
        controlled_context = require_experiment_provider_context(experiment_context)
    request_provider = (
        controlled_context.authorized_provider
        if controlled_context is not None
        else
        provider
        if api_key_override is not None or base_url_override is not None
        else (
            (getattr(settings, "AI_VISION_PROVIDER", "") or provider).strip().lower()
            if vision
            else provider
        )
    )
    request_base_url = (
        controlled_context.allowed_endpoint.rsplit("/chat/completions", 1)[0]
        if controlled_context is not None
        else base_url_override or (
        (getattr(settings, "AI_VISION_BASE_URL", "") or settings.AI_BASE_URL).strip()
        if vision
        else settings.AI_BASE_URL
        )
    )
    request_key = api_key_override or (
        (getattr(settings, "AI_VISION_API_KEY", "") or settings.AI_API_KEY).strip()
        if vision
        else settings.AI_API_KEY
    )
    request_timeout = timeout_seconds_override or (
        max(30, int(getattr(settings, "AI_VISION_TIMEOUT_SECONDS", 120) or 120))
        if vision
        else settings.AI_TIMEOUT_SECONDS
    )
    if str(request_provider or "").strip().lower() in LOCAL_PROVIDER_NAMES:
        from .local_multimodal_provider import (
            LocalMultimodalProvider,
            LocalMultimodalProviderError,
        )

        local_provider = LocalMultimodalProvider(
            model=str(payload.get("model") or ""),
            base_url=request_base_url or "http://127.0.0.1:11434",
            profile_id=ai_runtime_trace.current_context().profile_id or "phase-a-local",
            timeout_seconds=int(request_timeout or 180),
        )
        try:
            with ai_runtime_trace.provider_attempt("local_ollama", 1):
                result = local_provider.chat_completion(payload)
        except LocalInferenceNetworkBlocked as exc:
            raise AIProviderUnavailable(
                "Local-only inference rejected a non-loopback dispatch.",
                failure_code=str(exc),
            ) from exc
        except LocalMultimodalProviderError as exc:
            raise AIProviderUnavailable(
                "Local multimodal inference did not return a usable structured response.",
                failure_code=exc.failure_code,
            ) from exc
        ai_runtime_trace.record_schema_result("valid")
        output = dict(result.structured_output)
        output["_local_provider_runtime"] = {
            "contract_version": result.contract_version,
            "request_id": result.request_id,
            "provider": result.provider,
            "model": result.model,
            "model_version": result.model_version,
            "execution_profile": result.execution_profile,
            "response_channel": result.response_channel,
            "page_identifiers": list(result.page_identifiers),
            "latency_ms": result.latency_ms,
            "resources": result.resources.model_dump(mode="json"),
            "warnings": list(result.warnings),
            "failure_reason": result.failure_reason,
        }
        return json.dumps(output, separators=(",", ":"))
    if not request_key:
        label = "AI vision provider" if vision else "AI provider"
        raise AIProviderNotConfigured(f"{label} API key is not configured.")
    native_anthropic = request_provider in {"anthropic", "claude"}
    endpoint_surface = endpoint_surface_override or (
        "anthropic_messages" if native_anthropic else "chat_completions"
    )
    capability = capability_override or (
        "visual_document_understanding" if vision else "text_generation"
    )
    model = str(payload.get("model") or "").strip()
    _assert_provider_circuit_closed(
        provider=request_provider,
        model=model,
        endpoint_surface=endpoint_surface,
        capability=capability,
    )
    request_payload = _anthropic_payload(payload) if native_anthropic else payload
    url = (
        _anthropic_messages_url(request_base_url)
        if native_anthropic else _chat_completions_url(request_provider, request_base_url)
    )
    try:
        assert_dispatch_allowed(
            provider=request_provider,
            url=url,
            stage=endpoint_surface_override or ("vision" if vision else "text"),
        )
    except LocalInferenceNetworkBlocked as exc:
        if controlled_external_active():
            preflight_controlled_provider_route(
                provider_context=experiment_context,
                provider=request_provider,
                model=str(payload.get("model") or ""),
                profile_id=(
                    request_profile_id
                    or ai_runtime_trace.current_context().profile_id
                ),
                endpoint=url,
                call_purpose=ControlledCallPurpose.OTHER_VISUAL,
                stage=(
                    endpoint_surface_override
                    or ("vision" if vision else "text")
                ),
            )
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            ) from exc  # pragma: no cover - preflight always terminates
        raise AIProviderUnavailable(
            "Remote provider dispatch is disabled in local-only experiment mode.",
            failure_code=str(exc),
        ) from exc
    try:
        assert_controlled_external_dispatch_allowed(
            provider=request_provider,
            url=url,
            stage=endpoint_surface_override or ("vision" if vision else "text"),
            payload=request_payload,
            provider_context=experiment_context,
            call_purpose=controlled_call_purpose,
            profile_id=request_profile_id or ai_runtime_trace.current_context().profile_id,
            call_permit=controlled_call_permit,
        )
    except ControlledExternalGateTerminated:
        raise
    except ControlledExternalBlocked as exc:
        ai_runtime_trace.record_blocked_network_attempt(
            provider=request_provider,
            stage=endpoint_surface_override or ("vision" if vision else "text"),
            failure_code=exc.failure_code,
        )
        raise ControlledExternalGateTerminated(exc.failure_code) from exc
    raw = json.dumps(request_payload).encode("utf-8")
    response_char_limit = _response_char_limit(payload)
    label = "AI vision provider" if vision else "AI provider"
    retryable_statuses = {429, 500, 502, 503, 504}
    max_attempts = 1 if controlled_external_active() else (max_attempts_override or 3)
    last_http_error: tuple[int, str] | None = None
    successful_reservation = None
    for attempt in range(max_attempts):
        reservation = _experiment_reserve_attempt(provider=request_provider, model=model)
        try:
            headers = {"Content-Type": "application/json"}
            if native_anthropic:
                headers.update({"x-api-key": request_key, "anthropic-version": "2023-06-01"})
            else:
                headers["Authorization"] = f"Bearer {request_key}"
            req = urllib.request.Request(
                url,
                data=raw,
                headers=headers,
                method="POST",
            )
        except Exception:
            _experiment_abort_before_dispatch(
                reservation, reason="request_construction_failed",
            )
            raise
        try:
            with ai_runtime_trace.provider_attempt(request_provider, attempt + 1):
                reservation = _experiment_mark_dispatched(reservation)
                with controlled_urlopen(req, timeout=request_timeout) as resp:
                    body = resp.read(response_char_limit * 3).decode("utf-8", "replace")
            successful_reservation = reservation
            break
        except urllib.error.HTTPError as exc:
            safe_body = exc.read(1000).decode("utf-8", "replace")
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code=f"http_{int(exc.code)}",
            )
            last_http_error = (exc.code, safe_body)
            # Provider error bodies can echo masked or partial credentials.
            # Preserve the body only in-process for capability/error handling;
            # never emit it to normal logs.
            error_type, error_code, error_param = _safe_http_error_fields(safe_body)
            _LOG.warning(
                "%s HTTP error %s type=%s code=%s param=%s",
                label,
                exc.code,
                error_type or "unknown",
                error_code or "unknown",
                error_param or "none",
            )
            if vision and ("image_url" in safe_body or "expected `text`" in safe_body or "expected text" in safe_body):
                raise AIProviderNotConfigured(
                    "The configured AI vision provider/model does not accept image input. "
                    "Set AI_VISION_MODEL plus AI_VISION_BASE_URL/AI_VISION_API_KEY for a vision-capable OpenAI-compatible provider.",
                    failure_code="vision_input_unsupported",
                    http_status=exc.code,
                    provider_error_type=error_type,
                    provider_error_code=error_code,
                    provider_error_param=error_param,
                ) from exc
            # One bounded retry is allowed for a transient gateway 401.  A
            # second 401, or the first 403/404, opens the process-level route
            # circuit so subsequent invoices fail closed without transport.
            if exc.code == 401 and attempt == 0 and max_attempts > 1:
                time.sleep(0.75 * (attempt + 1))
                continue
            if exc.code not in retryable_statuses or attempt >= max_attempts - 1:
                if exc.code in _PERMANENT_PROVIDER_STATUSES:
                    _open_provider_circuit(
                        provider=request_provider,
                        model=model,
                        endpoint_surface=endpoint_surface,
                        capability=capability,
                        http_status=exc.code,
                        failure_code=(
                            "vision_http_error" if vision else "text_http_error"
                        ),
                    )
                if exc.code in {400, 401, 403, 404, 405, 415, 422}:
                    _mark_native_pdf_surface_unavailable(payload.get("model"))
                raise AIProviderUnavailable(
                    f"{label} returned HTTP {exc.code}.",
                    failure_code="vision_http_error" if vision else "text_http_error",
                    http_status=exc.code,
                    provider_error_type=error_type,
                    provider_error_code=error_code,
                    provider_error_param=error_param,
                ) from exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code="transport_error",
            )
            if attempt >= max_attempts - 1:
                raise AIProviderUnavailable(
                    f"{label} request failed or timed out.",
                    failure_code="vision_transport_error" if vision else "text_transport_error",
                ) from exc
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code=type(exc).__name__,
            )
            raise
    else:
        code, _ = last_http_error or (0, "")
        raise AIProviderUnavailable(
            f"{label} returned HTTP {code}.",
            failure_code="vision_http_error" if vision else "text_http_error",
            http_status=code or None,
        )

    envelope: dict[str, Any] | None = None
    try:
        envelope = json.loads(body)
        _capture_provider_response_metadata(
            envelope, payload=payload, native_anthropic=native_anthropic,
        )
        content = (
            envelope.get("content") if native_anthropic
            else envelope["choices"][0]["message"]["content"]
        )
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            content = "\n".join(parts)
    except Exception as exc:
        _experiment_settle_attempt(
            successful_reservation, envelope=envelope,
            native_anthropic=native_anthropic,
            failure_code="invalid_response_shape",
        )
        label = "AI vision provider" if vision else "AI provider"
        diagnostic = None
        if request_provider in {"gemini", "google_gemini"}:
            diagnostic = _record_gemini_structured_failure(
                body if isinstance(body, str) else "",
                provider=request_provider,
                model=model,
                request_profile=ai_runtime_trace.current_context().profile_id,
                parser_error_type="UnexpectedProviderResponseShape",
            )
        raise AIProviderInvalidJSON(
            f"{label} returned an unexpected response shape.",
            structured_diagnostic=diagnostic,
        ) from exc
    if not isinstance(content, str) or not content.strip():
        _experiment_settle_attempt(
            successful_reservation, envelope=envelope,
            native_anthropic=native_anthropic,
            failure_code="empty_response_content",
        )
        label = "AI vision provider" if vision else "AI provider"
        diagnostic = None
        if request_provider in {"gemini", "google_gemini"}:
            diagnostic = _record_gemini_structured_failure(
                content if isinstance(content, str) else "",
                provider=request_provider,
                model=model,
                request_profile=ai_runtime_trace.current_context().profile_id,
                parser_error_type="EmptyResponseContent",
            )
        raise AIProviderInvalidJSON(
            f"{label} response content was empty.", structured_diagnostic=diagnostic,
        )
    if len(content) > response_char_limit:
        _experiment_settle_attempt(
            successful_reservation, envelope=envelope,
            native_anthropic=native_anthropic,
            failure_code="response_content_limit_exceeded",
        )
        label = "AI vision provider" if vision else "AI provider"
        diagnostic = None
        if request_provider in {"gemini", "google_gemini"}:
            diagnostic = _record_gemini_structured_failure(
                content,
                provider=request_provider,
                model=model,
                request_profile=ai_runtime_trace.current_context().profile_id,
                parser_error_type="ResponseCharacterLimitExceeded",
            )
        raise AIProviderInvalidJSON(
            f"{label} response exceeded the configured output limit.",
            structured_diagnostic=diagnostic,
        )
    _experiment_settle_attempt(
        successful_reservation, envelope=envelope,
        native_anthropic=native_anthropic,
    )
    return content


def _extract_responses_text(envelope: dict[str, Any]) -> str:
    direct = envelope.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for output in envelope.get("output") or []:
        if not isinstance(output, dict):
            continue
        for item in output.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"output_text", "text"} and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(parts).strip()


def _send_openai_response(
    *,
    payload: dict[str, Any],
    api_key: str,
    base_url: str,
    timeout_seconds: int,
    max_attempts: int = 3,
) -> tuple[str, dict[str, int]]:
    """Send a native Responses request without logging private input or bodies."""

    if not api_key:
        raise AIProviderNotConfigured("AI vision provider API key is not configured.")
    raw = json.dumps(payload).encode("utf-8")
    model = str(payload.get("model") or "").strip()
    endpoint_surface = "responses_native_pdf"
    capability = "native_pdf"
    _assert_provider_circuit_closed(
        provider="openai",
        model=model,
        endpoint_surface=endpoint_surface,
        capability=capability,
    )
    response_char_limit = _response_char_limit(payload)
    url = _responses_url(base_url)
    try:
        assert_dispatch_allowed(
            provider="openai", url=url, stage="responses_native_pdf",
        )
    except LocalInferenceNetworkBlocked as exc:
        raise AIProviderUnavailable(
            "Remote native-PDF dispatch is disabled in local-only experiment mode.",
            failure_code=str(exc),
        ) from exc
    try:
        assert_controlled_external_dispatch_allowed(
            provider="openai", url=url, stage="responses_native_pdf", payload=payload,
        )
    except ControlledExternalBlocked as exc:
        ai_runtime_trace.record_blocked_network_attempt(
            provider="openai", stage="responses_native_pdf",
            failure_code=exc.failure_code,
        )
        raise AIProviderUnavailable(
            "Controlled external native-PDF dispatch is not authorized.",
            failure_code=exc.failure_code,
        ) from exc
    retryable_statuses = {429, 500, 502, 503, 504}
    body = ""
    successful_reservation = None
    for attempt in range(max(1, int(max_attempts or 1))):
        reservation = _experiment_reserve_attempt(provider="openai", model=model)
        try:
            req = urllib.request.Request(
                url,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
        except Exception:
            _experiment_abort_before_dispatch(
                reservation, reason="request_construction_failed",
            )
            raise
        try:
            with ai_runtime_trace.provider_attempt("openai", attempt + 1):
                reservation = _experiment_mark_dispatched(reservation)
                with controlled_urlopen(
                    req, timeout=max(30, int(timeout_seconds or 240))
                ) as resp:
                    body = resp.read(response_char_limit * 4).decode("utf-8", "replace")
            successful_reservation = reservation
            break
        except urllib.error.HTTPError as exc:
            safe_body = exc.read(1000).decode("utf-8", "replace")
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code=f"http_{int(exc.code)}",
            )
            error_type, error_code, error_param = _safe_http_error_fields(safe_body)
            _LOG.warning(
                "Native document vision HTTP error %s type=%s code=%s param=%s",
                exc.code,
                error_type or "unknown",
                error_code or "unknown",
                error_param or "none",
            )
            if exc.code == 401 and attempt == 0 and max_attempts > 1:
                time.sleep(0.75)
                continue
            if exc.code not in retryable_statuses or attempt >= max_attempts - 1:
                if exc.code in _PERMANENT_PROVIDER_STATUSES:
                    _open_provider_circuit(
                        provider="openai",
                        model=model,
                        endpoint_surface=endpoint_surface,
                        capability=capability,
                        http_status=exc.code,
                        failure_code="native_pdf_http_error",
                    )
                    _mark_native_pdf_surface_unavailable(model)
                raise AIProviderUnavailable(
                    f"Native document vision returned HTTP {exc.code}.",
                    failure_code="native_pdf_http_error",
                    http_status=exc.code,
                    provider_error_type=error_type,
                    provider_error_code=error_code,
                    provider_error_param=error_param,
                ) from exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code="transport_error",
            )
            if attempt >= max_attempts - 1:
                _mark_native_pdf_surface_unavailable(model)
                raise AIProviderUnavailable(
                    "Native document vision request failed or timed out.",
                    failure_code="native_pdf_transport_error",
                ) from exc
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            _experiment_settle_attempt(
                reservation, envelope=None, failure_code=type(exc).__name__,
            )
            raise
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _experiment_settle_attempt(
            successful_reservation, envelope=None,
            failure_code="invalid_response_shape",
        )
        raise AIProviderInvalidJSON(
            "Native document vision returned an unexpected response shape."
        ) from exc
    content = _extract_responses_text(envelope)
    if not content:
        _experiment_settle_attempt(
            successful_reservation, envelope=envelope,
            failure_code="empty_response_content",
        )
        raise AIProviderInvalidJSON("Native document vision response content was empty.")
    if len(content) > response_char_limit:
        _experiment_settle_attempt(
            successful_reservation, envelope=envelope,
            failure_code="response_content_limit_exceeded",
        )
        raise AIProviderInvalidJSON(
            "Native document vision response exceeded the configured output limit."
        )
    raw_usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    usage: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        try:
            usage[key] = max(0, int(raw_usage.get(key) or 0))
        except (TypeError, ValueError):
            usage[key] = 0
    _experiment_settle_attempt(successful_reservation, envelope=envelope)
    return content, usage


def _extract_json_object(text: str) -> dict[str, Any]:
    trimmed = text.strip()
    if trimmed.startswith("```"):
        lines = trimmed.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        trimmed = "\n".join(lines).strip()
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        start = trimmed.find("{")
        end = trimmed.rfind("}")
        if start < 0 or end <= start:
            raise AIProviderInvalidJSON("AI response was not valid JSON.")
        try:
            parsed = json.loads(trimmed[start:end + 1])
        except json.JSONDecodeError as exc:
            decoder = json.JSONDecoder()
            best: dict[str, Any] | None = None
            best_score = -1
            for idx, char in enumerate(trimmed):
                if char != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(trimmed[idx:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    score = sum(1 for key in _REQUIRED_SCHEMA_KEYS if key in candidate)
                    if score > best_score:
                        best = candidate
                        best_score = score
            if best is None:
                raise AIProviderInvalidJSON("AI response was not valid JSON.") from exc
            parsed = best
    if not isinstance(parsed, dict):
        raise AIProviderInvalidJSON("AI response JSON must be an object.")
    return parsed


_REQUIRED_SCHEMA_KEYS = {
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "service_date",
    "due_date",
    "due_date_text",
    "payment_terms",
    "bill_or_credit",
    "account_number",
    "service_address",
    "address_role",
    "location_candidate",
    "service_period_start",
    "service_period_end",
    "service_period",
    "property_candidate",
    "property_abbreviation",
    "invoice_description",
    "line_items",
    "excluded_paid_rows",
    "subtotal",
    "tax_amount",
    "shipping_amount",
    "fees_amount",
    "total_amount",
    "visual_extraction_status",
    "unresolved_visual_regions",
    "page_reconciliations",
}


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "vendor_name": ("vendor", "vendorName", "supplier", "supplier_name", "supplierName"),
    "invoice_number": ("invoiceNo", "invoice_no", "invoiceNumber", "number", "invoice_id"),
    "invoice_date": ("invoiceDate", "date", "bill_date", "billDate"),
    "service_date": ("serviceDate", "date_of_service", "dateOfService", "work_date"),
    "due_date": ("dueDate", "payment_due_date", "paymentDueDate"),
    "payment_terms": ("paymentTerms", "terms", "terms_and_conditions", "payment_terms_text"),
    "bill_or_credit": ("billOrCredit", "type", "document_type"),
    "account_number": ("accountNumber", "customer_number", "customerNumber", "customer_id"),
    "service_address": ("serviceAddress", "ship_to", "shipTo", "shipping_address", "billing_address"),
    "address_role": ("addressRole", "service_address_role", "address_type", "addressType"),
    "location_candidate": (
        "location",
        "unit",
        "unit_number",
        "unitNumber",
        "apartment",
        "apartment_number",
        "suite",
        "job_unit",
        "customer_order_unit",
    ),
    "service_period_start": ("servicePeriodStart", "service_start_date", "serviceStartDate", "billing_period_start", "billingPeriodStart", "period_start"),
    "service_period_end": ("servicePeriodEnd", "service_end_date", "serviceEndDate", "billing_period_end", "billingPeriodEnd", "period_end"),
    "service_period": ("servicePeriod", "billing_period", "billingPeriod", "period", "service_dates", "date_range"),
    "property_candidate": ("property", "propertyCandidate", "property_name", "propertyName"),
    "property_abbreviation": ("propertyAbbreviation", "property_abbrev", "property_code"),
    "invoice_description": ("description", "summary", "invoiceDescription"),
    "line_items": ("items", "invoice_items", "invoiceItems", "lineItems", "products"),
    "subtotal": ("sub_total", "merchandise_subtotal", "merchandiseSubtotal"),
    "tax_amount": ("tax", "sales_tax", "salesTax", "taxAmount"),
    "shipping_amount": ("shipping", "shippingAmount", "freight"),
    "fees_amount": ("fees", "feesAmount", "other_fees"),
    "total_amount": ("total", "amount_due", "amountDue", "invoice_total", "invoiceTotal"),
}


_LINE_ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "description": ("item_description", "itemDescription", "name", "product", "details"),
    "raw_description": ("source_description", "sourceDescription", "verbatim_description"),
    "normalized_description": ("normalizedDescription", "expanded_description"),
    "generated_description": ("generatedDescription", "item_meaning", "plain_language_description"),
    "quantity": ("qty", "ordered", "shipped"),
    "unit_price": ("unitPrice", "price", "unit_cost", "unitCost"),
    "amount": ("total", "line_total", "lineTotal", "extension", "extended_amount"),
    "gl_account_candidate": ("gl_account", "glAccount", "gl_code", "glCode", "category"),
    "expense_type": ("expenseType", "expense_category", "category_name"),
    "is_replacement_reserve": ("replacementReserve", "isReplacementReserve"),
    "activity": ("column_header", "columnHeader", "charge_type", "chargeType", "service_type"),
    "row_label": ("rowLabel", "unit_label", "unitLabel"),
    "location_candidate": ("location", "unit", "unit_number", "unitNumber", "apartment"),
    "section_header": ("sectionHeader", "group_header", "groupHeader"),
    "source_page": ("sourcePage", "page", "page_number", "pageNumber"),
}


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _coerce_invoice_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize recoverable provider variations before validation.

    Real OpenAI-compatible providers occasionally return good invoice data
    with slightly different field names. Missing canonical fields are still
    surfaced later by backend validation, but they should not discard the
    whole invoice before rows can be reviewed.
    """
    coerced = dict(payload)
    for canonical, aliases in _FIELD_ALIASES.items():
        if canonical not in coerced or coerced.get(canonical) in (None, ""):
            value = _first_present(coerced, aliases)
            if value is not None:
                coerced[canonical] = value

    defaults: dict[str, Any] = {
        "line_items": [],
        "excluded_paid_rows": [],
        "subtotal": 0.0,
        "tax_amount": 0.0,
        "shipping_amount": 0.0,
        "fees_amount": 0.0,
        "total_amount": 0.0,
        "visual_extraction_status": "unknown",
        "unresolved_visual_regions": [],
        "page_reconciliations": [],
        "confidence": None,
        "warnings": [],
        "needs_manual_review": False,
    }
    for key in _REQUIRED_SCHEMA_KEYS:
        coerced.setdefault(key, defaults.get(key, ""))
    coerced.setdefault("confidence", None)
    coerced.setdefault("warnings", [])
    coerced.setdefault("needs_manual_review", False)

    if isinstance(coerced.get("warnings"), str):
        coerced["warnings"] = [coerced["warnings"]]
    elif coerced.get("warnings") is None:
        coerced["warnings"] = []

    raw_items = coerced.get("line_items")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    normalized_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        for canonical, aliases in _LINE_ITEM_ALIASES.items():
            if canonical not in normalized or normalized.get(canonical) in (None, ""):
                value = _first_present(normalized, aliases)
                if value is not None:
                    normalized[canonical] = value
        normalized.setdefault("description", "")
        normalized.setdefault("raw_description", normalized.get("description") or "")
        normalized.setdefault("normalized_description", "")
        normalized.setdefault("generated_description", "")
        normalized.setdefault("quantity", None)
        normalized.setdefault("unit_price", None)
        normalized.setdefault("amount", 0.0)
        normalized.setdefault("gl_account_candidate", "")
        normalized.setdefault("expense_type", "General")
        normalized.setdefault("is_replacement_reserve", False)
        normalized.setdefault("confidence", None)
        normalized.setdefault("reason", "")
        normalized.setdefault("activity", "")
        normalized.setdefault("row_label", "")
        normalized.setdefault("location_candidate", "")
        normalized.setdefault("section_header", "")
        normalized.setdefault("source_page", None)
        normalized_items.append(normalized)
    coerced["line_items"] = normalized_items
    raw_excluded = coerced.get("excluded_paid_rows")
    coerced["excluded_paid_rows"] = raw_excluded if isinstance(raw_excluded, list) else []
    return coerced


def _validate_invoice_schema(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _coerce_invoice_schema(payload)
    missing = sorted(k for k in _REQUIRED_SCHEMA_KEYS if k not in payload)
    if missing:
        raise AIProviderInvalidSchema(
            "AI response is missing required field(s): " + ", ".join(missing[:5])
        )
    line_items = payload.get("line_items")
    if not isinstance(line_items, list):
        raise AIProviderInvalidSchema("AI response line_items must be a list.")
    for idx, item in enumerate(line_items, start=1):
        if not isinstance(item, dict):
            raise AIProviderInvalidSchema(f"AI response line item {idx} must be an object.")
    warnings = payload.get("warnings")
    if warnings is None:
        payload["warnings"] = []
    elif not isinstance(warnings, list):
        raise AIProviderInvalidSchema("AI response warnings must be a list.")
    payload.setdefault("confidence", None)
    payload.setdefault("needs_manual_review", False)
    for item in line_items:
        item.setdefault("confidence", None)
        item.setdefault("reason", "")
    return payload


def _parse_invoice_content(content: str) -> dict[str, Any]:
    return _validate_invoice_schema(_extract_json_object(content))


def _native_vision_json_schema() -> dict[str, Any]:
    """Strict Responses schema for source-fact extraction, not accounting decisions."""

    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["number", "null"]}
    nullable_boolean = {"type": ["boolean", "null"]}
    line_properties = {
        "source_page": {"type": ["integer", "null"]},
        "section_header": nullable_string,
        "row_label": nullable_string,
        "location_candidate": nullable_string,
        "activity": nullable_string,
        "description": nullable_string,
        "raw_description": nullable_string,
        "normalized_description": nullable_string,
        "generated_description": nullable_string,
        "quantity": nullable_number,
        "unit_price": nullable_number,
        "amount": nullable_number,
        "gl_account_candidate": nullable_string,
        "expense_type": nullable_string,
        "is_replacement_reserve": nullable_boolean,
        "confidence": nullable_number,
        "reason": nullable_string,
    }
    component_properties = {
        "label": {"type": "string"},
        "amount": nullable_number,
    }
    paid_evidence_properties = {
        "page": {"type": "integer"},
        "text": {"type": "string"},
        "bbox": {
            "type": ["object", "null"],
            "properties": {key: {"type": "number"} for key in ("x", "y", "w", "h")},
            "required": ["x", "y", "w", "h"],
            "additionalProperties": False,
        },
        "confidence": nullable_number,
    }
    excluded_paid_properties = {
        "raw_apartment_number": nullable_string,
        "component_amounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": component_properties,
                "required": list(component_properties),
                "additionalProperties": False,
            },
        },
        "row_total": nullable_number,
        "paid_marker_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": paid_evidence_properties,
                "required": list(paid_evidence_properties),
                "additionalProperties": False,
            },
        },
        "exclusion_reason": {"type": "string"},
    }
    bbox_properties = {key: {"type": "number"} for key in ("x", "y", "w", "h")}
    candidate_properties = {
        "field_key": {"type": "string"},
        "field_label": {"type": "string"},
        "value": nullable_string,
        "page": {"type": "integer"},
        "bbox": {
            "type": ["object", "null"],
            "properties": bbox_properties,
            "required": list(bbox_properties),
            "additionalProperties": False,
        },
        "confidence": {"type": "number"},
        "validation_status": {"type": "string"},
    }
    page_properties = {
        "page": {"type": "integer"},
        "printed_page_total": nullable_number,
        "extracted_component_total": {"type": "number"},
        "difference": {"type": "number"},
        "status": {"type": "string"},
    }
    properties: dict[str, Any] = {
        key: nullable_string for key in (
            "vendor_name", "invoice_nature", "category", "invoice_number",
            "invoice_date", "service_date", "purchase_date", "ship_date",
            "received_date", "due_date", "due_date_text", "payment_terms", "bill_or_credit",
            "account_number", "service_address", "address_role",
            "location_candidate", "service_period_start", "service_period_end",
            "service_period", "property_candidate", "property_abbreviation",
            "invoice_description", "visual_extraction_status",
        )
    }
    properties.update({
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": line_properties,
                "required": list(line_properties),
                "additionalProperties": False,
            },
        },
        "excluded_paid_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": excluded_paid_properties,
                "required": list(excluded_paid_properties),
                "additionalProperties": False,
            },
        },
        "subtotal": nullable_number,
        "tax_amount": nullable_number,
        "shipping_amount": nullable_number,
        "fees_amount": nullable_number,
        "total_amount": nullable_number,
        "confidence": nullable_number,
        "warnings": {"type": "array", "items": {"type": "string"}},
        "needs_manual_review": {"type": "boolean"},
        "unresolved_visual_regions": {"type": "array", "items": {"type": "string"}},
        "page_reconciliations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": page_properties,
                "required": list(page_properties),
                "additionalProperties": False,
            },
        },
        "vision_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": candidate_properties,
                "required": list(candidate_properties),
                "additionalProperties": False,
            },
        },
    })
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _validate_visual_line_structure(
    payload: dict[str, Any], *, require_generated_description: bool = True,
) -> dict[str, Any]:
    """Reject visually lossy table extractions before they reach accounting."""
    payload = _expand_reconciled_matrix_rows(payload)
    extraction_status = str(payload.get("visual_extraction_status") or "unknown").strip().lower()
    if extraction_status == "aggregate_fallback":
        raise AIProviderInvalidSchema(
            "Visual extraction replaced visible source structure with an aggregate fallback. "
            "Re-read the source rows/allocations and preserve their evidence separately."
        )
    items = list(payload.get("line_items") or [])
    if not items:
        raise AIProviderInvalidSchema(
            "Visual invoice extraction returned no payable line items. Re-read the page and emit "
            "visible component lines, or one explicit-total fallback line when components are illegible."
        )
    if abs(float(payload.get("total_amount") or 0)) <= 0.009:
        raise AIProviderInvalidSchema(
            "Visual invoice extraction returned no payable invoice total. Re-read the final total or "
            "amount due and keep ambiguous values in warnings rather than returning an empty invoice."
        )
    unresolved_matrix_items = [
        item for item in items
        if isinstance(item, dict) and item.get("matrix_expansion_status") == "unresolved_arithmetic"
    ]
    if unresolved_matrix_items and len(unresolved_matrix_items) == len(items):
        raise AIProviderInvalidSchema(
            "Visual matrix extraction did not uniquely reconcile any component row. "
            "Re-read the matrix cells; a partial result is acceptable only when at least one component is proven."
        )
    total_headers = {"unit total", "row total", "subtotal", "invoice total", "total"}
    collapsed = [
        item for item in items
        if str(item.get("activity") or "").strip().lower() in total_headers
    ]
    if collapsed:
        raise AIProviderInvalidSchema(
            "A total column was returned as line-item activity. Re-read the visual table and emit "
            "one line item for each non-empty component charge cell using its billable column header; "
            "use row/unit totals only for reconciliation."
        )

    collapsed_matrix_rows = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("row_label") or "").strip():
            continue
        activity = str(item.get("activity") or "").strip()
        parts = [
            part.strip()
            for part in re.split(r"\s*(?:,|;|/|\band\b)\s*", activity, flags=re.IGNORECASE)
            if part.strip()
        ]
        if len(parts) > 1:
            collapsed_matrix_rows.append(item)
    if collapsed_matrix_rows:
        raise AIProviderInvalidSchema(
            "A matrix row collapsed several billable column headers into one line item. "
            "Emit one line item per non-empty component cell with one exact activity header, "
            "the shared row_label/location, and that component amount."
        )

    for index, item in enumerate(items, start=1):
        raw_description = str(item.get("raw_description") or item.get("description") or "").strip()
        generated_description = str(item.get("generated_description") or "").strip()
        if require_generated_description and raw_description and not generated_description:
            raise AIProviderInvalidSchema(
                f"Visual line item {index} is missing generated_description. Preserve raw_description and add a separate 3-to-8-word plain-language item identification."
            )
        if require_generated_description and len(generated_description.split()) > 10:
            raise AIProviderInvalidSchema(
                f"Visual line item {index} generated_description is too long. Use a concise 3-to-8-word identification."
            )

    line_total = sum(float(item.get("amount") or 0) for item in items)
    invoice_total = float(payload.get("total_amount") or 0)
    adders = sum(float(payload.get(key) or 0) for key in ("tax_amount", "shipping_amount", "fees_amount"))
    if items and invoice_total and abs((line_total + adders) - invoice_total) > 0.01:
        raise AIProviderInvalidSchema(
            "Visual line-item component amounts do not reconcile to the selected invoice total. "
            "Re-read every page, preserve source_page, expand matrix cells, and do not select one "
            "page total when the images contain continuation pages or conflicting totals."
        )
    for page_result in payload.get("page_reconciliations") or []:
        if not isinstance(page_result, dict):
            continue
        try:
            difference = abs(float(page_result.get("difference") or 0))
        except (TypeError, ValueError):
            difference = 0.0
        if str(page_result.get("status") or "").strip().lower() == "reconciled" and difference > 0.01:
            raise AIProviderInvalidSchema(
                "A page was marked reconciled even though its component difference is non-zero."
            )
    return payload


def _internal_schema_failure_path(exc: AIProviderInvalidSchema) -> str:
    """Classify an internal validation failure without persisting its message."""

    message = str(exc).casefold()
    if "no payable line items" in message:
        return "line_items"
    if "no payable invoice total" in message:
        return "total_amount"
    if "aggregate fallback" in message:
        return "visual_extraction_status"
    if "total column" in message or "matrix row" in message:
        return "line_items.activity"
    if "component row" in message:
        return "line_items.matrix_expansion_status"
    if "component amounts do not reconcile" in message:
        return "reconciliation.line_items_to_invoice_total"
    if "page was marked reconciled" in message:
        return "page_reconciliations.status"
    if "missing generated_description" in message:
        return "line_items.generated_description"
    if "generated_description is too long" in message:
        return "line_items.generated_description"
    if "line_items must be a list" in message:
        return "line_items"
    if "line item" in message and "must be an object" in message:
        return "line_items.item"
    if "warnings must be a list" in message:
        return "warnings"
    if "missing required field" in message:
        return "required_fields"
    return "strict_internal_contract"


def normalize_gemini_initial_observation(
    transport_payload: dict[str, Any], *, opaque_document_id: str,
    provider: str, profile_id: str, model_id: str,
) -> InitialNormalizationOutcome:
    """Return a typed boundary outcome after valid Gemini transport.

    A line-items-to-total mismatch is an evidence-backed unresolved
    observation, not malformed provider transport.  Every other strict
    structural failure retains the existing exception behavior.
    """

    try:
        strict_input = _validate_invoice_schema(copy.deepcopy(transport_payload))
        strict = _validate_visual_line_structure(
            strict_input, require_generated_description=False,
        )
    except AIProviderInvalidSchema as exc:
        validation_path = _internal_schema_failure_path(exc)
        if validation_path == "reconciliation.line_items_to_invoice_total":
            observation = build_unreconciled_observation(
                strict_input,
                opaque_document_id=opaque_document_id,
                provider=provider,
                profile_id=profile_id,
                model_id=model_id,
            )
            return InitialNormalizationOutcome.supplementary_required(
                observation, validation_path=validation_path,
            )
        return InitialNormalizationOutcome.unsupported(
            validation_path=validation_path,
            failure_code="initial_structured_response_invalid",
        )
    return InitialNormalizationOutcome.facts_ready(strict)


def normalize_gemini_supplementary_observation(
    initial_outcome: InitialNormalizationOutcome,
    merged_payload: dict[str, Any],
) -> InitialNormalizationOutcome:
    """Validate a new supplementary revision without mutating the initial one."""

    initial = initial_outcome.observation
    if (
        initial_outcome.category is not InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED
        or initial is None
    ):
        raise ValueError("supplementary_normalization_requires_initial_observation")
    candidate = copy.deepcopy(merged_payload)
    before = initial.deterministic_reconciliation_delta
    after_snapshot = reconciliation_snapshot(candidate)
    after = _decimal_or_none(after_snapshot.get("difference"))
    candidate["initial_observation_revision"] = initial.model_dump(mode="json")
    candidate["observation_line_item_revisions"] = {
        "before": [item.model_dump(mode="json") for item in initial.line_items],
        "after": copy.deepcopy(list(candidate.get("line_items") or [])),
    }
    candidate["reconciliation_delta_before"] = (
        str(before) if before is not None else None
    )
    candidate["reconciliation_delta_after"] = (
        str(after) if after is not None else None
    )
    try:
        strict = _validate_visual_line_structure(
            _validate_invoice_schema(candidate),
            require_generated_description=False,
        )
    except AIProviderInvalidSchema as exc:
        validation_path = _internal_schema_failure_path(exc)
        if validation_path != "reconciliation.line_items_to_invoice_total":
            raise
        effective = build_unreconciled_observation(
            candidate,
            opaque_document_id=initial.opaque_document_id,
            provider=initial.provenance.provider,
            profile_id=initial.provenance.profile_id,
            model_id=initial.provenance.model_id,
        )
        candidate.update({
            "reconciliation_state": "ran_unreconciled",
            "reconciliation_ran": True,
            "reconciliation_status": "unreconciled",
            "reconciliation_source_stage": "supplementary_visual_verification",
            "reconciliation_before": "unreconciled",
            "reconciliation_after": "unreconciled",
            "needs_manual_review": True,
            "visual_extraction_status": "partial",
        })
        return InitialNormalizationOutcome(
            category=InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED,
            validation_path=validation_path,
            failure_code=_supplementary_outcome_reason(candidate),
            working_observation_payload=candidate,
            observation=effective,
        )
    supplementary_status = _supplementary_visual_status(candidate)
    unresolved_visual = supplementary_status != "resolved"
    strict.update({
        "reconciliation_state": "ran_reconciled",
        "reconciliation_ran": True,
        "reconciliation_status": "reconciled",
        "reconciliation_source_stage": "supplementary_visual_verification",
        "reconciliation_before": "unreconciled",
        "reconciliation_after": "reconciled",
        "reconciliation_delta_before": str(before) if before is not None else None,
        "reconciliation_delta_after": str(after) if after is not None else None,
        "supplementary_visual_status": supplementary_status,
        "needs_manual_review": bool(
            strict.get("needs_manual_review") or unresolved_visual
        ),
        "visual_extraction_status": (
            "partial" if unresolved_visual else strict.get("visual_extraction_status", "complete")
        ),
    })
    return InitialNormalizationOutcome.facts_ready(strict)


def _supplementary_outcome_reason(payload: dict[str, Any]) -> str:
    revisions = [
        item for item in payload.get("supplementary_evidence_revisions") or []
        if isinstance(item, dict)
    ]
    observations = [
        item.get("observation") for item in revisions
        if isinstance(item.get("observation"), dict)
    ]
    warnings = {str(item or "").strip() for item in payload.get("warnings") or []}
    if "supplementary_evidence_localization_unavailable" in warnings:
        return "supplementary_evidence_localization_unavailable"
    if "supplementary_request_limit_reached" in warnings:
        return "supplementary_request_limit_reached"
    if any(item.get("contradiction_flag") is True for item in observations):
        return "supplementary_visual_evidence_contradiction"
    return "supplementary_visual_evidence_unresolved"


def _supplementary_visual_status(payload: dict[str, Any]) -> str:
    """Return visual status independently from arithmetic reconciliation."""

    revisions = [
        item for item in payload.get("supplementary_evidence_revisions") or []
        if isinstance(item, dict)
    ]
    observations = [
        item.get("observation") for item in revisions
        if isinstance(item.get("observation"), dict)
    ]
    warnings = {str(item or "").strip() for item in payload.get("warnings") or []}
    if any(item.get("contradiction_flag") is True for item in observations):
        return "contradiction"
    if "supplementary_request_limit_reached" in warnings:
        return "request_limit_reached"
    if any(item.get("unresolved_flag") is True for item in observations):
        return "unresolved"
    return "resolved"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _expand_reconciled_matrix_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand provider-collapsed matrix rows only with exact arithmetic proof.

    This is document-shape logic, not supplier policy.  A row is expanded only
    when every comma/semicolon/slash-separated activity has exactly one visible
    numeric component token and those components sum to the provider row total.
    An ambiguous row is retained as one explicitly unresolved source row so it
    cannot erase the independently proven rows or masquerade as a resolved
    component classification.
    """
    items = payload.get("line_items")
    if not isinstance(items, list):
        return payload
    expanded_items: list[dict[str, Any]] = []
    changed = False
    unresolved_count = 0
    for item in items:
        if not isinstance(item, dict):
            expanded_items.append(item)
            continue
        row_label = str(item.get("row_label") or "").strip()
        activity = str(item.get("activity") or "").strip()
        activities = [
            part.strip()
            for part in re.split(r"\s*(?:,|;|/|\band\b)\s*", activity, flags=re.IGNORECASE)
            if part.strip()
        ]
        if not row_label or len(activities) <= 1:
            expanded_items.append(item)
            continue
        raw_description = str(item.get("raw_description") or item.get("description") or "")
        component_text = re.sub(
            re.escape(row_label),
            " ",
            raw_description,
            count=1,
            flags=re.IGNORECASE,
        )
        component_tokens = [
            (match.group(1) or "", match.group(2) or match.group(3))
            for match in re.finditer(
                r"(?:\b(\d+(?:\.\d+)?)\s*[@xX]\s*\$?\s*(\d+(?:\.\d{1,2})?)\b|\$?\b(\d+(?:\.\d{1,2})?)\b(?!\s*[@xX]))",
                component_text,
            )
        ]
        row_amount = round(float(item.get("amount") or 0), 2)
        reconciled_combinations: list[tuple[tuple[float, float, float, str], ...]] = []
        if len(component_tokens) == len(activities):
            component_options: list[list[tuple[float, float, float, str]]] = []
            for quantity_text, unit_price_text in component_tokens:
                quantity = float(quantity_text or 1)
                visible_value = float(unit_price_text)
                token = (
                    f"{quantity_text}@{unit_price_text}"
                    if quantity_text
                    else unit_price_text
                )
                if not quantity_text or quantity == 1:
                    component_options.append([(quantity, visible_value, visible_value, token)])
                    continue
                # Handwritten/AI shorthand is not standardized: ``2@100`` may
                # mean two at $100 each or quantity two with a displayed $100
                # component total.  Try both meanings and accept only a unique
                # combination that exactly reconciles the observed row total.
                options = {
                    (quantity, visible_value, round(quantity * visible_value, 2), token),
                    (quantity, round(visible_value / quantity, 6), visible_value, token),
                }
                component_options.append(sorted(options, key=lambda option: option[2]))
            reconciled_combinations = [
                combination
                for combination in itertools.product(*component_options)
                if abs(sum(component[2] for component in combination) - row_amount) <= 0.01
            ]
        if len(reconciled_combinations) != 1:
            changed = True
            unresolved_count += 1
            try:
                confidence = min(float(item.get("confidence") or 0.49), 0.49)
            except (TypeError, ValueError):
                confidence = 0.49
            expanded_items.append({
                **item,
                "activity": "Unresolved Matrix Components",
                "description": f"Unresolved matrix components - {row_label}",
                "raw_description": raw_description,
                "generated_description": "Matrix components require review",
                "gl_account_candidate": "",
                "confidence": confidence,
                "matrix_component_headers": activities,
                "matrix_expansion_status": "unresolved_arithmetic",
                "reason": "Component headers were observed, but their numeric shorthand did not uniquely reconcile to the source row total.",
            })
            continue
        components = reconciled_combinations[0]
        changed = True
        base_reason = str(item.get("reason") or "").strip()
        generated = str(item.get("generated_description") or "").strip()
        for component_activity, (quantity, unit_price, amount, token) in zip(activities, components):
            generated_words = " ".join(
                part for part in (component_activity, generated) if part
            ).split()
            generated_description = " ".join(generated_words[:8])
            expanded_items.append({
                **item,
                "activity": component_activity,
                "description": f"{component_activity} - {row_label}",
                # Preserve the complete observed row verbatim.  The isolated
                # token is provenance for this component, not replacement
                # source text.
                "raw_description": raw_description,
                "source_component_token": token,
                "generated_description": generated_description,
                "quantity": quantity,
                "unit_price": unit_price,
                "amount": amount,
                # The original suggestion described the collapsed row.  Each
                # expanded component must be ranked again by the central engine.
                "gl_account_candidate": "",
                "reason": " ".join(part for part in (
                    base_reason,
                    "Expanded from an exactly reconciled matrix row.",
                ) if part),
            })
    if not changed:
        return payload
    expanded = dict(payload)
    expanded["line_items"] = expanded_items
    warnings = expanded.get("warnings") if isinstance(expanded.get("warnings"), list) else []
    expanded["warnings"] = [
        *warnings,
        "collapsed_matrix_rows_expanded_from_exact_component_arithmetic",
        *(
            [f"matrix_rows_with_unresolved_component_arithmetic:{unresolved_count}"]
            if unresolved_count else []
        ),
    ]
    if unresolved_count:
        expanded["needs_manual_review"] = True
    return expanded


def _repair_prompt(original_prompt: str, error: str) -> str:
    return "\n".join(
        [
            "Your previous response could not be accepted by the invoice parser.",
            f"Parser error: {error}",
            "Return one valid JSON object only. No markdown, no comments, no prose.",
            "Use the exact schema from the original request.",
            "Include every top-level schema key even if the value is empty, null, 0, or an empty list.",
            "line_items must be an array of objects.",
            "",
            "Original request:",
            original_prompt,
        ]
    )


def _safe_document_text(document_text: str) -> tuple[str, bool]:
    limit = max(1000, int(settings.AI_MAX_TEXT_CHARS or 45000))
    if len(document_text or "") <= limit:
        return document_text or "", False
    return (document_text or "")[:limit], True


def extract_invoice_structured(
    *,
    vendor_hint: str,
    document_text: str,
    page_images_or_refs: list[str] | None,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]] | None,
    gl_reference: list[dict[str, Any]] | None,
    vendor_reference: list[dict[str, Any]] | None,
    model_override: str = "",
    force_model_override: bool = False,
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible provider and return strict JSON.

    Vision payloads are intentionally not sent in Phase AI-1. The parameter is
    accepted so later providers can add image support without changing callers.
    """
    controlled_permit: ControlledCallPermit | None = None
    if controlled_external_active():
        context = require_experiment_provider_context(experiment_context)
        controlled_permit = preflight_controlled_provider_route(
            provider_context=context, provider=context.authorized_provider,
            model=context.authorized_model,
            profile_id=context.authorized_profile_id,
            endpoint=context.allowed_endpoint,
            call_purpose=ControlledCallPurpose.OTHER_VISUAL,
            stage="text_extraction",
        )
    status = _require_configured(experiment_context)
    profile = (
        None
        if force_model_override
        else _select_cost_routing_profile(
            "text_extraction", experiment_context=experiment_context,
        )
    )
    configured_model = (status.model or "").strip()
    if model_override and (force_model_override or model_override != configured_model):
        profile = None
    provider = profile.provider if profile is not None else (status.provider or "")
    model = (
        profile.model_id
        if profile is not None
        else (model_override or configured_model)
    )
    profile_key = (
        profile.api_key.get_secret_value() if profile is not None and profile.api_key else None
    )
    profile_base = profile.base_url if profile is not None else None
    if provider == "mock":
        return _mock_extract_invoice_structured(document_text=document_text)
    safe_text, input_truncated = _safe_document_text(document_text)
    local_facts_only = str(provider or "").strip().lower() in LOCAL_PROVIDER_NAMES
    prompt = (
        _build_facts_only_vision_prompt(safe_text)
        if local_facts_only
        else _build_prompt(
            vendor_hint=vendor_hint,
            document_text=safe_text,
            template_schema=template_schema,
            property_reference=property_reference or [],
            gl_reference=gl_reference or [],
            vendor_reference=vendor_reference or [],
            has_page_refs=bool(page_images_or_refs),
        )
    )
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract invoice data into strict JSON only. "
                    "Never include prose, markdown, code fences, or comments."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        **_completion_controls(
            provider,
            int(getattr(settings, "AI_MAX_RESPONSE_TOKENS", 4096) or 4096),
        ),
    }
    effective_profile_id = (
        f"{profile.profile_id}:facts-only-v1"
        if profile and local_facts_only
        else (
            profile.profile_id
            if profile
            else (
                "runtime-text:facts-only-v1"
                if local_facts_only
                else ("runtime-text-forced" if force_model_override else "runtime-text")
            )
        )
    )
    cache_payload = _frozen_cache_payload(effective_profile_id, payload)
    cached = _load_extraction_cache(cache_payload, vision=False)
    if cached is not None:
        return cached
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=False)
    budget_scope = cost_scope_id or f"document:{hashlib.sha256(safe_text.encode()).hexdigest()[:20]}"
    last_parse_error: AIProviderError | None = None
    parsed: dict[str, Any] | None = None
    attempts = 1 if controlled_external_active() else 2
    for attempt in range(attempts):
        _update_profile_cost_context(profile, estimated_cost, vision=False)
        _reserve_cost_budget(budget_scope, estimated_cost)
        content = _send_chat_completion(
            provider=provider,
            payload=payload,
            api_key_override=profile_key,
            base_url_override=profile_base,
            timeout_seconds_override=profile.timeout_seconds if profile is not None else None,
            max_attempts_override=(profile.max_retries + 1) if profile is not None else None,
        )
        try:
            parsed = _parse_invoice_content(content)
            if local_facts_only:
                parsed = _validate_visual_line_structure(parsed)
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            last_parse_error = exc
            if attempt:
                raise
            _LOG.info("Retrying AI invoice extraction after invalid JSON/schema response.")
            payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You repair invoice extraction output into strict JSON only. "
                        "Never include prose, markdown, code fences, or comments."
                    ),
                },
                {"role": "user", "content": _repair_prompt(prompt, str(exc))},
            ]
    if parsed is None:
        raise last_parse_error or AIProviderInvalidJSON("AI response was not valid JSON.")
    if local_facts_only:
        for item in parsed.get("line_items") or []:
            if isinstance(item, dict):
                item["gl_account_candidate"] = ""
                item["expense_type"] = "General"
                item["is_replacement_reserve"] = False
                item["reason"] = "observed_document_fact"
        parsed["_facts_only"] = True
    if input_truncated:
        warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
        parsed["warnings"] = [*warnings, "ai_input_truncated"]
    parsed["_provider_profile_id"] = effective_profile_id
    parsed["_provider_name"] = provider
    parsed["_provider_model_id"] = model
    parsed["_estimated_cost_usd"] = estimated_cost
    _save_extraction_cache(cache_payload, parsed, vision=False)
    return parsed


def extract_invoice_vision_structured(
    *,
    vendor_hint: str,
    document_text: str,
    page_images_or_refs: list[str],
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]] | None,
    gl_reference: list[dict[str, Any]] | None,
    vendor_reference: list[dict[str, Any]] | None,
    model_override: str = "",
    force_model_override: bool = False,
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any]:
    """Run a vision-capable extraction call and return strict JSON.

    The caller is responsible for rendering/capping images. This function only
    accepts image refs after the status layer has confirmed that vision is
    explicitly enabled and supported.
    """
    status = _require_vision_configured(experiment_context)
    if controlled_external_active() and force_model_override:
        raise ControlledExternalGateTerminated("controlled_provider_route_blocked")
    profile = (
        None
        if force_model_override
        else _select_cost_routing_profile(
            "multimodal_extraction", experiment_context=experiment_context,
        )
    )
    configured_vision_model = (status.vision_model or status.model or "").strip()
    # An explicit escalation model belongs to the configured legacy vision
    # deployment. Otherwise cost routing may choose a separately verified
    # multimodal profile.
    if model_override and (force_model_override or model_override != configured_vision_model):
        profile = None
    provider = profile.provider if profile is not None else (
        status.vision_provider or status.provider or ""
    )
    model = (
        profile.model_id if profile is not None
        else (model_override or configured_vision_model)
    ).strip()
    profile_key = (
        profile.api_key.get_secret_value() if profile is not None and profile.api_key else None
    )
    profile_base = profile.base_url if profile is not None else None
    if provider == "mock":
        return _mock_extract_invoice_vision_structured(document_text=document_text)
    if not page_images_or_refs:
        raise AIProviderNotConfigured("No page images were supplied for vision assist.")
    if (
        str(provider or "").strip().lower() in LOCAL_PROVIDER_NAMES
        or (
            controlled_external_active()
            and str(provider or "").strip().lower() in {"gemini", "google_gemini"}
        )
    ):
        # The local Phase A runtime is deliberately an observation-only
        # extractor.  Do not send catalogs, tenant policy, or accounting
        # references to a small local vision model: those inputs both exhaust
        # its bounded context and blur the extraction/accounting authority
        # boundary.  Downstream semantic reasoning and AccountingDecisionEngine
        # remain responsible for candidate generation and final GL selection.
        return extract_invoice_facts_only_vision_structured(
            document_text=document_text,
            page_images_or_refs=page_images_or_refs,
            model_override=model,
            cost_scope_id=cost_scope_id,
            experiment_context=experiment_context,
        )
    safe_text, input_truncated = _safe_document_text(document_text)
    if controlled_external_active():
        # The controlled Phase A contract authorizes source pixels plus the
        # typed facts schema only. OCR/helper text is intentionally retained
        # locally and merged after extraction.
        safe_text = ""
        input_truncated = False
    prompt = _build_vision_prompt(
        vendor_hint=vendor_hint,
        document_text=safe_text,
        template_schema=template_schema,
        property_reference=property_reference or [],
        gl_reference=gl_reference or [],
        vendor_reference=vendor_reference or [],
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    # The renderer may provide one full-page image plus one table-detail crop
    # per configured source page. They are evidence views of the same pages,
    # not additional document pages.
    max_visual_refs = max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2)) * 2
    for ref in page_images_or_refs[:max_visual_refs]:
        content.append({"type": "image_url", "image_url": {"url": ref}})
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You visually inspect invoices and return strict JSON only. "
                    "Never include prose, markdown, code fences, or comments."
                ),
            },
            {"role": "user", "content": content},
        ],
        **_completion_controls(
            provider,
            int(getattr(settings, "AI_VISION_MAX_RESPONSE_TOKENS", 8192) or 8192),
        ),
    }
    cache_payload = _frozen_cache_payload(
        (
            profile.profile_id
            if profile
            else ("runtime-vision-forced" if force_model_override else "runtime-vision")
        ),
        payload,
    )
    cached = _load_extraction_cache(cache_payload, vision=True)
    if cached is not None:
        try:
            return _validate_visual_line_structure(cached)
        except AIProviderInvalidSchema:
            # Validators evolve as new document structures are supported.
            # Never allow a previously cached lossy extraction to bypass the
            # current source-facts contract.
            _LOG.info("Ignoring cached AI vision extraction that fails current structural validation.")
    last_parse_error: AIProviderError | None = None
    parsed: dict[str, Any] | None = None
    original_content = list(content)
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=True)
    budget_scope = cost_scope_id or f"document:{hashlib.sha256(safe_text.encode()).hexdigest()[:20]}"
    for attempt in range(2):
        _update_profile_cost_context(profile, estimated_cost, vision=True)
        _reserve_cost_budget(budget_scope, estimated_cost)
        content_text = _send_chat_completion(
            provider=provider,
            payload=payload,
            vision=True,
            api_key_override=profile_key,
            base_url_override=profile_base,
            timeout_seconds_override=profile.timeout_seconds if profile is not None else None,
            max_attempts_override=(profile.max_retries + 1) if profile is not None else None,
            experiment_context=experiment_context,
            controlled_call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
            request_profile_id=(profile.profile_id if profile else "runtime-vision"),
        )
        try:
            parsed = _validate_visual_line_structure(_parse_invoice_content(content_text))
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            last_parse_error = exc
            if attempt:
                raise
            _LOG.info("Retrying AI vision extraction after invalid JSON/schema response.")
            repaired_content = list(original_content)
            repaired_content[0] = {"type": "text", "text": _repair_prompt(prompt, str(exc))}
            payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You repair visual invoice extraction output into strict JSON only. "
                        "Never include prose, markdown, code fences, or comments."
                    ),
                },
                {"role": "user", "content": repaired_content},
            ]
    if parsed is None:
        raise last_parse_error or AIProviderInvalidJSON("AI vision response was not valid JSON.")
    parsed["vision_candidates"] = _normalize_vision_candidates(parsed.get("vision_candidates"))
    if input_truncated:
        warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
        parsed["warnings"] = [*warnings, "ai_input_truncated"]
    parsed["_provider_profile_id"] = profile.profile_id if profile else "runtime-vision"
    parsed["_provider_name"] = provider
    parsed["_provider_model_id"] = model
    parsed["_estimated_cost_usd"] = estimated_cost
    _save_extraction_cache(cache_payload, parsed, vision=True)
    return parsed


def extract_invoice_facts_only_vision_structured(
    *,
    document_text: str,
    page_images_or_refs: list[str],
    model_override: str = "",
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any] | InitialNormalizationOutcome:
    """Fast primary pass that observes document facts and never reasons about GL."""
    status = _require_vision_configured(experiment_context)
    profile = _select_cost_routing_profile(
        "multimodal_extraction", experiment_context=experiment_context,
    )
    configured_model = (status.vision_model or status.model or "").strip()
    if model_override and model_override != configured_model:
        profile = None
    provider = profile.provider if profile is not None else (status.vision_provider or status.provider)
    model = (profile.model_id if profile is not None else (model_override or configured_model)).strip()
    gemini_transport = str(provider or "").strip().lower() in {"gemini", "google_gemini"}
    profile_id = (profile.profile_id if profile else "runtime-vision") + (
        f":facts-only:{TRANSPORT_SCHEMA_VERSION}:{TRANSPORT_PROMPT_VERSION}"
        if gemini_transport else ":facts-only-v1"
    )
    permit_lifecycle: (
        ControlledCallPermitLifecycle | _UncontrolledCallPermitLifecycle
    )
    if controlled_external_active():
        context = require_experiment_provider_context(experiment_context)
        provider = context.authorized_provider
        model = context.authorized_model
        profile_id = (
            f"{context.authorized_profile_id}:facts-only:"
            f"{TRANSPORT_SCHEMA_VERSION}:{TRANSPORT_PROMPT_VERSION}"
        )
        reserved_permit = preflight_controlled_provider_route(
            provider_context=context, provider=provider, model=model,
            profile_id=profile_id, endpoint=context.allowed_endpoint,
            call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
            stage="rendered_visual_facts",
        )
        if reserved_permit is None:  # pragma: no cover - controlled preflight is strict
            raise ControlledExternalGateTerminated(
                "controlled_local_execution_error"
            )
        permit_lifecycle = ControlledCallPermitLifecycle(
            context.call_budget, reserved_permit
        )
    else:
        permit_lifecycle = _UncontrolledCallPermitLifecycle()

    with permit_lifecycle as active_permit:
        return _extract_invoice_facts_only_with_permit(
            document_text=document_text,
            page_images_or_refs=page_images_or_refs,
            cost_scope_id=cost_scope_id,
            experiment_context=experiment_context,
            provider=provider,
            model=model,
            profile=profile,
            profile_id=profile_id,
            gemini_transport=gemini_transport,
            permit_lifecycle=active_permit,
        )


class _UncontrolledCallPermitLifecycle:
    """Explicit no-op state for production calls without experiment permits."""

    def __enter__(self) -> "_UncontrolledCallPermitLifecycle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False

    def release_for_cache_hit(self) -> None:
        return None

    def permit_for_dispatch(self) -> None:
        return None


def _extract_invoice_facts_only_with_permit(
    *, document_text: str, page_images_or_refs: list[str], cost_scope_id: str,
    experiment_context: ExperimentProviderContext | None, provider: str,
    model: str, profile: Any, profile_id: str, gemini_transport: bool,
    permit_lifecycle: (
        ControlledCallPermitLifecycle | _UncontrolledCallPermitLifecycle
    ),
) -> dict[str, Any] | InitialNormalizationOutcome:
    """Execute one facts-only request under an explicit permit lifecycle."""

    if provider == "mock":
        result = _mock_extract_invoice_vision_structured(document_text=document_text)
        for item in result.get("line_items") or []:
            if isinstance(item, dict):
                item["gl_account_candidate"] = ""
        return result
    if not page_images_or_refs:
        raise AIProviderNotConfigured("No page images were supplied for facts-only extraction.")
    safe_text, input_truncated = _safe_document_text(document_text)
    if controlled_external_active():
        # External Phase A sees only pixels and the facts-only schema. Local
        # OCR text is merged after the provider response and never transmitted.
        safe_text = ""
        input_truncated = False
    prompt = (
        build_gemini_facts_prompt()
        if gemini_transport else _build_facts_only_vision_prompt(safe_text)
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    max_visual_refs = max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2)) * 2
    for ref in page_images_or_refs[:max_visual_refs]:
        content.append({"type": "image_url", "image_url": {"url": ref}})
    payload = {
        "model": model,
        "response_format": (
            gemini_response_format() if gemini_transport else {"type": "json_object"}
        ),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract observable document facts using the response schema. "
                    "Do not classify accounting, choose GL, or authorize readiness."
                ),
            },
            {"role": "user", "content": content},
        ],
        **_completion_controls(
            provider,
            int(getattr(settings, "AI_VISION_MAX_RESPONSE_TOKENS", 8192) or 8192),
        ),
    }
    cache_payload = _frozen_cache_payload(profile_id, payload)
    cached = _load_extraction_cache(cache_payload, vision=True)
    if cached is not None:
        permit_lifecycle.release_for_cache_hit()
        if gemini_transport and controlled_external_active():
            scope = current_document_scope()
            outcome = normalize_gemini_initial_observation(
                cached,
                opaque_document_id=(
                    scope.opaque_document_id if scope is not None
                    else hashlib.sha256(
                        json.dumps(
                            cache_payload, sort_keys=True, separators=(",", ":"),
                        ).encode()
                    ).hexdigest()[:24]
                ),
                provider=provider,
                profile_id=profile_id,
                model_id=model,
            )
            return (
                outcome.facts_payload
                if outcome.category is InitialNormalizationCategory.FACTS_READY
                else outcome
            )
        return _validate_visual_line_structure(cached)
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=True)
    budget_scope = cost_scope_id or f"facts:{hashlib.sha256(safe_text.encode()).hexdigest()[:20]}"
    parsed: dict[str, Any] | None = None
    normalization_outcome: InitialNormalizationOutcome | None = None
    last_error: AIProviderError | None = None
    for attempt in range(2):
        _update_profile_cost_context(profile, estimated_cost, vision=True)
        _reserve_cost_budget(budget_scope, estimated_cost)
        content_text = _send_chat_completion(
            provider=provider,
            payload=payload,
            vision=True,
            api_key_override=(
                profile.api_key.get_secret_value()
                if profile is not None and profile.api_key
                else None
            ),
            base_url_override=profile.base_url if profile is not None else None,
            timeout_seconds_override=profile.timeout_seconds if profile is not None else None,
            max_attempts_override=(
                1 if controlled_external_active()
                else (profile.max_retries + 1) if profile is not None else None
            ),
            experiment_context=experiment_context,
            controlled_call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
            request_profile_id=profile_id,
            controlled_call_permit=permit_lifecycle.permit_for_dispatch(),
        )
        try:
            if gemini_transport:
                try:
                    transport_result = parse_and_normalize_gemini_facts(
                        content_text,
                        provider=str(provider or "gemini"),
                        model=model,
                        request_profile=profile_id,
                        response_metadata=_LAST_PROVIDER_RESPONSE_METADATA.get(),
                    )
                except GeminiTransportError as exc:
                    ai_runtime_trace.record_structured_response_failure(exc.diagnostic)
                    error_type = (
                        AIProviderInvalidSchema
                        if exc.failure_code == "gemini_transport_invalid_schema"
                        else AIProviderInvalidJSON
                    )
                    raise error_type(
                        "Gemini facts-only transport validation failed.",
                        failure_code=(
                            "initial_structured_response_invalid"
                            if controlled_external_active() else exc.failure_code
                        ),
                        structured_diagnostic=exc.diagnostic,
                    ) from exc
                try:
                    scope = current_document_scope()
                    normalization_outcome = normalize_gemini_initial_observation(
                        transport_result,
                        opaque_document_id=(
                            scope.opaque_document_id if scope is not None
                            else hashlib.sha256(
                                f"{budget_scope}:{profile_id}".encode()
                            ).hexdigest()[:24]
                        ),
                        provider=str(provider or "gemini"),
                        profile_id=profile_id,
                        model_id=model,
                    )
                    if (
                        normalization_outcome.category
                        is InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED
                    ):
                        ai_runtime_trace.record_schema_result(
                            "escalated",
                            retry_reason=(
                                normalization_outcome.validation_path
                                or "supplementary_required"
                            ),
                        )
                        break
                    if (
                        normalization_outcome.category
                        is InitialNormalizationCategory.UNSUPPORTED
                    ):
                        diagnostic = build_gemini_safe_diagnostic(
                            content_text,
                            provider=str(provider or "gemini"),
                            model=model,
                            request_profile=profile_id,
                            response_metadata=_LAST_PROVIDER_RESPONSE_METADATA.get(),
                            parsed=extract_single_gemini_json_object(content_text),
                            parser_error_type="StrictInternalContractValidationError",
                            schema_validation_error_path=(
                                normalization_outcome.validation_path
                                or "strict_internal_contract"
                            ),
                        )
                        ai_runtime_trace.record_structured_response_failure(diagnostic)
                        raise AIProviderInvalidSchema(
                            "Gemini facts passed transport but failed the strict internal contract.",
                            failure_code=(
                                normalization_outcome.failure_code
                                if controlled_external_active()
                                else "gemini_internal_contract_invalid"
                            ),
                            structured_diagnostic=diagnostic,
                        )
                    parsed = copy.deepcopy(normalization_outcome.facts_payload or {})
                except AIProviderInvalidSchema as exc:
                    diagnostic = build_gemini_safe_diagnostic(
                        content_text,
                        provider=str(provider or "gemini"),
                        model=model,
                        request_profile=profile_id,
                        response_metadata=_LAST_PROVIDER_RESPONSE_METADATA.get(),
                        parsed=extract_single_gemini_json_object(content_text),
                        parser_error_type="StrictInternalContractValidationError",
                        schema_validation_error_path=_internal_schema_failure_path(exc),
                    )
                    ai_runtime_trace.record_structured_response_failure(diagnostic)
                    raise AIProviderInvalidSchema(
                        "Gemini facts passed transport but failed the strict internal contract.",
                        failure_code=(
                            "initial_structured_response_invalid"
                            if controlled_external_active()
                            else "gemini_internal_contract_invalid"
                        ),
                        structured_diagnostic=diagnostic,
                    ) from exc
            else:
                parsed = _validate_visual_line_structure(_parse_invoice_content(content_text))
            ai_runtime_trace.record_schema_result("valid")
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            ai_runtime_trace.record_schema_result(
                "invalid", retry_reason=type(exc).__name__
            )
            last_error = exc
            if attempt or controlled_external_active():
                raise
            payload["messages"][0]["content"] = (
                "Repair the prior response into the required facts-only JSON schema. "
                "Do not add accounting or GL judgments."
            )
    if normalization_outcome is not None and (
        normalization_outcome.category
        is InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED
    ):
        return normalization_outcome
    if parsed is None:
        raise last_error or AIProviderInvalidJSON("Facts-only response was not valid JSON.")
    for item in parsed.get("line_items") or []:
        if isinstance(item, dict):
            # Enforce the architectural boundary even if a provider ignored it.
            item["gl_account_candidate"] = ""
            item["expense_type"] = "General"
            item["is_replacement_reserve"] = False
            item["reason"] = "observed_document_fact"
    parsed["vision_candidates"] = _normalize_vision_candidates(parsed.get("vision_candidates"))
    if input_truncated:
        parsed["warnings"] = [*list(parsed.get("warnings") or []), "ai_input_truncated"]
    parsed["_provider_profile_id"] = profile_id
    parsed["_provider_name"] = provider
    parsed["_provider_model_id"] = model
    parsed["_estimated_cost_usd"] = estimated_cost
    parsed["_facts_only"] = True
    _save_extraction_cache(cache_payload, parsed, vision=True)
    return parsed


def controlled_gemini_supplementary_profile_identity(
    target: SupplementaryTarget,
    *, experiment_context: ExperimentProviderContext | None = None,
) -> tuple[str, str, str]:
    """Return the explicit Gemini experiment profile without runtime fallback."""

    if not controlled_external_active():
        raise AIProviderUnavailable(
            "Targeted Gemini verification is available only inside CONTROLLED_EXTERNAL.",
            failure_code="supplementary_experiment_scope_required",
        )
    context = require_experiment_provider_context(experiment_context)
    profile = _select_cost_routing_profile(
        "multimodal_extraction", experiment_context=context,
    )
    provider = str(getattr(profile, "provider", "") or "").strip().casefold()
    if profile is None or provider not in {"gemini", "google_gemini"}:
        raise AIProviderUnavailable(
            "The controlled supplementary profile is not an authorized Gemini profile.",
            failure_code="controlled_supplementary_gemini_profile_required",
        )
    model = str(profile.model_id or "").strip()
    if not model or not profile.credentials_present:
        raise AIProviderUnavailable(
            "The controlled supplementary Gemini profile is incomplete.",
            failure_code="controlled_supplementary_profile_incomplete",
        )
    profile_id = (
        f"{profile.profile_id}:supplementary:{target.target_type.value}:"
        f"{SUPPLEMENTARY_SCHEMA_VERSION}:{SUPPLEMENTARY_PROMPT_VERSION}"
    )
    return "gemini", profile_id, model


def extract_gemini_supplementary_facts_structured(
    *, initial_facts: dict[str, Any], target: SupplementaryTarget,
    evidence_plan: SupplementaryEvidencePlan,
    evidence_packet: SupplementaryEvidencePacket,
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> GeminiSupplementaryObservation:
    """Ask Gemini one bounded, target-specific visual question.

    This function has intentionally no provider/model fallback and no repair
    retry.  Spend and host authorization are enforced by the shared controlled
    transport before bytes leave the process.
    """

    context = require_experiment_provider_context(experiment_context)
    provider, profile_id, model = controlled_gemini_supplementary_profile_identity(
        target, experiment_context=context,
    )
    profile = _select_cost_routing_profile(
        "multimodal_extraction", experiment_context=context,
    )
    if profile is None or not profile_id.startswith(f"{profile.profile_id}:"):
        raise AIProviderUnavailable(
            "The controlled supplementary Gemini profile changed during request construction.",
            failure_code="controlled_supplementary_profile_changed",
        )
    if evidence_plan.target_id != target.target_id:
        raise AIProviderInvalidSchema(
            "The supplementary evidence plan does not match its target.",
            failure_code="supplementary_evidence_target_mismatch",
        )
    try:
        validate_evidence_packet(evidence_plan, evidence_packet)
    except EvidenceLocalizationError as exc:
        raise AIProviderInvalidSchema(
            "The supplementary visual evidence packet failed local validation.",
            failure_code=exc.failure_code,
        ) from exc
    document_scope = current_document_scope()
    if document_scope is None:
        raise AIProviderUnavailable(
            "Controlled document scope is required for supplementary verification.",
            failure_code="controlled_external_document_scope_missing",
        )
    if evidence_plan.opaque_document_id != document_scope.opaque_document_id:
        raise AIProviderInvalidSchema(
            "The supplementary evidence plan belongs to another document scope.",
            failure_code="supplementary_evidence_document_scope_mismatch",
        )
    stage = f"controlled_gemini_supplementary:{target.target_type.value}"
    controlled_permit = preflight_controlled_provider_route(
        provider_context=context, provider=provider, model=model,
        profile_id=profile_id, endpoint=context.allowed_endpoint,
        call_purpose=ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION,
        stage=stage,
    )
    minimized = build_minimized_initial_summary(initial_facts, target)
    prompt = build_supplementary_prompt(
        opaque_document_id=document_scope.opaque_document_id,
        target=target,
        minimized_summary=minimized,
        evidence_plan_summary=evidence_plan.provider_summary(),
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in evidence_packet.images:
        content.append({
            "type": "text",
            "text": (
                f"Evidence image crop_id={image.crop_id}; role={image.role.value}; "
                f"category={image.category.value}; page={image.page_number}."
            ),
        })
        content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    payload = {
        "model": model,
        "response_format": supplementary_response_format(target),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Verify one observable visual fact using the typed schema. "
                    "Never provide accounting, GL, readiness, export, or policy decisions."
                ),
            },
            {"role": "user", "content": content},
        ],
        **_completion_controls(provider, 2048),
    }
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=True)
    budget_scope = cost_scope_id or f"supplementary:{document_scope.opaque_document_id}"
    ai_runtime_trace.update_context(
        stage=stage, provider=provider, model=model, profile_id=profile_id,
    )
    _update_profile_cost_context(profile, estimated_cost, vision=True)
    _reserve_cost_budget(budget_scope, estimated_cost)
    content_text = _send_chat_completion(
        provider=provider,
        payload=payload,
        vision=True,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url,
        timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=1,
        endpoint_surface_override=stage,
        capability_override="visual_document_understanding",
        experiment_context=context,
        controlled_call_purpose=ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION,
        request_profile_id=profile_id,
        controlled_call_permit=controlled_permit,
    )
    try:
        observation = parse_supplementary_response(content_text, target=target)
        validate_observation_crop_references(
            observation,
            allowed_crop_ids={image.crop_id for image in evidence_packet.images},
        )
    except SupplementaryVerificationError as exc:
        ai_runtime_trace.record_schema_result("invalid", retry_reason=exc.failure_code)
        error_type = (
            AIProviderInvalidJSON
            if exc.failure_code == "supplementary_invalid_json"
            else AIProviderInvalidSchema
        )
        raise error_type(
            "Gemini supplementary verification failed its typed local contract.",
            failure_code=exc.failure_code,
        ) from exc
    ai_runtime_trace.record_schema_result("valid")
    return observation


def extract_invoice_critical_fields_vision_structured(
    *,
    page_images_or_refs: list[str],
    property_reference: list[dict[str, Any]] | None,
    model_override: str = "",
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any]:
    """Verify only ambiguous header facts from a bounded detail crop.

    This is deliberately separate from full invoice extraction: it cannot
    create line items, GL candidates, accounting decisions, or readiness.
    Its small response contract also avoids paying for a second reconstruction
    of a financial table that has already reconciled.
    """

    status = _require_vision_configured(experiment_context)
    profile = _select_cost_routing_profile(
        "multimodal_extraction", experiment_context=experiment_context,
    )
    configured_model = (status.vision_model or status.model or "").strip()
    if model_override and model_override != configured_model:
        profile = None
    provider = profile.provider if profile is not None else (
        status.vision_provider or status.provider or ""
    )
    model = (
        profile.model_id if profile is not None else (model_override or configured_model)
    ).strip()
    profile_key = (
        profile.api_key.get_secret_value() if profile is not None and profile.api_key else None
    )
    profile_base = profile.base_url if profile is not None else None
    if controlled_external_active():
        context = require_experiment_provider_context(experiment_context)
        preflight_controlled_provider_route(
            provider_context=context, provider=provider, model=model,
            profile_id=(
                profile.profile_id + ":critical-fields"
                if profile else "runtime-vision-critical-fields"
            ),
            endpoint=context.allowed_endpoint,
            call_purpose=ControlledCallPurpose.OTHER_VISUAL,
            stage="critical_header_verification",
        )
    if not page_images_or_refs:
        raise AIProviderNotConfigured("No header/detail image was supplied for critical-field verification.")

    property_names = [
        {
            "property_name": item.get("property_name") or item.get("name"),
            "property_abbreviation": item.get("property_abbreviation") or item.get("abbreviation"),
        }
        for item in (property_reference or [])[:160]
        if isinstance(item, dict)
    ]
    if controlled_external_active():
        # Phase A authorizes pixels and a facts-only schema. ResMan reference
        # data remains local and is applied only by the resolver after return.
        property_names = []
    schema = {
        "invoice_date": "",
        "service_date": "",
        "due_date": "",
        "due_date_text": "",
        "payment_terms": "",
        "sold_to_raw_text": "",
        "property_candidate": "",
        "job_site_raw_text": "",
        "location_candidate": "",
        "confidence": 0.0,
        "warnings": [],
    }
    prompt = "\n".join([
        "Inspect only the attached enlarged invoice header/detail crop.",
        "Return strict JSON only, using exactly the keys in the schema below.",
        "Transcribe visible source facts; do not infer accounting values and do not create line items.",
        "A Date Of Service belongs in service_date, not invoice_date.",
        "Keep non-calendar due text such as Upon Receipt in due_date_text and payment_terms; leave due_date empty.",
        "sold_to_raw_text must preserve the complete visible SOLD TO wording exactly.",
        "property_candidate may normalize spacing only when supported by sold-to text and the supplied reference.",
        "job_site_raw_text must preserve the visible JOB SITE wording even when it is not an address.",
        "Inspect every handwritten digit and letter at high visual detail. If genuinely ambiguous, leave the field empty and explain it in warnings.",
        "Do not use filename text as a document field.",
        "Required JSON schema:",
        json.dumps(schema, indent=2),
        "Known property names for exact transcription validation:",
        json.dumps(property_names, indent=2)[:8000],
    ])
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for ref in page_images_or_refs[:2]:
        content.append({"type": "image_url", "image_url": {"url": ref}})
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You verify visible invoice header facts from an enlarged crop and return JSON only."
                ),
            },
            {"role": "user", "content": content},
        ],
        **_completion_controls(provider, 1400),
    }
    cache_payload = _frozen_cache_payload(
        profile.profile_id + ":critical-fields" if profile else "runtime-vision-critical-fields",
        payload,
    )
    cached = _load_extraction_cache(cache_payload, vision=True)
    if cached is not None:
        return cached
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=True)
    budget_scope = cost_scope_id or "critical-fields"
    _update_profile_cost_context(profile, estimated_cost, vision=True)
    _reserve_cost_budget(budget_scope, estimated_cost)
    response_text = _send_chat_completion(
        provider=provider,
        payload=payload,
        vision=True,
        api_key_override=profile_key,
        base_url_override=profile_base,
        timeout_seconds_override=profile.timeout_seconds if profile is not None else None,
        max_attempts_override=(profile.max_retries + 1) if profile is not None else None,
    )
    parsed = _extract_json_object(response_text)
    result = {
        key: parsed.get(key, default)
        for key, default in schema.items()
    }
    for key in (
        "invoice_date", "service_date", "due_date", "due_date_text", "payment_terms",
        "sold_to_raw_text", "property_candidate", "job_site_raw_text", "location_candidate",
    ):
        result[key] = str(result.get(key) or "").strip()
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    warnings = result.get("warnings")
    result["warnings"] = [str(value).strip() for value in warnings if str(value).strip()] if isinstance(warnings, list) else []
    result["_provider_profile_id"] = (
        profile.profile_id + ":critical-fields" if profile else "runtime-vision-critical-fields"
    )
    result["_provider_name"] = provider
    result["_provider_model_id"] = model
    result["_estimated_cost_usd"] = estimated_cost
    _save_extraction_cache(cache_payload, result, vision=True)
    return result


def extract_handwritten_row_identities_vision_structured(
    *,
    apt_column_image_ref: str,
    crop_coordinates: dict[str, int],
    expected_visible_rows: int | None = None,
    model_override: str = "",
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any]:
    """Transcribe only visible row identifiers from an enlarged Apt. # crop.

    No catalog is supplied to the model: catalog membership must never bias
    handwriting recognition. The backend validates the returned observations
    independently after the call.
    """

    status = _require_vision_configured(experiment_context)
    profile = _select_cost_routing_profile(
        "multimodal_extraction", experiment_context=experiment_context,
    )
    configured_model = (status.vision_model or status.model or "").strip()
    if model_override and model_override != configured_model:
        profile = None
    provider = profile.provider if profile is not None else (
        status.vision_provider or status.provider or ""
    )
    model = (
        profile.model_id if profile is not None else (model_override or configured_model)
    ).strip()
    profile_key = (
        profile.api_key.get_secret_value() if profile is not None and profile.api_key else None
    )
    profile_base = profile.base_url if profile is not None else None
    if controlled_external_active():
        context = require_experiment_provider_context(experiment_context)
        preflight_controlled_provider_route(
            provider_context=context, provider=provider, model=model,
            profile_id=(
                profile.profile_id + ":row-identities"
                if profile else "runtime-vision-row-identities"
            ),
            endpoint=context.allowed_endpoint,
            call_purpose=ControlledCallPurpose.OTHER_VISUAL,
            stage="row_identity_verification",
        )
    if not apt_column_image_ref:
        raise AIProviderNotConfigured("No Apt. # crop was supplied for row-identity verification.")

    schema = {
        "visible_rows": [
            {
                "row_index": 0,
                "raw_value": "",
                "alternatives": [{"value": "", "confidence": 0.0}],
                "confidence": 0.0,
                "bbox": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "selection_marker": "unclear",
                "status": "needs_confirmation",
            }
        ],
        "warnings": [],
    }
    prompt = "\n".join([
        "Inspect only the attached enlarged Apt. # column crop.",
        "Return every visible table row from top to bottom, including crossed-out or illegible rows.",
        "Transcribe handwriting from pixels only. No property/unit catalog is provided and you must not infer a valid unit pattern.",
        "raw_value is the best literal reading, or empty when illegible.",
        "alternatives must contain every materially plausible different reading with its own confidence; do not hide ambiguity.",
        "bbox uses normalized 0..1 coordinates relative to this crop.",
        (
            "selection_marker is circled, crossed_out, unmarked, or unclear. "
            "This crop excludes the far-right PAID column, so do not infer PAID status; "
            "use crossed_out only when source ink visibly strikes through the Apt. row."
        ),
        "status is confirmed only when the handwriting itself is clear; otherwise needs_confirmation or illegible.",
        f"Expected visible row count is approximately {expected_visible_rows or 'unknown'}; do not invent rows to satisfy it.",
        "Return strict JSON only using this schema:",
        json.dumps(schema, indent=2),
    ])
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You transcribe handwritten invoice row identifiers from pixels and return JSON only.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": apt_column_image_ref}},
                ],
            },
        ],
        **_completion_controls(provider, 1800),
    }
    cache_payload = _frozen_cache_payload(
        profile.profile_id + ":row-identities" if profile else "runtime-vision-row-identities",
        payload,
    )
    cached = _load_extraction_cache(cache_payload, vision=True)
    if cached is not None:
        return cached
    estimated_cost = _estimated_profile_request_cost(profile, payload, vision=True)
    _update_profile_cost_context(profile, estimated_cost, vision=True)
    _reserve_cost_budget(cost_scope_id or "row-identities", estimated_cost)
    response_text = _send_chat_completion(
        provider=provider,
        payload=payload,
        vision=True,
        api_key_override=profile_key,
        base_url_override=profile_base,
        timeout_seconds_override=profile.timeout_seconds if profile is not None else None,
        max_attempts_override=(profile.max_retries + 1) if profile is not None else None,
    )
    parsed = _extract_json_object(response_text)
    raw_rows = parsed.get("visible_rows")
    rows: list[dict[str, Any]] = []
    if isinstance(raw_rows, list):
        for index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, dict):
                continue
            bbox = raw_row.get("bbox") if isinstance(raw_row.get("bbox"), dict) else {}
            alternatives = []
            for alternative in raw_row.get("alternatives") or []:
                if not isinstance(alternative, dict):
                    continue
                value = str(alternative.get("value") or "").strip()
                if not value:
                    continue
                try:
                    alt_confidence = max(0.0, min(1.0, float(alternative.get("confidence") or 0)))
                except (TypeError, ValueError):
                    alt_confidence = 0.0
                alternatives.append({"value": value, "confidence": alt_confidence})
            try:
                confidence = max(0.0, min(1.0, float(raw_row.get("confidence") or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            status_value = str(raw_row.get("status") or "needs_confirmation").strip().lower()
            if status_value not in {"confirmed", "needs_confirmation", "illegible"}:
                status_value = "needs_confirmation"
            rows.append({
                "row_index": int(raw_row.get("row_index") or index),
                "raw_value": str(raw_row.get("raw_value") or "").strip(),
                "alternatives": alternatives,
                "confidence": confidence,
                "bbox": {
                    key: max(0.0, min(1.0, float(bbox.get(key) or 0)))
                    for key in ("x", "y", "w", "h")
                },
                "selection_marker": (
                    str(raw_row.get("selection_marker") or "unclear").strip().lower()
                    if str(raw_row.get("selection_marker") or "unclear").strip().lower()
                    in {"circled", "crossed_out", "unmarked", "unclear"}
                    else "unclear"
                ),
                "status": status_value,
            })
    warnings = parsed.get("warnings")
    result = {
        "visible_rows": rows,
        "warnings": [str(value).strip() for value in warnings if str(value).strip()]
        if isinstance(warnings, list) else [],
        "crop_coordinates": dict(crop_coordinates),
        "_provider_profile_id": profile.profile_id + ":row-identities" if profile else "runtime-vision-row-identities",
        "_provider_name": provider,
        "_provider_model_id": model,
        "_estimated_cost_usd": estimated_cost,
    }
    _save_extraction_cache(cache_payload, result, vision=True)
    return result


def _native_pdf_usage_cost(model: str, usage: dict[str, int]) -> float:
    escalation_model = os.environ.get("AI_VISION_ESCALATION_MODEL", "").strip()
    prefix = (
        "AI_VISION_ESCALATION"
        if escalation_model and model == escalation_model
        else "AI_VISION"
    )
    try:
        input_rate = float(os.environ.get(f"{prefix}_INPUT_COST_USD_PER_MILLION", "0") or 0)
        output_rate = float(os.environ.get(f"{prefix}_OUTPUT_COST_USD_PER_MILLION", "0") or 0)
    except ValueError:
        return 0.0
    return round(
        usage.get("input_tokens", 0) * input_rate / 1_000_000
        + usage.get("output_tokens", 0) * output_rate / 1_000_000,
        6,
    )


def extract_invoice_native_pdf_structured(
    *,
    vendor_hint: str,
    document_text: str,
    pdf_evidence: NativePdfEvidence,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]] | None,
    gl_reference: list[dict[str, Any]] | None,
    vendor_reference: list[dict[str, Any]] | None,
    model_override: str = "",
    cost_scope_id: str = "",
    experiment_context: ExperimentProviderContext | None = None,
) -> dict[str, Any]:
    """Extract source facts from the original PDF through OpenAI Responses.

    This route is intentionally limited to difficult scanned PDFs.  It sends
    the original document with high-detail page processing, strict structured
    output, and an independently versioned cache identity.
    """

    if controlled_external_active():
        context = require_experiment_provider_context(experiment_context)
        preflight_controlled_provider_route(
            provider_context=context, provider="openai",
            model=(model_override or context.authorized_model),
            profile_id="runtime-vision-native-pdf",
            endpoint="https://api.openai.com/v1/responses",
            call_purpose=ControlledCallPurpose.OTHER_VISUAL,
            stage="native_pdf_visual_facts",
        )
    status = _require_vision_configured(experiment_context)
    provider = (status.vision_provider or status.provider or "").strip().lower()
    if provider != "openai":
        raise AIProviderNotConfigured(
            "Native PDF extraction requires a configured OpenAI vision profile.",
            failure_code="native_pdf_provider_unsupported",
        )
    model = (model_override or status.vision_model or status.model or "").strip()
    if not model:
        raise AIProviderNotConfigured(
            "Native PDF extraction has no configured model.",
            failure_code="native_pdf_model_missing",
        )
    detail = str(getattr(settings, "AI_VISION_NATIVE_PDF_DETAIL", "high") or "high").lower()
    if detail not in {"auto", "low", "high"}:
        detail = "high"
    effort = str(
        getattr(settings, "AI_VISION_NATIVE_PDF_REASONING_EFFORT", "medium") or "medium"
    ).lower()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        effort = "medium"
    max_output_tokens = min(
        128_000,
        max(
            4096,
            int(getattr(settings, "AI_VISION_NATIVE_PDF_MAX_RESPONSE_TOKENS", 32768) or 32768),
        ),
    )
    safe_text, input_truncated = _safe_document_text(document_text)
    prompt = _build_vision_prompt(
        vendor_hint=vendor_hint,
        document_text=safe_text,
        template_schema=template_schema,
        property_reference=property_reference or [],
        gl_reference=gl_reference or [],
        vendor_reference=vendor_reference or [],
    )
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You extract observed invoice facts from native documents. Return the strict "
                    "schema only. Do not make accounting decisions or expose chain-of-thought."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": pdf_evidence.filename,
                        "file_data": pdf_evidence.data_url,
                        "detail": detail,
                    },
                    {"type": "input_text", "text": prompt},
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "innerview_invoice_facts",
                "strict": True,
                "schema": _native_vision_json_schema(),
            }
        },
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
    }
    cache_payload = _frozen_cache_payload("runtime-vision-native-pdf", payload)
    cached = _load_extraction_cache(cache_payload, vision=True)
    if cached is not None:
        try:
            return _validate_visual_line_structure(cached)
        except AIProviderInvalidSchema:
            _LOG.info("Ignoring cached native PDF extraction that fails current validation.")

    try:
        reserved_cost = max(
            0.0,
            float(os.environ.get("AI_ESTIMATED_NATIVE_PDF_COST_USD", "0.05") or 0.05),
        )
    except ValueError:
        reserved_cost = 0.05
    escalation_model = os.environ.get("AI_VISION_ESCALATION_MODEL", "").strip()
    cost_prefix = "AI_VISION_ESCALATION" if escalation_model and model == escalation_model else "AI_VISION"
    try:
        input_rate = float(os.environ.get(f"{cost_prefix}_INPUT_COST_USD_PER_MILLION", "0") or 0)
        output_rate = float(os.environ.get(f"{cost_prefix}_OUTPUT_COST_USD_PER_MILLION", "0") or 0)
    except ValueError:
        input_rate = output_rate = 0.0
    ai_runtime_trace.update_context(
        estimated_cost_usd=reserved_cost,
        input_cost_usd_per_million=max(0.0, input_rate),
        output_cost_usd_per_million=max(0.0, output_rate),
        fixed_request_cost_usd=0.0,
    )
    _reserve_cost_budget(
        cost_scope_id or f"document:{pdf_evidence.content_sha256[:20]}",
        reserved_cost,
    )
    parsed: dict[str, Any] | None = None
    last_error: AIProviderError | None = None
    usage: dict[str, int] = {}
    for attempt in range(2):
        content_text, usage = _send_openai_response(
            payload=payload,
            api_key=(getattr(settings, "AI_VISION_API_KEY", "") or settings.AI_API_KEY).strip(),
            base_url=(getattr(settings, "AI_VISION_BASE_URL", "") or settings.AI_BASE_URL).strip(),
            timeout_seconds=int(
                getattr(settings, "AI_VISION_NATIVE_PDF_TIMEOUT_SECONDS", 240) or 240
            ),
            max_attempts=1,
        )
        try:
            parsed = _validate_visual_line_structure(_parse_invoice_content(content_text))
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            last_error = exc
            if attempt:
                raise
            _LOG.info("Retrying native PDF extraction after structural validation failure.")
            payload["input"][1]["content"][1]["text"] = _repair_prompt(prompt, str(exc))
    if parsed is None:
        raise last_error or AIProviderInvalidJSON(
            "Native document vision response was not valid JSON."
        )
    parsed["vision_candidates"] = _normalize_vision_candidates(parsed.get("vision_candidates"))
    if input_truncated:
        parsed["warnings"] = [
            *list(parsed.get("warnings") or []),
            "ai_input_truncated",
        ]
    parsed["_provider_profile_id"] = "runtime-vision-native-pdf"
    parsed["_provider_name"] = provider
    parsed["_provider_model_id"] = model
    parsed["_provider_request_surface"] = "responses_native_pdf"
    parsed["_provider_usage"] = usage
    parsed["_estimated_cost_usd"] = _native_pdf_usage_cost(model, usage) or reserved_cost
    _save_extraction_cache(cache_payload, parsed, vision=True)
    return parsed


def _mock_extract_invoice_structured(*, document_text: str) -> dict[str, Any]:
    """Deterministic fixture provider used by Phase AI-1.1 tests.

    No network calls, no API keys, and mode can be forced with
    ``AI_MOCK_MODE`` or fixture text markers.
    """
    delay = max(0, int(getattr(settings, "AI_MOCK_DELAY_SECONDS", 0) or 0))
    if delay:
        time.sleep(delay)
    mode = (getattr(settings, "AI_MOCK_MODE", "") or "").strip().lower()
    text_upper = (document_text or "").upper()
    if "MOCK_MALFORMED_JSON" in text_upper:
        mode = "malformed_json"
    elif "MOCK_TOTAL_MISMATCH" in text_upper:
        mode = "total_mismatch"
    elif "MOCK_LOW_CONFIDENCE" in text_upper:
        mode = "low_confidence"

    if mode == "malformed_json":
        return _extract_json_object("this is not valid json")

    low_confidence = mode == "low_confidence"
    total_amount = 206.65
    if mode == "total_mismatch":
        total_amount = 211.65

    return _validate_invoice_schema({
        "vendor_name": "HD Supply Facilities Maintenance, Ltd",
        "invoice_number": "HDS-104857",
        "invoice_date": "05/06/2026",
        "due_date": "06/05/2026",
        "bill_or_credit": "Bill",
        "account_number": "40293817",
        "service_address": "1726 Stone Street, Union City, TN 38261",
        "address_role": "service_address",
        "location_candidate": "",
        "property_candidate": "1732-Hillwood Manor",
        "property_abbreviation": "1732-HMA",
        "invoice_description": "Maintenance supplies for 1732-Hillwood Manor",
        "line_items": [
            {
                "description": "Angle stop valve, chrome, 1/2 inch",
                "quantity": 6,
                "unit_price": 8.12,
                "amount": 48.72,
                "gl_account_candidate": "6615 Building Maintenance & Repairs - Minor",
                "expense_type": "Repairs and maintenance",
                "is_replacement_reserve": False,
                "confidence": 0.93 if not low_confidence else 0.58,
                "reason": "Matched plumbing repair supply line on invoice detail.",
            },
            {
                "description": "LED exterior fixture, bronze",
                "quantity": 1,
                "unit_price": 139.99,
                "amount": 139.99,
                "gl_account_candidate": "6627 Electrical Parts & Supplies",
                "expense_type": "Electrical supplies",
                "is_replacement_reserve": False,
                "confidence": 0.88 if not low_confidence else 0.52,
                "reason": "Item description references electrical fixture replacement.",
            },
        ],
        "subtotal": 188.71,
        "tax_amount": 17.94,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": total_amount,
        "confidence": 0.89 if not low_confidence else 0.55,
        "warnings": (
            ["Mock low confidence: supplier invoice line descriptions are abbreviated."]
            if low_confidence
            else []
        ),
        "needs_manual_review": low_confidence,
    })


def _mock_extract_invoice_vision_structured(*, document_text: str) -> dict[str, Any]:
    payload = _mock_extract_invoice_structured(document_text=document_text)
    payload["confidence"] = max(float(payload.get("confidence") or 0), 0.92)
    payload["warnings"] = list(payload.get("warnings") or [])
    payload["vision_candidates"] = [
        {
            "field_key": "vendor_name",
            "field_label": "Vendor",
            "value": payload.get("vendor_name"),
            "page": 1,
            "bbox": {"x": 0.09, "y": 0.08, "w": 0.28, "h": 0.07},
            "confidence": 0.93,
            "validation_status": "candidate",
        },
        {
            "field_key": "invoice_number",
            "field_label": "Invoice number",
            "value": payload.get("invoice_number"),
            "page": 1,
            "bbox": {"x": 0.63, "y": 0.11, "w": 0.22, "h": 0.05},
            "confidence": 0.91,
            "validation_status": "candidate",
        },
        {
            "field_key": "total_amount",
            "field_label": "Invoice total",
            "value": payload.get("total_amount"),
            "page": 1,
            "bbox": {"x": 0.70, "y": 0.78, "w": 0.18, "h": 0.05},
            "confidence": 0.94,
            "validation_status": "candidate",
        },
        {
            "field_key": "line_items_table",
            "field_label": "Line items",
            "value": "Detected line item table",
            "page": 1,
            "bbox": {"x": 0.08, "y": 0.34, "w": 0.82, "h": 0.24},
            "confidence": 0.89,
            "validation_status": "candidate",
        },
    ]
    return _validate_invoice_schema(payload)


def _build_prompt(
    *,
    vendor_hint: str,
    document_text: str,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]],
    gl_reference: list[dict[str, Any]],
    vendor_reference: list[dict[str, Any]],
    has_page_refs: bool,
) -> str:
    schema = {
        "vendor_name": "",
        "invoice_nature": "unknown",
        "category": "unknown",
        "invoice_number": "",
        "invoice_date": "",
        "service_date": "",
        "purchase_date": "",
        "ship_date": "",
        "received_date": "",
        "due_date": "",
        "due_date_text": "",
        "payment_terms": "",
        "bill_or_credit": "Bill",
        "account_number": "",
        "service_address": "",
        "address_role": "unknown",
        "location_candidate": "",
        "service_period_start": "",
        "service_period_end": "",
        "service_period": "",
        "property_candidate": "",
        "property_abbreviation": "",
        "invoice_description": "",
        "line_items": [
            {
                "source_page": 1,
                "section_header": "",
                "row_label": "",
                "location_candidate": "",
                "activity": "",
                "description": "",
                "raw_description": "",
                "normalized_description": "",
                "generated_description": "",
                "quantity": None,
                "unit_price": None,
                "amount": 0.00,
                "gl_account_candidate": "",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": 0.0,
                "reason": "",
            }
        ],
        "subtotal": 0.00,
        "tax_amount": 0.00,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": 0.00,
        "confidence": 0.0,
        "warnings": [],
        "needs_manual_review": True,
        "visual_extraction_status": "complete",
        "unresolved_visual_regions": [],
        "page_reconciliations": [
            {
                "page": 1,
                "printed_page_total": None,
                "extracted_component_total": 0.00,
                "difference": 0.00,
                "status": "reconciled",
            }
        ],
        "excluded_paid_rows": [
            {
                "raw_apartment_number": "",
                "component_amounts": [{"label": "", "amount": None}],
                "row_total": None,
                "paid_marker_evidence": [
                    {"page": 1, "text": "PAID", "bbox": None, "confidence": 0.0}
                ],
                "exclusion_reason": "visible_paid_marker",
            }
        ],
    }
    return "\n".join(
        [
            "Extract this invoice into the exact JSON schema below.",
            "Use null for unknown numeric values and empty strings for unknown text.",
            "Line item amounts must be signed numbers. Credits should be negative when applicable.",
            "If the source lists products/services but does not show per-line dollar amounts, return one payable line item for the explicit invoice total and include a warning that line amounts were not visible.",
            "If the invoice total is explicit but the line table is incomplete, never return an empty payable invoice; use the invoice total fallback line for operator review.",
            "Every top-level and line-item confidence must be a number from 0.0 to 1.0.",
            "Do not omit confidence. Use 0.90+ only when the source text is explicit and totals reconcile.",
            "Use 0.70-0.89 when fields are mostly clear but mapping is uncertain.",
            "Use below 0.70 when key fields are inferred, missing, or ambiguous.",
            "Every line item must include a short reason explaining the extraction and GL suggestion.",
            "Set needs_manual_review=true only when a specific missing/ambiguous/invalid field requires operator review.",
            "When needs_manual_review=true, include a clear human-readable warning explaining why.",
            "Do not invent missing property, GL, service address, or date values.",
            "For recurring bills/utilities, extract the visible service/billing period as service_period_start and service_period_end. Examples include '03/26/26 to 04/27/26' or 'service from ... to ...'.",
            "Search for invoice number, bill number, statement number, account number, and billing ID. If no invoice number exists on a bill, leave invoice_number empty; the backend will generate a required stable bill number from account/date/source context.",
            "If the invoice has no explicit invoice date, leave invoice_date empty and use purchase_date, ship_date, or received_date only when that source is explicit.",
            "If payment terms are visible but no calendar due date is visible, copy that text into payment_terms and leave due_date empty.",
            "Also preserve that non-calendar source wording verbatim in due_date_text.",
            "Rows visibly marked PAID must never enter line_items or payable totals. Preserve every such row in excluded_paid_rows with raw apartment text, every visible component amount, row total, PAID marker evidence, and exclusion reason.",
            "Do not put vendor-side labels such as GL CODE:MISCELLANEOUS into gl_account_candidate unless it is a real numeric ResMan/Chart of Accounts code from the reference.",
            "Use the exact source line-item descriptions where possible; avoid vague summaries like 'hardware and miscellaneous items'.",
            "For a one-time purchase, repair, installation, or other non-recurring invoice, invoice_description must be one concise content summary (75 characters maximum) of the goods or work across all payable lines. Do not put the vendor, property, address, invoice date, or service month in that summary.",
            "Use period-based descriptions only for genuinely recurring bills or recurring services such as utilities, pest control, landscaping, marketing subscriptions, or trash collection. Words like repair, labor, maintenance, appliance, or service call alone do not make an invoice recurring.",
            "Set invoice_nature to one_time, recurring, utility_bill, or unknown. Set category to utilities only for a utility provider bill supported by metering, consumption, billing-period, or account-charge evidence; a contractor repairing a water, drain, electric, gas, or sewer system is one_time, not utilities.",
            "Include zero-dollar source lines only when they carry accounting meaning; otherwise include a warning that zero-dollar lines were omitted.",
            "Do not decide the ResMan property or location unless it is explicit in the source text or a reference match is highly clear.",
            "Distinguish the vendor/remit address from the customer, job, ship-to, or service address; service_address must describe where the work occurred.",
            "Set address_role to one of service_address, job_site, ship_to, sold_to, bill_to, remit_to, vendor_address, or unknown. A SOLD TO or BILL TO address is not a service address unless the document explicitly says the work occurred there.",
            "Use customer/property names and property-specific email domains as property_candidate evidence. For example, a customer name or email containing Aspen Meadow is stronger property evidence than an unrelated administrative billing address.",
            "Never return field labels such as ACCOUNT, NUMBER, SALES, LOCATION, or DATE as invoice_number. Read the actual value printed beneath or beside INVOICE NUMBER.",
            "The invoice total is the final payable TOTAL/AMOUNT DUE, not subtotal or a line extension. Reconcile line items plus tax, shipping, and fees to total_amount.",
            "Extract a visible customer order unit, apartment, suite, or job-unit value into location_candidate. Keep service_address as the actual street address.",
            "When the source has distinct allocation rows, coverage rows, units, locations, departments, projects, or cost centers with explicit amounts, preserve one line_item per source allocation. Never group allocations merely because descriptions or amounts repeat.",
            "Return JSON only.",
            "",
            canonical_rules.prompt_rules_summary(),
            "",
            f"Vendor hint: {vendor_hint or 'unknown'}",
            f"Vision references supplied: {'yes' if has_page_refs else 'no'}",
            "",
            "Required JSON schema:",
            json.dumps(schema, indent=2),
            "",
            "ResMan template columns:",
            json.dumps(template_schema, indent=2)[:5000],
            "",
            "Known vendors (sample/reference):",
            json.dumps(vendor_reference[:80], indent=2)[:6000],
            "",
            "Property reference (sample/reference):",
            json.dumps(property_reference[:120], indent=2)[:6000],
            "",
            "General ledger reference (sample/reference):",
            json.dumps(gl_reference[:120], indent=2)[:6000],
            "",
            "Document text:",
            document_text,
        ]
    )


def _build_facts_only_vision_prompt(document_text: str) -> str:
    schema = {
        "vendor_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "service_date": None,
        "due_date": None,
        "due_date_text": None,
        "payment_terms": None,
        "bill_or_credit": "Bill",
        "account_number": None,
        "service_address": None,
        "address_role": "unknown",
        "location_candidate": None,
        "service_period_start": None,
        "service_period_end": None,
        "service_period": None,
        "property_candidate": None,
        "property_abbreviation": None,
        "invoice_description": None,
        "line_items": [{
            "source_page": 1,
            "section_header": None,
            "row_label": None,
            "location_candidate": None,
            "activity": None,
            "description": None,
            "raw_description": None,
            "normalized_description": None,
            "generated_description": None,
            "quantity": None,
            "unit_price": None,
            "amount": None,
            "gl_account_candidate": "",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": None,
            "reason": "observed_document_fact",
        }],
        "excluded_paid_rows": [{
            "raw_apartment_number": None,
            "component_amounts": [{"label": "", "amount": None}],
            "row_total": None,
            "paid_marker_evidence": [{
                "page": 1, "text": "PAID", "bbox": None, "confidence": None,
            }],
            "exclusion_reason": "visible_paid_marker",
        }],
        "subtotal": None,
        "tax_amount": None,
        "shipping_amount": None,
        "fees_amount": None,
        "total_amount": None,
        "confidence": None,
        "warnings": [],
        "needs_manual_review": False,
        "visual_extraction_status": "complete",
        "unresolved_visual_regions": [],
        "page_reconciliations": [],
        "vision_candidates": [],
    }
    return "\n".join([
        "Return strict JSON only using the exact schema below.",
        "Observe the document; do not perform accounting classification or GL reasoning.",
        "gl_account_candidate must always be empty. Do not infer readiness or export permission.",
        "Preserve raw visible text and separate it from normalized or generated descriptions.",
        "Extract every visible payable row/cell with source page, row identity, location, quantities, amounts, and table header context.",
        "Never collapse a visible financial table into one invoice-total line.",
        "Rows visibly PAID or crossed out belong only in excluded_paid_rows with component amounts and marker evidence.",
        "Keep printed date facts distinct: service_date is not invoice_date; textual terms such as Upon Receipt belong in due_date_text.",
        "Use null and an unresolved_visual_regions entry for ambiguity; never choose a value because it resembles a catalog entry.",
        "Reconcile page components and totals. Mark partial when facts are missing or arithmetic does not reconcile.",
        "Do not expose chain-of-thought. Return observable facts, confidence, concise evidence reasons, and warnings only.",
        "Schema:",
        json.dumps(schema, indent=2),
        "OCR helper text (visual evidence wins when they conflict):",
        document_text or "(none)",
    ])


def _build_vision_prompt(
    *,
    vendor_hint: str,
    document_text: str,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]],
    gl_reference: list[dict[str, Any]],
    vendor_reference: list[dict[str, Any]],
) -> str:
    schema = {
        "vendor_name": "",
        "invoice_nature": "unknown",
        "category": "unknown",
        "invoice_number": "",
        "invoice_date": "",
        "service_date": "",
        "purchase_date": "",
        "ship_date": "",
        "received_date": "",
        "due_date": "",
        "due_date_text": "",
        "payment_terms": "",
        "bill_or_credit": "Bill",
        "account_number": "",
        "service_address": "",
        "address_role": "unknown",
        "location_candidate": "",
        "service_period_start": "",
        "service_period_end": "",
        "service_period": "",
        "property_candidate": "",
        "property_abbreviation": "",
        "invoice_description": "",
        "line_items": [
            {
                "source_page": 1,
                "section_header": "",
                "row_label": "",
                "location_candidate": "",
                "activity": "",
                "description": "",
                "raw_description": "",
                "normalized_description": "",
                "generated_description": "",
                "quantity": None,
                "unit_price": None,
                "amount": 0.00,
                "gl_account_candidate": "",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": 0.0,
                "reason": "",
            }
        ],
        "excluded_paid_rows": [
            {
                "raw_apartment_number": "",
                "component_amounts": [{"label": "", "amount": None}],
                "row_total": None,
                "paid_marker_evidence": [
                    {"page": 1, "text": "PAID", "bbox": None, "confidence": 0.0}
                ],
                "exclusion_reason": "visible_paid_marker",
            }
        ],
        "subtotal": 0.00,
        "tax_amount": 0.00,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": 0.00,
        "confidence": 0.0,
        "warnings": [],
        "needs_manual_review": True,
        "vision_candidates": [
            {
                "field_key": "invoice_number",
                "field_label": "Invoice number",
                "value": "",
                "page": 1,
                "bbox": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "confidence": 0.0,
                "validation_status": "candidate",
            }
        ],
    }
    return "\n".join(
        [
            "Visually inspect the attached invoice page image(s) or native PDF and return the exact JSON schema below.",
            "Return JSON only. Do not include prose, markdown, code fences, or comments.",
            "Use null when unknown. Do not invent values that are not visible.",
            "Preserve visible text exactly where possible, especially invoice number, vendor, dates, totals, and line descriptions.",
            "For every line item, raw_description must preserve the visible source wording exactly; normalized_description may normalize casing and spacing but must not guess or expand an ambiguous abbreviation; generated_description must identify the item in 3 to 8 plain-language words for a human reviewer.",
            "Only expand an abbreviation when the page itself or unambiguous product wording proves the expansion. Otherwise use cautious generated text such as 'door or panel item, abbreviation unclear' instead of inventing a product meaning.",
            "Never place generated_description into raw_description or normalized_description. The generated description is explanatory display text, not source evidence and not an accounting decision.",
            "For a one-time purchase, repair, installation, or other non-recurring invoice, invoice_description must be one concise content summary (75 characters maximum) of the goods or work across all payable lines. Do not put the vendor, property, address, invoice date, or service month in that summary.",
            "Use period-based descriptions only for genuinely recurring bills or recurring services such as utilities, pest control, landscaping, marketing subscriptions, or trash collection. Words like repair, labor, maintenance, appliance, or service call alone do not make an invoice recurring.",
            "Set invoice_nature to one_time, recurring, utility_bill, or unknown. Set category to utilities only for a utility provider bill supported by metering, consumption, billing-period, or account-charge evidence; a contractor repairing a water, drain, electric, gas, or sewer system is one_time, not utilities.",
            "When an additional image is an enlarged crop of the same page, use it to verify small handwritten dates, invoice numbers, units, and addresses before finalizing the JSON.",
            "Extract a visibly labeled Date of Service into service_date. Do not relabel it as a printed invoice_date; the backend applies the documented accounting-date fallback while preserving provenance.",
            "Keep visible non-calendar due wording such as Upon Receipt in due_date_text and payment_terms; leave due_date empty unless a calendar date is printed.",
            "Rows visibly marked PAID are source evidence but not payable lines. Put each one only in excluded_paid_rows, including its raw apartment number, all visible component amounts, row total, and PAID marker evidence.",
            "For recurring bills/utilities, extract the visible service/billing period as service_period_start and service_period_end. Examples include '03/26/26 to 04/27/26' or 'service from ... to ...'.",
            "Search for invoice number, bill number, statement number, account number, and billing ID. If no invoice number exists on a bill, leave invoice_number empty; the backend will generate a required stable bill number from account/date/source context.",
            "If payment terms are visible but no calendar due date is visible, copy that text into payment_terms and leave due_date empty.",
            "If line-level dollar amounts are not visible and no row/allocation structure is visible, return one payable line item using the explicit invoice total and warn that line amounts were not visible.",
            "An invoice-total fallback is never sufficient when visible row labels, unit totals, allocation rows, or matrix columns exist. Re-read those regions and preserve at least each source row/allocation; never replace a visible matrix with one invoice-total line.",
            "Preserve hierarchical and tabular context. A row label by itself is not a complete line description when a section header or column header defines the charge.",
            "For matrix tables, create one line_item for every non-empty billable amount cell, not one summary item per physical row.",
            "When the source has distinct allocation rows, coverage rows, units, locations, departments, projects, or cost centers with explicit amounts, preserve one line_item per source allocation. Never group allocations merely because descriptions or amounts repeat.",
            "For each matrix cell: source_page is the visible page number; row_label is the row header such as unit/apartment; activity is the exact billable column header; section_header is the nearest applicable group heading; description combines the applicable header context and row label without losing either.",
            "Do not treat Unit Total, Row Total, Subtotal, or Invoice Total columns as additional line items when their component cells are visible; use them only to reconcile the component cells.",
            "Before returning JSON, silently perform a second visual pass over dates, row labels, column headers, crossed-out entries, page totals, and arithmetic. Do not expose chain-of-thought; return only evidence fields, concise reasons, and warnings.",
            "Set visual_extraction_status to complete only when all payable source rows are represented and all page/component arithmetic reconciles exactly. Otherwise set it to partial or aggregate_fallback, list each unresolved region, and set needs_manual_review true.",
            "For every page, populate page_reconciliations with the printed page total, extracted component total, exact difference, and status reconciled or mismatch. Across continuation pages, total_amount must equal the sum of payable page totals and all emitted components plus explicit adders must reconcile to it within one cent.",
            "Carry merged or visually spanning headers to every child cell they govern. Never drop a charge heading merely because it appears once above multiple rows.",
            "When multiple images are pages of one invoice, retain source_page on every line and reconcile all page-level component charges. If repeated invoice totals conflict or it is unclear whether pages are continuations, preserve the page evidence, add a warning, lower confidence, and require review rather than silently choosing one total.",
            "Include confidence per field/line item from 0.0 to 1.0.",
            "Include candidate bounding boxes only when visually confident.",
            "Create vision_candidates for every clearly visible critical field: vendor_name, invoice_number, account_number, invoice_date, service_date, due_date, subtotal, tax_amount, shipping_amount, fees_amount, total_amount, property_candidate, service_address, address_role, and location_candidate. These candidates are independently validated by the backend.",
            "Bounding boxes must be normalized page coordinates: x, y, w, h from 0.0 to 1.0.",
            "If OCR text is provided, use it as helper context but trust the image when OCR is weak or missing.",
            "Distinguish the vendor/remit address from the customer, job, ship-to, or service address; service_address must describe where the work occurred.",
            "Set address_role to one of service_address, job_site, ship_to, sold_to, bill_to, remit_to, vendor_address, or unknown. A SOLD TO or BILL TO address is not a service address unless the document explicitly says the work occurred there.",
            "Use customer/property names and property-specific email domains as property_candidate evidence. Property identity is stronger than an unrelated administrative billing address.",
            "Never return field labels such as ACCOUNT, NUMBER, SALES, LOCATION, or DATE as invoice_number. Read the actual value printed beneath or beside INVOICE NUMBER.",
            "The invoice total is the final payable TOTAL/AMOUNT DUE, not subtotal or a line extension. Reconcile line items plus tax, shipping, and fees to total_amount.",
            "Extract a visible customer order unit, apartment, suite, or job-unit value into location_candidate. Keep service_address as the actual street address.",
            "Source filename and batch context are plausibility hints only. Never use them as invoice fields, but use them to resolve an otherwise ambiguous handwritten year.",
            "For handwritten dates, inspect every digit carefully and lower confidence or add a warning when the date is ambiguous.",
            "Treat vendor-side category text as source text only; do not invent ResMan GL accounts.",
            "Flag ambiguity in warnings and needs_manual_review.",
            "",
            canonical_rules.prompt_rules_summary(),
            "",
            f"Vendor hint: {vendor_hint or 'unknown'}",
            "",
            "Required JSON schema:",
            json.dumps(schema, indent=2),
            "",
            "ResMan template columns:",
            json.dumps(template_schema, indent=2)[:5000],
            "",
            "Known vendors (sample/reference):",
            json.dumps(vendor_reference[:80], indent=2)[:6000],
            "",
            "Property reference (sample/reference):",
            json.dumps(property_reference[:120], indent=2)[:6000],
            "",
            "General ledger reference (sample/reference):",
            json.dumps(gl_reference[:120], indent=2)[:6000],
            "",
            "OCR/text helper context:",
            document_text or "(none)",
        ]
    )


def _normalize_vision_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, dict):
            continue
        try:
            x = float(bbox.get("x"))
            y = float(bbox.get("y"))
            w = float(bbox.get("w"))
            h = float(bbox.get("h"))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        out.append({
            "field_key": str(item.get("field_key") or item.get("field") or "vision_candidate"),
            "field_label": str(item.get("field_label") or item.get("field_key") or "AI vision candidate"),
            "value": item.get("value"),
            "page": max(1, int(item.get("page") or 1)),
            "bbox": {
                "x": max(0.0, min(1.0, x)),
                "y": max(0.0, min(1.0, y)),
                "w": max(0.001, min(1.0, w)),
                "h": max(0.001, min(1.0, h)),
            },
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
            "validation_status": str(item.get("validation_status") or "candidate"),
        })
    return out


__all__ = [
    "AIProviderError",
    "AIProviderInvalidJSON",
    "AIProviderInvalidSchema",
    "AIProviderNotConfigured",
    "AIProviderStatus",
    "AIProviderUnavailable",
    "controlled_gemini_supplementary_profile_identity",
    "extract_gemini_supplementary_facts_structured",
    "extract_invoice_native_pdf_structured",
    "extract_invoice_critical_fields_vision_structured",
    "extract_handwritten_row_identities_vision_structured",
    "extract_invoice_facts_only_vision_structured",
    "extract_invoice_vision_structured",
    "extraction_profile_identity",
    "extract_invoice_structured",
    "provider_status",
    "status_payload",
]
