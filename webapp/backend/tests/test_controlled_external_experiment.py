from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from webapp.backend.services import ai_runtime_trace
from webapp.backend.services.controlled_external_experiment import (
    ALLOWED_PROVIDER_HOSTS,
    CONTROLLED_EXTERNAL_CONTRACT_VERSION,
    PRIVATE_AUTHORIZATION_SHA256,
    ControlledExternalBlocked,
    ControlledExternalController,
    ControlledCallPurpose,
    activate_controlled_external,
    activate_experiment_provider_context,
    assert_controlled_external_dispatch_allowed,
    build_experiment_provider_context,
    build_deepseek_minimized_facts,
    controlled_document_scope,
    execution_mode,
)
from webapp.backend.services.gemini_facts_transport import gemini_response_format
from webapp.backend.services.gemini_supplementary_verification import (
    SupplementaryTarget,
    SupplementaryTargetType,
    supplementary_response_format,
)


SYNTHETIC_PNG = "data:image/png;base64,iVBORw0KGgo="


def _controller(tmp_path, *, authorized: bool = False):
    private = tmp_path / "private"
    private.mkdir()
    digest = hashlib.sha256(b"synthetic-document").hexdigest()
    inventory = private / "inventory.jsonl"
    inventory.write_text(json.dumps({
        "document_id": "private-source-id",
        "content_sha256": digest,
    }) + "\n", encoding="utf-8")
    manifest = private / "calibration_manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "document-learning-phase-a-calibration/1.0",
        "assignments": [{"document_id": "private-source-id"}],
        "answers_embedded": False,
    }, sort_keys=True), encoding="utf-8")
    authorization = private / "authorization.json"
    controller = ControlledExternalController(
        private_root=private,
        experiment_id="exp-controlled-test",
        manifest_path=manifest,
        inventory_path=inventory,
        authorization_path=authorization,
    )
    if authorized:
        authorization.write_text(json.dumps({
            "contract_version": CONTROLLED_EXTERNAL_CONTRACT_VERSION,
            "experiment_id": controller.experiment_id,
            "manifest_sha256": controller.manifest_sha256,
            "authorization_sha256": PRIVATE_AUTHORIZATION_SHA256,
            "authorization_text_accepted": True,
            "gemini_account_settings_reviewed": True,
            "gemini_paid_project_operator_confirmed": True,
            "operator_confirmed_project_name": "Vision",
            "operator_confirmed_plan": "Paid, Tier 1 Prepay",
            "sensitive_private_transfer_risk_accepted": True,
            "provider_retention_risk_accepted": True,
            "operator_id": "authorized-operator",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
    return controller, digest


def test_controlled_telemetry_reduces_hostile_text_to_typed_categories(tmp_path):
    controller, _digest = _controller(tmp_path)
    hostile_values = (
        r"C:\private\client-invoice.pdf",
        "person@example.invalid",
        "account 1234567890",
        "provider output\nsecond line",
        '{"private":"value"}',
        "Authorization: Bearer private-token",
    )
    hostile = " | ".join(hostile_values)
    controller.record_event(
        event=hostile,
        provider=hostile,
        model=hostile,
        profile_id=hostile,
        document_sha256=hostile,
        opaque_document_id=hostile,
        purpose=hostile,
        result=hostile,
        failure_code=hostile,
        reservation_id=hostile,
        estimated_cost_usd=hostile,
        actual_cost_usd=hostile,
        host=hostile,
    )

    trace = controller.telemetry_path.read_text(encoding="utf-8")
    for value in hostile_values:
        assert value not in trace
    event = json.loads(trace)
    assert event["event"] == "unknown"
    assert event["provider"] == "unknown"
    assert event["purpose"] == "unknown"
    assert event["result"] == "unknown"
    assert event["failure_code"] == "unknown_controlled_failure"


def _gemini_payload():
    return {
        "model": "configured-gemini-model",
        "response_format": gemini_response_format(),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract observable facts as JSON."},
                {"type": "image_url", "image_url": {"url": SYNTHETIC_PNG}},
            ],
        }],
    }


