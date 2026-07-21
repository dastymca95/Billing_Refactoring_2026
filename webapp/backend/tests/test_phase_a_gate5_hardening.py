from __future__ import annotations

import hashlib
import inspect
import json
import threading
from pathlib import Path

import pytest

from webapp.backend import settings
from webapp.backend.services import ai_invoice_processor, ai_provider, ai_runtime_trace
from webapp.backend.services.controlled_external_experiment import (
    CONTROLLED_EXTERNAL_CONTRACT_VERSION,
    PRIVATE_AUTHORIZATION_SHA256,
    ControlledCallPurpose,
    ControlledExternalController,
    ControlledExternalGateTerminated,
    ControlledGateExecutionState,
    activate_controlled_external,
    activate_experiment_provider_context,
    assert_controlled_external_dispatch_allowed,
    build_experiment_provider_context,
    controlled_document_scope,
    preflight_controlled_provider_route,
)
from webapp.backend.services.phase_a_calibration_runner import (
    _derive_terminal_disposition,
    _finalize_controlled_processor_result,
    _persist_controlled_gate_failure_result,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "phase_a_gate5_rejected_synthetic.json"
)
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/openai/chat/completions"
)


def _controlled(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    digest = hashlib.sha256(b"synthetic-gate5-document").hexdigest()
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
        }, sort_keys=True),
        encoding="utf-8",
    )
    authorization = private / "authorization.json"
    controller = ControlledExternalController(
        private_root=private,
        experiment_id="exp-synthetic-gate5",
        manifest_path=manifest,
        inventory_path=inventory,
        authorization_path=authorization,
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


def _scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, context, digest = _controlled(tmp_path)
    return controller, context, digest


def _preflight(context, *, provider="gemini", purpose=ControlledCallPurpose.INITIAL_EXTRACTION):
    return preflight_controlled_provider_route(
        provider_context=context,
        provider=provider,
        model=(
            context.authorized_model if provider == "gemini" else "forbidden-model"
        ),
        profile_id=(
            context.authorized_profile_id if provider == "gemini" else "runtime-vision"
        ),
        endpoint=(
            context.allowed_endpoint
            if provider == "gemini"
            else {
                "openai": "https://api.openai.com/v1/chat/completions",
                "deepseek": "https://api.deepseek.com/chat/completions",
                "claude": "https://api.anthropic.com/v1/messages",
            }[provider]
        ),
        call_purpose=purpose,
        stage="synthetic_gate5",
    )


def test_every_controlled_visual_route_has_explicit_context_parameter():
    routes = (
        ai_invoice_processor.process_ai_vendor_files,
        ai_invoice_processor._process_segmented_invoice_groups,
        ai_provider.extraction_profile_identity,
        ai_provider.extract_invoice_structured,
        ai_provider.extract_invoice_vision_structured,
        ai_provider.extract_invoice_facts_only_vision_structured,
        ai_provider.controlled_gemini_supplementary_profile_identity,
        ai_provider.extract_gemini_supplementary_facts_structured,
        ai_provider.extract_invoice_critical_fields_vision_structured,
        ai_provider.extract_handwritten_row_identities_vision_structured,
        ai_provider.extract_invoice_native_pdf_structured,
    )
    for route in routes:
        parameters = inspect.signature(route).parameters
        expected = (
            "experiment_provider_context"
            if route in {
                ai_invoice_processor.process_ai_vendor_files,
                ai_invoice_processor._process_segmented_invoice_groups,
            }
            else "experiment_context"
        )
        assert expected in parameters, route.__name__


def test_controlled_status_never_inherits_production_openai_defaults(
    tmp_path, monkeypatch,
):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "AI_VISION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "AI_VISION_MODEL", "gpt-5.6-luna")
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        status = ai_provider.provider_status(context)
    assert status.provider == "gemini"
    assert status.vision_provider == "gemini"
    assert status.model == "synthetic-gemini-model"
    assert "gpt-5.6" not in json.dumps(status.__dict__)


@pytest.mark.parametrize("provider", ["openai", "deepseek", "claude"])
def test_unauthorized_provider_is_fatal_before_budget_or_spend(
    tmp_path, monkeypatch, provider,
):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    before = context.remaining_call_budget
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        with pytest.raises(
            ControlledExternalGateTerminated,
            match="controlled_provider_route_blocked",
        ):
            _preflight(context, provider=provider)
    assert context.remaining_call_budget == before
    assert not (tmp_path / "private" / "spend_ledger.json").exists()


def test_shared_budget_rejects_fourth_external_call_before_request_construction(
    tmp_path, monkeypatch,
):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    constructed: list[str] = []

    def construct_after_preflight(purpose: ControlledCallPurpose):
        permit = _preflight(context, purpose=purpose)
        constructed.append(purpose.value)
        return permit

    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        construct_after_preflight(ControlledCallPurpose.INITIAL_EXTRACTION)
        construct_after_preflight(ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION)
        construct_after_preflight(ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION)
        with pytest.raises(
            ControlledExternalGateTerminated,
            match="supplementary_request_limit_reached",
        ):
            construct_after_preflight(ControlledCallPurpose.OTHER_VISUAL)

    assert constructed == [
        "initial_extraction",
        "supplementary_verification",
        "supplementary_verification",
    ]
    assert context.remaining_call_budget == {
        "initial_used": 1,
        "initial_remaining": 0,
        "supplementary_used": 2,
        "supplementary_remaining": 0,
    }


def test_preflight_permit_can_be_consumed_only_once(tmp_path, monkeypatch):
    controller, context, digest = _scope(tmp_path, monkeypatch)
    payload = {
        "model": context.authorized_model,
        "response_format": ai_provider.gemini_response_format(),
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AA=="},
            }],
        }],
    }
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context), ai_runtime_trace.operation(
        batch_id="", stage="synthetic_gate5", provider="gemini",
        model=context.authorized_model, profile_id=context.authorized_profile_id,
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.001,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=1.0,
        )
        permit = _preflight(context)
        assert_controlled_external_dispatch_allowed(
            provider="gemini", url=context.allowed_endpoint,
            stage="synthetic_gate5", payload=payload,
            provider_context=context,
            call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
            profile_id=context.authorized_profile_id,
            call_permit=permit,
        )
        with pytest.raises(
            ControlledExternalGateTerminated,
            match="controlled_provider_route_blocked",
        ):
            assert_controlled_external_dispatch_allowed(
                provider="gemini", url=context.allowed_endpoint,
                stage="synthetic_gate5", payload=payload,
                provider_context=context,
                call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
                profile_id=context.authorized_profile_id,
                call_permit=permit,
            )


