"""Historical diagnostic for the minimal-thinking Flash Preview probe.

It is retained for the exact successful synthetic contract and is not imported
by production or used by the primary experiment path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

from scripts.run_gemini_3_flash_preview_candidate_probe import (  # noqa: E402
    AUTHORIZED_HOST,
    GENERATION_ENDPOINT,
    INPUT_RATE_USD_PER_MILLION,
    MODEL_ID,
    OUTPUT_RATE_USD_PER_MILLION,
    SYNTHETIC_PNG,
    _safe_error,
    _usage,
    _validate_response,
)


MAX_OUTPUT_TOKENS = 2048
ESTIMATED_IMAGE_SAFETY_USD = Decimal("0.002000")


def _payload() -> dict:
    from webapp.backend.services.gemini_probe_contract_audit import (
        build_native_gemini_probe,
    )

    candidate = build_native_gemini_probe(SYNTHETIC_PNG)
    payload = json.loads(json.dumps(candidate.payload))
    config = payload["generationConfig"]
    config["thinkingConfig"] = {"thinkingLevel": "minimal"}
    config["maxOutputTokens"] = MAX_OUTPUT_TOKENS
    return payload


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(
        GENERATION_ENDPOINT.encode("ascii") + b"\n" + canonical
    ).hexdigest()


def _estimate(payload: dict) -> Decimal:
    scrubbed = json.loads(json.dumps(payload))
    scrubbed["contents"][0]["parts"][1]["inlineData"]["data"] = "<synthetic-image>"
    input_estimate = (
        len(json.dumps(scrubbed, separators=(",", ":")).encode("utf-8")) + 3
    ) // 4
    return (
        Decimal(input_estimate) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(MAX_OUTPUT_TOKENS) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + ESTIMATED_IMAGE_SAFETY_USD
    ).quantize(Decimal("0.000001"))


def _actual_cost(usage: dict[str, int], thinking_tokens: int | None) -> Decimal:
    billed_output = usage["output_tokens"] + int(thinking_tokens or 0)
    return (
        Decimal(usage["input_tokens"]) * INPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
        + Decimal(billed_output) * OUTPUT_RATE_USD_PER_MILLION / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


def _validate_contract(payload: dict) -> bool:
    parsed = urlparse(GENERATION_ENDPOINT)
    config = payload.get("generationConfig", {})
    parts = payload.get("contents", [{}])[0].get("parts", [])
    image_parts = [part for part in parts if isinstance(part, dict) and "inlineData" in part]
    forbidden = {"temperature", "topP", "topK", "thinkingBudget"}
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == AUTHORIZED_HOST
        and len(image_parts) == 1
        and config.get("thinkingConfig") == {"thinkingLevel": "minimal"}
        and config.get("maxOutputTokens") == MAX_OUTPUT_TOKENS
        and config.get("responseMimeType") == "application/json"
        and isinstance(config.get("responseJsonSchema"), dict)
        and not forbidden.intersection(config)
        and set(payload) == {"contents", "generationConfig"}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController,
        spend_cost_accounting_view,
    )
    from webapp.backend.services.supplementary_ab_arm_b_candidate import (
        PreparedArmBExecutionOverlay,
        calculate_candidate_budget,
    )

    payload = _payload()
    fingerprint = _fingerprint(payload)
    if not _validate_contract(payload):
        print(json.dumps({"result": "NATIVE REQUEST FAILED", "category": "offline_contract_invalid"}))
        return 2
    if not args.execute:
        print(json.dumps({
            "result": "OFFLINE_PREFLIGHT_VALID",
            "request_fingerprint": fingerprint,
            "thinking_level": "minimal",
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "provider_requests": 0,
            "private_bundle_opened": False,
        }, indent=2, sort_keys=True))
        return 0

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = next((
        str(os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEY", "AI_VISION_API_KEY", "AI_API_KEY")
        if str(os.environ.get(name) or "").strip()
    ), "")
    if not api_key:
        print(json.dumps({"result": "NATIVE REQUEST FAILED", "category": "credential_unavailable"}))
        return 2

    private_root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    spend = ExperimentSpendController(private_root, "exp-document-learning-simulation")
    before = spend.snapshot("A")
    if before.canceled:
        print(json.dumps({"result": "NATIVE REQUEST FAILED", "category": "spend_ledger_canceled"}))
        return 2

    reservation = spend.reserve(
        phase="A",
        estimated_cost_usd=_estimate(payload),
        provider="gemini",
        model_id=MODEL_ID,
        profile_id="runtime-vision-native-synthetic-minimal",
        stage="gemini_3_flash_preview_minimal_capability_probe",
        document_sha256=hashlib.sha256(SYNTHETIC_PNG).hexdigest(),
        purpose="synthetic_native_minimal_thinking_capability_probe",
    )

    http_status: int | None = None
    envelope: object = None
    failure_code: str | None = None
    safe_category: str | None = None
    safe_status: str | None = None
    safe_code: int | None = None
    started = time.perf_counter()
    provider_requests = 0
    try:
        request = urllib.request.Request(
            GENERATION_ENDPOINT,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        reservation = spend.mark_dispatched(reservation.reservation_id)
        provider_requests = 1
        with urllib.request.urlopen(request, timeout=120) as response:
            http_status = int(response.status)
            raw = response.read(100_000)
        envelope = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        safe_status, safe_code, safe_category = _safe_error(exc.read(4096))
        failure_code = f"http_{http_status}"
    except urllib.error.URLError:
        safe_category = "native_transport_unavailable"
        failure_code = "native_transport_unavailable"
    except (TypeError, ValueError, json.JSONDecodeError):
        safe_category = "native_response_invalid_json"
        failure_code = "native_response_invalid_json"
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    usage, thinking_tokens = _usage(envelope)
    usage_reported = bool(any(usage.values()))
    schema_valid, finish_reason = _validate_response(envelope)
    returned_model = str(envelope.get("modelVersion") or "") if isinstance(envelope, dict) else ""
    exact_model = returned_model == MODEL_ID
    transport_accepted = bool(http_status == 200 and exact_model)
    passed = bool(
        transport_accepted
        and finish_reason != "MAX_TOKENS"
        and schema_valid
        and usage_reported
    )
    if passed:
        result_category = "CAPABILITY PROBE PASSED"
    elif transport_accepted and (finish_reason == "MAX_TOKENS" or not schema_valid):
        result_category = "OUTPUT STILL TRUNCATED"
        failure_code = (
            "native_output_limit_before_schema_completion"
            if finish_reason == "MAX_TOKENS"
            else "native_incomplete_structured_json"
        )
        safe_category = failure_code
    else:
        result_category = "NATIVE REQUEST FAILED"
        failure_code = failure_code or (
            "native_model_version_mismatch" if http_status == 200 and not exact_model
            else "native_usage_unavailable" if http_status == 200 and not usage_reported
            else "native_capability_request_failed"
        )
        safe_category = safe_category or failure_code

    actual_cost = _actual_cost(usage, thinking_tokens) if usage_reported else None
    reservation = spend.settle(
        reservation.reservation_id,
        actual_cost_usd=actual_cost,
        usage={**usage, "provider_request_count": provider_requests},
        provider_reported_usage=usage_reported,
        failure_code=None if passed else failure_code,
    )
    cost_view = spend_cost_accounting_view(reservation)

    prepared_contract_written = False
    if passed:
        current_spend = Decimal(spend.snapshot("A").cumulative_charged_usd)
        budget = calculate_candidate_budget(
            expected_input_tokens_per_arm=27_507,
            expected_output_tokens_per_arm=3_719,
            phase_a_cumulative_spend_usd=current_spend,
        )
        overlay = PreparedArmBExecutionOverlay(
            technically_eligible=True,
            budget=budget,
        )
        safe_path = private_root / "telemetry" / "prepared_arm_b_gemini_3_flash_preview.json"
        safe_path.write_text(
            json.dumps(overlay.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prepared_contract_written = True

    result = {
        "result": result_category,
        "http_status": http_status,
        "authentication_result": "succeeded" if http_status == 200 else "failed_or_unavailable",
        "requested_model": MODEL_ID,
        "exact_model_id_match": exact_model,
        "image_input_accepted": transport_accepted,
        "structured_output_transport_accepted": transport_accepted,
        "finish_reason": finish_reason,
        "complete_json_object": schema_valid,
        "schema_valid": schema_valid,
        "thinking_tokens": thinking_tokens,
        "visible_output_tokens": usage["output_tokens"] if usage_reported else None,
        "input_tokens": usage["input_tokens"] if usage_reported else None,
        "usage_reported": usage_reported,
        "latency_ms": latency_ms,
        "provider_requests": provider_requests,
        "retry_count": 0,
        "fallback_attempts": 0,
        "endpoint_host": AUTHORIZED_HOST if provider_requests else None,
        "safe_failure_category": safe_category,
        "safe_error_status": safe_status,
        "safe_error_code": safe_code,
        "request_fingerprint": fingerprint,
        **cost_view.model_dump(),
        "cumulative_phase_a_charged_usd": spend.snapshot("A").cumulative_charged_usd,
        "provider_response_persisted": False,
        "credential_or_header_persisted": False,
        "private_documents_transmitted": 0,
        "paired_bundle_opened": False,
        "paired_bundle_requests": 0,
        "prepared_execution_contract_written": prepared_contract_written,
        "private_ab_execution_authorized": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
