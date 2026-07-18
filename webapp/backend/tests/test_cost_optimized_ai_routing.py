import json
from types import SimpleNamespace

import pytest

from webapp.backend.services import ai_provider, semantic_reasoning_gateway
from webapp.backend.services.accounting_integration_bridges import RowAccountingV2Adapter
from webapp.backend.services.provider_capabilities import ProfileLoader


def _complete_row(gl="6530"):
    return {
        "Invoice Number": "COST-1",
        "Bill or Credit": "Bill",
        "Invoice Date": "2026-01-01",
        "Accounting Date": "2026-01-01",
        "Vendor": "Generic Vendor",
        "Invoice Description": "Repair service",
        "Line Item Number": 1,
        "Property Abbreviation": "TEST",
        "GL Account": gl,
        "Line Item Description": "Plumbing repair service",
        "Amount": 25,
        "Expense Type": "General",
        "Is Replacement Reserve": False,
        "Document Url": "https://example.invalid/invoice",
        "_meta": {"source_line_description": "raw plumbing repair service"},
    }


def test_deterministically_complete_row_makes_zero_semantic_ai_calls(monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("deterministic payable GL must not invoke semantic AI")

    monkeypatch.setattr(semantic_reasoning_gateway, "enrich_invoice_semantics", forbidden)
    row = _complete_row()
    RowAccountingV2Adapter().enrich_rows([row], {"document_id": "doc-cost"})
    assert row["GL Account"]
    assert row["_meta"]["accounting_decision"]["selected_gl_code"] == row["GL Account"]


@__import__("pytest").mark.parametrize(("description", "expected_gl"), [
    ("05/28/26-06/29/26 - Water", "6955"),
    ("05/28/26-06/29/26 - Sewer Pilot", "6955"),
    ("05/28/26-06/29/26 - Sanitation", "6940"),
])
def test_vendor_neutral_utility_period_lines_resolve_without_ai(
    monkeypatch, description, expected_gl
):
    from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row

    row = _complete_row("")
    row["Invoice Number"] = f"UTILITY-{expected_gl}"
    row["Line Item Number"] = int(expected_gl)
    row["Line Item Description"] = description
    row["_meta"]["source_line_description"] = description
    capture_source_fields(
        row, document_id=f"utility-{expected_gl}", line_item_id=expected_gl,
    )
    decision = decide_row(
        row,
        document_id=f"utility-{expected_gl}",
        line_item_id=expected_gl,
        extraction_route="universal_utility_test",
        allow_ai_semantic_reasoning=False,
    )
    assert row["GL Account"] == expected_gl
    assert decision.selected_gl_code == expected_gl
    assert row["_meta"]["semantic_reasoning_trace"]["called"] is False


def test_bottled_water_merchandise_is_not_misclassified_as_utility():
    from webapp.backend.services.accounting_contracts import LineItemFacts
    from webapp.backend.services.semantic_classifier import classify_line

    semantics = classify_line(
        LineItemFacts(
            line_item_id="retail-water",
            raw_description="Case of bottled water 24 pack",
            quantity=1,
            unit_price=8,
            amount=8,
        ),
        document_id="retail-document",
    )
    assert semantics.trade_family != "utility"
    assert semantics.line_family == "materials"


def test_provider_profiles_require_named_key_and_explicit_model(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("GEMINI_", "DEEPSEEK_", "ANTHROPIC_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-secret")
    assert not [profile for profile in ProfileLoader._environment_profiles()
                if profile.provider == "gemini"]
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "provisioned-model")
    profiles = [profile for profile in ProfileLoader._environment_profiles()
                if profile.provider == "gemini"]
    assert [profile.profile_id for profile in profiles] == ["gemini-text"]
    assert "private-test-secret" not in json.dumps(
        profiles[0].model_dump(mode="json"), default=str
    )


def test_unverified_provider_profile_cannot_receive_cost_routed_traffic(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("AI_", "GEMINI_", "DEEPSEEK_", "ANTHROPIC_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-secret")
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "provisioned-model")
    monkeypatch.setenv("GEMINI_INPUT_COST_USD_PER_MILLION", "0.10")
    monkeypatch.setenv("GEMINI_OUTPUT_COST_USD_PER_MILLION", "0.40")
    assert ai_provider._select_cost_routing_profile("text_extraction") is None
    monkeypatch.setenv("AI_COST_ROUTING_VERIFIED_PROFILE_IDS", "gemini-text")
    selected = ai_provider._select_cost_routing_profile("text_extraction")
    assert selected is not None and selected.profile_id == "gemini-text"


def test_openai_compatible_urls_and_provider_controls_are_provider_safe():
    assert ai_provider._chat_completions_url("gemini", "") == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert ai_provider._chat_completions_url("deepseek", "") == (
        "https://api.deepseek.com/chat/completions"
    )
    assert ai_provider._chat_completions_url("anthropic", "") == (
        "https://api.anthropic.com/v1/chat/completions"
    )
    assert ai_provider._completion_controls("anthropic", 1024) == {
        "max_completion_tokens": 1024,
    }


def test_explicit_verified_profile_selection_does_not_fall_back_ambiguously(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("AI_", "GEMINI_", "DEEPSEEK_", "ANTHROPIC_")):
            monkeypatch.delenv(key, raising=False)
    for prefix, cost in (("GEMINI", "0.50"), ("DEEPSEEK", "0.10")):
        monkeypatch.setenv(f"{prefix}_API_KEY", f"{prefix.lower()}-test-secret")
        monkeypatch.setenv(f"{prefix}_TEXT_MODEL", f"{prefix.lower()}-model")
        monkeypatch.setenv(f"{prefix}_INPUT_COST_USD_PER_MILLION", cost)
        monkeypatch.setenv(f"{prefix}_OUTPUT_COST_USD_PER_MILLION", cost)
    monkeypatch.setenv(
        "AI_COST_ROUTING_VERIFIED_PROFILE_IDS", "gemini-text,deepseek-text"
    )
    assert ai_provider._select_cost_routing_profile("text_extraction").profile_id == "deepseek-text"
    monkeypatch.setenv("AI_TEXT_ROUTING_PROFILE_ID", "gemini-text")
    assert ai_provider._select_cost_routing_profile("text_extraction").profile_id == "gemini-text"
    monkeypatch.setenv("AI_TEXT_ROUTING_PROFILE_ID", "not-verified")
    assert ai_provider._select_cost_routing_profile("text_extraction") is None


def test_anthropic_uses_native_messages_contract_without_bearer_key(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}'

    def send(request, **_kwargs):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", send)
    content = ai_provider._send_chat_completion(
        provider="anthropic",
        payload={
            "model": "configured-claude-model",
            "max_completion_tokens": 1000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return JSON"},
                {"role": "user", "content": "invoice"},
            ],
        },
        api_key_override="private-test-secret",
        base_url_override="https://api.anthropic.com/v1",
        max_attempts_override=1,
    )
    assert content == '{"ok":true}'
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["X-api-key"] == "private-test-secret"
    assert "Authorization" not in captured["headers"]
    assert captured["payload"]["system"] == "Return JSON"
    assert captured["payload"]["max_tokens"] == 1000
    assert "response_format" not in captured["payload"]


