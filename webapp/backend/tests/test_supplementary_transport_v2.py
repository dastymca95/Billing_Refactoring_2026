from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from scripts.run_phase_a_paired_supplementary_ab import _request_payload
from webapp.backend.services.gemini_probe_contract_audit import (
    audit_schema,
    parse_provider_transport_response,
    provider_compatible_transport_schema,
)
from webapp.backend.services.gemini_supplementary_transport import (
    SUPPLEMENTARY_TRANSPORT_V1_VERSION,
    SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    parse_supplementary_transport_v2_response_with_audit,
    supplementary_transport_v2_family_sha256,
    supplementary_transport_v2_response_format,
    supplementary_transport_v2_schema_sha256,
)
from webapp.backend.services.gemini_supplementary_verification import (
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
    supplementary_response_format,
)
from webapp.backend.services.supplementary_ab_experiment import (
    ExperimentArm,
    canonical_json_bytes,
)


PLANNED_CROPS = {
    "crop-summary": {"role": "summary", "ordinal": 0},
    "crop-identity": {"role": "identity", "ordinal": 1},
}


def _target(
    target_type: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=target_type,
        page_number=1,
        field_name="synthetic_field",
        local_trigger_codes=["synthetic_v2_matrix"],
    )


def _base_payload(
    *,
    target_type: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> dict:
    return {
        "contract_version": SUPPLEMENTARY_TRANSPORT_V2_VERSION,
        "target_type": target_type.value,
        "visibility_status": "visible",
        "unresolved_flag": False,
        "contradiction_flag": False,
        "page_number": 1,
        "confidence": 0.9,
        "raw_visible_text": {
            "value": "synthetic visible evidence",
            "evidence_refs": [{
                "crop_id": "crop-summary", "evidence_kind": "visible_label",
            }],
            "confidence": 0.9,
            "visibility_status": "visible",
        },
        "observed_candidate_value": {
            "resolution_kind": "total_amount",
            "field_name": "total_amount",
            "value": "123.45",
            "source_page": None,
            "section_header": None,
            "row_label": None,
            "location_candidate": None,
            "activity": None,
            "raw_description": None,
            "quantity": None,
            "unit_price": None,
            "amount": None,
            "tax": None,
            "evidence_refs": [{
                "crop_id": "crop-summary",
                "evidence_kind": "primary_observation",
            }],
            "confidence": 0.9,
            "visibility_status": "visible",
        },
        "observed_candidates": [],
        "financial_components": [{
            "component_type": "amount_due",
            "raw_value": "123.45",
            "evidence_refs": [{
                "crop_id": "crop-summary",
                "evidence_kind": "financial_component",
            }],
            "confidence": 0.9,
            "visibility_status": "visible",
        }],
        "visible_labels": [],
        "contradiction_observations": [],
        "warnings": [],
    }


def _unresolved_payload(
    *,
    target_type: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> dict:
    payload = _base_payload(target_type=target_type)
    payload.update({
        "visibility_status": "not_visible",
        "unresolved_flag": True,
        "page_number": None,
        "confidence": None,
        "raw_visible_text": {
            "value": None,
            "evidence_refs": [],
            "confidence": None,
            "visibility_status": "not_visible",
        },
        "observed_candidate_value": {
            "resolution_kind": "none",
            "field_name": None,
            "value": None,
            "source_page": None,
            "section_header": None,
            "row_label": None,
            "location_candidate": None,
            "activity": None,
            "raw_description": None,
            "quantity": None,
            "unit_price": None,
            "amount": None,
            "tax": None,
            "evidence_refs": [],
            "confidence": None,
            "visibility_status": "not_visible",
        },
        "observed_candidates": [],
        "financial_components": [],
        "visible_labels": [],
        "contradiction_observations": [],
        "warnings": [],
    })
    return payload


def _parse(payload: dict, target: SupplementaryTarget | None = None):
    target_value = payload.get("target_type", payload.get("targetType"))
    chosen = target or _target(SupplementaryTargetType(target_value))
    return parse_supplementary_transport_v2_response_with_audit(
        json.dumps(payload), target=chosen, planned_crops=PLANNED_CROPS,
    )


def _failure(payload: dict, expected: str, target: SupplementaryTarget | None = None):
    with pytest.raises(SupplementaryVerificationError) as raised:
        _parse(payload, target=target)
    assert raised.value.failure_code == expected
    assert raised.value.diagnostics is not None
    assert raised.value.diagnostics.failure_code == expected
    return raised.value.diagnostics


def _identity_payload(*, ambiguous: bool = False) -> dict:
    target_type = SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY
    payload = _base_payload(target_type=target_type)
    payload["observed_candidate_value"].update({
        "resolution_kind": "scalar",
        "field_name": "invoice_number",
        "value": "SYN-100",
        "evidence_refs": [{
            "crop_id": "crop-identity",
            "evidence_kind": "primary_observation",
        }],
    })
    payload["financial_components"] = []
    payload["observed_candidates"] = [{
        "value": "SYN-100",
        "adjacent_label": "Invoice",
        "candidate_type": "invoice_number",
        "evidence_refs": [{
            "crop_id": "crop-identity", "evidence_kind": "identity_candidate",
        }],
        "confidence": 0.8,
        "visibility_status": "ambiguous" if ambiguous else "visible",
    }, {
        "value": "SYN-10O",
        "adjacent_label": "Invoice",
        "candidate_type": "invoice_number",
        "evidence_refs": [{
            "crop_id": "crop-identity", "evidence_kind": "identity_candidate",
        }],
        "confidence": 0.6,
        "visibility_status": "ambiguous",
    }]
    if ambiguous:
        payload["visibility_status"] = "ambiguous"
        payload["unresolved_flag"] = True
    return payload


def test_v2_schema_is_direct_bounded_and_shared_by_both_model_profiles():
    target = _target()
    flash_lite = supplementary_transport_v2_response_format(
        target, planned_crops=PLANNED_CROPS,
    )
    flash_preview = supplementary_transport_v2_response_format(
        target, planned_crops=PLANNED_CROPS,
    )
    assert flash_lite == flash_preview
    schema = flash_lite["json_schema"]["schema"]
    assert "payload_json" not in schema["properties"]
    assert set(schema["properties"]) == {
        "contract_version", "target_type", "visibility_status",
        "unresolved_flag", "contradiction_flag", "page_number", "confidence",
        "raw_visible_text", "observed_candidate_value", "observed_candidates",
        "financial_components", "visible_labels", "contradiction_observations",
        "warnings",
    }
    audit = audit_schema(schema)
    assert audit.one_of_count == audit.any_of_count == audit.all_of_count == 0
    assert audit.recursive_reference_count == 0
    assert audit.object_schema_without_additional_properties_count == 0
    assert audit.property_count < 80
    assert len(supplementary_transport_v2_schema_sha256(
        target, planned_crops=PLANNED_CROPS,
    )) == 64
    assert supplementary_transport_v2_family_sha256() == (
        "7143a627faf5ef0ffd969c3229fb12be0516710256b41e9f4a2d1ec6abd401d8"
    )


def test_new_request_schema_is_v2_while_historical_v1_is_still_readable():
    v1 = provider_compatible_transport_schema()["json_schema"]["schema"]
    assert SUPPLEMENTARY_TRANSPORT_V1_VERSION == "supplementary-transport/1.x"
    assert set(v1["properties"]) == {"payload_json"}
    historical = {
        "target_type": "total_mismatch",
        "observed_candidate_value": None,
        "raw_visible_text": None,
        "page_number": None,
        "evidence_reference": None,
        "confidence": None,
        "contradiction_flag": False,
        "unresolved_flag": True,
        "warnings": [],
        "visibility_status": "not_visible",
        "observed_candidates": [],
        "financial_components": None,
    }
    observation = parse_provider_transport_response(
        json.dumps({"payload_json": json.dumps(historical)}), target=_target(),
    )
    assert observation.unresolved_flag is True


def test_request_builder_uses_direct_v2_for_both_arms_and_never_v1():
    target = _target()
    record = type("Record", (), {
        "target_category": "total_mismatch",
        "packet_sha256": "a" * 64,
        "plan_id": "synthetic-plan",
        "crops": (
            type("Crop", (), {
                "crop_id": "crop-summary",
                "role": "summary",
                "category": "summary",
                "ordinal": 0,
                "mime_type": "image/png",
            })(),
        ),
    })()
    semantic_schema = canonical_json_bytes(supplementary_response_format(target))
    for arm in (ExperimentArm.A, ExperimentArm.B):
        _, request, _ = _request_payload(
            arm=arm,
            prompt=b"synthetic offline prompt",
            schema=semantic_schema,
            record=record,
            crops=(b"synthetic-image-bytes",),
        )
        wire = request["generationConfig"]["responseJsonSchema"]
        assert "payload_json" not in wire["properties"]
        assert wire["properties"]["contract_version"]["enum"] == [
            SUPPLEMENTARY_TRANSPORT_V2_VERSION
        ]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda value: value.pop("warnings"), "supplementary_required_field_missing"),
        (lambda value: value.update({"confidence": {}}), "supplementary_field_type_invalid"),
        (lambda value: value.update({"visibility_status": "invented"}), "supplementary_enum_invalid"),
        (lambda value: value.update({"wrapper": {}}), "supplementary_unexpected_field"),
        (lambda value: value.update({"contract_version": "supplementary-transport/9.9"}), "supplementary_transport_version_invalid"),
    ],
)
def test_v2_failure_taxonomy_is_specific(mutate, expected):
    payload = _base_payload()
    mutate(payload)
    diagnostics = _failure(payload, expected)
    serialized = diagnostics.model_dump_json()
    assert "synthetic visible evidence" not in serialized
    assert "123.45" not in serialized


