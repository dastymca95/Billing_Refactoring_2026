from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from webapp.backend.services import ai_invoice_processor, ai_provider
from webapp.backend.services.gemini_facts_transport import (
    GeminiTransportJSONError,
    parse_and_normalize_gemini_facts,
)
from webapp.backend.services.gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    SupplementaryTarget,
    SupplementaryTargetType,
    build_minimized_initial_summary,
    merge_supplementary_observations,
    select_supplementary_targets,
)
from webapp.backend.services.intermediate_invoice_observation import (
    InitialNormalizationCategory,
    InitialNormalizationOutcome,
)
from webapp.backend.services.phase_a_calibration_runner import (
    PhaseATerminalDisposition,
    _derive_terminal_disposition,
    _finalize_controlled_processor_result,
    _terminal_quality_metrics,
)


FIXTURE = Path(__file__).parent / "fixtures" / (
    "phase_a_unreconciled_observations_synthetic.json"
)


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _transport(case: dict) -> dict:
    return parse_and_normalize_gemini_facts(
        json.dumps(case["payload"]),
        provider="gemini",
        model="synthetic-offline-model",
        request_profile="synthetic-offline-facts-only",
    )


def _initial(case: dict):
    return ai_provider.normalize_gemini_initial_observation(
        _transport(case),
        opaque_document_id=f"opaque-{case['case_id']}",
        provider="gemini",
        profile_id="synthetic-offline-facts-only",
        model_id="synthetic-offline-model",
    )


def _target() -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1,
        field_name="reconciliation",
        local_trigger_codes=["invoice_reconciliation_failed"],
    )


def _observation(
    *, kind: str, value: str | None = None, line_item: dict | None = None,
    contradiction: bool = False, unresolved: bool = False,
) -> GeminiSupplementaryObservation:
    return GeminiSupplementaryObservation.model_validate({
        "target_type": "total_mismatch",
        "observed_candidate_value": None if unresolved else {
            "resolution_kind": kind,
            "field_name": kind if kind.endswith("_amount") else None,
            "raw_value": value,
            "line_item": line_item,
        },
        "raw_visible_text": "Synthetic visible supplementary evidence",
        "page_number": 1,
        "evidence_reference": {
            "page_number": 1,
            "bbox": [20.0, 30.0, 80.0, 12.0],
        },
        "confidence": 0.96,
        "contradiction_flag": contradiction,
        "unresolved_flag": unresolved,
        "warnings": [],
    })


def _merged(initial, observation: GeminiSupplementaryObservation) -> dict:
    return merge_supplementary_observations(
        initial.working_observation_payload,
        [(_target(), observation)],
    )


def _review_payload(outcome) -> dict:
    review = ai_invoice_processor._intermediate_observation_review_item(
        source_file="synthetic.pdf",
        vendor_name="Synthetic Provider",
        outcome=outcome,
    )
    return {
        "summary": {"processing_failures": 0},
        "invoices": [],
        "manual_review_rows": [review],
        "unsupported_files": [],
    }


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["case_id"])
def test_transport_valid_mismatch_is_preserved_as_immutable_intermediate(case):
    transport = _transport(case)
    original = copy.deepcopy(transport)

    # This is the exact pre-fix rejection point: strict facts validation still
    # rejects the mismatch.  The boundary now preserves it instead of losing it.
    with pytest.raises(ai_provider.AIProviderInvalidSchema) as old_failure:
        ai_provider._validate_visual_line_structure(
            copy.deepcopy(transport), require_generated_description=False,
        )
    assert ai_provider._internal_schema_failure_path(old_failure.value) == (
        "reconciliation.line_items_to_invoice_total"
    )

    outcome = _initial(case)
    assert transport == original
    assert outcome.category is InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED
    assert outcome.facts_payload is None
    assert outcome.observation is not None
    assert outcome.observation.reconciliation_state.value == "ran_unreconciled"
    assert str(outcome.observation.deterministic_reconciliation_delta) == (
        case["expected_delta"]
    )
    assert [item.value for item in outcome.observation.eligible_supplementary_targets] == [
        "total_mismatch"
    ]
    assert outcome.working_observation_payload["reconciliation_state"] == (
        "ran_unreconciled"
    )
    with pytest.raises(ValidationError):
        outcome.observation.opaque_document_id = "mutated"


