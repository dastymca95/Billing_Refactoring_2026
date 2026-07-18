from webapp.backend.services.model_registry import ModelRole
from webapp.backend.services import ai_provider
from webapp.backend.services.provider_capabilities import (
    ModelCapability, ModelProfile, ModelProfileRole, ProfileLoader,
    ProviderCapabilityValidator, VerifiedCapabilityRegistry,
)


def _clear_provider_environment(monkeypatch):
    prefixes = ("AI_", "GEMINI_", "DEEPSEEK_", "ANTHROPIC_")
    for key in list(__import__("os").environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)


def test_environment_profiles_require_explicit_configuration(monkeypatch):
    _clear_provider_environment(monkeypatch)
    assert ProfileLoader._environment_profiles() == []


def test_missing_credentials_disables_profile_without_transport_call():
    calls = []
    profile = ModelProfile(provider="openai", profile_id="p", model_id="configured-model",
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION], credentials_present=False)
    report = ProviderCapabilityValidator(lambda *args: calls.append(args)).validate(profile)
    assert report.health_status == "disabled" and report.failure_reason == "credentials_missing"
    assert not calls and report.verified_capabilities == []


def test_capabilities_are_verified_individually_and_not_from_declaration(monkeypatch):
    monkeypatch.setenv("AI_CAPABILITY_VISION_PROBE_EXPECTED", "IMAGE42")
    def transport(_profile, capability, _request):
        return {
            ModelCapability.TEXT_EXTRACTION: {"probe": "IV39C_TEXT"},
            ModelCapability.STRUCTURED_OUTPUT: {"probe": "IV39C_JSON", "ok": True},
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING: {"probe": "wrong"},
        }[capability]
    profile = ModelProfile(provider="openai", profile_id="p", model_id="configured-model",
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT,
                               ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING], credentials_present=True, vision=True)
    report = ProviderCapabilityValidator(transport).validate(profile)
    assert report.health_status == "degraded"
    assert report.verified_capabilities == [ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT]
    assert report.unavailable_capabilities == [ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING]


def test_independent_verification_probe_requires_corrected_value():
    profile = ModelProfile(provider="openai", profile_id="reason", model_id="reasoner",
        declared_capabilities=[ModelCapability.INDEPENDENT_VERIFICATION], credentials_present=True)
    bad = ProviderCapabilityValidator(lambda *_: {"probe": "IV39C_VERIFY", "corrected_total": "11.00"}).validate(profile)
    good = ProviderCapabilityValidator(lambda *_: {"probe": "IV39C_VERIFY", "corrected_total": "10.00"}).validate(profile)
    assert bad.verified_capabilities == [] and good.verified_capabilities == [ModelCapability.INDEPENDENT_VERIFICATION]


def test_only_probe_verified_models_enter_activated_registry():
    healthy = ProviderCapabilityValidator(lambda *_: {"probe": "IV39C_TEXT"}).validate(ModelProfile(
        provider="openai", profile_id="text", model_id="verified-text",
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION], credentials_present=True))
    disabled = ProviderCapabilityValidator().validate(ModelProfile(provider="openai", profile_id="vision",
        model_id="unverified-vision", declared_capabilities=[ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING],
        credentials_present=False, vision=True))
    registry = VerifiedCapabilityRegistry([healthy, disabled]).activated_model_registry()
    assert [spec.model_id for spec in registry.for_role(ModelRole.EXTRACTION_TEXT)] == ["verified-text"]
    assert registry.for_role(ModelRole.EXTRACTION_VISION) == []


def test_audit_never_exposes_secrets():
    profile = ModelProfile(provider="openai", profile_id="p", model_id="m",
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION], credentials_present=False)
    payload = ProviderCapabilityValidator().audit([profile]).model_dump(mode="json")
    serialized = __import__("json").dumps(payload)
    assert payload["secrets_exposed"] is False and "api_key" not in serialized.lower()


