from __future__ import annotations

import copy
import json

import pytest

from webapp.backend.services.gemini_probe_contract_audit import (
    parse_provider_transport_response,
)
from webapp.backend.services.gemini_supplementary_transport import (
    SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    parse_supplementary_transport_v2_response_with_audit,
    supplementary_transport_v2_response_format,
)
from webapp.backend.services.gemini_supplementary_verification import (
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
)
from webapp.backend.services.supplementary_crop_framing import (
    AuthorizedCropDescriptor,
    SupplementaryCropFramingError,
    build_supplementary_crop_framing,
    validate_ordered_crop_parts,
    validate_packet_specific_schema,
)


PACKET_SHA = "a" * 64
PLAN_ID = "synthetic-plan"
PLANNED = {
    "crop-primary": {
        "role": "primary_target", "ordinal": 0, "page_number": 1,
        "plan_id": PLAN_ID, "packet_sha256": PACKET_SHA,
        "source_kind": "synthetic_crop",
    },
    "crop-context": {
        "role": "context_thumbnail", "ordinal": 1, "page_number": 1,
        "plan_id": PLAN_ID, "packet_sha256": PACKET_SHA,
        "source_kind": "synthetic_crop",
    },
    "crop-related": {
        "role": "related_evidence", "ordinal": 2, "page_number": 2,
        "plan_id": PLAN_ID, "packet_sha256": PACKET_SHA,
        "source_kind": "synthetic_crop",
    },
}


def _target(
    kind: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=kind,
        page_number=1,
        field_name="synthetic_field",
        local_trigger_codes=["synthetic_evidence_linkage"],
    )


def _ref(crop_id: str, kind: str) -> dict:
    return {"crop_id": crop_id, "evidence_kind": kind}


def _payload(
    *, crop_id: str = "crop-primary", visibility: str = "visible",
    target_type: SupplementaryTargetType = SupplementaryTargetType.TOTAL_MISMATCH,
) -> dict:
    value = "synthetic-value" if visibility != "not_visible" else None
    references = (
        [_ref(crop_id, "primary_observation")]
        if visibility != "not_visible" else []
    )
    return {
        "contract_version": SUPPLEMENTARY_TRANSPORT_V2_VERSION,
        "target_type": target_type.value,
        "visibility_status": visibility,
        "unresolved_flag": visibility != "visible",
        "contradiction_flag": False,
        "page_number": 1 if visibility != "not_visible" else None,
        "confidence": 0.8 if visibility != "not_visible" else None,
        "raw_visible_text": {
            "value": value,
            "evidence_refs": (
                [_ref(crop_id, "visible_label")]
                if visibility != "not_visible" else []
            ),
            "confidence": 0.8 if visibility != "not_visible" else None,
            "visibility_status": visibility,
        },
        "observed_candidate_value": {
            "resolution_kind": "total_amount" if value else "none",
            "field_name": "total_amount" if value else None,
            "value": value,
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
            "evidence_refs": references,
            "confidence": 0.8 if value else None,
            "visibility_status": visibility,
        },
        "observed_candidates": [],
        "financial_components": [],
        "visible_labels": [],
        "contradiction_observations": [],
        "warnings": [],
    }


def _parse(payload: dict, target: SupplementaryTarget | None = None):
    chosen = target or _target(SupplementaryTargetType(payload["target_type"]))
    return parse_supplementary_transport_v2_response_with_audit(
        json.dumps(payload),
        target=chosen,
        planned_crops=PLANNED,
        plan_id=PLAN_ID,
        packet_sha256=PACKET_SHA,
    )


def _failure(payload: dict, code: str, target: SupplementaryTarget | None = None):
    with pytest.raises(SupplementaryVerificationError) as raised:
        _parse(payload, target)
    assert raised.value.failure_code == code
    return raised.value.diagnostics


def _descriptors() -> tuple[AuthorizedCropDescriptor, ...]:
    return tuple(
        AuthorizedCropDescriptor(
            crop_id=crop_id,
            crop_role=value["role"],
            ordinal=value["ordinal"],
            target_relevance="total_mismatch",
            mime_type="image/png",
            page_number=value["page_number"],
            source_kind=value["source_kind"],
        )
        for crop_id, value in PLANNED.items()
    )