def test_extra_top_level_wrapper_is_never_unwrapped_or_aliased():
    payload = {"result": _base_payload()}
    diagnostics = _failure(payload, "supplementary_unexpected_field", target=_target())
    assert diagnostics.decoding_count == 1


def test_numeric_strings_and_approved_camel_case_normalize_losslessly():
    payload = _base_payload()
    payload["pageNumber"] = "1"
    del payload["page_number"]
    payload["contractVersion"] = payload.pop("contract_version")
    payload["targetType"] = payload.pop("target_type")
    payload["visibilityStatus"] = "VISIBLE"
    del payload["visibility_status"]
    payload["unresolvedFlag"] = payload.pop("unresolved_flag")
    payload["contradictionFlag"] = payload.pop("contradiction_flag")
    payload["rawVisibleText"] = payload.pop("raw_visible_text")
    payload["observedCandidateValue"] = payload.pop("observed_candidate_value")
    payload["observedCandidates"] = payload.pop("observed_candidates")
    payload["financialComponents"] = payload.pop("financial_components")
    payload["visibleLabels"] = payload.pop("visible_labels")
    payload["contradictionObservations"] = payload.pop("contradiction_observations")
    raw_text = payload["rawVisibleText"]
    raw_text["evidenceRefs"] = raw_text.pop("evidence_refs")
    raw_text["visibilityStatus"] = raw_text.pop("visibility_status")
    raw_text["evidenceRefs"][0]["cropId"] = raw_text["evidenceRefs"][0].pop("crop_id")
    raw_text["evidenceRefs"][0]["evidenceKind"] = raw_text["evidenceRefs"][0].pop("evidence_kind")
    primary = payload["observedCandidateValue"]
    primary["resolutionKind"] = "TOTAL-AMOUNT"
    del primary["resolution_kind"]
    primary["sourcePage"] = "1"
    del primary["source_page"]
    primary["unitPrice"] = primary.pop("unit_price")
    primary["evidenceRefs"] = primary.pop("evidence_refs")
    primary["evidenceRefs"][0]["cropId"] = primary["evidenceRefs"][0].pop("crop_id")
    primary["evidenceRefs"][0]["evidenceKind"] = primary["evidenceRefs"][0].pop("evidence_kind")
    primary["visibilityStatus"] = primary.pop("visibility_status")
    parsed = _parse(payload)
    assert parsed.observation.page_number == 1
    assert parsed.observation.observed_candidate_value.raw_value == "123.45"
    assert "alias:contract_version" in parsed.diagnostics.normalization_actions
    assert "integer_string:page_number" in parsed.diagnostics.normalization_actions
    assert "enum:resolution_kind" in parsed.diagnostics.normalization_actions


