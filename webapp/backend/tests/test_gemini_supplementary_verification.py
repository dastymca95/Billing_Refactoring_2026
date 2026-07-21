from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from webapp.backend import settings
from webapp.backend.services import ai_invoice_processor, ai_provider, ai_runtime_trace
from webapp.backend.services.experiment_spend_controller import (
    ExperimentSpendController,
    SpendAuthorizationError,
)
from webapp.backend.services.gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    SupplementaryRequestLimiter,
    SupplementaryTarget,
    SupplementaryTargetType,
    SupplementaryVerificationError,
    merge_supplementary_observations,
    parse_supplementary_response,
    reconciliation_snapshot,
    select_supplementary_targets,
)


def _facts(*, line_amount="100.00", total="108.00") -> dict:
    return {
        "vendor_name": "Synthetic Vendor",
        "invoice_number": "SYN-1",
        "total_amount": total,
        "tax_amount": None,
        "shipping_amount": None,
        "fees_amount": None,
        "line_items": [{
            "source_page": 1,
            "row_label": "A",
            "activity": "Visible service",
            "raw_description": "Visible service",
            "quantity": "1",
            "unit_price": line_amount,
            "amount": line_amount,
            "evidence": [{"page": 1, "bbox": [1, 2, 3, 4], "text": "visible"}],
        }],
        "page_reconciliations": [{
            "page": 1,
            "component_total": line_amount,
            "printed_total": total,
            "status": "mismatch",
        }],
        "unresolved_visual_regions": [],
        "warnings": ["page_reconciliation_failed"],
        "visual_extraction_status": "partial",
        "evidence": [{"page": 1, "bbox": [0, 0, 10, 10], "text": "source"}],
    }


def _observation(target: SupplementaryTarget, *, kind: str, field: str | None = None,
                 value=None, line_item: dict | None = None, contradiction=False,
                 unresolved=False) -> GeminiSupplementaryObservation:
    payload = {
        "target_type": target.target_type.value,
        "observed_candidate_value": None if unresolved else {
            "resolution_kind": kind,
            "field_name": field,
            "raw_value": None if value is None else str(value),
            "line_item": line_item,
        },
        "raw_visible_text": "synthetic visible evidence",
        "page_number": target.page_number or 1,
        "evidence_reference": {"page_number": 1, "bbox": [10, 20, 30, 40]},
        "confidence": 0.95,
        "contradiction_flag": contradiction,
        "unresolved_flag": unresolved,
        "warnings": [],
    }
    return GeminiSupplementaryObservation.model_validate(payload)


def test_missing_tax_resolves_reconciliation_and_preserves_initial_source():
    original = _facts()
    frozen = copy.deepcopy(original)
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1, field_name="reconciliation",
    )
    merged = merge_supplementary_observations(
        original, [(target, _observation(target, kind="tax_amount", field="tax_amount", value=8))],
    )
    assert original == frozen
    assert str(merged["tax_amount"]) == "8"
    assert reconciliation_snapshot(merged)["reconciled"] is True
    assert merged["needs_manual_review"] is False
    assert merged["evidence"][0] == original["evidence"][0]
    assert merged["supplementary_evidence_revisions"][0]["source_role"] == (
        "supplementary_visual_observation"
    )


def test_missing_line_item_resolves_without_collapsing_existing_rows():
    original = _facts(line_amount="60.00", total="100.00")
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.MISSING_LINE_ITEM,
        page_number=1, field_name="line_items",
    )
    line = {
        "source_page": 1, "section_header": "Charges", "row_label": "B",
        "location_candidate": None, "activity": "Visible fee",
        "raw_description": "Visible fee", "quantity": 1, "unit_price": 40,
        "amount": 40, "tax": None,
    }
    merged = merge_supplementary_observations(
        original, [(target, _observation(target, kind="line_item", line_item=line))],
    )
    assert len(merged["line_items"]) == 2
    assert merged["line_items"][0] == original["line_items"][0]
    assert reconciliation_snapshot(merged)["reconciled"] is True
    assert merged["line_items"][1]["evidence"][0]["extraction_method"] == (
        "gemini_supplementary_verification"
    )


def test_supplementary_contradiction_preserves_original_and_remains_blocked():
    original = _facts(line_amount="100.00", total="100.00")
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1, field_name="reconciliation",
    )
    merged = merge_supplementary_observations(
        original,
        [(target, _observation(
            target, kind="total_amount", field="total_amount", value=110,
            contradiction=True,
        ))],
    )
    assert merged["total_amount"] == "100.00"
    assert merged["needs_manual_review"] is True
    assert any("supplementary_contradiction" in item for item in merged["warnings"])
    normalized = ai_invoice_processor.validate_ai_extraction(
        merged,
        references={"vendors": [], "properties": [], "gl_accounts": []},
    )
    assert "supplementary_visual_evidence_contradiction" in (
        normalized["manual_review_codes"]
    )


