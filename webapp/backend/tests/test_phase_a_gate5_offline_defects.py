from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from webapp.backend.services import ai_invoice_processor
from webapp.backend.services.gemini_facts_transport import (
    GeminiTransportJSONError,
    SafeSchemaFailureCategory,
    build_safe_diagnostic,
    classify_safe_diagnostic,
    gemini_facts_transport_json_schema,
    parse_and_normalize_gemini_facts,
)
from webapp.backend.services.phase_a_calibration_runner import (
    _canonical_review_codes,
    _derive_terminal_disposition,
    _finalize_controlled_processor_result,
    _local_trace_metrics,
    _terminal_quality_metrics,
)
from webapp.backend.services.reconciliation_observability import (
    ReconciliationStatus,
    SupplementaryVisualStatus,
    observe_reconciliation,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "phase_a_gate5_clean_offline_synthetic.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _transport_payload() -> dict:
    payload = {
        key: None for key in gemini_facts_transport_json_schema()["required"]
    }
    for key in (
        "line_items", "excluded_paid_rows", "unresolved_visual_regions",
        "page_reconciliations", "evidence", "warnings",
    ):
        payload[key] = []
    return payload


def _fact_row() -> dict:
    return {
        "_meta": {
            "document_facts": {
                "schema_version": "synthetic/1.0",
                "evidence": [{"source_type": "synthetic_evidence"}],
                "line_items": [],
            },
            "source_text": {"raw_description": "synthetic evidence"},
        }
    }


def _payload(document: dict) -> dict:
    reasons = list(document["input_reason_codes"])
    if not document["facts_exist"]:
        return {
            "summary": {"processing_failures": 0},
            "invoices": [],
            "manual_review_rows": [{"reason_codes": [reasons[0]]}],
            "unsupported_files": [{
                "reason_code": "initial_structured_response_invalid",
            }],
        }
    reconciliation_payload = {
        "supplementary_reconciliation": document["supplementary_reconciliation"],
        "supplementary_evidence_revisions": [{
            "observation": document["supplementary_observation"],
        }],
        "warnings": reasons,
        "manual_review_codes": reasons,
    }
    observation = observe_reconciliation(
        reconciliation_payload, facts_exist=True,
    )
    return {
        "summary": {"processing_failures": 0},
        "invoices": [{
            "rows": [_fact_row()],
            "validation_summary": {
                "reconciliation_observation": observation.model_dump(mode="json"),
            },
        }],
        "manual_review_rows": [{"reason_codes": reasons}],
        "unsupported_files": [],
    }


def test_clean_gate_schema_failures_are_classified_without_private_content():
    fixture = _fixture()
    diagnostics = [
        item["diagnostic"] for item in fixture["documents"] if item.get("diagnostic")
    ]
    assert len(diagnostics) == 2
    for diagnostic in diagnostics:
        assert classify_safe_diagnostic(diagnostic) is (
            SafeSchemaFailureCategory.INTERNAL_NORMALIZATION_FAILURE
        )
        serialized = json.dumps(diagnostic, sort_keys=True)
        for forbidden in (
            "vendor", "address", "filename", "account_number", "raw_response",
        ):
            assert forbidden not in serialized.casefold()


def test_synthetic_fixture_reproduces_the_three_legacy_defects():
    legacy = _fixture()["legacy_observation"]
    assert legacy == {
        "generic_reason_duplicated": True,
        "terminal_reconciliation_ran": False,
        "supplementary_trace_reconciliation_after": "reconciled",
        "schema_failure_subtype": "strict_internal_contract",
    }


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ({"json_parser_error_type": "TruncatedJSON"}, "truncation"),
        ({"output_token_limit_reached": True}, "output_limit_exhaustion"),
        ({"json_parser_error_type": "SchemaValidationError", "missing_required_field_count": 1}, "missing_required_field"),
        ({"json_parser_error_type": "SchemaValidationError", "schema_validation_error_type": "string_type"}, "incorrect_field_type"),
        ({"json_parser_error_type": "SchemaValidationError", "unknown_field_count": 1}, "additional_unsupported_field"),
        ({"json_parser_error_type": "TrailingStructuredData"}, "multiple_json_objects"),
    ],
)
def test_safe_schema_diagnostic_categories(diagnostic, expected):
    assert classify_safe_diagnostic(diagnostic).value == expected


