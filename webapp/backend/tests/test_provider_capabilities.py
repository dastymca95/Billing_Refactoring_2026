from webapp.backend.services.model_registry import ModelRole
from webapp.backend.services.provider_capabilities import (
    ModelCapability, ModelProfile, ProfileLoader, ProviderCapabilityValidator, VerifiedCapabilityRegistry,
)


def test_environment_profiles_require_explicit_configuration(monkeypatch):
    for key in ("AI_PROVIDER", "AI_MODEL", "AI_VISION_MODEL", "AI_ACCOUNTING_REASONING_MODEL", "AI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
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
    def report(profile_id, model_id, capabilities):
        responses = {
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING: {"probe": "IMAGE42"},
            ModelCapability.STRUCTURED_OUTPUT: {"probe": "IV39C_JSON", "ok": True},
            ModelCapability.ACCOUNTING_REASONING: {"probe": "IV39C_ACCOUNTING", "balanced": True},
            ModelCapability.INDEPENDENT_VERIFICATION: {
                "probe": "IV39C_VERIFY", "corrected_total": "10.00"
            },
        }
        return ProviderCapabilityValidator(
            lambda _profile, capability, _request: responses[capability]
        ).validate(ModelProfile(provider="openai", profile_id=profile_id, model_id=model_id,
                                declared_capabilities=capabilities, credentials_present=True))

    import os
    old_marker = os.environ.get("AI_CAPABILITY_VISION_PROBE_EXPECTED")
    os.environ["AI_CAPABILITY_VISION_PROBE_EXPECTED"] = "IMAGE42"
    try:
        vision = report("vision", "vision-model", [
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
            ModelCapability.STRUCTURED_OUTPUT,
        ])
        reasoner = report("reasoner", "reason-model", [
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
            ModelCapability.INDEPENDENT_VERIFICATION,
        ])
    finally:
        if old_marker is None:
            os.environ.pop("AI_CAPABILITY_VISION_PROBE_EXPECTED", None)
        else:
            os.environ["AI_CAPABILITY_VISION_PROBE_EXPECTED"] = old_marker
    activation = VerifiedCapabilityRegistry([vision, reasoner]).activation_report()
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