def test_rejected_gate5_synthetic_fixture_stops_after_first_block(
    tmp_path, monkeypatch,
):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["legacy_observation"] == {
        "continued_after_first_block": True,
        "generic_processor_failure_present": True,
        "supplementary_over_limit_attempt_present": True,
        "unauthorized_route_attempt_present": True,
    }
    controller, context, digest = _scope(tmp_path, monkeypatch)
    state = ControlledGateExecutionState(len(fixture["documents"]))
    persisted: list[dict] = []

    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context):
        for index, _document in enumerate(fixture["documents"]):
            if not state.claim(index):
                break
            try:
                _preflight(context, provider="openai")
            except ControlledExternalGateTerminated as exc:
                state.terminate(exc.failure_code)
                persisted.append(_persist_controlled_gate_failure_result(
                    result_path=tmp_path / f"result-{index}.json",
                    failure_code=exc.failure_code,
                ))
                state.complete(index)
                break

    snapshot = state.snapshot()
    assert snapshot["started_indices"] == [0]
    assert snapshot["completed_indices"] == [0]
    assert snapshot["not_started_indices"] == [1, 2, 3, 4]
    assert snapshot["failure_code"] == "controlled_provider_route_blocked"
    assert persisted[0]["gate_failure_reason"] == "controlled_provider_route_blocked"
    assert persisted[0]["export_allowed"] is False
    assert persisted[0]["phase_a_terminal_disposition"]["accepted"] is False


