from __future__ import annotations

import json
from decimal import Decimal

import pytest

from webapp.backend.services.gemini_probe_contract_audit import (
    decode_provider_transport_response,
    parse_provider_transport_response,
    parse_provider_transport_response_with_audit,
)
from webapp.backend.services.gemini_supplementary_verification import (
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
    normalize_supplementary_provider_payload,
    parse_supplementary_response,
    validate_observation_crop_references,
)


def _target(
    target_type: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=target_type,
        page_number=1,
        field_name="synthetic_field",
        local_trigger_codes=["synthetic_offline_contract_test"],
    )


def _resolved_payload() -> dict:
    return {
        "target_type": "total_mismatch",
        "observed_candidate_value": {
            "resolution_kind": "total_amount",
            "field_name": "total_amount",
            "raw_value": "synthetic-value",
            "line_item": None,
        },
        "raw_visible_text": "synthetic-visible-evidence",
        "page_number": 1,
        "evidence_reference": {
            "page_number": 1,
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "crop_id": "crop-summary",
            "crop_role": "summary",
        },
        "confidence": 0.9,
        "contradiction_flag": False,
        "unresolved_flag": False,
        "warnings": [],
        "visibility_status": "visible",
        "observed_candidates": [],
        "financial_components": None,
    }


def _unresolved_payload() -> dict:
    payload = _resolved_payload()
    payload.update({
        "observed_candidate_value": None,
        "raw_visible_text": None,
        "evidence_reference": None,
        "confidence": None,
        "unresolved_flag": True,
        "visibility_status": "not_visible",
        "warnings": ["synthetic_unresolved"],
    })
    return payload


def _envelope(payload: object) -> str:
    return json.dumps({"payload_json": json.dumps(payload)})


def _assert_failure(raw: str, expected: str):
    with pytest.raises(SupplementaryVerificationError) as raised:
        parse_provider_transport_response(raw, target=_target())
    assert raised.value.failure_code == expected
    assert raised.value.diagnostics is not None
    assert raised.value.diagnostics.failure_code == expected
    return raised.value.diagnostics


def test_valid_envelope_payload_is_decoded_exactly_once_and_validated():
    parsed = parse_provider_transport_response_with_audit(
        _envelope(_resolved_payload()), target=_target(),
    )
    assert parsed.observation.unresolved_flag is False
    assert parsed.diagnostics.decoding_count == 1
    assert parsed.diagnostics.payload_parse_result == "object_decoded_once"
    assert parsed.diagnostics.failure_code is None


def test_double_encoded_payload_is_detected_but_never_repeatedly_decoded():
    raw = json.dumps({"payload_json": json.dumps(json.dumps(_resolved_payload()))})
    diagnostics = _assert_failure(raw, "supplementary_payload_double_encoded")
    assert diagnostics.decoding_count == 1
    assert diagnostics.payload_parse_result == "double_encoded_object_detected"


@pytest.mark.parametrize(
    ("raw", "expected", "category"),
    [
        (json.dumps({"payload_json": None}), "supplementary_payload_json_missing", "payload_json_missing"),
        (json.dumps({"payload_json": ""}), "supplementary_payload_json_missing", "payload_json_empty"),
        (json.dumps({"payload_json": {}}), "supplementary_field_type_invalid", "payload_json_unexpected_dict"),
        (json.dumps({"payload_json": "{"}), "supplementary_payload_json_malformed", "malformed_json"),
        (
            json.dumps({"payload_json": "{} {}"}),
            "supplementary_payload_json_malformed",
            "multiple_or_trailing_content",
        ),
        (
            json.dumps({"payload_json": "explanation {}"}),
            "supplementary_payload_json_malformed",
            "malformed_json",
        ),
    ],
)
def test_payload_json_invalid_representations_fail_closed(raw, expected, category):
    diagnostics = _assert_failure(raw, expected)
    assert diagnostics.payload_parse_result == category


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not-json", "supplementary_envelope_invalid"),
        (json.dumps([]), "supplementary_envelope_invalid"),
        (json.dumps({}), "supplementary_payload_json_missing"),
        (
            json.dumps({"payload_json": json.dumps(_resolved_payload()), "extra": True}),
            "supplementary_envelope_invalid",
        ),
    ],
)
def test_invalid_envelope_shapes_are_classified_specifically(raw, expected):
    _assert_failure(raw, expected)


def test_numeric_strings_blank_nullable_and_known_camel_case_aliases_normalize_losslessly():
    payload = _resolved_payload()
    payload["pageNumber"] = "1"
    del payload["page_number"]
    payload["rawVisibleText"] = "  "
    del payload["raw_visible_text"]
    payload["observedCandidateValue"] = payload.pop("observed_candidate_value")
    payload["evidenceReference"] = payload.pop("evidence_reference")
    payload["contradictionFlag"] = payload.pop("contradiction_flag")
    payload["unresolvedFlag"] = payload.pop("unresolved_flag")
    payload["visibilityStatus"] = "VISIBLE"
    del payload["visibility_status"]
    payload["observedCandidates"] = payload.pop("observed_candidates")
    payload["financialComponents"] = payload.pop("financial_components")
    payload["observedCandidateValue"]["resolutionKind"] = "TOTAL-AMOUNT"
    del payload["observedCandidateValue"]["resolution_kind"]
    payload["observedCandidateValue"]["fieldName"] = payload["observedCandidateValue"].pop("field_name")
    payload["observedCandidateValue"]["rawValue"] = payload["observedCandidateValue"].pop("raw_value")
    payload["observedCandidateValue"]["lineItem"] = payload["observedCandidateValue"].pop("line_item")
    parsed = parse_provider_transport_response_with_audit(
        _envelope(payload), target=_target(),
    )
    assert parsed.observation.page_number == 1
    assert parsed.observation.raw_visible_text is None
    assert parsed.observation.observed_candidate_value.resolution_kind.value == "total_amount"
    assert "alias:page_number" in parsed.diagnostics.normalization_actions
    assert "enum:resolution_kind" in parsed.diagnostics.normalization_actions