def test_total_mismatch_selects_only_minimized_target_without_accounting_authority():
    outcome = _initial(_cases()[0])
    payload = outcome.working_observation_payload
    targets = select_supplementary_targets(
        payload, ["invoice_reconciliation_failed"],
    )
    assert [item.target_type.value for item in targets] == ["total_mismatch"]
    minimized = build_minimized_initial_summary(payload, targets[0])
    encoded = json.dumps(minimized, sort_keys=True).casefold()
    for forbidden in (
        "gl_account", "selected_gl", "export_allowed", "readiness",
        "ground_truth", "holdout", "human_correction", "governed_rule",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["case_id"])
def test_missing_tax_or_fee_supplement_reconciles_as_separate_revision(case):
    initial = _initial(case)
    initial_dump = initial.observation.model_dump(mode="json")
    merged = _merged(initial, _observation(
        kind=case["resolving_kind"], value=case["resolving_value"],
    ))
    final = ai_provider.normalize_gemini_supplementary_observation(initial, merged)

    assert final.category is InitialNormalizationCategory.FACTS_READY
    assert final.facts_payload["reconciliation_state"] == "ran_reconciled"
    assert final.facts_payload["reconciliation_delta_before"] == case["expected_delta"]
    assert final.facts_payload["reconciliation_delta_after"] == "0.00"
    assert final.facts_payload["supplementary_visual_status"] == "resolved"
    assert final.facts_payload["initial_observation_revision"] == initial_dump
    assert len(final.facts_payload["supplementary_evidence_revisions"]) == 1
    assert final.facts_payload["observation_line_item_revisions"]["before"]
    assert final.facts_payload["observation_line_item_revisions"]["after"]
    assert initial.observation.model_dump(mode="json") == initial_dump


def test_missing_line_item_supplement_reconciles_without_replacing_initial_rows():
    initial = _initial(_cases()[0])
    line = {
        "source_page": 1,
        "section_header": "Observed charges",
        "row_label": "C",
        "location_candidate": None,
        "activity": "Synthetic omitted charge",
        "raw_description": "Synthetic omitted charge",
        "quantity": "1",
        "unit_price": "15.00",
        "amount": "15.00",
        "tax": None,
    }
    final = ai_provider.normalize_gemini_supplementary_observation(
        initial, _merged(initial, _observation(kind="line_item", line_item=line)),
    )
    assert final.category is InitialNormalizationCategory.FACTS_READY
    assert len(final.facts_payload["line_items"]) == 3
    assert len(final.facts_payload["observation_line_item_revisions"]["before"]) == 2
    assert len(final.facts_payload["observation_line_item_revisions"]["after"]) == 3
    assert final.facts_payload["reconciliation_delta_after"] == "0.00"


