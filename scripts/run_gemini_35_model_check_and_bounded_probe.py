"""Historical diagnostic for Gemini 3.5 metadata and bounded retry behavior.

It is retained to reproduce the non-private availability result and is not
imported by production or used by the primary experiment path.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_gemini_35_native_synthetic_probe import (  # noqa: E402
    AUTHORIZED_ENDPOINT,
    AUTHORIZED_HOST,
    EXPECTED_FINGERPRINT,
    INPUT_RATE_USD_PER_MILLION,
    MODEL_ID,
    OUTPUT_RATE_USD_PER_MILLION,
    SYNTHETIC_PNG,
    _estimate,
    _safe_error,
    _usage,
    _validate_response,
)


MODEL_RESOURCE = f"models/{MODEL_ID}"
MODEL_ENDPOINT = f"https://{AUTHORIZED_HOST}/v1beta/{MODEL_RESOURCE}"
MAX_GENERATION_ATTEMPTS = 3
RETRYABLE_STATUSES = {429, 503}


def _metadata_check(api_key: str) -> dict:
    request = urllib.request.Request(
        MODEL_ENDPOINT,
        headers={"x-goog-api-key": api_key},
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            raw = response.read(100_000)
        payload = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("model_metadata_not_object")
        name = str(payload.get("name") or "")
        methods = payload.get("supportedGenerationMethods")
        methods = [str(item) for item in methods] if isinstance(methods, list) else []
        try:
            input_limit = int(payload.get("inputTokenLimit"))
        except (TypeError, ValueError):
            input_limit = None
        try:
            output_limit = int(payload.get("outputTokenLimit"))
        except (TypeError, ValueError):
            output_limit = None
        return {
            "http_status": status,
            "resource_exists": name == MODEL_RESOURCE,
            "base_model_id": str(payload.get("baseModelId") or "") or None,
            "supported_actions": sorted(methods),
            "input_token_limit": input_limit,
            "output_token_limit": output_limit,
            "generate_content_supported": "generateContent" in methods,
            "visible_to_configured_key": name == MODEL_RESOURCE,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "safe_error_status": None,
            "safe_error_code": None,
            "metadata_requests": 1,
        }
    except urllib.error.HTTPError as exc:
        error_status, error_code, _category = _safe_error(exc.read(4096))
        return {
            "http_status": int(exc.code),
            "resource_exists": False,
            "base_model_id": None,
            "supported_actions": [],
            "input_token_limit": None,
            "output_token_limit": None,
            "generate_content_supported": False,
            "visible_to_configured_key": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "safe_error_status": error_status,
            "safe_error_code": error_code,
            "metadata_requests": 1,
        }
    except (urllib.error.URLError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "http_status": None,
            "resource_exists": False,
            "base_model_id": None,
            "supported_actions": [],
            "input_token_limit": None,
            "output_token_limit": None,
            "generate_content_supported": False,
            "visible_to_configured_key": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "safe_error_status": type(exc).__name__,
            "safe_error_code": None,
            "metadata_requests": 1,
        }


def _actual_cost(usage: dict[str, int], thinking_tokens: int | None) -> Decimal:
    billed_output = usage["output_tokens"] + int(thinking_tokens or 0)
    return (
        Decimal(usage["input_tokens"]) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(billed_output) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController,
        spend_cost_accounting_view,
    )
    from webapp.backend.services.gemini_probe_contract_audit import build_native_gemini_probe

    candidate = build_native_gemini_probe(SYNTHETIC_PNG)
    parsed = urlparse(candidate.endpoint)
    if (
        candidate.payload_fingerprint != EXPECTED_FINGERPRINT
        or candidate.endpoint != AUTHORIZED_ENDPOINT
        or parsed.scheme != "https"
        or parsed.hostname != AUTHORIZED_HOST
    ):
        print(json.dumps({"result": "BLOCKED_FROZEN_CANDIDATE_MISMATCH"}))
        return 2
    if not args.execute:
        print(json.dumps({
            "result": "OFFLINE_PREFLIGHT_VALID",
            "fingerprint": candidate.payload_fingerprint,
            "external_requests": 0,
        }, sort_keys=True))
        return 0

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEY", "AI_VISION_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        print(json.dumps({"result": "AUTHENTICATION_OR_PERMISSION_FAILED", "reason": "credential_unavailable"}))
        return 2

    metadata = _metadata_check(api_key)
    eligible = bool(metadata["resource_exists"] and metadata["generate_content_supported"])
    if not eligible:
        result = {
            "result": "MODEL_NOT_VISIBLE_TO_CONFIGURED_KEY",
            "metadata": metadata,
            "generation_attempts": [],
            "generation_requests": 0,
            "fallback_attempts": 0,
            "provider_response_persisted": False,
            "credential_or_header_persisted": False,
            "private_documents_transmitted": 0,
            "paired_ab_bundle_opened": False,
            "endpoint_hosts_contacted": [AUTHORIZED_HOST],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1

    private_root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    spend = ExperimentSpendController(private_root, "exp-document-learning-simulation")
    if spend.snapshot("A").canceled:
        print(json.dumps({"result": "BLOCKED_EXPERIMENT_SPEND_LEDGER_CANCELED"}))
        return 2

    raw_request = json.dumps(candidate.payload, separators=(",", ":")).encode("utf-8")
    attempts: list[dict] = []
    final_result = "NATIVE_REQUEST_CONTRACT_INVALID"
    generation_requests = 0
    delays = [random.SystemRandom().uniform(1.8, 2.2), random.SystemRandom().uniform(7.2, 8.8)]

    for attempt_number in range(1, MAX_GENERATION_ATTEMPTS + 1):
        if attempt_number > 1:
            time.sleep(delays[attempt_number - 2])
        reservation = spend.reserve(
            phase="A",
            estimated_cost_usd=_estimate(dict(candidate.payload)),
            provider="gemini",
            model_id=MODEL_ID,
            profile_id="runtime-vision-native",
            stage="gemini_3_5_native_bounded_capability_probe",
            document_sha256="c2153f77e11087fcb078ae38527fa83bef29791e3700e30cc87fec4405a66d0f",
            purpose=f"synthetic_native_capability_probe_attempt_{attempt_number}",
        )
        http_status: int | None = None
        envelope: object = None
        failure_code: str | None = None
        error_status: str | None = None
        error_code: int | None = None
        safe_category = "native_error_unclassified"
        started = time.perf_counter()
        try:
            request = urllib.request.Request(
                candidate.endpoint,
                data=raw_request,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            reservation = spend.mark_dispatched(reservation.reservation_id)
            generation_requests += 1
            if generation_requests > MAX_GENERATION_ATTEMPTS:
                raise RuntimeError("native_probe_total_request_limit_reached")
            with urllib.request.urlopen(request, timeout=90) as response:
                http_status = int(response.status)
                body = response.read(100_000)
            envelope = json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            error_status, error_code, safe_category = _safe_error(exc.read(4096))
            failure_code = f"http_{http_status}"
        except urllib.error.URLError:
            failure_code = "native_transport_unavailable"
            safe_category = "native_transport_unavailable"
        except (TypeError, ValueError, json.JSONDecodeError):
            failure_code = "native_response_invalid_json"
            safe_category = "native_response_invalid_json"
        except Exception as exc:
            failure_code = type(exc).__name__
            safe_category = "native_local_dispatch_failure"

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        usage, thinking_tokens = _usage(envelope)
        usage_reported = bool(any(usage.values()))
        schema_valid, finish_reason = _validate_response(envelope)
        actual_cost = _actual_cost(usage, thinking_tokens) if usage_reported else None
        returned_model = (
            str(envelope.get("modelVersion") or "")
            if isinstance(envelope, dict) else ""
        )
        exact_model = returned_model == MODEL_ID
        if http_status is not None and 200 <= http_status < 300:
            if schema_valid and usage_reported and exact_model:
                final_result = "NATIVE_CAPABILITY_CONFIRMED"
            else:
                failure_code = failure_code or (
                    "native_model_version_mismatch" if not exact_model
                    else "native_usage_unavailable" if not usage_reported
                    else "native_response_schema_invalid"
                )
                safe_category = failure_code
                final_result = "NATIVE_REQUEST_CONTRACT_INVALID"
        elif http_status in {401, 403}:
            final_result = "AUTHENTICATION_OR_PERMISSION_FAILED"
        elif http_status == 400:
            final_result = "NATIVE_REQUEST_CONTRACT_INVALID"
        elif http_status in RETRYABLE_STATUSES:
            final_result = "TEMPORARILY_UNAVAILABLE_AFTER_BOUNDED_RETRY"
        else:
            final_result = "NATIVE_REQUEST_CONTRACT_INVALID"

        reservation = spend.settle(
            reservation.reservation_id,
            actual_cost_usd=actual_cost,
            usage={**usage, "provider_request_count": 1},
            provider_reported_usage=usage_reported,
            failure_code=failure_code,
        )
        cost_view = spend_cost_accounting_view(reservation)
        attempts.append({
            "attempt": attempt_number,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "safe_error_category": safe_category if failure_code else None,
            "safe_error_status": error_status,
            "safe_error_code": error_code,
            "finish_reason": finish_reason,
            "returned_model_matches": exact_model,
            "schema_valid": schema_valid,
            "usage_reported": usage_reported,
            "input_tokens": usage["input_tokens"] if usage_reported else None,
            "output_tokens": usage["output_tokens"] if usage_reported else None,
            "thinking_tokens": thinking_tokens,
            **cost_view.model_dump(),
        })

        if final_result == "NATIVE_CAPABILITY_CONFIRMED":
            break
        if http_status not in RETRYABLE_STATUSES:
            break
        if attempt_number == MAX_GENERATION_ATTEMPTS:
            final_result = "TEMPORARILY_UNAVAILABLE_AFTER_BOUNDED_RETRY"

    result = {
        "result": final_result,
        "metadata": metadata,
        "request_fingerprint": candidate.payload_fingerprint,
        "generation_attempts": attempts,
        "generation_requests": generation_requests,
        "retry_count": max(0, generation_requests - 1),
        "fallback_attempts": 0,
        "ab_packet_requests": 0,
        "initial_document_extractions": 0,
        "cumulative_phase_a_charged_usd": spend.snapshot("A").cumulative_charged_usd,
        "provider_response_persisted": False,
        "credential_or_header_persisted": False,
        "private_documents_transmitted": 0,
        "paired_ab_bundle_opened": False,
        "endpoint_hosts_contacted": [AUTHORIZED_HOST],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if final_result == "NATIVE_CAPABILITY_CONFIRMED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