def test_financial_numeric_strings_normalize_to_decimal_without_guessing():
    payload = _resolved_payload()
    payload["financial_components"] = {
        "subtotal": "1,234.50",
        "tax": None,
        "fees": "",
        "credits": None,
        "discounts": None,
        "previous_balance": None,
        "payments": None,
        "deposits": None,
        "current_charges": None,
        "amount_due": None,
        "line_item_sum": None,
        "total_label": None,
        "page_continuation_status": None,
        "evidence_references": [],
    }
    original = json.loads(json.dumps(payload))
    result = normalize_supplementary_provider_payload(payload)
    components = result.normalized_payload["financial_components"]
    assert components["subtotal"] == Decimal("1234.50")
    assert components["fees"] is None
    assert "numeric_string:subtotal" in result.diagnostics.normalization_actions
    assert payload == original


def test_nullable_unknown_is_valid_but_remains_unresolved_and_non_resolving():
    parsed = parse_provider_transport_response(
        _envelope(_unresolved_payload()), target=_target(),
    )
    assert parsed.unresolved_flag is True
    assert parsed.observed_candidate_value is None


def test_missing_required_field_has_specific_safe_diagnostics():
    payload = _resolved_payload()
    del payload["unresolved_flag"]
    diagnostics = _assert_failure(
        _envelope(payload), "supplementary_required_field_missing",
    )
    assert diagnostics.missing_required_fields == ("unresolved_flag",)
    assert "synthetic-visible-evidence" not in diagnostics.model_dump_json()


def test_invalid_enum_reports_category_without_echoing_bad_value():
    payload = _resolved_payload()
    payload["visibility_status"] = "synthetic-private-looking-invalid-value"
    diagnostics = _assert_failure(_envelope(payload), "supplementary_enum_invalid")
    serialized = diagnostics.model_dump_json()
    assert diagnostics.invalid_enum_categories == ("SupplementaryVisibilityStatus",)
    assert "synthetic-private-looking-invalid-value" not in serialized


def test_evidence_reference_wrong_shape_is_specific():
    payload = _resolved_payload()
    payload["evidence_reference"]["bbox"] = {"unexpected": "shape"}
    diagnostics = _assert_failure(
        _envelope(payload), "supplementary_evidence_reference_invalid",
    )
    assert diagnostics.evidence_reference_validation == "invalid"


def test_candidate_object_is_not_silently_coerced_to_candidate_array():
    payload = _resolved_payload()
    payload["observed_candidates"] = {
        "raw_candidate": "synthetic",
        "adjacent_visible_label": None,
        "candidate_type": "unknown",
        "evidence_reference": None,
        "confidence": None,
        "unresolved": True,
    }
    _assert_failure(_envelope(payload), "supplementary_field_type_invalid")


def test_unexpected_field_is_hashed_and_never_persisted_verbatim():
    payload = _resolved_payload()
    payload["synthetic_secret_named_field"] = "synthetic-secret-value"
    diagnostics = _assert_failure(
        _envelope(payload), "supplementary_internal_contract_invalid",
    )
    serialized = diagnostics.model_dump_json()
    assert diagnostics.unexpected_field_name_hashes
    assert "synthetic_secret_named_field" not in serialized
    assert "synthetic-secret-value" not in serialized


def test_contradiction_with_valid_evidence_stays_explicit_and_unaccepted_by_contract():
    payload = _resolved_payload()
    payload["contradiction_flag"] = True
    observation = parse_supplementary_response(json.dumps(payload), target=_target())
    assert observation.contradiction_flag is True
    assert observation.unresolved_flag is False


def test_unknown_crop_role_packet_hash_and_order_fail_without_touching_observation():
    observation = parse_provider_transport_response(
        _envelope(_resolved_payload()), target=_target(),
    )
    planned = {"crop-summary": {"role": "summary", "ordinal": 0}}
    validate_observation_crop_references(
        observation,
        allowed_crop_ids={"crop-summary"},
        planned_crops=planned,
        expected_packet_sha256="a" * 64,
        actual_packet_sha256="a" * 64,
    )
    with pytest.raises(SupplementaryVerificationError, match="supplementary_unplanned_crop_reference"):
        changed = observation.model_copy(deep=True)
        changed.evidence_reference.crop_id = "arbitrary-model-crop"
        validate_observation_crop_references(
            changed, allowed_crop_ids={"crop-summary"}, planned_crops=planned,
        )
    with pytest.raises(SupplementaryVerificationError, match="supplementary_evidence_reference_invalid"):
        validate_observation_crop_references(
            observation,
            allowed_crop_ids={"crop-summary"},
            planned_crops={"crop-summary": {"role": "different", "ordinal": 0}},
        )
    with pytest.raises(SupplementaryVerificationError, match="supplementary_evidence_reference_invalid"):
        validate_observation_crop_references(
            observation,
            allowed_crop_ids={"crop-summary"},
            planned_crops=planned,
            expected_packet_sha256="a" * 64,
            actual_packet_sha256="b" * 64,
        )


def test_diagnostics_preserve_only_hash_length_shape_and_safe_contract_names():
    envelope = _envelope(_resolved_payload())
    decoded = decode_provider_transport_response(envelope)
    serialized = decoded.diagnostics.model_dump_json()
    assert decoded.diagnostics.payload_byte_length > 0
    assert len(decoded.diagnostics.payload_sha256) == 64
    assert "synthetic-visible-evidence" not in serialized
    assert "synthetic-value" not in serialized
