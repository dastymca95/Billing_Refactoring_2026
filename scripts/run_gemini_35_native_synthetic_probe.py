"""Historical diagnostic for the non-private native Gemini 3.5 probe.

It is retained for exact request-fingerprint reproducibility and is neither a
production import nor a primary experiment entry point.
"""

from __future__ import annotations

import argparse
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

EXPECTED_FINGERPRINT = "f38ced1465971cd7e77f74847980e759a5f85b15158ba6569f5b49727dc8faaf"
AUTHORIZED_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.5-flash:generateContent"
)
AUTHORIZED_HOST = "generativelanguage.googleapis.com"
MODEL_ID = "gemini-3.5-flash"
INPUT_RATE_USD_PER_MILLION = Decimal("1.50")
OUTPUT_RATE_USD_PER_MILLION = Decimal("9.00")
ESTIMATED_IMAGE_SAFETY_USD = Decimal("0.002000")

SYNTHETIC_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _safe_error(raw_body: bytes) -> tuple[str | None, int | None, str]:
    """Return category/code/classification only; never return provider text."""
    try:
        envelope = json.loads(raw_body.decode("utf-8", "replace"))
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if not isinstance(error, dict):
            return None, None, "native_error_unclassified"
        status = str(error.get("status") or "")[:80] or None
        try:
            code = int(error.get("code")) if error.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        message = str(error.get("message") or "").casefold()
        if status in {"UNAUTHENTICATED", "PERMISSION_DENIED"} or code in {401, 403}:
            category = "native_authentication_or_permission_failed"
        elif status == "NOT_FOUND" or code == 404 or (
            "model" in message and any(term in message for term in ("not found", "not available", "unsupported"))
        ):
            category = "native_model_unavailable"
        elif status == "INVALID_ARGUMENT" or code == 400:
            category = "native_request_contract_invalid"
        else:
            category = "native_error_unclassified"
        return status, code, category
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, "native_error_unclassified"


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
        usage["total_tokens"] = (
            usage["input_tokens"] + usage["output_tokens"] + int(thinking or 0)
        )
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