def _deepseek_payload(content: dict | None = None):
    return {
        "model": "configured-deepseek-model",
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": json.dumps(content or {
                "schema_version": "controlled-deepseek-derived-facts/1.0",
                "experiment_document_id": "doc_opaque",
                "lines": [],
            }),
        }],
    }


def _assert_dispatch(controller, digest, *, provider, url, payload, stage="synthetic"):
    profile_id = "synthetic-preflight"
    context = build_experiment_provider_context(
        controller=controller,
        document_sha256=digest,
        authorized_provider="gemini",
        authorized_model="configured-gemini-model",
        authorized_profile_id=profile_id,
        allowed_endpoint=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/chat/completions"
        ),
    )
    purpose = (
        ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION
        if stage.startswith("controlled_gemini_supplementary:")
        else ControlledCallPurpose.INITIAL_EXTRACTION
    )
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context), ai_runtime_trace.operation(
        batch_id="", stage=stage, provider=provider,
        model=str(payload.get("model") or ""), profile_id=profile_id,
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.001,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=1.0,
        )
        assert_controlled_external_dispatch_allowed(
            provider=provider, url=url, stage=stage, payload=payload,
            provider_context=context, call_purpose=purpose,
            profile_id=profile_id,
        )


def test_mode_is_explicit_and_normal_routing_is_unchanged(monkeypatch):
    monkeypatch.delenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", raising=False)
    assert execution_mode().value == "NORMAL"
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    assert execution_mode().value == "CONTROLLED_EXTERNAL"


def test_exact_provider_host_and_endpoint_allowlist_accepts_only_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    _assert_dispatch(
        controller, digest, provider="gemini",
        url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        payload=_gemini_payload(), stage="vision",
    )
    assert set(ALLOWED_PROVIDER_HOSTS) == {"gemini"}


def test_targeted_supplementary_schema_is_allowed_only_for_its_exact_stage(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    target = SupplementaryTarget(target_type=SupplementaryTargetType.TOTAL_MISMATCH)
    payload = {
        **_gemini_payload(),
        "response_format": supplementary_response_format(target),
    }
    stage = "controlled_gemini_supplementary:total_mismatch"
    _assert_dispatch(
        controller, digest, provider="gemini",
        url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        payload=payload, stage=stage,
    )
    with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
        _assert_dispatch(
            controller, digest, provider="gemini",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            payload={**payload, "response_format": gemini_response_format()}, stage=stage,
        )


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1/chat/completions",
    "https://generativelanguage.googleapis.com.evil.invalid/chat/completions",
    "http://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "https://generativelanguage.googleapis.com:444/chat/completions",
    "https://generativelanguage.googleapis.com/v1beta/files",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions?store=true",
])
def test_unauthorized_host_scheme_or_port_is_blocked(tmp_path, monkeypatch, url):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
        _assert_dispatch(
            controller, digest, provider="gemini", url=url,
            payload=_gemini_payload(), stage="vision",
        )


def test_private_document_requires_manifest_and_informed_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    with activate_controlled_external(controller):
        with pytest.raises(ControlledExternalBlocked, match="outside_frozen_manifest"):
            with controlled_document_scope(document_sha256="f" * 64):
                pass
        with pytest.raises(ControlledExternalBlocked, match="authorization_missing"):
            with controlled_document_scope(document_sha256=digest):
                pass


def test_authorized_manifest_document_can_enter_private_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path, authorized=True)
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest,
    ) as scope:
        assert scope.document_sha256 == digest
        assert scope.opaque_document_id.startswith("doc_")


def test_deepseek_is_not_an_authorized_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    payload = _deepseek_payload({"source": SYNTHETIC_PNG})
    with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
        _assert_dispatch(
            controller, digest, provider="deepseek",
            url="https://api.deepseek.com/chat/completions", payload=payload,
            stage="accounting_semantic_reasoning",
        )


