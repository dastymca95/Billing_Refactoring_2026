from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from webapp.backend.services.experiment_spend_controller import (
    ExperimentPhase,
    SpendReservation,
    spend_cost_accounting_view,
)
from webapp.backend.services.gemini_probe_contract_audit import (
    ContractSurface,
    RequestContractError,
    audit_schema,
    build_corrected_openai_probe,
    build_native_gemini_probe,
    current_strict_internal_schema,
    diff_failed_openai_request,
    gemini_openai_generation_controls,
    minimal_probe_response_format,
    parse_provider_transport_response,
    provider_compatible_transport_schema,
    sanitize_request_shape,
    validate_image_data_url,
    validate_openai_probe_payload,
)
from webapp.backend.services.gemini_supplementary_verification import (
    SupplementaryVerificationError,
    SupplementaryTarget,
    SupplementaryTargetType,
)


# Valid 1x1 transparent PNG. It contains no private or accounting content.
SYNTHETIC_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _target() -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1,
        field_name="reconciliation",
        local_trigger_codes=["synthetic_capability_probe"],
    )


def _strict_unresolved_payload() -> dict:
    return {
        "target_type": "total_mismatch",
        "observed_candidate_value": None,
        "raw_visible_text": None,
        "page_number": 1,
        "evidence_reference": None,
        "confidence": None,
        "contradiction_flag": False,
        "unresolved_flag": True,
        "warnings": ["synthetic_unresolved"],
        "visibility_status": "not_visible",
        "observed_candidates": [],
        "financial_components": None,
    }


@pytest.mark.parametrize("effort", ["none", "medium", "high", "think", "thinking_budget"])
def test_unsupported_thinking_controls_fail_closed(effort):
    with pytest.raises(RequestContractError, match="unsupported_gemini_reasoning_effort"):
        gemini_openai_generation_controls(reasoning_effort=effort)


@pytest.mark.parametrize("effort", [None, "minimal", "low"])
def test_omitted_minimal_and_low_reasoning_controls_are_supported(effort):
    controls = gemini_openai_generation_controls(
        reasoning_effort=effort, max_output_tokens=256,
    )
    assert controls["max_completion_tokens"] == 256
    assert controls.get("reasoning_effort") == effort
    assert "temperature" not in controls
    assert "max_tokens" not in controls


def test_provider_transport_schema_is_small_but_local_contract_remains_strict():
    strict = current_strict_internal_schema(_target())
    wire = provider_compatible_transport_schema()
    strict_audit = audit_schema(strict["json_schema"]["schema"])
    wire_audit = audit_schema(wire["json_schema"]["schema"])
    assert strict_audit.property_count > wire_audit.property_count
    assert strict_audit.maximum_depth > wire_audit.maximum_depth
    assert strict_audit.object_schema_without_additional_properties_count > 0
    assert wire_audit.object_schema_without_additional_properties_count == 0
    assert wire_audit.complexity_risk == "low"

    envelope = json.dumps({"payload_json": json.dumps(_strict_unresolved_payload())})
    observation = parse_provider_transport_response(envelope, target=_target())
    assert observation.unresolved_flag is True
    with pytest.raises(SupplementaryVerificationError):
        parse_provider_transport_response(
            json.dumps({"payload_json": json.dumps({"target_type": "total_mismatch"})}),
            target=_target(),
        )


def test_openai_and_native_payload_formats_remain_distinct():
    openai_candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    native_candidate = build_native_gemini_probe(SYNTHETIC_PNG)
    assert openai_candidate.endpoint.endswith("/openai/chat/completions")
    assert native_candidate.endpoint.endswith(":generateContent")
    assert set(openai_candidate.payload) == {
        "model", "messages", "response_format", "max_completion_tokens",
    }
    assert set(native_candidate.payload) == {"contents", "generationConfig"}
    with pytest.raises(RequestContractError, match="unsupported_openai_probe_parameter"):
        validate_openai_probe_payload(native_candidate.payload)


def test_image_data_url_validation_accepts_valid_and_rejects_invalid():
    candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    image_url = candidate.payload["messages"][0]["content"][1]["image_url"]["url"]
    mime, content = validate_image_data_url(image_url)
    assert mime == "image/png"
    assert content == SYNTHETIC_PNG
    for invalid in ("", "https://example.invalid/image.png", "data:image/png;base64,***"):
        with pytest.raises(RequestContractError):
            validate_image_data_url(invalid)


