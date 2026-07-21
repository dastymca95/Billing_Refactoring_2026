"""Historical diagnostic for the Gemini 3 Flash Preview candidate probe.

This is not the private Arm B executor, a production import, or a primary
experiment entry point. It is retained for exact synthetic reproducibility.

This script never opens the paired private bundle.  It performs one model
metadata lookup and, only when eligible, exactly one synthetic generateContent
request with no retry or fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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

AUTHORIZED_HOST = "generativelanguage.googleapis.com"
MODEL_ID = "gemini-3-flash-preview"
MODEL_RESOURCE = f"models/{MODEL_ID}"
METADATA_ENDPOINT = f"https://{AUTHORIZED_HOST}/v1beta/{MODEL_RESOURCE}"
GENERATION_ENDPOINT = (
    f"https://{AUTHORIZED_HOST}/v1beta/{MODEL_RESOURCE}:generateContent"
)
INPUT_RATE_USD_PER_MILLION = Decimal("0.50")
OUTPUT_RATE_USD_PER_MILLION = Decimal("3.00")
ESTIMATED_IMAGE_SAFETY_USD = Decimal("0.002000")
EXPECTED_INPUT_TOKENS_PER_ARM = 27_507
EXPECTED_OUTPUT_TOKENS_PER_ARM = 3_719
SYNTHETIC_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _safe_error(raw_body: bytes) -> tuple[str | None, int | None, str]:
    """Classify an error without returning provider message or response data."""
    try:
        payload = json.loads(raw_body.decode("utf-8", "replace"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None, None, "native_error_unclassified"
        status = str(error.get("status") or "")[:80] or None
        try:
            code = int(error.get("code")) if error.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        if status in {"UNAUTHENTICATED", "PERMISSION_DENIED"} or code in {401, 403}:
            category = "authentication_or_permission_failed"
        elif status == "INVALID_ARGUMENT" or code == 400:
            category = "native_request_contract_invalid"
        elif status == "UNAVAILABLE" or code == 503:
            category = "temporarily_unavailable"
        elif status == "NOT_FOUND" or code == 404:
            category = "model_not_visible"
        else:
            category = "native_error_unclassified"
        return status, code, category
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, "native_error_unclassified"


def _metadata_check(api_key: str) -> dict:
    request = urllib.request.Request(
        METADATA_ENDPOINT,
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
        methods = payload.get("supportedGenerationMethods")
        actions = sorted(str(item) for item in methods) if isinstance(methods, list) else []

        def optional_int(name: str) -> int | None:
            try:
                return int(payload.get(name))
            except (TypeError, ValueError):
                return None

        resource_exists = str(payload.get("name") or "") == MODEL_RESOURCE
        return {
            "http_status": status,
            "resource_exists": resource_exists,
            "base_model_id": str(payload.get("baseModelId") or "") or None,
            "supported_actions": actions,
            "input_token_limit": optional_int("inputTokenLimit"),
            "output_token_limit": optional_int("outputTokenLimit"),
            "generate_content_supported": "generateContent" in actions,
            "visible_to_configured_key": resource_exists,
            "metadata_image_capability_field_present": False,
            "metadata_structured_output_field_present": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "safe_error_status": None,
            "safe_error_code": None,
            "metadata_requests": 1,
        }
    except urllib.error.HTTPError as exc:
        error_status, error_code, _ = _safe_error(exc.read(4096))
        return {
            "http_status": int(exc.code),
            "resource_exists": False,
            "base_model_id": None,
            "supported_actions": [],
            "input_token_limit": None,
            "output_token_limit": None,
            "generate_content_supported": False,
            "visible_to_configured_key": False,
            "metadata_image_capability_field_present": False,
            "metadata_structured_output_field_present": False,
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
            "metadata_image_capability_field_present": False,
            "metadata_structured_output_field_present": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "safe_error_status": type(exc).__name__,
            "safe_error_code": None,
            "metadata_requests": 1,
        }


def _usage(envelope: object) -> tuple[dict[str, int], int | None]:
    raw = envelope.get("usageMetadata") if isinstance(envelope, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def integer(name: str) -> int:
        try:
            return max(0, int(raw.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    thinking = integer("thoughtsTokenCount") if raw.get("thoughtsTokenCount") is not None else None
    usage = {
        "input_tokens": integer("promptTokenCount"),
        "output_tokens": integer("candidatesTokenCount"),
        "total_tokens": integer("totalTokenCount"),
    }
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"] + int(thinking or 0)
    return usage, thinking


def _validate_response(envelope: object) -> tuple[bool, str | None]:
    if not isinstance(envelope, dict):
        return False, None
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return False, None
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    finish_reason = str(candidate.get("finishReason") or "")[:80] or None
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    if len(texts) != 1:
        return False, finish_reason
    try:
        payload = json.loads(texts[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, finish_reason
    valid = (
        isinstance(payload, dict)
        and set(payload) == {"visible", "synthetic_label"}
        and isinstance(payload.get("visible"), bool)
        and isinstance(payload.get("synthetic_label"), str)
    )
    return bool(valid), finish_reason


def _actual_cost(usage: dict[str, int], thinking_tokens: int | None) -> Decimal:
    billed_output = usage["output_tokens"] + int(thinking_tokens or 0)
    return (
        Decimal(usage["input_tokens"]) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(billed_output) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def _estimated_probe_cost(payload: dict) -> Decimal:
    scrubbed = json.loads(json.dumps(payload))
    scrubbed["contents"][0]["parts"][1]["inlineData"]["data"] = "<synthetic-image>"
    estimated_input_tokens = math.ceil(len(json.dumps(scrubbed, separators=(",", ":"))) / 4)
    output_limit = int(payload["generationConfig"]["maxOutputTokens"])
    return (
        Decimal(estimated_input_tokens) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(output_limit) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + ESTIMATED_IMAGE_SAFETY_USD
    ).quantize(Decimal("0.000001"))


def _request_fingerprint(endpoint: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(endpoint.encode("ascii") + b"\n" + canonical).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController,
        spend_cost_accounting_view,
    )
    from webapp.backend.services.gemini_probe_contract_audit import build_native_gemini_probe
    from webapp.backend.services.supplementary_ab_arm_b_candidate import (
        PreparedArmBExecutionOverlay,
        calculate_candidate_budget,
    )

    base_candidate = build_native_gemini_probe(SYNTHETIC_PNG)
    payload = dict(base_candidate.payload)
    endpoint = urlparse(GENERATION_ENDPOINT)
    parts = payload.get("contents", [{}])[0].get("parts", [])
    images = [item for item in parts if isinstance(item, dict) and "inlineData" in item]
    generation_config = payload.get("generationConfig") if isinstance(payload.get("generationConfig"), dict) else {}
    local_contract_valid = bool(
        endpoint.scheme == "https"
        and endpoint.hostname == AUTHORIZED_HOST
        and len(images) == 1
        and generation_config.get("responseMimeType") == "application/json"
        and isinstance(generation_config.get("responseJsonSchema"), dict)
        and generation_config.get("maxOutputTokens") == 256
    )
    request_fingerprint = _request_fingerprint(GENERATION_ENDPOINT, payload)
    if not local_contract_valid:
        print(json.dumps({"result": "BLOCKED_LOCAL_SYNTHETIC_CONTRACT_INVALID"}))
        return 2

    private_root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    spend = ExperimentSpendController(private_root, "exp-document-learning-simulation")
    initial_snapshot = spend.snapshot("A")
    budget = calculate_candidate_budget(
        expected_input_tokens_per_arm=EXPECTED_INPUT_TOKENS_PER_ARM,
        expected_output_tokens_per_arm=EXPECTED_OUTPUT_TOKENS_PER_ARM,
        phase_a_cumulative_spend_usd=Decimal(initial_snapshot.cumulative_charged_usd),
    )
    preflight = {
        "previous_arm_b_model": "gemini-3.5-flash",
        "previous_arm_b_status": "temporarily_unavailable_after_bounded_retry",
        "candidate_model": MODEL_ID,
        "local_image_input_contract_configured": True,
        "local_structured_output_contract_configured": True,
        "request_fingerprint": request_fingerprint,
        "paired_bundle_opened": False,
        "ab_packet_requests": 0,
        "budget": budget.model_dump(mode="json"),
    }
    if not args.execute:
        print(json.dumps({"result": "OFFLINE_PREFLIGHT_VALID", **preflight}, indent=2, sort_keys=True))
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
    if initial_snapshot.canceled:
        print(json.dumps({"result": "BLOCKED_EXPERIMENT_SPEND_LEDGER_CANCELED"}))
        return 2

    metadata = _metadata_check(api_key)
    eligible = bool(metadata["resource_exists"] and metadata["generate_content_supported"])
    if not eligible:
        print(json.dumps({
            "result": "MODEL_NOT_VISIBLE_TO_CONFIGURED_KEY",
            "metadata": metadata,
            "generation_requests": 0,
            "retry_count": 0,
            "fallback_attempts": 0,
            "provider_response_persisted": False,
            "credential_or_header_persisted": False,
            "private_documents_transmitted": 0,
            "paired_bundle_opened": False,
            "endpoint_hosts_contacted": [AUTHORIZED_HOST],
            **preflight,
        }, indent=2, sort_keys=True))
        return 1

    reservation = spend.reserve(
        phase="A",
        estimated_cost_usd=_estimated_probe_cost(payload),
        provider="gemini",
        model_id=MODEL_ID,
        profile_id="runtime-vision-native-synthetic",
        stage="gemini_3_flash_preview_arm_b_candidate_probe",
        document_sha256=hashlib.sha256(SYNTHETIC_PNG).hexdigest(),
        purpose="synthetic_native_arm_b_candidate_capability_probe",
    )

    http_status: int | None = None
    envelope: object = None
    failure_code: str | None = None
    safe_error_category: str | None = None
    safe_error_status: str | None = None
    safe_error_code: int | None = None
    generation_requests = 0
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            GENERATION_ENDPOINT,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        reservation = spend.mark_dispatched(reservation.reservation_id)
        generation_requests = 1
        with urllib.request.urlopen(request, timeout=90) as response:
            http_status = int(response.status)
            body = response.read(100_000)
        envelope = json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        safe_error_status, safe_error_code, safe_error_category = _safe_error(exc.read(4096))
        failure_code = f"http_{http_status}"
    except urllib.error.URLError:
        failure_code = "native_transport_unavailable"
        safe_error_category = "native_transport_unavailable"
    except (TypeError, ValueError, json.JSONDecodeError):
        failure_code = "native_response_invalid_json"
        safe_error_category = "native_response_invalid_json"
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    usage, thinking_tokens = _usage(envelope)
    usage_reported = bool(any(usage.values()))
    schema_valid, finish_reason = _validate_response(envelope)
    returned_model = str(envelope.get("modelVersion") or "") if isinstance(envelope, dict) else ""
    exact_model = returned_model == MODEL_ID
    actual_cost = _actual_cost(usage, thinking_tokens) if usage_reported else None

    transport_accepted = bool(http_status == 200 and exact_model)
    success = bool(transport_accepted and schema_valid and usage_reported)
    if success:
        result_category = "NATIVE_GEMINI_3_FLASH_PREVIEW_CAPABILITY_CONFIRMED"
    elif http_status == 503:
        result_category = "TEMPORARILY_UNAVAILABLE"
    elif http_status == 400:
        result_category = "NATIVE_REQUEST_CONTRACT_INVALID"
    elif http_status in {401, 403}:
        result_category = "AUTHENTICATION_OR_PERMISSION_FAILED"
    else:
        result_category = "NATIVE_CAPABILITY_PROBE_FAILED"
    if http_status == 200 and not success:
        failure_code = (
            "native_model_version_mismatch" if not exact_model
            else "native_usage_unavailable" if not usage_reported
            else "native_output_limit_before_schema_completion"
            if finish_reason == "MAX_TOKENS"
            else "native_response_schema_invalid"
        )
        safe_error_category = failure_code

    reservation = spend.settle(
        reservation.reservation_id,
        actual_cost_usd=actual_cost,
        usage={**usage, "provider_request_count": generation_requests},
        provider_reported_usage=usage_reported,
        failure_code=failure_code,
    )
    cost_view = spend_cost_accounting_view(reservation)

    prepared_contract_written = False
    if success:
        overlay = PreparedArmBExecutionOverlay(
            technically_eligible=True,
            budget=budget,
        )
        output_path = private_root / "telemetry" / "prepared_arm_b_gemini_3_flash_preview.json"
        output_path.write_text(
            json.dumps(overlay.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prepared_contract_written = True

    authentication = (
        "failed" if http_status in {401, 403}
        else "succeeded" if metadata["http_status"] == 200
        else "not_verified"
    )
    result = {
        "result": result_category,
        "authentication_result": authentication,
        "metadata": metadata,
        "requested_model": MODEL_ID,
        "exact_model_returned": exact_model,
        "image_input_accepted": transport_accepted,
        "structured_output_request_accepted": transport_accepted,
        "structured_output_schema_valid": schema_valid,
        "schema_validation_result": "valid" if schema_valid else "invalid_or_unavailable",
        "finish_reason": finish_reason,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "usage_reported": usage_reported,
        "input_tokens": usage["input_tokens"] if usage_reported else None,
        "output_tokens": usage["output_tokens"] if usage_reported else None,
        "thinking_tokens": thinking_tokens,
        "generation_requests": generation_requests,
        "retry_count": 0,
        "fallback_attempts": 0,
        "safe_error_category": safe_error_category,
        "safe_error_status": safe_error_status,
        "safe_error_code": safe_error_code,
        **cost_view.model_dump(),
        "cumulative_phase_a_charged_usd": spend.snapshot("A").cumulative_charged_usd,
        "endpoint_contacted": GENERATION_ENDPOINT if generation_requests else None,
        "endpoint_hosts_contacted": [AUTHORIZED_HOST],
        "provider_response_persisted": False,
        "credential_or_header_persisted": False,
        "private_documents_transmitted": 0,
        "paired_bundle_opened": False,
        "ab_packet_requests": 0,
        "prepared_execution_contract_written": prepared_contract_written,
        "private_execution_authorized": False,
        **preflight,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