def test_safe_diagnostic_contains_shape_only_and_classification():
    private_marker = "DO-NOT-PERSIST-PRIVATE-VALUE"
    raw = json.dumps({**_transport_payload(), "vendor_name": private_marker})
    diagnostic = build_safe_diagnostic(
        raw,
        provider="gemini",
        model="synthetic-model",
        request_profile="synthetic-profile",
        parsed=json.loads(raw),
        parser_error_type="StrictInternalContractValidationError",
        schema_validation_error_path="line_items",
    )
    serialized = json.dumps(diagnostic, sort_keys=True)
    assert private_marker not in serialized
    assert diagnostic["schema_failure_category"] == "internal_normalization_failure"


def test_safely_normalizable_transport_variants_remain_local_and_typed():
    payload = _transport_payload()
    payload.update({
        "vendor_name": "   ",
        "subtotal": "10.25",
        "total_amount": 10.25,
        "harmless_optional_shape": None,
    })
    normalized = parse_and_normalize_gemini_facts(
        json.dumps(payload), provider="gemini", model="synthetic-model",
        request_profile="synthetic-profile",
    )
    assert normalized["vendor_name"] is None
    assert normalized["subtotal"] == Decimal("10.25")
    assert normalized["total_amount"] == Decimal("10.25")
    assert any("unknown_field" in warning for warning in normalized["warnings"])


@pytest.mark.parametrize("raw", ["{", '{"line_items": []', "{}\n{}"])
def test_malformed_truncated_and_multiple_json_remain_rejected(raw):
    with pytest.raises(GeminiTransportJSONError):
        parse_and_normalize_gemini_facts(
            raw, provider="gemini", model="synthetic-model",
            request_profile="synthetic-profile",
        )


def test_all_five_logical_outcomes_use_specific_reason_priority():
    fixture = _fixture()
    for document in fixture["documents"]:
        codes = _canonical_review_codes(document["input_reason_codes"])
        assert "processor_failure" not in codes
        assert "ai_processing_failed" not in codes
        disposition = _derive_terminal_disposition(_payload(document))
        assert disposition.sanitized_failure_code == document["expected_primary_reason"]
        assert disposition.disposition.value == document["expected_disposition"]
        assert disposition.accepted is False
        assert disposition.exportable is False
    contradiction_codes = _canonical_review_codes(
        fixture["documents"][2]["input_reason_codes"]
    )
    assert "supplementary_visual_evidence_contradiction" in contradiction_codes
    assert "supplementary_request_limit_reached" in contradiction_codes


def test_specific_reason_suppresses_generic_in_persisted_safe_result(tmp_path):
    document = _fixture()["documents"][0]
    result = _finalize_controlled_processor_result(
        _payload(document), result_path=tmp_path / "_webapp_result.json",
        normalize_result=lambda _value: None,
        attach_readiness=lambda _value: None,
        assert_provenance=lambda _value: None,
    )
    assert "processor_failure" not in json.dumps(result)
    assert result["phase_a_terminal_disposition"]["sanitized_failure_code"] == (
        "initial_structured_response_invalid"
    )


def test_reconciled_supplementary_state_does_not_resolve_visual_review():
    for document in _fixture()["documents"][1:4]:
        disposition = _derive_terminal_disposition(_payload(document))
        assert disposition.reconciliation_ran is True
        assert disposition.reconciliation_status == ReconciliationStatus.RECONCILED.value
        assert disposition.reconciliation_source_stage == "supplementary_visual_verification"
        assert disposition.reconciliation_before == "reconciled"
        assert disposition.reconciliation_after == "reconciled"
        assert disposition.supplementary_visual_status in {
            SupplementaryVisualStatus.UNRESOLVED.value,
            SupplementaryVisualStatus.CONTRADICTION.value,
        }
        assert disposition.review_required is True
        assert disposition.accepted is False
        assert disposition.exportable is False