@pytest.mark.parametrize("field,value", [
    ("temperature", 0),
    ("max_tokens", 256),
    ("extra_body", {"thinking": {"budget": 0}}),
    ("thinking_level", "low"),
])
def test_unsupported_parameters_are_rejected(field, value):
    candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    payload = dict(candidate.payload)
    payload[field] = value
    with pytest.raises(RequestContractError, match="unsupported_openai_probe_parameter"):
        validate_openai_probe_payload(payload)


def test_failed_request_without_usage_is_safety_charge_not_actual_provider_cost():
    reservation = SpendReservation(
        reservation_id="dlr_synthetic",
        phase=ExperimentPhase.A,
        provider="gemini",
        model_id="gemini-3.5-flash",
        profile_id="runtime-vision",
        stage="synthetic_probe",
        estimated_cost_usd="0.023371",
        status="failed",
        actual_cost_usd=None,
        charged_cost_usd="0.023371",
        provider_reported_usage=False,
        failure_code="http_400",
        created_at=datetime.now(timezone.utc),
        settled_at=datetime.now(timezone.utc),
    )
    view = spend_cost_accounting_view(reservation)
    assert view.estimated_reserved_cost_usd == "0.023371"
    assert view.estimated_safety_charge_usd == "0.023371"
    assert view.actual_provider_cost_usd is None
    assert view.usage_reported is False
    assert view.settlement_status == "failed_without_usage_safety_charged"


def test_sanitized_diagnostics_contain_hashes_not_prompt_or_image_data():
    candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    shape = sanitize_request_shape(
        candidate.payload, surface=ContractSurface.OPENAI_COMPATIBLE,
    )
    serialized = shape.model_dump_json()
    assert shape.image_byte_lengths == (len(SYNTHETIC_PNG),)
    assert len(shape.image_sha256s[0]) == 64
    assert "data:image" not in serialized
    assert "Identify the non-private" not in serialized
    assert "authorization" not in serialized.casefold()
    assert "api_key" not in serialized.casefold()


def test_failed_shape_diff_identifies_legacy_controls_without_claiming_model_unavailable():
    candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    failed = dict(candidate.payload)
    failed.pop("max_completion_tokens")
    failed.pop("reasoning_effort", None)
    failed["max_tokens"] = 2048
    failed["temperature"] = 0
    diff = diff_failed_openai_request(failed)
    assert diff.unsupported_or_undocumented_fields == ("max_tokens", "temperature")
    assert "temperature_forced_on_reasoning_model" in diff.conflicting_fields
    assert diff.model_surface_availability == "not_provable_offline"


def test_audit_does_not_open_paired_bundle_or_call_network(monkeypatch):
    import pathlib
    import urllib.request

    original_open = pathlib.Path.open
    protected_bundle_name = "paired-" + "bundle-v1"

    def guarded_open(path, *args, **kwargs):
        if protected_bundle_name in str(path):
            raise AssertionError("paired bundle must remain unopened")
        return original_open(path, *args, **kwargs)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("offline audit must not call a provider")

    monkeypatch.setattr(pathlib.Path, "open", guarded_open)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    openai_candidate = build_corrected_openai_probe(SYNTHETIC_PNG)
    native_candidate = build_native_gemini_probe(SYNTHETIC_PNG)
    sanitize_request_shape(
        openai_candidate.payload, surface=ContractSurface.OPENAI_COMPATIBLE,
    )
    assert openai_candidate.maximum_requests == native_candidate.maximum_requests == 1
    assert openai_candidate.retries == native_candidate.retries == 0
    assert openai_candidate.fallback is native_candidate.fallback is False


def test_response_format_inventory_has_no_composition_keywords_for_minimal_probe():
    response_format = minimal_probe_response_format(ContractSurface.OPENAI_COMPATIBLE)
    audit = audit_schema(response_format["json_schema"]["schema"])
    assert audit.one_of_count == audit.any_of_count == audit.all_of_count == 0
    assert audit.recursive_reference_count == audit.pattern_count == 0
    assert audit.complexity_risk == "low"