def test_unresolved_supplement_remains_visible_and_blocked():
    original = _facts()
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1, field_name="reconciliation",
    )
    merged = merge_supplementary_observations(
        original, [(target, _observation(target, kind="none", unresolved=True))],
    )
    assert merged["needs_manual_review"] is True
    assert merged["visual_extraction_status"] == "partial"
    assert merged["unresolved_visual_regions"][-1]["reason"] == (
        "supplementary_visual_evidence_unresolved"
    )


def test_malformed_supplementary_json_fails_closed():
    target = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    with pytest.raises(SupplementaryVerificationError, match="supplementary_invalid_json"):
        parse_supplementary_response('{"target_type":', target=target)


def test_structured_placeholder_line_item_is_ignored_for_scalar_resolution():
    target = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    payload = _observation(
        target, kind="tax_amount", field="tax_amount", value=8,
    ).model_dump(mode="json")
    payload["observed_candidate_value"]["line_item"] = {
        "source_page": None, "section_header": None, "row_label": None,
        "location_candidate": None, "activity": None, "raw_description": None,
        "quantity": None, "unit_price": None, "amount": None, "tax": None,
    }
    parsed = parse_supplementary_response(json.dumps(payload), target=target)
    assert parsed.observed_candidate_value.resolution_kind.value == "tax_amount"


def test_nonempty_line_item_conflict_is_preserved_and_blocked_for_scalar_resolution():
    target = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    payload = _observation(
        target, kind="tax_amount", field="tax_amount", value=8,
    ).model_dump(mode="json")
    payload["observed_candidate_value"]["line_item"] = {
        "source_page": 1, "section_header": None, "row_label": None,
        "location_candidate": None, "activity": None, "raw_description": "other charge",
        "quantity": None, "unit_price": None, "amount": "8", "tax": None,
    }
    parsed = parse_supplementary_response(json.dumps(payload), target=target)
    assert parsed.contradiction_flag is True
    assert "supplementary_incompatible_candidate_branches" in parsed.warnings
    merged = merge_supplementary_observations(_facts(), [(target, parsed)])
    assert merged["needs_manual_review"] is True
    assert merged["tax_amount"] is None


def test_second_supplementary_limit_blocks_third_request():
    limiter = SupplementaryRequestLimiter()
    first = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    second = SupplementaryTarget(target_type=SupplementaryTargetType.DATE_AMBIGUITY)
    third = SupplementaryTarget(target_type=SupplementaryTargetType.VENDOR_NAME_AMBIGUITY)
    limiter.authorize(first)
    limiter.authorize(second)
    with pytest.raises(SupplementaryVerificationError, match="request_limit_reached"):
        limiter.authorize(third)


def test_recursive_verification_is_impossible():
    facts = _facts()
    facts["supplementary_evidence_revisions"] = [{"revision_number": 1}]
    with pytest.raises(SupplementaryVerificationError, match="recursive"):
        select_supplementary_targets(facts, ["page_reconciliation_failed"])


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_non_gemini_supplementary_profile_is_blocked_before_transport(monkeypatch, provider):
    from webapp.backend.services.controlled_external_experiment import (
        ControlledDocumentCallBudget,
        ExperimentExecutionMode,
        ExperimentProviderContext,
    )

    context = ExperimentProviderContext(
        execution_mode=ExperimentExecutionMode.CONTROLLED_EXTERNAL,
        authorized_provider="gemini",
        authorized_model="configured-model",
        authorized_profile_id="configured-profile",
        allowed_endpoint=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/chat/completions"
        ),
        manifest_sha256="a" * 64,
        document_sha256="b" * 64,
        call_budget=ControlledDocumentCallBudget(),
    )
    monkeypatch.setattr(ai_provider, "controlled_external_active", lambda: True)
    monkeypatch.setattr(
        ai_provider,
        "require_experiment_provider_context",
        lambda value: value,
    )
    monkeypatch.setattr(ai_provider, "_select_cost_routing_profile", lambda _role, experiment_context=None: SimpleNamespace(
        provider=provider, model_id="configured-model", profile_id="configured-profile",
        credentials_present=True, base_url=context.allowed_endpoint.rsplit("/chat/completions", 1)[0],
    ))
    target = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    with pytest.raises(ai_provider.AIProviderUnavailable, match="authorized Gemini"):
        ai_provider.controlled_gemini_supplementary_profile_identity(
            target, experiment_context=context,
        )


def test_target_outside_deterministic_allowlist_is_rejected():
    with pytest.raises(ValidationError):
        SupplementaryTarget(target_type="repair_whole_invoice")