@pytest.mark.parametrize(
    ("observation", "warning", "primary", "visual_status"),
    [
        (
            _observation(kind="total_amount", value="110.00", contradiction=True),
            None,
            "supplementary_visual_evidence_contradiction",
            "contradiction",
        ),
        (
            _observation(kind="none", unresolved=True),
            None,
            "supplementary_visual_evidence_unresolved",
            "unresolved",
        ),
        (
            _observation(kind="none", unresolved=True),
            "supplementary_request_limit_reached",
            "supplementary_request_limit_reached",
            "request_limit_reached",
        ),
        (
            _observation(kind="none", unresolved=True),
            "supplementary_evidence_localization_unavailable",
            "supplementary_evidence_localization_unavailable",
            "unresolved",
        ),
    ],
    ids=("contradiction", "unresolved", "limit", "localization"),
)
def test_unresolved_supplement_is_review_required_and_fail_closed(
    observation, warning, primary, visual_status,
):
    initial = _initial(_cases()[0])
    merged = _merged(initial, observation)
    if warning:
        merged["warnings"] = [*merged.get("warnings", []), warning]
    final = ai_provider.normalize_gemini_supplementary_observation(initial, merged)
    assert final.category is InitialNormalizationCategory.SUPPLEMENTARY_REQUIRED
    assert final.facts_payload is None
    assert final.failure_code == primary

    payload = _review_payload(final)
    review = payload["manual_review_rows"][0]
    disposition = _derive_terminal_disposition(payload)
    assert disposition.disposition is PhaseATerminalDisposition.REVIEW_REQUIRED
    assert disposition.sanitized_failure_code == primary
    assert disposition.document_facts_exist is False
    assert disposition.intermediate_observation_exists is True
    assert disposition.accepted is False
    assert disposition.exportable is False
    assert disposition.reconciliation_state in {
        "ran_unreconciled", "ran_inconclusive",
    }
    assert disposition.supplementary_visual_status == visual_status
    assert "processor_failure" not in review["reason_codes"]
    assert "ai_processing_failed" not in review["reason_codes"]
    if primary == "supplementary_evidence_localization_unavailable":
        assert review["reason_codes"] == [primary]
        assert "supplementary_visual_evidence_unresolved" not in review["reason_codes"]


