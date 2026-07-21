"""Historical diagnostic: Gemini 3.5 OpenAI-compatible probe reproduction.

This module is not imported by production and is not a primary experiment
entry point. It is retained only to reproduce the exact non-private contract.

The provider response is processed in memory and never persisted.  Persistent
output is limited to the existing spend ledger's sanitized reservation,
settlement and usage fields.
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

EXPECTED_FINGERPRINT = "3e7037d64fc12e4548ce65987a7cee6b02d163dc164914d988df6a043b4ac9f1"
AUTHORIZED_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
AUTHORIZED_HOST = "generativelanguage.googleapis.com"
MODEL_ID = "gemini-3.5-flash"
INPUT_RATE_USD_PER_MILLION = Decimal("1.50")
OUTPUT_RATE_USD_PER_MILLION = Decimal("9.00")
ESTIMATED_IMAGE_SAFETY_USD = Decimal("0.002000")

# Valid transparent 1x1 PNG containing no document or accounting information.
SYNTHETIC_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _safe_provider_error(raw_body: bytes) -> dict[str, str | None]:
    try:
        envelope = json.loads(raw_body.decode("utf-8", "replace"))
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if not isinstance(error, dict):
            return {"type": None, "code": None, "param": None}
        return {
            key: (str(error.get(key) or "")[:120] or None)
            for key in ("type", "code", "param")
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"type": None, "code": None, "param": None}


def _usage(envelope: object) -> tuple[dict[str, int], int | None]:
    raw = envelope.get("usage") if isinstance(envelope, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def integer(*names: str) -> int:
        for name in names:
            if raw.get(name) is not None:
                try:
                    return max(0, int(raw[name]))
                except (TypeError, ValueError):
                    return 0
        return 0

    result = {
        "input_tokens": integer("prompt_tokens", "input_tokens"),
        "output_tokens": integer("completion_tokens", "output_tokens"),
        "total_tokens": integer("total_tokens"),
    }
    if not result["total_tokens"]:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    details = raw.get("completion_tokens_details")
    thinking_tokens: int | None = None
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        try:
            thinking_tokens = max(0, int(details["reasoning_tokens"]))
        except (TypeError, ValueError):
            thinking_tokens = None
    return result, thinking_tokens


def _validate_response(envelope: object) -> tuple[bool, str | None]:
    if not isinstance(envelope, dict):
        return False, None
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return False, None
    finish_reason = str(choices[0].get("finish_reason") or "")[:80] or None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return False, finish_reason
    try:
        payload = json.loads(content)
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
    content = scrubbed["messages"][0]["content"]
    content[1]["image_url"]["url"] = "<synthetic-image>"
    estimated_input_tokens = math.ceil(len(json.dumps(scrubbed, separators=(",", ":"))) / 4)
    output_limit = int(payload["max_completion_tokens"])
    estimated = (
        Decimal(estimated_input_tokens) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(output_limit) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + ESTIMATED_IMAGE_SAFETY_USD
    )
    return estimated.quantize(Decimal("0.000001"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController,
        spend_cost_accounting_view,
    )
    from webapp.backend.services.gemini_probe_contract_audit import (
        build_corrected_openai_probe,
    )

    candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    if candidate.payload_fingerprint != EXPECTED_FINGERPRINT:
        print(json.dumps({"status": "blocked_candidate_fingerprint_mismatch"}))
        return 2
    if candidate.endpoint != AUTHORIZED_ENDPOINT:
        print(json.dumps({"status": "blocked_endpoint_mismatch"}))
        return 2
    endpoint = urlparse(candidate.endpoint)
    if endpoint.scheme != "https" or endpoint.hostname != AUTHORIZED_HOST:
        print(json.dumps({"status": "blocked_unauthorized_host"}))
        return 2
    if not args.execute:
        print(json.dumps({
            "status": "offline_candidate_valid",
            "fingerprint": candidate.payload_fingerprint,
            "provider_requests": 0,
        }, sort_keys=True))
        return 0

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("AI_VISION_API_KEY", "GEMINI_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        print(json.dumps({"status": "blocked_private_credential_unavailable"}))
        return 2

    private_root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    spend = ExperimentSpendController(private_root, "exp-document-learning-simulation")
    if spend.snapshot("A").canceled:
        spend.reauthorize_dispatch_after_operator_approval(
            phase="A",
            expected_cancel_reason="provider_usage_or_pricing_indeterminate",
            actor="experiment_operator",
            authorization_reference="gemini_35_corrected_openai_probe_2026_07_20",
        )
    estimate = _estimate(dict(candidate.payload))
    reservation = spend.reserve(
        phase="A",
        estimated_cost_usd=estimate,
        provider="gemini",
        model_id=MODEL_ID,
        profile_id="runtime-vision",
        stage="gemini_3_5_corrected_openai_capability_probe",
        document_sha256=(
            "c2153f77e11087fcb078ae38527fa83bef29791e3700e30cc87fec4405a66d0f"
        ),
        purpose="synthetic_openai_compatible_capability_probe",
    )

    network_calls = 0
    http_status: int | None = None
    envelope: object = None
    failure_code: str | None = None
    provider_error = {"type": None, "code": None, "param": None}
    started = time.perf_counter()
    try:
        raw = json.dumps(candidate.payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            candidate.endpoint,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        reservation = spend.mark_dispatched(reservation.reservation_id)
        network_calls += 1
        if network_calls != 1:
            raise RuntimeError("synthetic_probe_request_limit_reached")
        with urllib.request.urlopen(request, timeout=90) as response:
            http_status = int(response.status)
            body = response.read(100_000)
        envelope = json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        provider_error = _safe_provider_error(exc.read(4096))
        failure_code = f"http_{http_status}"
    except urllib.error.URLError:
        failure_code = "transport_unavailable"
    except (TypeError, ValueError, json.JSONDecodeError):
        failure_code = "provider_response_invalid_json"
    except Exception as exc:
        failure_code = type(exc).__name__
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    usage, thinking_tokens = _usage(envelope)
    usage_reported = bool(any(usage.values()))
    schema_valid, finish_reason = _validate_response(envelope)
    actual_cost: Decimal | None = None
    if usage_reported:
        actual_cost = (
            Decimal(usage["input_tokens"]) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
            + Decimal(usage["output_tokens"]) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))
    if http_status is not None and 200 <= http_status < 300 and not schema_valid:
        failure_code = failure_code or "synthetic_response_schema_invalid"
    reservation = spend.settle(
        reservation.reservation_id,
        actual_cost_usd=actual_cost,
        usage={**usage, "provider_request_count": network_calls},
        provider_reported_usage=usage_reported,
        failure_code=failure_code,
    )
    cost_view = spend_cost_accounting_view(reservation)

    success = bool(
        http_status is not None and 200 <= http_status < 300
        and schema_valid and usage_reported and failure_code is None
    )
    authentication = (
        "failed" if http_status in {401, 403}
        else "succeeded" if http_status is not None
        else "not_verified"
    )
    result = {
        "status": "corrected_openai_probe_passed" if success else "corrected_openai_probe_failed",
        "http_status": http_status,
        "authentication_result": authentication,
        "requested_model": MODEL_ID,
        "model_available": success,
        "image_input_accepted": success,
        "structured_output_accepted": success,
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
        "request_fingerprint": candidate.payload_fingerprint,
        "failure_code": failure_code,
        "provider_error_type": provider_error["type"],
        "provider_error_code": provider_error["code"],
        "provider_error_param": provider_error["param"],
        **cost_view.model_dump(),
        "cumulative_phase_a_charged_usd": spend.snapshot("A").cumulative_charged_usd,
        "provider_response_persisted": False,
        "credential_or_header_logged": False,
        "private_documents_transmitted": 0,
        "paired_ab_bundle_opened": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
