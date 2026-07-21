from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from webapp.backend.services import ai_invoice_processor, ai_provider
from webapp.backend.services.controlled_external_experiment import (
    ControlledCallPermit,
    ControlledCallPermitLifecycle,
    ControlledCallPermitState,
    ControlledCallPurpose,
    ControlledDocumentCallBudget,
    ControlledExternalController,
    ControlledExternalGateTerminated,
    activate_controlled_external,
    activate_experiment_provider_context,
    build_experiment_provider_context,
    controlled_document_scope,
)
from webapp.backend.services.phase_a_calibration_runner import (
    _derive_terminal_disposition,
    _persist_controlled_gate_failure_result,
)


GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/openai/chat/completions"
)


def _scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL"
    )
    private = tmp_path / "private"
    private.mkdir()
    digest = hashlib.sha256(b"synthetic-permit-document").hexdigest()
    inventory = private / "inventory.jsonl"
    inventory.write_text(
        json.dumps({"document_id": "synthetic-doc", "content_sha256": digest})
        + "\n",
        encoding="utf-8",
    )
    manifest = private / "manifest.json"
    manifest.write_text(
        json.dumps({
            "schema_version": "document-learning-phase-a-calibration/1.0",
            "assignments": [{"document_id": "synthetic-doc"}],
            "answers_embedded": False,
        }),
        encoding="utf-8",
    )
    controller = ControlledExternalController(
        private_root=private,
        experiment_id="exp-synthetic-permit",
        manifest_path=manifest,
        inventory_path=inventory,
    )
    context = build_experiment_provider_context(
        controller=controller,
        document_sha256=digest,
        authorized_provider="gemini",
        authorized_model="synthetic-gemini-model",
        authorized_profile_id="synthetic-gemini-profile",
        allowed_endpoint=GEMINI_ENDPOINT,
    )
    return controller, context, digest


def _profile(context):
    return SimpleNamespace(
        provider="gemini",
        model_id=context.authorized_model,
        profile_id=context.authorized_profile_id,
        api_key=SecretStr("synthetic-only"),
        base_url=GEMINI_ENDPOINT.rsplit("/chat/completions", 1)[0],
        timeout_seconds=5,
        max_retries=0,
        input_cost_usd_per_million=1.0,
        output_cost_usd_per_million=1.0,
    )


def _facts():
    return {
        "vendor_name": "",
        "invoice_number": "",
        "invoice_date": "",
        "due_date": "",
        "total_amount": 0,
        "line_items": [],
        "warnings": ["synthetic_review_required"],
        "vision_candidates": [],
    }