def test_fully_resolved_and_unresolved_total_mismatch_variants_are_distinct():
    resolved = _parse(_base_payload())
    unresolved = _parse(_unresolved_payload())
    assert resolved.observation.unresolved_flag is False
    assert resolved.observation.financial_components.amount_due == Decimal("123.45")
    assert unresolved.observation.unresolved_flag is True
    assert unresolved.observation.observed_candidate_value is None
    assert unresolved.observation.evidence_reference is None


def test_contradictory_components_remain_explicit_and_never_become_resolved_truth():
    payload = _base_payload()
    payload["contradiction_flag"] = True
    payload["contradiction_observations"] = [{
        "value": "A",
        "observation_kind": "observed_value",
        "evidence_refs": [{
            "crop_id": "crop-summary", "evidence_kind": "contradiction",
        }],
        "confidence": 0.7,
        "visibility_status": "visible",
    }, {
        "value": "B",
        "observation_kind": "observed_value",
        "evidence_refs": [{
            "crop_id": "crop-identity", "evidence_kind": "contradiction",
        }],
        "confidence": 0.6,
        "visibility_status": "ambiguous",
    }]
    payload["financial_components"].extend([{
        "component_type": "subtotal",
        "raw_value": "120.00",
        "evidence_refs": [{
            "crop_id": "crop-summary", "evidence_kind": "financial_component",
        }],
        "confidence": 0.8,
        "visibility_status": "visible",
    }, {
        "component_type": "tax",
        "raw_value": "8.00",
        "evidence_refs": [{
            "crop_id": "crop-summary", "evidence_kind": "financial_component",
        }],
        "confidence": 0.7,
        "visibility_status": "visible",
    }])
    parsed = _parse(payload)
    assert parsed.observation.contradiction_flag is True
    assert parsed.observation.financial_components.subtotal == Decimal("120.00")
    assert parsed.observation.financial_components.tax == Decimal("8.00")