def test_worker_state_persists_inflight_and_rejects_late_claims():
    state = ControlledGateExecutionState(5)
    barrier = threading.Barrier(2)

    def inflight(index: int):
        assert state.claim(index)
        barrier.wait()
        state.complete(index)

    worker = threading.Thread(target=inflight, args=(0,))
    worker.start()
    barrier.wait()
    state.terminate("controlled_provider_route_blocked")
    assert state.claim(1) is False
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert state.snapshot() == {
        "assignment_count": 5,
        "started_indices": [0],
        "completed_indices": [0],
        "not_started_indices": [1, 2, 3, 4],
        "failure_code": "controlled_provider_route_blocked",
        "cancelled": True,
    }


def test_independent_safe_synthetic_gate_preserves_all_dispositions():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    state = ControlledGateExecutionState(5)
    dispositions = []
    for index, document in enumerate(fixture["documents"]):
        assert state.claim(index)
        dispositions.append(document["legacy_disposition"])
        state.complete(index)
    assert state.snapshot()["not_started_indices"] == []
    assert dispositions == [
        "review_required", "review_required", "review_required",
        "review_required", "unsupported",
    ]
    assert "accepted" not in dispositions


def test_canonical_reasons_prevent_generic_processor_failure(tmp_path):
    for code in (
        "controlled_provider_route_blocked",
        "supplementary_request_limit_reached",
        "initial_structured_response_invalid",
        "supplementary_visual_evidence_unresolved",
        "supplementary_visual_evidence_contradiction",
        "visual_evidence_unavailable",
    ):
        result = _persist_controlled_gate_failure_result(
            result_path=tmp_path / f"{code}.json",
            failure_code=code,
        )
        terminal = result["phase_a_terminal_disposition"]
        assert terminal["sanitized_failure_code"] == code
        assert "processor_failure" not in json.dumps(result)
        assert terminal["accepted"] is False
        assert result["export_allowed"] is False
        if code == "supplementary_request_limit_reached":
            assert terminal["disposition"] == "review_required"
        if code in {
            "initial_structured_response_invalid",
            "visual_evidence_unavailable",
        }:
            assert terminal["disposition"] == "unsupported"


def test_schema_invalid_initial_response_is_unsupported_and_never_runs_authorities(
    tmp_path,
):
    payload = {
        "summary": {"processing_failures": 0},
        "invoices": [],
        "manual_review_rows": [],
        "unsupported_files": [{
            "reason_code": "initial_structured_response_invalid",
            "reason": "initial_structured_response_invalid",
        }],
    }
    disposition = _derive_terminal_disposition(payload)
    assert disposition.disposition.value == "unsupported"
    assert disposition.sanitized_failure_code == "initial_structured_response_invalid"
    calls = {"normalize": 0, "readiness": 0, "provenance": 0}
    result = _finalize_controlled_processor_result(
        payload,
        result_path=tmp_path / "invalid-schema-result.json",
        normalize_result=lambda _: calls.__setitem__("normalize", 1),
        attach_readiness=lambda _: calls.__setitem__("readiness", 1),
        assert_provenance=lambda _: calls.__setitem__("provenance", 1),
    )
    assert calls == {"normalize": 0, "readiness": 0, "provenance": 0}
    assert result["export_allowed"] is False
    assert result["phase_a_terminal_disposition"]["accepted"] is False


def test_normal_production_provider_status_is_unchanged(monkeypatch):
    monkeypatch.delenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "AI_MODEL", "production-model")
    monkeypatch.setattr(settings, "AI_VISION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "AI_VISION_MODEL", "production-vision-model")
    status = ai_provider.provider_status()
    assert status.provider == "openai"
    assert status.model == "production-model"
    assert status.vision_provider == "openai"
    assert status.vision_model == "production-vision-model"