def test_batch_cost_budget_counts_retries_and_reset_starts_a_new_run(monkeypatch):
    monkeypatch.setenv("AI_MAX_COST_PER_BATCH_USD", "0.01")
    scope = "budget-test-batch"
    ai_provider.reset_cost_budget(scope)
    ai_provider._reserve_cost_budget(scope, 0.006)
    with pytest.raises(ai_provider.AIProviderUnavailable) as raised:
        ai_provider._reserve_cost_budget(scope, 0.006)
    assert raised.value.failure_code == "cost_budget_exceeded"

    ai_provider.reset_cost_budget(scope)
    ai_provider._reserve_cost_budget(scope, 0.006)
    ai_provider.reset_cost_budget(scope)


def test_profile_cost_estimate_ignores_base64_bytes_and_includes_vision_floor(monkeypatch):
    monkeypatch.setenv("AI_ESTIMATED_VISION_IMAGE_COST_USD", "0.002")
    profile = SimpleNamespace(
        input_cost_usd_per_million=0.25,
        output_cost_usd_per_million=1.50,
    )
    payload = {
        "messages": [{"content": [
            {"type": "text", "text": "invoice"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + "A" * 100_000,
            }},
        ]}],
        "max_tokens": 1000,
    }
    estimate = ai_provider._estimated_profile_request_cost(
        profile, payload, vision=True,
    )
    assert 0.0035 <= estimate < 0.004