def test_activation_requires_probe_verified_multimodal_reasoning_and_verification():
    def report(profile_id, model_id, role, capabilities):
        responses = {
            ModelCapability.TEXT_EXTRACTION: {"probe": "IV39C_TEXT"},
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING: {"probe": "IMAGE42"},
            ModelCapability.HANDWRITING_INTERPRETATION: {"probe": "IMAGE42"},
            ModelCapability.STRUCTURED_OUTPUT: {"probe": "IV39C_JSON", "ok": True},
            ModelCapability.ACCOUNTING_REASONING: {"probe": "IV39C_ACCOUNTING", "balanced": True},
            ModelCapability.INDEPENDENT_VERIFICATION: {
                "probe": "IV39C_VERIFY", "corrected_total": "10.00"
            },
        }
        return ProviderCapabilityValidator(
            lambda _profile, capability, _request: responses[capability]
        ).validate(ModelProfile(provider="openai", profile_id=profile_id, model_id=model_id,
                                role=role, declared_capabilities=capabilities,
                                credentials_present=True))

    import os
    old_marker = os.environ.get("AI_CAPABILITY_VISION_PROBE_EXPECTED")
    os.environ["AI_CAPABILITY_VISION_PROBE_EXPECTED"] = "IMAGE42"
    try:
        text = report("text", "text-model", ModelProfileRole.TEXT_EXTRACTION, [
            ModelCapability.TEXT_EXTRACTION,
            ModelCapability.STRUCTURED_OUTPUT,
        ])
        vision = report("vision", "vision-model", ModelProfileRole.MULTIMODAL_EXTRACTION, [
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
            ModelCapability.HANDWRITING_INTERPRETATION,
            ModelCapability.STRUCTURED_OUTPUT,
        ])
        verifier = report("verifier", "verify-model", ModelProfileRole.INDEPENDENT_VERIFICATION, [
            ModelCapability.INDEPENDENT_VERIFICATION,
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
            ModelCapability.STRUCTURED_OUTPUT,
        ])
        reasoner = report("reasoner", "reason-model", ModelProfileRole.ACCOUNTING_REASONING, [
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ])
    finally:
        if old_marker is None:
            os.environ.pop("AI_CAPABILITY_VISION_PROBE_EXPECTED", None)
        else:
            os.environ["AI_CAPABILITY_VISION_PROBE_EXPECTED"] = old_marker
    activation = VerifiedCapabilityRegistry([text, vision, verifier, reasoner]).activation_report()
    assert activation.autonomous_gateway_enabled is True
    assert activation.strong_reasoning_mode == "shadow"


def test_partial_verification_does_not_enable_autonomous_gateway():
    report = ProviderCapabilityValidator(lambda *_: {"probe": "IV39C_TEXT"}).validate(ModelProfile(
        provider="openai", profile_id="text", model_id="text-model",
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION], credentials_present=True,
    ))
    activation = VerifiedCapabilityRegistry([report]).activation_report()
    assert activation.autonomous_gateway_enabled is False
    assert "verified_multimodal_profile_missing" in activation.blocking_reasons