def test_spend_cap_blocks_before_dispatch(tmp_path):
    controller = ExperimentSpendController(tmp_path / "private", "exp-supplementary")
    controller.reserve(
        phase="A", estimated_cost_usd="10.00", provider="gemini",
        model_id="configured-model", profile_id="gemini-vision",
        stage="controlled_gemini_supplementary:total_mismatch",
        purpose="controlled_gemini_supplementary:total_mismatch",
    )
    with pytest.raises(SpendAuthorizationError):
        controller.reserve(
            phase="A", estimated_cost_usd="0.01", provider="gemini",
            model_id="configured-model", profile_id="gemini-vision",
            stage="controlled_gemini_supplementary:total_mismatch",
            purpose="controlled_gemini_supplementary:total_mismatch",
        )
    assert not any(
        item.get("status") == "dispatched"
        for item in controller._read_state()["reservations"].values()
    )


def test_production_fast_first_escalation_keeps_existing_route(monkeypatch):
    initial = _facts()
    monkeypatch.setattr(ai_invoice_processor.fast_first_facts, "production_enabled", lambda: True)
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider,
        "extract_invoice_facts_only_vision_structured",
        lambda **_kwargs: copy.deepcopy(initial),
    )
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider, "controlled_external_active", lambda: False,
    )
    calls = []
    monkeypatch.setattr(
        ai_invoice_processor, "_extract_vision_with_reduced_retry",
        lambda **kwargs: calls.append(kwargs) or {"warnings": [], "line_items": []},
    )
    result = ai_invoice_processor._extract_fast_first_or_standard(
        document_text="synthetic", page_images_or_refs=["data:image/png;base64,AA=="],
        model_override="configured", cost_scope_id="batch_safe",
        _source_page_numbers=[1],
    )
    assert len(calls) == 1
    assert "_source_page_numbers" not in calls[0]
    assert any("fast_first_escalated" in item for item in result["warnings"])


def test_supplementary_telemetry_contains_only_safe_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    with ai_runtime_trace.operation(
        batch_id="batch_safe", stage="controlled_gemini_supplementary:total_mismatch",
        provider="gemini", model="configured-model", profile_id="gemini-vision",
    ):
        ai_runtime_trace.record_supplementary_verification(
            target_category="total_mismatch", request_count=1, schema_valid=True,
            reconciliation_before="mismatch", reconciliation_after="reconciled",
            resolved=True, evidence_reference_count=1,
        )
    text = (tmp_path / "batch_safe" / "audit" / "ai_request_trace.jsonl").read_text()
    assert "Synthetic Vendor" not in text
    event = json.loads(text.splitlines()[-1])
    assert set(event) <= {
        "schema", "event", "batch_id", "request_id", "stage", "provider", "model",
        "profile_id", "target_category", "request_count", "schema_valid",
        "reconciliation_before", "reconciliation_after", "resolved",
        "evidence_reference_count", "failure_code", "at",
    }


def test_supplementary_plan_telemetry_excludes_private_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    with ai_runtime_trace.operation(
        batch_id="batch_safe", stage="supplementary_planning",
        provider="", model="", profile_id="",
    ):
        ai_runtime_trace.record_supplementary_evidence_plan(
            target_category="total_mismatch",
            target_subtype="payment_or_deposit",
            outcome="packet_validated",
            crop_count=3,
            crop_roles=["primary_target", "related_evidence", "not_allowed"],
            combined_pixels=500000,
            plan_id="a" * 24,
            second_slot_reason="distinct_deterministic_target",
        )
    text = (tmp_path / "batch_safe" / "audit" / "ai_request_trace.jsonl").read_text()
    assert "private filename" not in text
    event = json.loads(text.splitlines()[-1])
    assert event["event"] == "supplementary_evidence_plan"
    assert event["crop_roles"] == ["primary_target", "related_evidence"]
    assert "coordinates" not in event
    assert "image_sha256" not in event