def test_role_specific_cost_overrides_are_loaded(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("GEMINI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-secret")
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "configured-text")
    monkeypatch.setenv("GEMINI_VISION_MODEL", "configured-vision")
    monkeypatch.setenv("GEMINI_INPUT_COST_USD_PER_MILLION", "9")
    monkeypatch.setenv("GEMINI_OUTPUT_COST_USD_PER_MILLION", "10")
    monkeypatch.setenv("GEMINI_TEXT_INPUT_COST_USD_PER_MILLION", "0.25")
    monkeypatch.setenv("GEMINI_TEXT_OUTPUT_COST_USD_PER_MILLION", "1.50")
    profiles = {
        profile.profile_id: profile for profile in ProfileLoader._environment_profiles()
        if profile.provider == "gemini"
    }
    assert profiles["gemini-text"].input_cost_usd_per_million == 0.25
    assert profiles["gemini-text"].output_cost_usd_per_million == 1.50
    assert profiles["gemini-vision"].input_cost_usd_per_million == 9
    assert profiles["gemini-vision"].output_cost_usd_per_million == 10


def test_deepseek_grouped_reasoning_can_disable_thinking(monkeypatch):
    from webapp.backend.services.gl_catalog import load_gl_catalog
    from webapp.backend.services.provider_capabilities import (
        ModelCapability,
        ModelProfile,
        ModelProfileRole,
    )

    captured = {}
    profile = ModelProfile(
        provider="deepseek",
        profile_id="deepseek-accounting",
        model_id="configured-reasoner",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
        credentials_present=True,
        api_key="private-test-secret",
        base_url="https://example.invalid",
    )
    monkeypatch.setenv("DEEPSEEK_ACCOUNTING_THINKING_ENABLED", "false")

    def respond(**kwargs):
        captured.update(kwargs["payload"])
        return json.dumps({"proposals": [{
            "line_item_id": "line-1",
            "line_family": "subscription_membership",
            "trade_family": "software",
            "work_mode": "renewal",
            "confidence": 0.9,
            "evidence_quotes": ["Annual subscription"],
            "candidate_gl_codes": ["6118"],
            "reasoning_summary": "Recurring software access.",
        }]})

    monkeypatch.setattr(ai_provider, "_send_chat_completion", respond)
    _, catalog = load_gl_catalog()
    result = semantic_reasoning_gateway._request_invoice_group(
        profile,
        [{
            "line_item_id": "line-1",
            "source_text": "Annual subscription",
            "quantity": "1",
            "unit_price": "10",
            "amount": "10",
            "current_semantics": {},
            "allowed_candidate_gl_codes": ["6118"],
        }],
        "Annual subscription",
        catalog,
    )
    assert result.proposals[0].candidate_gl_codes == ["6118"]
    assert captured["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured


def test_actual_provider_observability_uses_serving_profile_without_secrets():
    from webapp.backend.services.ai_invoice_processor import _actual_provider_identity

    provider, model = _actual_provider_identity(
        {
            "_provider_profile_id": "gemini-text",
            "_provider_name": "gemini",
            "_provider_model_id": "configured-model",
        },
        fallback_provider="openai",
        fallback_model="legacy-model",
    )
    assert (provider, model) == ("gemini", "configured-model")
