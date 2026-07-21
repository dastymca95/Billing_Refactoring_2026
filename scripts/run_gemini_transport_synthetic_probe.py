"""Historical diagnostic for the Gemini 3.5 compatibility transport probe.

It is retained for exact request-contract reproducibility and is not imported
by production or used by the primary experiment path.

No private packet, document, prompt, schema, manifest value, response body,
credential, or header is printed or persisted. Provider content exists only in
memory long enough for the strict supplementary schema validator to consume it.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MODEL_ID = "gemini-3.5-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
EXACT_ENDPOINT = BASE_URL + "/chat/completions"
PROFILE_ID = "runtime-vision"


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    root = (PROJECT_ROOT / "tmp" / "document-learning-simulation").resolve(strict=True)
    manifest = _one((root / "calibration").glob("phase-a-*/calibration_manifest.json"))
    inventory = _one((root / "snapshots").glob("corpus-*/inventory.jsonl"))

    # Pin the exact authorized topology without mutating the private .env.
    os.environ["AI_VISION_PROVIDER"] = "gemini"
    os.environ["AI_VISION_MODEL"] = MODEL_ID
    os.environ["AI_VISION_BASE_URL"] = BASE_URL
    os.environ["AI_VISION_INPUT_COST_USD_PER_MILLION"] = "1.50"
    os.environ["AI_VISION_OUTPUT_COST_USD_PER_MILLION"] = "9.00"
    os.environ["AI_COST_ROUTING_VERIFIED_PROFILE_IDS"] = PROFILE_ID
    os.environ["AI_VISION_ROUTING_PROFILE_ID"] = PROFILE_ID
    os.environ["INNER_VIEW_EXPERIMENT_EXECUTION_MODE"] = "CONTROLLED_EXTERNAL"
    if not str(os.environ.get("AI_VISION_API_KEY") or os.environ.get("AI_API_KEY") or "").strip():
        print(json.dumps({"status": "blocked_gemini_credential_unavailable"}))
        return 2

    from PIL import Image, ImageDraw, ImageFont
    from webapp.backend import settings
    from webapp.backend.services import ai_provider, ai_runtime_trace
    from webapp.backend.services.controlled_external_experiment import (
        ControlledDocumentCallBudget, ControlledExternalController,
        ExperimentExecutionMode, ExperimentProviderContext,
        activate_controlled_external, activate_experiment_provider_context,
        controlled_document_scope,
    )
    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController, activate_experiment_spend_gate,
    )
    from webapp.backend.services.gemini_supplementary_verification import (
        SupplementaryTarget, SupplementaryTargetType,
    )
    from webapp.backend.services.provider_capabilities import (
        ModelProfileRole, ProfileLoader,
    )
    from webapp.backend.services.supplementary_evidence_planner import (
        build_evidence_packet, build_supplementary_evidence_plan,
    )

    profiles = ProfileLoader().load()
    eligible = [
        profile for profile in profiles
        if profile.profile_id == PROFILE_ID
        and profile.provider == "gemini"
        and profile.role is ModelProfileRole.MULTIMODAL_EXTRACTION
        and profile.model_id == MODEL_ID
        and profile.enabled and profile.credentials_present
        and str(profile.base_url or "").rstrip("/") == BASE_URL
        and profile.input_cost_usd_per_million == 1.50
        and profile.output_cost_usd_per_million == 9.00
    ]
    if len(eligible) != 1:
        print(json.dumps({"status": "blocked_exact_gemini_3_5_profile_unavailable"}))
        return 2

    isolated = root / "preflight" / "gemini-3-5-supplementary-probe-v1" / "runtime"
    isolated.mkdir(parents=True, exist_ok=True)
    settings.WEBAPP_DATA_ROOT = isolated
    settings.BATCHES_ROOT = isolated / "batches"

    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    draw.text((60, 60), "SYNTHETIC DOCUMENT - NO PRIVATE DATA", fill="black", font=font)
    draw.text((60, 190), "DESCRIPTION          QTY     UNIT PRICE     AMOUNT", fill="black", font=font)
    draw.text((60, 300), "Synthetic service     1          10.00        10.00", fill="black", font=font)
    draw.text((760, 620), "SUBTOTAL 10.00", fill="black", font=font)
    draw.text((760, 690), "TAX 1.00", fill="black", font=font)
    draw.text((760, 760), "TOTAL 11.00", fill="black", font=font)
    buffer = io.BytesIO(); image.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    digest = hashlib.sha256(image_bytes).hexdigest()
    initial_facts = {
        "line_items": [{
            "source_page": 1, "raw_description": "Synthetic service",
            "quantity": "1", "unit_price": "10.00", "amount": "10.00",
        }],
        "subtotal": "10.00", "tax_amount": None, "total_amount": "11.00",
        "page_reconciliations": [{"page": 1, "printed_total": "11.00", "status": "mismatch"}],
        "warnings": ["total_mismatch"], "evidence": [],
    }
    layout = {"page_count": 1, "pages": [{"page_number": 1, "blocks": [
        {"text": "DESCRIPTION QTY UNIT PRICE AMOUNT", "bbox": [0.04, 0.18, 0.88, 0.28], "source": "synthetic"},
        {"text": "SUBTOTAL 10.00 TAX 1.00 TOTAL 11.00", "bbox": [0.52, 0.62, 0.44, 0.28], "source": "synthetic"},
    ]}]}
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1, field_name="reconciliation",
        local_trigger_codes=["synthetic_capability_probe"],
    )

    controller = ControlledExternalController(
        private_root=root, experiment_id="exp-document-learning-simulation",
        manifest_path=manifest, inventory_path=inventory,
    )
    spend = ExperimentSpendController(root, "exp-document-learning-simulation")
    before_ledger = json.loads(spend.path.read_text(encoding="utf-8"))
    before_reservations = set((before_ledger.get("reservations") or {}).keys())
    before_files = {path.resolve() for path in isolated.rglob("*") if path.is_file()}
    network_calls = 0
    contacted_endpoints: list[str] = []
    thinking_tokens: int | None = None

    original_transport = ai_provider.controlled_urlopen
    original_capture = ai_provider._capture_provider_response_metadata

    def counted_transport(request, *, timeout):
        nonlocal network_calls
        endpoint = str(getattr(request, "full_url", "") or "")
        parsed = urlparse(endpoint)
        if endpoint != EXACT_ENDPOINT or parsed.hostname != "generativelanguage.googleapis.com":
            raise RuntimeError("synthetic_probe_unauthorized_endpoint")
        network_calls += 1
        if network_calls > 1:
            raise RuntimeError("synthetic_probe_request_limit_reached")
        contacted_endpoints.append(endpoint)
        return original_transport(request, timeout=timeout)

    def capture_usage(envelope, *, payload, native_anthropic):
        nonlocal thinking_tokens
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            try: thinking_tokens = max(0, int(details.get("reasoning_tokens") or 0))
            except (TypeError, ValueError): thinking_tokens = None
        return original_capture(envelope, payload=payload, native_anthropic=native_anthropic)

    ai_provider.controlled_urlopen = counted_transport
    ai_provider._capture_provider_response_metadata = capture_usage
    started = time.perf_counter()
    status = "synthetic_gemini_3_5_supplementary_probe_failed"
    failure_code = None
    schema_valid = False
    try:
        with activate_controlled_external(controller), controlled_document_scope(
            document_sha256=digest, synthetic=True,
        ) as scope:
            provider_context = ExperimentProviderContext(
                execution_mode=ExperimentExecutionMode.CONTROLLED_EXTERNAL,
                authorized_provider="gemini", authorized_model=MODEL_ID,
                authorized_profile_id=PROFILE_ID, allowed_endpoint=EXACT_ENDPOINT,
                manifest_sha256=controller.manifest_sha256,
                document_sha256=digest, fallback_allowed=False,
                maximum_initial_calls=1, maximum_supplementary_calls=2,
                call_budget=ControlledDocumentCallBudget(maximum_initial=1, maximum_supplementary=2),
            )
            plan = build_supplementary_evidence_plan(
                opaque_document_id=scope.opaque_document_id, target=target,
                initial_facts=initial_facts, document_layout=layout,
            )
            packet = build_evidence_packet(plan, page_images={1: [data_url]})
            with activate_experiment_provider_context(provider_context), activate_experiment_spend_gate(
                spend, phase="A", pricing_version="google-gemini-standard-2026-07-20",
            ), ai_runtime_trace.operation(
                batch_id="batch_synthetic_gemini_3_5_probe",
                stage="gemini_3_5_supplementary_capability_probe",
                provider="gemini", model=MODEL_ID, profile_id=PROFILE_ID,
                media_bytes=len(image_bytes), media_pixels=1400 * 900,
            ):
                ai_provider.extract_gemini_supplementary_facts_structured(
                    initial_facts=initial_facts, target=target,
                    evidence_plan=plan, evidence_packet=packet,
                    cost_scope_id="phase-a-gemini-3-5-synthetic-probe-v1",
                    experiment_context=provider_context,
                )
        schema_valid = True
        status = "synthetic_gemini_3_5_supplementary_probe_passed"
    except ai_provider.AIProviderError as exc:
        failure_code = exc.safe_diagnostic().get("failure_code")
    except Exception as exc:  # safe class-only diagnostic; never serialize provider bodies
        failure_code = type(exc).__name__
    finally:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        ai_provider.controlled_urlopen = original_transport
        ai_provider._capture_provider_response_metadata = original_capture

    ledger = json.loads(spend.path.read_text(encoding="utf-8"))
    new_rows = [
        row for key, row in (ledger.get("reservations") or {}).items()
        if key not in before_reservations
    ]
    settled = [row for row in new_rows if row.get("status") in {"settled", "failed"}]
    usage = (settled[0].get("usage") or {}) if len(settled) == 1 else {}
    after_files = {path.resolve() for path in isolated.rglob("*") if path.is_file()}
    created_files = after_files - before_files
    # Only categorical trace/spend metadata is written. The parsed provider
    # observation is never passed to a serializer or file writer.
    response_persisted = False
    snapshot = spend.snapshot("A")
    result = {
        "status": status,
        "authentication": "succeeded" if schema_valid else "failed",
        "model_id": MODEL_ID,
        "model_available": schema_valid,
        "image_input_accepted": schema_valid,
        "structured_output_accepted": schema_valid,
        "schema_valid": schema_valid,
        "latency_ms": latency_ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": thinking_tokens,
        "thinking_tokens_separately_reported": thinking_tokens is not None,
        "actual_cost_usd": settled[0].get("actual_cost_usd") if len(settled) == 1 else None,
        "cumulative_phase_a_spend_usd": snapshot.cumulative_charged_usd,
        "provider_requests": network_calls,
        "spend_reservations": len(new_rows),
        "settled_usage_records": len(settled),
        "endpoint_contacted": contacted_endpoints[0] if len(contacted_endpoints) == 1 else None,
        "other_endpoints_contacted": 0,
        "retry_count": max(0, network_calls - 1),
        "fallback_attempts": 0,
        "private_documents_transmitted": 0,
        "paired_ab_packets_opened_or_dispatched": 0,
        "provider_response_persisted": response_persisted,
        "credential_or_header_logged": False,
        "created_safe_metadata_files": len(created_files),
        "failure_code": failure_code,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        schema_valid and network_calls == 1 and len(settled) == 1
        and not response_persisted and failure_code is None
    ) else 1


def _one(values):
    rows = list(values)
    if len(rows) != 1:
        raise RuntimeError("exactly_one_frozen_experiment_artifact_required")
    return rows[0]


if __name__ == "__main__":
    raise SystemExit(main())