@pytest.mark.parametrize(("provider", "url"), [
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("deepseek", "https://api.deepseek.com/chat/completions"),
    ("claude", "https://api.anthropic.com/v1/messages"),
])
def test_phase_a_compatibility_matrix_blocks_unauthorized_fallback(
    tmp_path, monkeypatch, provider, url,
):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
        _assert_dispatch(
            controller, digest, provider=provider, url=url,
            payload=_gemini_payload(), stage="facts_only_fallback",
        )


def test_deepseek_minimization_adapter_is_disabled_in_controlled_phase_a(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ):
        with pytest.raises(ControlledExternalBlocked, match="provider_not_allowed"):
            build_deepseek_minimized_facts(lines=[])


@pytest.mark.parametrize("forbidden", [
    {"cached_content": "cache-id"},
    {"tools": [{"google_search": {}}]},
    {"store": True},
    {"file_uri": "gs://private/document.pdf"},
    {"interactions": [{"id": "stored"}]},
])
def test_gemini_forbids_storage_grounding_files_and_interactions(
    tmp_path, monkeypatch, forbidden,
):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    payload = {**_gemini_payload(), **forbidden}
    with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
        _assert_dispatch(
            controller, digest, provider="gemini",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            payload=payload, stage="vision",
        )


def test_gemini_private_contract_requires_visual_source_and_typed_schema(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path, authorized=True)
    context = build_experiment_provider_context(
        controller=controller,
        document_sha256=digest,
        authorized_provider="gemini",
        authorized_model="configured-gemini-model",
        authorized_profile_id="runtime-vision",
        allowed_endpoint=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/chat/completions"
        ),
    )
    payload = {
        "model": "configured-gemini-model",
        "messages": [{"role": "user", "content": "extract facts"}],
    }
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest,
    ), activate_experiment_provider_context(context), ai_runtime_trace.operation(
        batch_id="", stage="vision", provider="gemini",
        model="configured-gemini-model", profile_id="runtime-vision",
    ):
        ai_runtime_trace.update_context(
            estimated_cost_usd=0.001,
            input_cost_usd_per_million=1.0,
            output_cost_usd_per_million=1.0,
        )
        with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
            assert_controlled_external_dispatch_allowed(
                provider="gemini",
                url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                stage="vision", payload=payload,
                provider_context=context,
                call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
                profile_id="runtime-vision",
            )


def test_missing_pricing_blocks_before_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    context = build_experiment_provider_context(
        controller=controller,
        document_sha256=digest,
        authorized_provider="gemini",
        authorized_model="configured-gemini-model",
        authorized_profile_id="synthetic-preflight",
        allowed_endpoint=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/chat/completions"
        ),
    )
    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=digest, synthetic=True,
    ), activate_experiment_provider_context(context), ai_runtime_trace.operation(
        batch_id="", stage="vision", provider="gemini",
        model="configured-gemini-model", profile_id="synthetic-preflight",
    ):
        ai_runtime_trace.update_context(estimated_cost_usd=0.001)
        with pytest.raises(ControlledExternalBlocked, match="controlled_external_pricing_indeterminate"):
            assert_controlled_external_dispatch_allowed(
                provider="gemini",
                url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                stage="vision", payload=_gemini_payload(),
                provider_context=context,
                call_purpose=ControlledCallPurpose.INITIAL_EXTRACTION,
                profile_id="synthetic-preflight",
            )


def test_openai_and_claude_cannot_be_fallback_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_VIEW_EXPERIMENT_EXECUTION_MODE", "CONTROLLED_EXTERNAL")
    controller, digest = _controller(tmp_path)
    for provider, url in (
        ("openai", "https://api.openai.com/v1/chat/completions"),
        ("anthropic", "https://api.anthropic.com/v1/messages"),
    ):
        with pytest.raises(ControlledExternalBlocked, match="controlled_provider_route_blocked"):
            _assert_dispatch(
                controller, digest, provider=provider, url=url,
                payload={"model": "forbidden", "messages": []}, stage="fallback",
            )