def _install_runtime(monkeypatch, context, *, cache_value=None, events=None):
    events = events if events is not None else []
    profile = _profile(context)
    monkeypatch.setattr(
        ai_provider, "_select_cost_routing_profile",
        lambda *_args, **_kwargs: profile,
    )

    def cache_lookup(*_args, **_kwargs):
        events.append("cache_lookup")
        return cache_value

    monkeypatch.setattr(ai_provider, "_load_extraction_cache", cache_lookup)
    monkeypatch.setattr(
        ai_provider, "_estimated_profile_request_cost",
        lambda *_args, **_kwargs: 0.001,
    )
    monkeypatch.setattr(
        ai_provider, "_update_profile_cost_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ai_provider, "_reserve_cost_budget",
        lambda *_args, **_kwargs: events.append("local_cost_budget"),
    )
    monkeypatch.setattr(
        ai_provider, "parse_and_normalize_gemini_facts",
        lambda *_args, **_kwargs: _facts(),
    )
    monkeypatch.setattr(ai_provider, "_validate_invoice_schema", lambda value: value)
    monkeypatch.setattr(
        ai_provider, "_validate_visual_line_structure",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(
        ai_provider, "_save_extraction_cache", lambda *_args, **_kwargs: None
    )
    return events


def _extract(context):
    return ai_provider.extract_invoice_facts_only_vision_structured(
        document_text="synthetic text",
        page_images_or_refs=["data:image/png;base64,AA=="],
        cost_scope_id="synthetic-scope",
        experiment_context=context,
    )


def test_cache_miss_reserves_before_lookup_and_passes_same_permit(
    tmp_path, monkeypatch,
):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    events: list[str] = []
    _install_runtime(monkeypatch, context, events=events)
    reserved: list[ControlledCallPermit] = []
    dispatched: list[ControlledCallPermit] = []
    original_reserve = context.call_budget.reserve

    def reserve(purpose):
        events.append("reserve")
        permit = original_reserve(purpose)
        reserved.append(permit)
        return permit

    def send(**kwargs):
        events.append("dispatch_guard")
        permit = kwargs["controlled_call_permit"]
        dispatched.append(permit)
        kwargs["experiment_context"].call_budget.consume(
            permit, ControlledCallPurpose.INITIAL_EXTRACTION
        )
        return "{}"

    monkeypatch.setattr(context.call_budget, "reserve", reserve)
    monkeypatch.setattr(ai_provider, "_send_chat_completion", send)
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        result = _extract(context)

    assert result["_facts_only"] is True
    assert events.index("reserve") < events.index("cache_lookup")
    assert reserved == dispatched
    assert context.call_budget.permit_state(reserved[0]) is (
        ControlledCallPermitState.CONSUMED_FOR_DISPATCH
    )
    assert context.remaining_call_budget["initial_used"] == 1


def test_cache_hit_releases_slot_without_request_or_spend(tmp_path, monkeypatch):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    events: list[str] = []
    _install_runtime(monkeypatch, context, cache_value=_facts(), events=events)
    permits: list[ControlledCallPermit] = []
    original_reserve = context.call_budget.reserve

    def reserve(purpose):
        permit = original_reserve(purpose)
        permits.append(permit)
        return permit

    monkeypatch.setattr(context.call_budget, "reserve", reserve)
    monkeypatch.setattr(
        ai_provider, "_send_chat_completion",
        lambda **_kwargs: pytest.fail("cache hit must not construct provider request"),
    )
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        _extract(context)

    assert events == ["cache_lookup"]
    assert context.call_budget.permit_state(permits[0]) is (
        ControlledCallPermitState.RELEASED_FOR_CACHE_HIT
    )
    assert context.remaining_call_budget["initial_used"] == 0
    assert not (tmp_path / "private" / "telemetry" / "spend_ledger.json").exists()


def test_local_exception_before_dispatch_cancels_unconsumed_permit(
    tmp_path, monkeypatch,
):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    _install_runtime(monkeypatch, context)
    permits: list[ControlledCallPermit] = []
    original_reserve = context.call_budget.reserve

    def reserve(purpose):
        permit = original_reserve(purpose)
        permits.append(permit)
        return permit

    monkeypatch.setattr(context.call_budget, "reserve", reserve)
    monkeypatch.setattr(
        ai_provider, "_load_extraction_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local")),
    )
    monkeypatch.setattr(
        ai_provider, "_send_chat_completion",
        lambda **_kwargs: pytest.fail("transport must not be reached"),
    )
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        with pytest.raises(RuntimeError, match="local"):
            _extract(context)

    assert context.call_budget.permit_state(permits[0]) is (
        ControlledCallPermitState.CANCELED_BEFORE_DISPATCH
    )
    assert context.remaining_call_budget["initial_used"] == 0
    assert not (tmp_path / "private" / "telemetry" / "spend_ledger.json").exists()