def test_localization_reason_persists_from_review_row_through_terminal_result(
    tmp_path,
):
    initial = _initial(_cases()[0])
    merged = _merged(initial, _observation(kind="none", unresolved=True))
    merged["warnings"] = [
        *merged.get("warnings", []),
        "supplementary_evidence_localization_unavailable",
    ]
    outcome = ai_provider.normalize_gemini_supplementary_observation(initial, merged)
    payload = _review_payload(outcome)
    assert payload["manual_review_rows"][0]["reason_codes"] == [
        "supplementary_evidence_localization_unavailable",
    ]

    result_path = tmp_path / "_webapp_result.json"
    result = _finalize_controlled_processor_result(
        payload,
        result_path=result_path,
        normalize_result=lambda _value: pytest.fail(
            "normalization must not run without valid DocumentFacts"
        ),
        attach_readiness=lambda _value: pytest.fail(
            "readiness must not run without valid DocumentFacts"
        ),
        assert_provenance=lambda _value: pytest.fail(
            "strict provenance must not run without valid DocumentFacts"
        ),
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    for safe_result in (result, persisted):
        assert len(safe_result["manual_review_rows"]) == 1
        review = safe_result["manual_review_rows"][0]
        assert review["review_required"] is True
        assert review["reason_codes"] == [
            "supplementary_evidence_localization_unavailable",
        ]
        assert "supplementary_visual_evidence_unresolved" not in review["reason_codes"]
        terminal = safe_result["phase_a_terminal_disposition"]
        assert terminal["disposition"] == "review_required"
        assert terminal["sanitized_failure_code"] == (
            "supplementary_evidence_localization_unavailable"
        )
        assert terminal["accepted"] is False
        assert terminal["exportable"] is False
        assert safe_result["export_allowed"] is False


def test_reconciled_but_visually_unresolved_facts_remain_review_marked():
    initial = _initial(_cases()[0])
    resolving = _observation(kind="tax_amount", value="15.00")
    unresolved = _observation(kind="none", unresolved=True)
    merged = merge_supplementary_observations(
        initial.working_observation_payload,
        [(_target(), resolving), (_target().model_copy(update={
            "field_name": "page_continuation",
        }), unresolved)],
    )
    final = ai_provider.normalize_gemini_supplementary_observation(initial, merged)
    assert final.category is InitialNormalizationCategory.FACTS_READY
    assert final.facts_payload["reconciliation_state"] == "ran_reconciled"
    assert final.facts_payload["supplementary_visual_status"] == "unresolved"
    assert final.facts_payload["needs_manual_review"] is True
    assert final.facts_payload["visual_extraction_status"] == "partial"


def test_malformed_transport_remains_unsupported_not_intermediate():
    with pytest.raises(GeminiTransportJSONError):
        parse_and_normalize_gemini_facts(
            '{"vendor_name":',
            provider="gemini",
            model="synthetic-offline-model",
            request_profile="synthetic-offline-facts-only",
        )
    payload = {
        "summary": {"processing_failures": 1},
        "invoices": [],
        "manual_review_rows": [],
        "unsupported_files": [{
            "reason_code": "initial_structured_response_invalid",
        }],
    }
    disposition = _derive_terminal_disposition(payload)
    assert disposition.disposition is PhaseATerminalDisposition.UNSUPPORTED
    assert disposition.reconciliation_state == "unavailable_due_to_missing_facts"
    assert disposition.intermediate_observation_exists is False


def test_typed_outcomes_cover_unusable_structure_and_controlled_block():
    payload = _transport(_cases()[0])
    payload["line_items"] = []
    unsupported = ai_provider.normalize_gemini_initial_observation(
        payload,
        opaque_document_id="opaque-unsupported",
        provider="gemini",
        profile_id="synthetic-offline-facts-only",
        model_id="synthetic-offline-model",
    )
    assert unsupported.category is InitialNormalizationCategory.UNSUPPORTED
    assert unsupported.facts_payload is None
    assert unsupported.observation is None
    assert unsupported.failure_code == "initial_structured_response_invalid"

    blocked = InitialNormalizationOutcome.blocked(
        validation_path="controlled_execution",
        failure_code="controlled_provider_route_blocked",
    )
    assert blocked.category is InitialNormalizationCategory.BLOCKED
    assert blocked.facts_payload is None
    assert blocked.observation is None


def test_intermediate_never_reaches_accounting_or_readiness(tmp_path):
    initial = _initial(_cases()[0])
    final = ai_provider.normalize_gemini_supplementary_observation(
        initial, _merged(initial, _observation(kind="none", unresolved=True)),
    )
    called: list[str] = []

    def forbidden(name):
        def _call(_value):
            called.append(name)
            raise AssertionError(f"{name} must not run without DocumentFacts")
        return _call

    result = _finalize_controlled_processor_result(
        _review_payload(final),
        result_path=tmp_path / "_webapp_result.json",
        normalize_result=forbidden("AccountingDecisionEngine"),
        attach_readiness=forbidden("AccountingReadiness"),
        assert_provenance=forbidden("strict provenance assertion"),
    )
    assert called == []
    assert result["export_allowed"] is False
    terminal = result["phase_a_terminal_disposition"]
    assert terminal["accepted"] is False
    assert terminal["exportable"] is False
    assert terminal["reconciliation_state"] in {
        "ran_unreconciled", "ran_inconclusive",
    }


def test_metrics_separate_transport_intermediate_strict_and_supplement_resolution():
    initial = _initial(_cases()[0])
    unresolved = ai_provider.normalize_gemini_supplementary_observation(
        initial, _merged(initial, _observation(kind="none", unresolved=True)),
    )
    review_disposition = _derive_terminal_disposition(
        _review_payload(unresolved)
    ).to_dict()
    strict_disposition = {
        **review_disposition,
        "disposition": "review_required",
        "document_facts_exist": True,
        "intermediate_observation_exists": False,
        "provenance_exists": True,
        "reconciliation_state": "ran_reconciled",
        "reconciliation_ran": True,
        "reconciliation_status": "reconciled",
        "supplementary_visual_status": "resolved",
    }
    metrics = _terminal_quality_metrics(
        [review_disposition, strict_disposition],
        [
            {"supplementary_visual_evidence_unresolved"},
            set(),
        ],
    )
    assert metrics["transport_valid_observation_count"] == 2
    assert metrics["intermediate_unreconciled_observation_count"] == 1
    assert metrics["document_facts_document_count"] == 1
    assert metrics["review_required_unresolved_arithmetic_count"] == 1
    assert metrics["supplementary_attempted_document_count"] == 2
    assert metrics["supplementary_resolved_document_count"] == 1
    assert metrics["supplementary_resolution_rate"] == 0.5