@pytest.mark.parametrize(
    "component_type",
    ["tax", "fees", "payments", "deposits", "previous_balance"],
)
def test_visible_financial_component_types_are_bounded_and_evidence_backed(component_type):
    payload = _base_payload()
    payload["financial_components"] = [{
        "component_type": component_type,
        "raw_value": "7.25",
        "evidence_refs": [{
            "crop_id": "crop-summary", "evidence_kind": "financial_component",
        }],
        "confidence": 0.8,
        "visibility_status": "visible",
    }]
    parsed = _parse(payload)
    components = parsed.observation.financial_components.model_dump()
    assert components[component_type] == Decimal("7.25")
    assert components["evidence_references"]


def test_multiple_identity_candidates_and_ambiguous_identity_are_preserved():
    resolved = _parse(_identity_payload())
    ambiguous = _parse(_identity_payload(ambiguous=True))
    assert len(resolved.observation.observed_candidates) == 2
    assert resolved.observation.observed_candidates[1].unresolved is True
    assert ambiguous.observation.unresolved_flag is True
    assert len(ambiguous.observation.observed_candidates) == 2


def test_flat_line_item_transport_maps_to_strict_line_item_without_json_strings():
    target_type = SupplementaryTargetType.MISSING_LINE_ITEM
    payload = _base_payload(target_type=target_type)
    payload["observed_candidate_value"].update({
        "resolution_kind": "line_item",
        "field_name": "line_items",
        "value": None,
        "source_page": "1",
        "section_header": "Synthetic section",
        "row_label": "Synthetic row",
        "location_candidate": None,
        "activity": "Synthetic service",
        "raw_description": "Synthetic observable line",
        "quantity": "2",
        "unit_price": "10.50",
        "amount": "21.00",
        "tax": None,
    })
    payload["financial_components"] = []
    payload["observed_candidate_value"]["evidence_refs"][0]["evidence_kind"] = "line_item"
    parsed = _parse(payload)
    line_item = parsed.observation.observed_candidate_value.line_item
    assert line_item is not None
    assert line_item.source_page == 1
    assert line_item.quantity == Decimal("2")
    assert line_item.unit_price == Decimal("10.50")
    assert line_item.amount == Decimal("21.00")


