from __future__ import annotations

import json
import urllib.request

import pytest

from webapp.backend.services import ai_provider, provider_capabilities
from webapp.backend.services.local_inference_guard import (
    LocalInferenceNetworkBlocked,
    assert_dispatch_allowed,
    local_network_isolation,
)
from webapp.backend.services.local_multimodal_provider import (
    LocalMultimodalProvider,
    LocalMultimodalProviderError,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")


def test_local_only_guard_blocks_remote_before_transport(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport must not be reached")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with pytest.raises(LocalInferenceNetworkBlocked, match="remote_endpoint_blocked"):
        assert_dispatch_allowed(
            provider="gemini",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            stage="private_document_extraction",
        )
    assert called is False


def test_ai_provider_cannot_fall_back_to_remote_in_local_mode(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    calls = []
    monkeypatch.setattr(
        ai_provider.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: calls.append(True),
    )
    with pytest.raises(ai_provider.AIProviderUnavailable) as caught:
        ai_provider._send_chat_completion(
            provider="openai",
            payload={"model": "remote-model", "messages": []},
            api_key_override="private-test-secret",
            base_url_override="https://api.openai.com/v1",
        )
    assert caught.value.failure_code == "remote_endpoint_blocked"
    assert calls == []


def test_process_socket_gate_allows_literal_loopback_and_blocks_dns(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    calls = []
    monkeypatch.setattr(
        "socket.create_connection",
        lambda address, *_args, **_kwargs: calls.append(address) or "connected",
    )
    import socket

    with local_network_isolation():
        assert socket.create_connection(("127.0.0.1", 11434)) == "connected"
        with pytest.raises(LocalInferenceNetworkBlocked, match="socket_remote_connect_blocked"):
            socket.create_connection(("localhost", 11434))
        with pytest.raises(LocalInferenceNetworkBlocked, match="socket_remote_connect_blocked"):
            socket.create_connection(("8.8.8.8", 443))
    assert calls == [("127.0.0.1", 11434)]


def test_local_provider_uses_loopback_and_removes_authoritative_outputs(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    observed_urls = []
    provider_output = {
        "vendor_name": "Observed Vendor",
        "total_amount": 12.34,
        "selected_gl": "9999",
        "export_allowed": True,
        "line_items": [{
            "source_page": 1,
            "raw_description": "Visible service",
            "amount": 12.34,
            "selected_gl": "9999",
            "gl_account_candidate": "9999",
        }],
    }

    def respond(request, **_kwargs):
        observed_urls.append(request.full_url)
        return _Response({
            "model": "qwen3-vl:2b",
            "message": {"content": json.dumps(provider_output)},
        })

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    result = LocalMultimodalProvider(model="qwen3-vl:2b").chat_completion({
        "messages": [{"role": "user", "content": "Extract facts"}],
    })
    assert observed_urls == ["http://127.0.0.1:11434/api/chat"]
    assert "selected_gl" not in result.structured_output
    assert "export_allowed" not in result.structured_output
    assert result.structured_output["line_items"][0]["gl_account_candidate"] == ""


def test_local_provider_accepts_only_schema_valid_structured_thinking(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    provider_output = {
        "vendor_name": "Observed Vendor",
        "total_amount": 12.34,
        "line_items": [{
            "source_page": 1,
            "raw_description": "Visible service",
            "amount": 12.34,
        }],
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({
            "done_reason": "stop",
            "message": {"content": "", "thinking": json.dumps(provider_output)},
        }),
    )

    result = LocalMultimodalProvider(model="qwen3-vl:2b").chat_completion({
        "messages": [{"role": "user", "content": "Extract facts"}],
    })

    assert result.structured_output["vendor_name"] == "Observed Vendor"
    assert result.response_channel == "validated_thinking"
    assert "thinking" not in result.model_dump()
    assert "thinking" not in result.structured_output


def test_local_provider_rejects_free_form_thinking(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({
            "done_reason": "stop",
            "message": {"content": "", "thinking": "private free-form reasoning"},
        }),
    )

    with pytest.raises(LocalMultimodalProviderError, match="thinking_non_json"):
        LocalMultimodalProvider(model="qwen3-vl:2b").chat_completion({
            "messages": [{"role": "user", "content": "Extract facts"}],
        })


def test_local_profile_loader_ignores_remote_environment(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    monkeypatch.setenv("LOCAL_MULTIMODAL_MODEL", "qwen3-vl:2b")
    monkeypatch.setenv("LOCAL_MULTIMODAL_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_MODEL", "remote-model")
    monkeypatch.setenv("AI_API_KEY", "must-be-ignored")
    profiles = provider_capabilities.ProfileLoader().load()
    assert {profile.profile_id for profile in profiles} == {
        "local-text", "local-vision", "local-verification", "local-accounting",
    }
    assert {profile.provider for profile in profiles} == {"local_ollama"}
    assert all(profile.base_url == "http://127.0.0.1:11434" for profile in profiles)


def test_instruct_evaluation_uses_separate_profile_ids(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    monkeypatch.setenv("LOCAL_MULTIMODAL_MODEL", "qwen3-vl:2b-instruct")
    monkeypatch.setenv(
        "LOCAL_MULTIMODAL_PROFILE_ID", "local-qwen3-vl-2b-instruct",
    )
    profiles = provider_capabilities.ProfileLoader().load()

    assert {profile.profile_id for profile in profiles} == {
        "local-qwen3-vl-2b-instruct-text",
        "local-qwen3-vl-2b-instruct",
        "local-qwen3-vl-2b-instruct-verification",
        "local-qwen3-vl-2b-instruct-accounting",
    }
    assert {profile.model_id for profile in profiles} == {"qwen3-vl:2b-instruct"}


def test_instruct_structured_thinking_is_reported_as_anomaly(monkeypatch):
    monkeypatch.setenv("INNER_VIEW_LOCAL_INFERENCE_ONLY", "1")
    provider_output = {
        "vendor_name": "Observed Vendor",
        "total_amount": 12.34,
        "line_items": [{
            "source_page": 1,
            "raw_description": "Visible service",
            "amount": 12.34,
        }],
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({
            "done_reason": "stop",
            "message": {"content": "", "thinking": json.dumps(provider_output)},
        }),
    )

    result = LocalMultimodalProvider(
        model="qwen3-vl:2b-instruct",
        profile_id="local-qwen3-vl-2b-instruct",
    ).chat_completion({"messages": [{"role": "user", "content": "Extract facts"}]})

    assert "instruct_profile_structured_thinking_anomaly" in result.warnings
    assert result.response_channel == "validated_thinking"


def test_local_text_extraction_is_facts_only_and_excludes_accounting_references(monkeypatch):
    status = ai_provider.AIProviderStatus(
        enabled=True,
        provider="local_ollama",
        model="qwen3-vl:2b",
        configured=True,
        supports_vision=True,
        vision_enabled=True,
        vision_provider="local_ollama",
        vision_model="qwen3-vl:2b",
        vision_mode="fallback_only",
        message="local",
    )
    observed_payloads = []
    provider_output = {
        "vendor_name": "Observed Vendor",
        "invoice_number": "INV-1",
        "total_amount": 12.34,
        "line_items": [{
            "source_page": 1,
            "activity": "Visible service",
            "raw_description": "Visible service",
            "generated_description": "Visible service charge",
            "amount": 12.34,
            "gl_account_candidate": "9999",
        }],
    }
    monkeypatch.setattr(ai_provider, "_require_configured", lambda _context=None: status)
    monkeypatch.setattr(
        ai_provider, "_select_cost_routing_profile",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(ai_provider, "_load_extraction_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_provider, "_save_extraction_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_provider, "_reserve_cost_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ai_provider,
        "_send_chat_completion",
        lambda **kwargs: observed_payloads.append(kwargs["payload"]) or json.dumps(provider_output),
    )

    result = ai_provider.extract_invoice_structured(
        vendor_hint="Do not trust",
        document_text="Invoice INV-1 Visible service $12.34 Total $12.34",
        page_images_or_refs=None,
        template_schema={"secret": "SECRET-TEMPLATE"},
        property_reference=[{"secret": "SECRET-PROPERTY-REF"}],
        gl_reference=[{"secret": "SECRET-GL-REF"}],
        vendor_reference=[{"secret": "SECRET-VENDOR-REF"}],
    )

    prompt = observed_payloads[0]["messages"][1]["content"]
    assert "SECRET-GL-REF" not in prompt
    assert "SECRET-PROPERTY-REF" not in prompt
    assert "SECRET-VENDOR-REF" not in prompt
    assert result["_facts_only"] is True
    assert result["line_items"][0]["gl_account_candidate"] == ""
    assert result["_provider_profile_id"] == "runtime-text:facts-only-v1"