def _estimate(payload: dict) -> Decimal:
    scrubbed = json.loads(json.dumps(payload))
    scrubbed["contents"][0]["parts"][1]["inlineData"]["data"] = "<synthetic-image>"
    estimated_input_tokens = math.ceil(len(json.dumps(scrubbed, separators=(",", ":"))) / 4)
    output_limit = int(payload["generationConfig"]["maxOutputTokens"])
    estimate = (
        Decimal(estimated_input_tokens) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(output_limit) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + ESTIMATED_IMAGE_SAFETY_USD
    )
    return estimate.quantize(Decimal("0.000001"))


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
    if candidate.payload_fingerprint != EXPECTED_FINGERPRINT:
        print(json.dumps({"status": "blocked_candidate_fingerprint_mismatch"}))
        return 2
    endpoint = urlparse(candidate.endpoint)
    if candidate.endpoint != AUTHORIZED_ENDPOINT or endpoint.scheme != "https" or endpoint.hostname != AUTHORIZED_HOST:
        print(json.dumps({"status": "blocked_unauthorized_endpoint"}))
        return 2
    if not args.execute:
        print(json.dumps({
            "status": "offline_native_candidate_valid",
            "fingerprint": candidate.payload_fingerprint,
            "provider_requests": 0,
        }, sort_keys=True))
        return 0

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEY", "AI_VISION_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        print(json.dumps({"status": "blocked_private_credential_unavailable"}))
        return 2

    private_root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    spend = ExperimentSpendController(private_root, "exp-document-learning-simulation")
    if spend.snapshot("A").canceled:
        print(json.dumps({"status": "blocked_experiment_spend_ledger_canceled"}))
        return 2
    reservation = spend.reserve(
        phase="A",
        estimated_cost_usd=_estimate(dict(candidate.payload)),
        provider="gemini",
        model_id=MODEL_ID,
        profile_id="runtime-vision-native",
        stage="gemini_3_5_native_capability_probe",
        document_sha256="c2153f77e11087fcb078ae38527fa83bef29791e3700e30cc87fec4405a66d0f",
        purpose="synthetic_native_capability_probe",
    )

    network_calls = 0
    http_status: int | None = None
    envelope: object = None
    failure_code: str | None = None
    provider_error_status: str | None = None
    provider_error_code: int | None = None
    result_category = "native_request_contract_invalid"
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            candidate.endpoint,
            data=json.dumps(candidate.payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        reservation = spend.mark_dispatched(reservation.reservation_id)
        network_calls += 1
        if network_calls != 1:
            raise RuntimeError("native_synthetic_probe_request_limit_reached")
        with urllib.request.urlopen(request, timeout=90) as response:
            http_status = int(response.status)
            body = response.read(100_000)
        envelope = json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        provider_error_status, provider_error_code, result_category = _safe_error(exc.read(4096))
        failure_code = f"http_{http_status}"
    except urllib.error.URLError:
        failure_code = "native_transport_unavailable"
        result_category = "native_request_contract_invalid"
    except (TypeError, ValueError, json.JSONDecodeError):
        failure_code = "native_response_invalid_json"
        result_category = "native_request_contract_invalid"
    except Exception as exc:
        failure_code = type(exc).__name__
        result_category = "native_request_contract_invalid"
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    usage, thinking_tokens = _usage(envelope)
    usage_reported = bool(any(usage.values()))
    schema_valid, finish_reason = _validate_response(envelope)
    actual_cost: Decimal | None = None
    if usage_reported:
        billed_output = usage["output_tokens"] + int(thinking_tokens or 0)
        actual_cost = (
            Decimal(usage["input_tokens"]) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
            + Decimal(billed_output) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))
    if http_status is not None and 200 <= http_status < 300:
        if schema_valid and usage_reported:
            result_category = "native_capability_confirmed"
        else:
            failure_code = failure_code or (
                "native_usage_unavailable" if not usage_reported
                else "native_response_schema_invalid"
            )
            result_category = "native_request_contract_invalid"
    reservation = spend.settle(
        reservation.reservation_id,
        actual_cost_usd=actual_cost,
        usage={**usage, "provider_request_count": network_calls},
        provider_reported_usage=usage_reported,
        failure_code=failure_code,
    )
    cost_view = spend_cost_accounting_view(reservation)

    success = result_category == "native_capability_confirmed"
    authentication = (
        "failed" if http_status in {401, 403} or result_category == "native_authentication_or_permission_failed"
        else "succeeded" if http_status is not None
        else "not_verified"
    )
    result = {
        "result_category": result_category,
        "http_status": http_status,
        "authentication_result": authentication,
        "requested_model": MODEL_ID,
        "native_model_available": success,
        "image_input_accepted": success,
        "native_structured_output_accepted": success,
        "schema_validation_result": "valid" if schema_valid else "invalid_or_unavailable",
        "finish_reason": finish_reason,
        "provider_reported_usage": usage_reported,
        "input_tokens": usage["input_tokens"] if usage_reported else None,
        "output_tokens": usage["output_tokens"] if usage_reported else None,
        "thinking_tokens": thinking_tokens,
        "latency_ms": latency_ms,
        "endpoint_contacted": candidate.endpoint if network_calls == 1 else None,
        "provider_requests": network_calls,
        "retry_count": 0,
        "fallback_attempts": 0,
        "ab_packet_requests": 0,
        "initial_document_extractions": 0,
        "request_fingerprint": candidate.payload_fingerprint,
        "failure_code": failure_code,
        "provider_error_status": provider_error_status,
        "provider_error_code": provider_error_code,
        **cost_view.model_dump(),
        "cumulative_phase_a_charged_usd": spend.snapshot("A").cumulative_charged_usd,
        "provider_response_persisted": False,
        "credential_or_header_persisted": False,
        "private_documents_transmitted": 0,
        "paired_ab_bundle_opened": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
