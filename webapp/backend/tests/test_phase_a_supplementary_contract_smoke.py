from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from scripts.run_phase_a_supplementary_contract_smoke import (
    AUTHORIZED_PACKET_SHA256,
    AUTHORIZED_SCHEMA_FAMILY_SHA256,
    MAXIMUM_REQUESTS,
    _safe_contract_result,
    _technical_smoke_acceptance,
    select_historical_invalid_flash_lite_record,
)
from webapp.backend.services.gemini_supplementary_transport import (
    supplementary_transport_v2_family_sha256,
)


def _record():
    return SimpleNamespace(
        packet_id="synthetic-packet",
        packet_sha256="a" * 64,
        target_category="total_mismatch",
        plan_id="synthetic-plan",
        crops=(SimpleNamespace(
            crop_id="synthetic-crop",
            role="summary",
            ordinal=0,
            category="summary",
        ),),
    )


def _envelope(payload: dict) -> str:
    return json.dumps(payload)


def _unresolved_payload() -> dict:
    return {
        "contract_version": "supplementary-transport/2.0",
        "target_type": "total_mismatch",
        "visibility_status": "not_visible",
        "unresolved_flag": True,
        "contradiction_flag": False,
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
        "warnings": ["synthetic-unresolved"],
    }


def _preflight():
    packet = b"synthetic-packet-bytes"
    record = _record()
    record.packet_sha256 = hashlib.sha256(packet).hexdigest()
    return {"record": record, "packet": packet}


def test_selection_requires_historical_success_and_old_schema_failure():
    record = _record()
    manifest = SimpleNamespace(packet_records=(record,))
    state = {
        "requests": [{
            "packet_id": record.packet_id,
            "arm": "arm_a",
            "http_status": 200,
            "usage_reported": True,
            "raw_response_persisted": False,
        }],
        "evaluations": [{
            "packet_id": record.packet_id,
            "arm": "arm_a",
            "failure_code": "supplementary_invalid_schema",
            "schema_valid": False,
            "accepted": False,
            "export_allowed": False,
        }],
    }
    assert select_historical_invalid_flash_lite_record(manifest, state) is record
    assert MAXIMUM_REQUESTS == 1


def test_private_smoke_authorization_is_pinned_to_packet_and_v2_schema_family():
    assert AUTHORIZED_PACKET_SHA256 == (
        "385b8e3ef8f7bac593f07325d3df3a9e62a4629f5e4c7178ac39dd2e1e490b88"
    )
    assert AUTHORIZED_SCHEMA_FAMILY_SHA256 != supplementary_transport_v2_family_sha256()


def test_valid_unresolved_contract_remains_review_required_and_non_exportable():
    result = _safe_contract_result(
        preflight=_preflight(),
        raw_text=_envelope(_unresolved_payload()),
        initial_facts={
            "line_items": [], "evidence": [], "page_reconciliations": [],
            "needs_manual_review": True,
        },
        finish_reason="STOP",
    )
    assert result["internal_contract_result"] == "valid"
    assert result["transport_validation_status"] == "passed"
    assert result["transport_normalization_status"] == "passed"
    assert result["evidence_validation_status"] == "passed"
    assert result["internal_observation_status"] == "constructed"
    assert result["visible_or_ambiguous_evidence_status"] == "passed"
    assert result["crop_enrichment_status"] == "passed"
    assert result["canonical_outcome"] == "contract_valid_unresolved"
    assert result["final_disposition"] == "review_required"
    assert result["accepted"] is False
    assert result["export_allowed"] is False
    assert result["false_safe_export"] is False

    acceptance = _technical_smoke_acceptance(
        result,
        terminal_disposition_persisted=True,
        terminal_disposition_count=1,
    )
    assert acceptance["technical_success"] is True
    assert acceptance["terminal_action"] == "stop_technical_success"
    assert acceptance["micro_ab_started"] is False
    assert acceptance["retry_allowed"] is False


def test_invalid_contract_uses_specific_reason_and_never_generic_schema_code():
    payload = _unresolved_payload()
    del payload["visibility_status"]
    result = _safe_contract_result(
        preflight=_preflight(),
        raw_text=_envelope(payload),
        initial_facts={"line_items": [], "evidence": []},
        finish_reason="STOP",
    )
    assert result["canonical_outcome"] == "supplementary_required_field_missing"
    assert result["final_disposition"] == "blocked"
    assert result["canonical_outcome"] != "supplementary_invalid_schema"
    assert result["accepted"] is False
    assert result["export_allowed"] is False


def test_evidence_failure_stops_with_specific_reason_and_no_retry_or_micro_ab():
    payload = _unresolved_payload()
    payload["visibility_status"] = "ambiguous"
    result = _safe_contract_result(
        preflight=_preflight(),
        raw_text=_envelope(payload),
        initial_facts={"line_items": [], "evidence": []},
        finish_reason="STOP",
    )

    assert result["canonical_outcome"] == (
        "supplementary_ambiguous_value_without_evidence"
    )
    assert result["canonical_outcome"] != "supplementary_invalid_schema"
    assert result["evidence_validation_status"] == "failed"
    assert result["visible_or_ambiguous_evidence_status"] == "failed"
    assert result["crop_enrichment_status"] == "failed"
    assert result["final_disposition"] == "blocked"
    assert result["accepted"] is False
    assert result["export_allowed"] is False
    assert result["false_safe_export"] is False

    acceptance = _technical_smoke_acceptance(
        result,
        terminal_disposition_persisted=True,
        terminal_disposition_count=1,
    )
    assert acceptance["technical_success"] is False
    assert acceptance["terminal_action"] == "stop_evidence_validation_failed"
    assert acceptance["canonical_reason"] == (
        "supplementary_ambiguous_value_without_evidence"
    )
    assert acceptance["retry_allowed"] is False
    assert acceptance["retry_count"] == 0
    assert acceptance["micro_ab_started"] is False
    assert acceptance["micro_ab_eligible_for_separate_authorization"] is False


def test_technical_success_requires_one_persisted_terminal_disposition():
    result = _safe_contract_result(
        preflight=_preflight(),
        raw_text=_envelope(_unresolved_payload()),
        initial_facts={"line_items": [], "evidence": []},
        finish_reason="STOP",
    )
    acceptance = _technical_smoke_acceptance(
        result,
        terminal_disposition_persisted=False,
        terminal_disposition_count=0,
    )
    assert acceptance["technical_success"] is False
    assert acceptance["checks"]["one_terminal_disposition_persisted"] is False


def test_unresolved_output_cannot_pass_gate_if_marked_accepted_or_exportable():
    result = _safe_contract_result(
        preflight=_preflight(),
        raw_text=_envelope(_unresolved_payload()),
        initial_facts={"line_items": [], "evidence": []},
        finish_reason="STOP",
    )
    result["accepted"] = True
    result["export_allowed"] = True
    result["false_safe_export"] = True
    acceptance = _technical_smoke_acceptance(
        result,
        terminal_disposition_persisted=True,
        terminal_disposition_count=1,
    )
    assert acceptance["technical_success"] is False
    assert acceptance["checks"]["unresolved_or_contradictory_remains_safe"] is False
    assert acceptance["checks"]["false_safe_exports_zero"] is False