def test_illegal_double_consume_and_double_release_are_rejected():
    budget = ControlledDocumentCallBudget()
    consumed = budget.reserve(ControlledCallPurpose.INITIAL_EXTRACTION)
    budget.consume(consumed, ControlledCallPurpose.INITIAL_EXTRACTION)
    with pytest.raises(
        ControlledExternalGateTerminated, match="controlled_provider_route_blocked"
    ):
        budget.consume(consumed, ControlledCallPurpose.INITIAL_EXTRACTION)
    with pytest.raises(
        ControlledExternalGateTerminated, match="controlled_provider_route_blocked"
    ):
        budget.release_for_cache_hit(consumed)

    released = ControlledDocumentCallBudget().reserve(
        ControlledCallPurpose.INITIAL_EXTRACTION
    )
    other_budget = ControlledDocumentCallBudget()
    with pytest.raises(
        ControlledExternalGateTerminated, match="controlled_provider_route_blocked"
    ):
        ControlledCallPermitLifecycle(other_budget, released)

    release_budget = ControlledDocumentCallBudget()
    released = release_budget.reserve(ControlledCallPurpose.INITIAL_EXTRACTION)
    release_budget.release_for_cache_hit(released)
    with pytest.raises(
        ControlledExternalGateTerminated, match="controlled_provider_route_blocked"
    ):
        release_budget.release_for_cache_hit(released)


def test_nested_initial_permit_and_total_budget_limits_are_enforced():
    budget = ControlledDocumentCallBudget()
    budget.reserve(ControlledCallPurpose.INITIAL_EXTRACTION)
    with pytest.raises(ControlledExternalGateTerminated):
        budget.reserve(ControlledCallPurpose.INITIAL_EXTRACTION)
    budget.reserve(ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION)
    budget.reserve(ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION)
    with pytest.raises(
        ControlledExternalGateTerminated,
        match="supplementary_request_limit_reached",
    ):
        budget.reserve(ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION)
    assert budget.snapshot() == {
        "initial_used": 1,
        "initial_remaining": 0,
        "supplementary_used": 2,
        "supplementary_remaining": 0,
    }


def test_all_local_name_errors_have_one_canonical_blocked_disposition(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL"
    )
    reason, _ = ai_invoice_processor._safe_processing_failure(
        NameError("undefined synthetic symbol")
    )
    assert reason == "controlled_local_execution_error"
    payloads = [
        {
            "summary": {"processing_failures": index % 2},
            "invoices": [],
            "manual_review_rows": ([{
                "reason_codes": [reason], "provenance_exists": bool(index % 2),
            }] if index < 3 else []),
            "unsupported_files": ([{"reason": reason}] if index >= 3 else []),
        }
        for index in range(5)
    ]
    dispositions = [_derive_terminal_disposition(payload) for payload in payloads]
    assert {item.disposition.value for item in dispositions} == {"blocked"}
    assert {item.sanitized_failure_code for item in dispositions} == {reason}
    assert not any(item.accepted or item.exportable for item in dispositions)


def test_offline_five_document_cache_miss_replay_has_no_transport_or_spend(
    tmp_path, monkeypatch,
):
    controller, first_context, digest = _scope(tmp_path, monkeypatch)
    _install_runtime(monkeypatch, first_context)
    injected_responses = 0

    def send(**kwargs):
        nonlocal injected_responses
        injected_responses += 1
        permit = kwargs["controlled_call_permit"]
        kwargs["experiment_context"].call_budget.consume(
            permit, ControlledCallPurpose.INITIAL_EXTRACTION
        )
        return "{}"

    monkeypatch.setattr(ai_provider, "_send_chat_completion", send)
    contexts = [first_context]
    for _ in range(4):
        contexts.append(build_experiment_provider_context(
            controller=controller,
            document_sha256=digest,
            authorized_provider="gemini",
            authorized_model=first_context.authorized_model,
            authorized_profile_id=first_context.authorized_profile_id,
            allowed_endpoint=first_context.allowed_endpoint,
        ))

    results = []
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ):
        for index, context in enumerate(contexts):
            with activate_experiment_provider_context(context):
                facts = _extract(context)
            assert facts["_facts_only"] is True
            assert context.remaining_call_budget["initial_used"] == 1
            results.append(_persist_controlled_gate_failure_result(
                result_path=tmp_path / f"result-{index}.json",
                failure_code="supplementary_visual_evidence_unresolved",
            ))

    assert injected_responses == 5
    assert len(results) == 5
    assert all(result["phase_a_terminal_disposition"]["disposition"] == "review_required" for result in results)
    assert all(result["export_allowed"] is False for result in results)
    assert not (tmp_path / "private" / "telemetry" / "spend_ledger.json").exists()