def test_local_normalization_propagates_typed_reconciliation_observation():
    payload = _transport_payload()
    payload.update({
        "vendor_name": "Synthetic Vendor",
        "invoice_number": "SYNTHETIC-1",
        "invoice_date": "2026-01-01",
        "bill_or_credit": "Bill",
        "line_items": [{
            "source_page": 1,
            "section_header": None,
            "row_label": "synthetic-row",
            "location_candidate": None,
            "activity": "synthetic service",
            "raw_description": "synthetic service",
            "quantity": 1,
            "unit_price": 10,
            "amount": 10,
            "tax": None,
            "confidence": 0.9,
            "evidence": [{
                "page": 1,
                "text": "synthetic",
                "bbox": [0, 0, 1, 1],
                "source_type": "synthetic_evidence",
                "confidence": 0.9,
            }],
        }],
        "subtotal": 10,
        "total_amount": 10,
        "confidence": 0.9,
        "visual_extraction_status": "partial",
        "warnings": ["supplementary_visual_evidence_unresolved"],
        "supplementary_reconciliation": {
            "before": {"reconciled": True},
            "after": {"reconciled": True},
            "resolved": False,
        },
        "supplementary_evidence_revisions": [{
            "observation": {
                "unresolved_flag": True,
                "contradiction_flag": False,
            },
        }],
    })
    normalized = ai_invoice_processor.validate_ai_extraction(
        payload, references={"vendors": [], "properties": [], "gl_accounts": []},
    )
    assert normalized["reconciliation_ran"] is True
    assert normalized["reconciliation_status"] == "reconciled"
    assert normalized["reconciliation_source_stage"] == (
        "supplementary_visual_verification"
    )
    assert normalized["supplementary_visual_status"] == "unresolved"
    assert "supplementary_visual_evidence_unresolved" in (
        normalized["manual_review_codes"]
    )
    assert normalized["validation_summary"]["valid"] is False


def test_localization_failure_remains_distinct_in_strict_facts_validation():
    payload = _transport_payload()
    payload.update({
        "vendor_name": "Synthetic Vendor",
        "invoice_number": "SYNTHETIC-LOCALIZATION",
        "invoice_date": "2026-01-01",
        "bill_or_credit": "Bill",
        "line_items": [{
            "source_page": 1,
            "section_header": None,
            "row_label": "synthetic-row",
            "location_candidate": None,
            "activity": "synthetic service",
            "raw_description": "synthetic service",
            "quantity": 1,
            "unit_price": 10,
            "amount": 10,
            "tax": None,
            "confidence": 0.9,
            "evidence": [{
                "page": 1,
                "text": "synthetic",
                "bbox": [0, 0, 1, 1],
                "source_type": "synthetic_evidence",
                "confidence": 0.9,
            }],
        }],
        "subtotal": 10,
        "total_amount": 10,
        "confidence": 0.9,
        "visual_extraction_status": "partial",
        "warnings": ["supplementary_evidence_localization_unavailable"],
        "supplementary_reconciliation": {
            "before": {"reconciled": True},
            "after": {"reconciled": True},
            "resolved": False,
        },
        "supplementary_evidence_revisions": [{
            "observation": {
                "unresolved_flag": True,
                "contradiction_flag": False,
            },
        }],
    })
    normalized = ai_invoice_processor.validate_ai_extraction(
        payload, references={"vendors": [], "properties": [], "gl_accounts": []},
    )
    codes = normalized["manual_review_codes"]
    assert "supplementary_evidence_localization_unavailable" in codes
    assert "supplementary_visual_evidence_unresolved" not in codes
    assert "processor_failure" not in codes
    assert "ai_processing_failed" not in codes
    assert normalized["validation_summary"]["valid"] is False