def test_01_visible_value_with_valid_primary_crop():
    parsed = _parse(_payload())
    assert parsed.observation.evidence_reference.crop_id == "crop-primary"


def test_02_visible_value_with_valid_related_crop():
    parsed = _parse(_payload(crop_id="crop-related"))
    assert parsed.observation.evidence_reference.crop_role == "related_evidence"


def test_03_ambiguous_candidate_with_valid_context_crop():
    payload = _payload(
        crop_id="crop-context", visibility="ambiguous",
        target_type=SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY,
    )
    payload["observed_candidate_value"].update({
        "resolution_kind": "scalar", "field_name": "invoice_number",
    })
    payload["observed_candidate_value"]["evidence_refs"][0][
        "evidence_kind"
    ] = "primary_observation"
    parsed = _parse(payload, _target(SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY))
    assert parsed.observation.unresolved_flag is True


def test_04_visible_value_without_evidence():
    payload = _payload()
    payload["observed_candidate_value"]["evidence_refs"] = []
    diagnostics = _failure(payload, "supplementary_visible_value_without_evidence")
    assert diagnostics.evidence_validation_status.value == "failed"


def test_05_ambiguous_value_without_evidence():
    payload = _payload(visibility="ambiguous")
    payload["observed_candidate_value"]["evidence_refs"] = []
    _failure(payload, "supplementary_ambiguous_value_without_evidence")


def test_06_not_visible_value_with_no_crop():
    parsed = _parse(_payload(visibility="not_visible"))
    assert parsed.observation.unresolved_flag is True
    assert parsed.observation.evidence_reference is None


def test_07_unknown_invented_crop_id():
    _failure(_payload(crop_id="invented-crop"), "supplementary_unplanned_crop_reference")


def test_08_crop_from_another_packet():
    payload = _payload()
    payload["raw_visible_text"]["evidence_refs"][0]["crop_id"] = "other-packet-crop"
    _failure(payload, "supplementary_unplanned_crop_reference")


def test_09_crop_enum_and_packet_mismatch():
    schema = supplementary_transport_v2_response_format(
        _target(), planned_crops=PLANNED,
    )["json_schema"]["schema"]
    changed = copy.deepcopy(schema)
    changed["properties"]["raw_visible_text"]["properties"]["evidence_refs"][
        "items"
    ]["properties"]["crop_id"]["enum"] = ["crop-primary"]
    with pytest.raises(
        SupplementaryCropFramingError,
        match="supplementary_crop_enum_packet_mismatch",
    ):
        validate_packet_specific_schema(
            schema=changed,
            packet_sha256=PACKET_SHA,
            descriptors=_descriptors(),
            transport_version=SUPPLEMENTARY_TRANSPORT_V2_VERSION,
        )


def test_10_image_label_order_mismatch():
    schema = supplementary_transport_v2_response_format(
        _target(), planned_crops=PLANNED,
    )["json_schema"]["schema"]
    framing = build_supplementary_crop_framing(
        descriptors=_descriptors(), images=(b"a", b"b", b"c"), schema=schema,
        packet_sha256=PACKET_SHA,
        transport_version=SUPPLEMENTARY_TRANSPORT_V2_VERSION,
    )
    changed = list(framing.parts)
    changed[0], changed[2] = changed[2], changed[0]
    with pytest.raises(
        SupplementaryCropFramingError,
        match="supplementary_crop_label_order_mismatch",
    ):
        validate_ordered_crop_parts(changed, _descriptors())
    with pytest.raises(
        SupplementaryCropFramingError,
        match="supplementary_crop_label_order_mismatch",
    ):
        validate_ordered_crop_parts(framing.parts[:-2], _descriptors())
    wrong_role = list(framing.parts)
    wrong_role[0] = {
        "text": str(wrong_role[0]["text"]).replace(
            "CROP_ROLE: primary_target", "CROP_ROLE: context_thumbnail",
        ),
    }
    with pytest.raises(
        SupplementaryCropFramingError,
        match="supplementary_crop_role_mismatch",
    ):
        validate_ordered_crop_parts(wrong_role, _descriptors())