def test_downstream_diagnostic_contains_only_explicitly_authorized_categories(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    with ai_runtime_trace.operation(
        batch_id="batch_safe", stage="segmented_processing_failure:1",
        provider="must-not-persist", model="must-not-persist",
        profile_id="must-not-persist",
    ):
        ai_runtime_trace.record_processing_stage_failure(
            processing_stage="normalization",
            exception_type="ValidationError",
            failure_code="unsafe value / redacted",
            disposition_transition="processing->processing_failure",
            document_facts_created=False,
            reconciliation_completed=False,
            provenance_attached=True,
            persistence_attempted=False,
            final_disposition_written=False,
        )
    text = (tmp_path / "batch_safe" / "audit" / "ai_request_trace.jsonl").read_text()
    event = json.loads(text.splitlines()[-1])
    assert set(event) == {
        "schema", "event", "local_processing_stage", "safe_exception_type",
        "sanitized_failure_code", "disposition_transition",
        "document_facts_created", "reconciliation_completed",
        "provenance_attached", "persistence_attempted",
        "final_disposition_written",
    }
    assert event["sanitized_failure_code"] == "unspecified_failure"
    assert "must-not-persist" not in text
    assert "batch_safe" not in text


def test_runtime_trace_rejects_hostile_diagnostic_text(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    hostile_values = [
        r"C:\private\invoice.pdf",
        "person@example.invalid",
        "1234567890123456",
        "provider output\nsecond line",
        '{"account":"private"}',
        "Authorization: Bearer private-token",
    ]
    hostile = " | ".join(hostile_values)
    with ai_runtime_trace.operation(
        batch_id="batch_hostile_trace",
        stage=hostile,
        provider=hostile,
        model=hostile,
        profile_id=hostile,
    ):
        ai_runtime_trace.record_schema_result(hostile, retry_reason=hostile)
        ai_runtime_trace.record_supplementary_verification(
            target_category=hostile,
            request_count=0,
            schema_valid=False,
            reconciliation_before=hostile,
            reconciliation_after=hostile,
            resolved=False,
            evidence_reference_count=0,
            failure_code=hostile,
        )
        ai_runtime_trace.record_blocked_network_attempt(
            provider=hostile, stage=hostile, failure_code=hostile,
        )
        ai_runtime_trace.record_processing_stage_failure(
            processing_stage=hostile,
            exception_type=hostile,
            failure_code=hostile,
            disposition_transition=hostile,
        )
        ai_runtime_trace.record_structured_response_failure({
            "provider": hostile,
            "model": hostile,
            "request_profile": hostile,
            "response_byte_length": hostile,
            "response_character_length": hostile,
            "response_sha256": hostile,
            "first_non_whitespace_character_class": hostile,
            "last_non_whitespace_character_class": hostile,
            "json_object_boundary_count": hostile,
            "json_array_boundary_count": hostile,
            "finish_reason": hostile,
            "prompt_token_count": hostile,
            "output_token_count": hostile,
            "json_parser_error_type": hostile,
            "json_parser_error_character_offset": hostile,
            "schema_validation_error_path": hostile,
            "schema_validation_error_type": hostile,
            "schema_failure_category": hostile,
            "received_top_level_field_names": hostile_values,
            "missing_required_field_names": hostile_values,
            "received_top_level_value_types": {hostile: "string"},
            "unexpected_field_name_hashes": hostile_values,
            "unknown_field_count": hostile,
            "missing_required_field_count": hostile,
            "transport_schema_version": hostile,
        })

    trace = (
        tmp_path / "batch_hostile_trace" / "audit" / "ai_request_trace.jsonl"
    ).read_text(encoding="utf-8")
    for value in hostile_values:
        assert value not in trace
    assert "private-token" not in trace
    assert "person@example.invalid" not in trace
    events = [json.loads(line) for line in trace.splitlines()]
    assert {event.get("stage") for event in events if "stage" in event} == {"unknown"}
    assert any(event.get("sanitized_failure_code") == "unspecified_failure" for event in events)


def test_incompatible_provider_branches_become_review_not_processing_failure(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1, field_name="reconciliation",
    )
    payload = _observation(
        target, kind="tax_amount", field="tax_amount", value=8,
    ).model_dump(mode="json")
    payload["observed_candidate_value"]["line_item"] = {
        "source_page": 1, "section_header": None, "row_label": "conflict",
        "location_candidate": None, "activity": "other", "raw_description": "other",
        "quantity": 1, "unit_price": 8, "amount": 8, "tax": None,
    }
    observation = parse_supplementary_response(json.dumps(payload), target=target)
    monkeypatch.setattr(
        ai_provider, "controlled_gemini_supplementary_profile_identity",
        lambda _target, experiment_context=None: (
            "gemini", "gemini-vision:supplementary", "configured-model"
        ),
    )
    monkeypatch.setattr(
        ai_provider, "extract_gemini_supplementary_facts_structured",
        lambda **_kwargs: observation,
    )
    with ai_runtime_trace.operation(
        batch_id="batch_safe", stage="rendered_visual_facts", provider="gemini",
        model="configured-model", profile_id="gemini-vision",
    ):
        result = ai_invoice_processor._run_controlled_gemini_supplementary(
            initial_facts=_facts(),
            escalation_reasons=["page_reconciliation_failed"],
            page_images_or_refs=["data:image/png;base64,AA=="],
            page_numbers=[1], cost_scope_id="batch_safe",
        )
    assert result["needs_manual_review"] is True
    assert result["tax_amount"] is None
    normalized = ai_invoice_processor.validate_ai_extraction(
        result, references={"vendors": [], "properties": [], "gl_accounts": []},
    )
    assert normalized["validation_summary"]["valid"] is False
    assert normalized["manual_review_codes"]