def test_not_visible_target_uses_empty_arrays_and_no_fake_values_or_evidence():
    parsed = _parse(_unresolved_payload())
    transport = parsed.transport
    assert transport.visibility_status.value == "not_visible"
    assert transport.page_number is None
    assert transport.observed_candidates == []
    assert transport.financial_components == []
    assert transport.raw_visible_text.evidence_refs == []


def test_unknown_crop_id_wrong_kind_and_missing_visible_evidence_fail_specifically():
    unknown = _base_payload()
    unknown["observed_candidate_value"]["evidence_refs"][0]["crop_id"] = (
        "provider-created-crop"
    )
    _failure(unknown, "supplementary_unplanned_crop_reference")

    wrong_kind = _base_payload()
    wrong_kind["observed_candidate_value"]["evidence_refs"][0][
        "evidence_kind"
    ] = "identity_candidate"
    _failure(wrong_kind, "supplementary_evidence_reference_invalid")

    missing = _base_payload()
    missing["observed_candidate_value"]["evidence_refs"] = []
    _failure(missing, "supplementary_visible_value_without_evidence")


def test_candidate_object_is_not_coerced_into_required_array():
    payload = _identity_payload()
    payload["observed_candidates"] = payload["observed_candidates"][0]
    _failure(payload, "supplementary_field_type_invalid")


def test_raw_and_normalized_transport_are_separate_and_input_is_immutable():
    payload = _base_payload()
    payload["page_number"] = "1"
    original = copy.deepcopy(payload)
    parsed = _parse(payload)
    assert parsed.raw_transport["page_number"] == "1"
    assert parsed.normalized_transport["page_number"] == 1
    assert payload == original
    assert parsed.observation.page_number == 1


def test_financial_numeric_string_reaches_internal_contract_without_float_loss():
    payload = _base_payload()
    payload["financial_components"][0]["raw_value"] = "1,234.50"
    parsed = _parse(payload)
    assert Decimal(parsed.observation.financial_components.amount_due) == Decimal("1234.50")


def test_v2_schema_forbids_accounting_and_authorization_fields():
    schema = supplementary_transport_v2_response_format(
        _target(), planned_crops=PLANNED_CROPS,
    )["json_schema"]["schema"]
    forbidden = {
        "final_gl", "candidate_gl", "readiness", "export_allowed", "accepted",
        "ground_truth", "human_corrections", "governed_rules",
    }
    serialized = json.dumps(schema, sort_keys=True)
    assert not any(name in serialized for name in forbidden)


def test_no_provider_call_occurs_during_v2_matrix(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("V2 transport tests are completely offline")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    parsed = _parse(_unresolved_payload())
    assert parsed.observation.unresolved_flag is True