def test_missing_facts_report_reconciliation_unavailable():
    disposition = _derive_terminal_disposition(_payload(_fixture()["documents"][0]))
    assert disposition.reconciliation_ran is False
    assert disposition.reconciliation_status == "unavailable_due_to_missing_facts"
    assert disposition.reconciliation_source_stage == "facts_validation"


def test_quality_metrics_use_fact_bearing_document_denominators():
    fixture = _fixture()
    dispositions = [
        _derive_terminal_disposition(_payload(document)).to_dict()
        for document in fixture["documents"]
    ]
    reasons = [
        set(_canonical_review_codes(document["input_reason_codes"]))
        for document in fixture["documents"]
    ]
    metrics = _terminal_quality_metrics(dispositions, reasons)
    assert metrics == {
        "transport_valid_observation_count": 3,
        "transport_valid_observation_rate": 0.6,
        "intermediate_unreconciled_observation_count": 0,
        "intermediate_unreconciled_observation_rate": 0.0,
        "document_facts_document_count": 3,
        "document_facts_coverage": 0.6,
        "provenance_document_count": 3,
        "provenance_coverage": 1.0,
        "reconciliation_document_count": 3,
        "reconciliation_coverage": 1.0,
        "reconciliation_status_distribution": {"reconciled": 3},
        "reconciliation_state_distribution": {
            "ran_reconciled": 3,
            "unavailable_due_to_missing_facts": 2,
        },
        "review_required_unresolved_arithmetic_count": 0,
        "unsupported_unusable_transport_count": 2,
        "supplementary_attempted_document_count": 3,
        "supplementary_resolved_document_count": 0,
        "supplementary_resolution_rate": 0.0,
        "supplementary_contradiction_rate": pytest.approx(1 / 3),
        "supplementary_limit_rate": pytest.approx(1 / 3),
        "canonical_reason_distribution": {
            "initial_structured_response_invalid": 2,
            "supplementary_request_limit_reached": 1,
            "supplementary_visual_evidence_unresolved": 2,
        },
    }


def test_aggregate_reasons_keep_localization_and_provider_unresolved_distinct():
    dispositions = [
        {
            "disposition": "review_required",
            "sanitized_failure_code": reason,
            "document_facts_exist": False,
            "intermediate_observation_exists": True,
            "provenance_exists": True,
            "reconciliation_state": "ran_inconclusive",
            "reconciliation_ran": True,
            "reconciliation_status": "inconclusive",
            "supplementary_visual_status": "unresolved",
        }
        for reason in (
            "supplementary_evidence_localization_unavailable",
            "supplementary_visual_evidence_unresolved",
        )
    ]
    metrics = _terminal_quality_metrics(
        dispositions,
        [
            {"supplementary_evidence_localization_unavailable"},
            {"supplementary_visual_evidence_unresolved"},
        ],
    )
    assert metrics["canonical_reason_distribution"] == {
        "supplementary_evidence_localization_unavailable": 1,
        "supplementary_visual_evidence_unresolved": 1,
    }


def test_trace_metrics_measure_structured_and_unresolved_rates(tmp_path):
    trace = tmp_path / "batches" / "synthetic" / "audit" / "ai_request_trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join((
        json.dumps({"event": "schema_validation", "schema_result": "valid"}),
        json.dumps({"event": "schema_validation", "schema_result": "invalid"}),
        json.dumps({"event": "supplementary_verification", "resolved": False}),
        json.dumps({"event": "supplementary_verification", "resolved": True}),
    )) + "\n", encoding="utf-8")
    metrics = _local_trace_metrics(tmp_path)
    assert metrics["schema_valid_count"] == 1
    assert metrics["schema_invalid_count"] == 1
    assert metrics["supplementary_verification_count"] == 2
    assert metrics["supplementary_unresolved_target_count"] == 1
    assert metrics["supplementary_unresolved_target_rate"] == 0.5