def test_environment_topology_has_four_isolated_profiles_with_safe_fallback(monkeypatch):
    _clear_provider_environment(monkeypatch)
    values = {
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": "text-model",
        "AI_API_KEY": "base-secret",
        "AI_BASE_URL": "https://provider.invalid/v1",
        "AI_VISION_MODEL": "vision-model",
        "AI_VERIFICATION_MODEL": "vision-model",
        "AI_ACCOUNTING_REASONING_MODEL": "reason-model",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    profiles = ProfileLoader._environment_profiles()
    assert [profile.profile_id for profile in profiles] == [
        "runtime-text", "runtime-vision", "runtime-verification", "runtime-accounting",
    ]
    assert len({profile.trace_namespace for profile in profiles}) == 4
    assert len({profile.cache_namespace for profile in profiles}) == 4
    assert all(profile.credentials_present and profile.base_url_configured for profile in profiles)
    verifier = profiles[2]
    assert verifier.verification_independence == "isolated_same_family"
    assert verifier.api_key is not None
    assert verifier.credentials_present is True


def test_profile_specific_credentials_and_endpoints_override_base(monkeypatch):
    _clear_provider_environment(monkeypatch)
    for key, value in {
        "AI_PROVIDER": "openai_compatible", "AI_MODEL": "text", "AI_API_KEY": "base",
        "AI_BASE_URL": "https://base.invalid/v1", "AI_VERIFICATION_MODEL": "verify",
        "AI_VERIFICATION_PROVIDER": "openai_compatible", "AI_VERIFICATION_API_KEY": "verify-secret",
        "AI_VERIFICATION_BASE_URL": "https://verify.invalid/v1",
    }.items():
        monkeypatch.setenv(key, value)
    verifier = next(profile for profile in ProfileLoader._environment_profiles()
                    if profile.profile_id == "runtime-verification")
    assert verifier.api_key is not None
    assert verifier.base_url == "https://verify.invalid/v1"


def test_probe_requests_have_role_specific_prompts_traces_and_cache_keys(monkeypatch):
    captured = []
    validator = ProviderCapabilityValidator(
        lambda profile, capability, request: captured.append((profile, capability, request))
        or {"probe": "IV39C_JSON", "ok": True}
    )
    for profile_id, role in (("text", ModelProfileRole.TEXT_EXTRACTION),
                             ("reason", ModelProfileRole.ACCOUNTING_REASONING)):
        validator.validate(ModelProfile(
            provider="openai", profile_id=profile_id, model_id="shared-model", role=role,
            declared_capabilities=[ModelCapability.STRUCTURED_OUTPUT], credentials_present=True,
            trace_namespace=f"trace:{profile_id}", cache_namespace=f"cache:{profile_id}",
        ))
    text_request, reason_request = captured[0][2], captured[1][2]
    assert text_request["system"] != reason_request["system"]
    assert text_request["trace_id"] != reason_request["trace_id"]
    assert text_request["cache_key"] != reason_request["cache_key"]


def test_same_underlying_model_can_serve_distinct_verified_logical_profiles():
    text = ProviderCapabilityValidator(lambda *_: {"probe": "IV39C_TEXT"}).validate(ModelProfile(
        provider="openai", profile_id="text", model_id="shared-model",
        role=ModelProfileRole.TEXT_EXTRACTION,
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION], credentials_present=True,
    ))
    reason = ProviderCapabilityValidator(
        lambda *_: {"probe": "IV39C_ACCOUNTING", "balanced": True}
    ).validate(ModelProfile(
        provider="openai", profile_id="reason", model_id="shared-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[ModelCapability.ACCOUNTING_REASONING], credentials_present=True,
    ))
    registry = VerifiedCapabilityRegistry([text, reason]).activated_model_registry()
    assert registry.get("shared-model") is not None
    assert registry.get("shared-model").roles == {
        ModelRole.EXTRACTION_TEXT, ModelRole.ACCOUNTING_REASONING,
    }


def test_provider_http_error_body_is_not_written_to_logs(monkeypatch, caplog):
    import io
    import urllib.error

    marker = "partial-secret-must-not-be-logged"

    def reject(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {},
            io.BytesIO(f'{{"error":"{marker}"}}'.encode()),
        )

    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", reject)
    with __import__("pytest").raises(ai_provider.AIProviderUnavailable):
        ai_provider._send_chat_completion(
            provider="openai", payload={"model": "configured-model", "messages": []},
            api_key_override="local-test-secret", base_url_override="https://api.openai.com/v1",
            max_attempts_override=1,
        )
    assert marker not in caplog.text
    assert "local-test-secret" not in caplog.text


def test_openai_probe_uses_gpt56_compatible_completion_parameters(monkeypatch):
    from pydantic import SecretStr
    from webapp.backend.services.provider_capabilities import OpenAICompatibleProbeTransport

    captured = {}

    def send(**kwargs):
        captured.update(kwargs)
        return '{"probe":"IV39C_JSON","ok":true}'

    monkeypatch.setattr(ai_provider, "_send_chat_completion", send)
    profile = ModelProfile(
        provider="openai", profile_id="runtime-text", model_id="configured-model",
        role=ModelProfileRole.TEXT_EXTRACTION,
        declared_capabilities=[ModelCapability.STRUCTURED_OUTPUT], credentials_present=True,
        base_url_configured=True, api_key=SecretStr("local-test-secret"),
        base_url="https://api.openai.com/v1",
    )
    result = OpenAICompatibleProbeTransport()(profile, ModelCapability.STRUCTURED_OUTPUT, {
        "system": "Return JSON", "prompt": "Return JSON", "trace_id": "trace:test",
        "cache_key": "cache:test",
    })
    payload = captured["payload"]
    assert result["ok"] is True
    assert payload["max_completion_tokens"] == 512
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload and "max_tokens" not in payload