def test_11_multiple_candidates_preserve_separate_crop_evidence():
    payload = _payload(target_type=SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY)
    payload["observed_candidate_value"].update({
        "resolution_kind": "scalar", "field_name": "invoice_number",
    })
    payload["observed_candidates"] = [{
        "value": "A", "adjacent_label": "Label", "candidate_type": "invoice_number",
        "evidence_refs": [_ref("crop-primary", "identity_candidate")],
        "confidence": 0.7, "visibility_status": "visible",
    }, {
        "value": "B", "adjacent_label": "Label", "candidate_type": "invoice_number",
        "evidence_refs": [_ref("crop-context", "identity_candidate")],
        "confidence": 0.6, "visibility_status": "ambiguous",
    }]
    parsed = _parse(payload, _target(SupplementaryTargetType.INVOICE_NUMBER_AMBIGUITY))
    assert [
        item.evidence_references[0].crop_id
        for item in parsed.observation.observed_candidates
    ] == ["crop-primary", "crop-context"]


def test_12_contradictory_candidates_preserve_separate_evidence():
    payload = _payload()
    payload["contradiction_flag"] = True
    payload["contradiction_observations"] = [{
        "value": "A", "observation_kind": "observed_value",
        "evidence_refs": [_ref("crop-primary", "contradiction")],
        "confidence": 0.7, "visibility_status": "visible",
    }, {
        "value": "B", "observation_kind": "observed_value",
        "evidence_refs": [_ref("crop-related", "contradiction")],
        "confidence": 0.6, "visibility_status": "ambiguous",
    }]
    parsed = _parse(payload)
    assert len(parsed.observation.contradiction_observations) == 2


def test_13_financial_components_use_different_crops():
    payload = _payload()
    payload["financial_components"] = [{
        "component_type": "subtotal", "raw_value": "10",
        "evidence_refs": [_ref("crop-primary", "financial_component")],
        "confidence": 0.8, "visibility_status": "visible",
    }, {
        "component_type": "tax", "raw_value": "1",
        "evidence_refs": [_ref("crop-related", "financial_component")],
        "confidence": 0.7, "visibility_status": "visible",
    }]
    parsed = _parse(payload)
    mapping = parsed.observation.financial_components.component_evidence_references
    assert mapping["subtotal"][0].crop_id == "crop-primary"
    assert mapping["tax"][0].crop_id == "crop-related"


def test_14_global_evidence_reference_is_not_reused():
    payload = _payload()
    payload["evidence_references"] = [_ref("crop-primary", "primary_observation")]
    payload["observed_candidate_value"]["evidence_refs"] = []
    _failure(payload, "supplementary_unexpected_field")


def test_15_local_enrichment_uses_packet_metadata_not_provider_claims():
    parsed = _parse(_payload(crop_id="crop-related"))
    reference = parsed.observation.evidence_reference
    assert reference.crop_role == "related_evidence"
    assert reference.page_number == 2
    assert reference.plan_id == PLAN_ID
    assert reference.packet_sha256 == PACKET_SHA
    assert reference.source_kind == "synthetic_crop"


def test_16_transport_valid_but_evidence_invalid_is_observable():
    payload = _payload()
    payload["observed_candidate_value"]["evidence_refs"] = []
    diagnostics = _failure(payload, "supplementary_visible_value_without_evidence")
    assert diagnostics.transport_validation_status.value == "passed"
    assert diagnostics.transport_normalization_status.value == "passed"
    assert diagnostics.evidence_validation_status.value == "failed"
    assert diagnostics.internal_observation_status.value == "not_constructed"


def test_17_transport_and_evidence_valid_construct_internal_observation():
    diagnostics = _parse(_payload()).diagnostics
    assert diagnostics.transport_validation_status.value == "passed"
    assert diagnostics.transport_normalization_status.value == "passed"
    assert diagnostics.evidence_validation_status.value == "passed"
    assert diagnostics.internal_observation_status.value == "constructed"


def test_18_historical_v1_remains_read_only_compatible():
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
