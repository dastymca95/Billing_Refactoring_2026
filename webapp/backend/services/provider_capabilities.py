"""Typed, probe-backed provider capability discovery for Phase 3.9C."""
from __future__ import annotations

import json
import hashlib
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from pydantic import BaseModel, Field, SecretStr

from . import ai_provider
from .model_registry import ModelRegistry, ModelRole, ModelSpec
from .local_inference_guard import LOCAL_PROVIDER_NAMES, local_inference_only


class ModelCapability(str, Enum):
    TEXT_EXTRACTION = "text_extraction"
    VISUAL_DOCUMENT_UNDERSTANDING = "visual_document_understanding"
    HANDWRITING_INTERPRETATION = "handwriting_interpretation"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_DOCUMENT_PROCESSING = "long_document_processing"
    ACCOUNTING_REASONING = "accounting_reasoning"
    INDEPENDENT_VERIFICATION = "independent_verification"


class ModelProfileRole(str, Enum):
    TEXT_EXTRACTION = "text_extraction"
    MULTIMODAL_EXTRACTION = "multimodal_extraction"
    INDEPENDENT_VERIFICATION = "independent_verification"
    ACCOUNTING_REASONING = "accounting_reasoning"


class ModelProfile(BaseModel):
    provider: str
    profile_id: str
    model_id: str
    role: ModelProfileRole = ModelProfileRole.TEXT_EXTRACTION
    declared_capabilities: list[ModelCapability]
    base_url_configured: bool = False
    credentials_present: bool = False
    enabled: bool = True
    vision: bool = False
    timeout_seconds: int = Field(default=45, ge=1, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    base_url: str | None = Field(default=None, exclude=True, repr=False)
    trace_namespace: str = "profile"
    cache_namespace: str = "profile"
    model_family: str | None = None
    verification_independence: str | None = None
    input_cost_usd_per_million: float | None = Field(default=None, ge=0)
    output_cost_usd_per_million: float | None = Field(default=None, ge=0)
    routing_priority: int = Field(default=100, ge=0, le=1000)


class CapabilityProbeEvidence(BaseModel):
    capability: ModelCapability
    trace_id: str
    cache_key: str
    request_role: ModelProfileRole
    passed: bool
    failure_reason: str | None = None


class ModelProfileCapabilityReport(BaseModel):
    provider: str
    profile_id: str
    model_id: str
    declared_capabilities: list[ModelCapability]
    verified_capabilities: list[ModelCapability] = Field(default_factory=list)
    unavailable_capabilities: list[ModelCapability] = Field(default_factory=list)
    health_status: str
    failure_reason: str | None = None
    role: ModelProfileRole
    verification_independence: str | None = None
    probe_evidence: list[CapabilityProbeEvidence] = Field(default_factory=list)
    verified_at: datetime


class ProviderAuditReport(BaseModel):
    schema_version: str = "provider-capability-audit/1.0"
    profiles: list[ModelProfileCapabilityReport]
    configured_provider_count: int
    verified_profile_count: int
    credentials_present_count: int
    secrets_exposed: bool = False
    generated_at: datetime


class ProviderActivationReport(BaseModel):
    """Conservative gate between probes and autonomous runtime wiring."""

    schema_version: str = "provider-activation/1.0"
    text_profile_ids: list[str] = Field(default_factory=list)
    multimodal_profile_ids: list[str] = Field(default_factory=list)
    reasoning_profile_ids: list[str] = Field(default_factory=list)
    independent_verifier_profile_ids: list[str] = Field(default_factory=list)
    autonomous_gateway_enabled: bool = False
    strong_reasoning_mode: str = "shadow"
    blocking_reasons: list[str] = Field(default_factory=list)


Transport = Callable[[ModelProfile, ModelCapability, dict[str, Any]], Mapping[str, Any]]


class ProfileLoader:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(__file__).resolve().parents[3] / "config" / "model_profiles.yaml"

    def load(self) -> list[ModelProfile]:
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        profiles = [ModelProfile(**row) for row in payload.get("profiles", [])]
        profiles.extend(self._environment_profiles())
        unique = {}
        for profile in profiles: unique[profile.profile_id] = profile
        return list(unique.values())

    @staticmethod
    def _environment_profiles() -> list[ModelProfile]:
        if local_inference_only():
            return _local_environment_profiles()
        provider = os.environ.get("AI_PROVIDER", "").strip().lower()
        key = os.environ.get("AI_API_KEY", "").strip()
        base = os.environ.get("AI_BASE_URL", "").strip()
        timeout = _int_env("AI_TIMEOUT_SECONDS", 45)
        profiles: list[ModelProfile] = []
        text = os.environ.get("AI_MODEL", "").strip()
        if provider and text:
            profiles.append(_environment_profile(
                provider=provider,
                profile_id="runtime-text",
                model_id=text,
                role=ModelProfileRole.TEXT_EXTRACTION,
                capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT,
                              ModelCapability.LONG_DOCUMENT_PROCESSING],
                api_key=key,
                base_url=base,
                timeout=timeout,
                model_family=os.environ.get("AI_MODEL_FAMILY", "").strip() or None,
                input_cost=_float_env("AI_INPUT_COST_USD_PER_MILLION"),
                output_cost=_float_env("AI_OUTPUT_COST_USD_PER_MILLION"),
            ))
        vision_model = os.environ.get("AI_VISION_MODEL", "").strip()
        vision_provider = os.environ.get("AI_VISION_PROVIDER", "").strip().lower() or provider
        vision_key = os.environ.get("AI_VISION_API_KEY", "").strip() or key
        vision_base = os.environ.get("AI_VISION_BASE_URL", "").strip() or base
        if vision_provider and vision_model:
            profiles.append(_environment_profile(
                provider=vision_provider,
                profile_id="runtime-vision",
                model_id=vision_model,
                role=ModelProfileRole.MULTIMODAL_EXTRACTION,
                capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                              ModelCapability.HANDWRITING_INTERPRETATION, ModelCapability.STRUCTURED_OUTPUT],
                api_key=vision_key,
                base_url=vision_base,
                timeout=timeout,
                vision=True,
                model_family=os.environ.get("AI_VISION_MODEL_FAMILY", "").strip() or None,
                input_cost=_float_env("AI_VISION_INPUT_COST_USD_PER_MILLION"),
                output_cost=_float_env("AI_VISION_OUTPUT_COST_USD_PER_MILLION"),
            ))
        verification_model = os.environ.get("AI_VERIFICATION_MODEL", "").strip()
        verification_provider = os.environ.get("AI_VERIFICATION_PROVIDER", "").strip().lower() or provider
        verification_key = os.environ.get("AI_VERIFICATION_API_KEY", "").strip() or key
        verification_base = os.environ.get("AI_VERIFICATION_BASE_URL", "").strip() or base
        if verification_provider and verification_model:
            profiles.append(_environment_profile(
                provider=verification_provider,
                profile_id="runtime-verification",
                model_id=verification_model,
                role=ModelProfileRole.INDEPENDENT_VERIFICATION,
                capabilities=[ModelCapability.INDEPENDENT_VERIFICATION,
                              ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                              ModelCapability.STRUCTURED_OUTPUT],
                api_key=verification_key,
                base_url=verification_base,
                timeout=timeout,
                vision=True,
                model_family=os.environ.get("AI_VERIFICATION_MODEL_FAMILY", "").strip() or None,
                input_cost=_float_env("AI_VERIFICATION_INPUT_COST_USD_PER_MILLION"),
                output_cost=_float_env("AI_VERIFICATION_OUTPUT_COST_USD_PER_MILLION"),
            ))
        reasoner = os.environ.get("AI_ACCOUNTING_REASONING_MODEL", "").strip()
        reason_provider = os.environ.get("AI_ACCOUNTING_REASONING_PROVIDER", "").strip().lower() or provider
        reason_key = os.environ.get("AI_ACCOUNTING_REASONING_API_KEY", "").strip() or key
        reason_base = os.environ.get("AI_ACCOUNTING_REASONING_BASE_URL", "").strip() or base
        if reason_provider and reasoner:
            profiles.append(_environment_profile(
                provider=reason_provider,
                profile_id="runtime-accounting",
                model_id=reasoner,
                role=ModelProfileRole.ACCOUNTING_REASONING,
                capabilities=[ModelCapability.ACCOUNTING_REASONING, ModelCapability.STRUCTURED_OUTPUT],
                api_key=reason_key,
                base_url=reason_base,
                timeout=timeout,
                model_family=os.environ.get("AI_ACCOUNTING_REASONING_MODEL_FAMILY", "").strip() or None,
                input_cost=_float_env("AI_ACCOUNTING_REASONING_INPUT_COST_USD_PER_MILLION"),
                output_cost=_float_env("AI_ACCOUNTING_REASONING_OUTPUT_COST_USD_PER_MILLION"),
            ))
        profiles.extend(_provider_family_profiles())
        _record_verification_independence(profiles)
        return profiles


class ProviderCapabilityValidator:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or OpenAICompatibleProbeTransport()

    def validate(self, profile: ModelProfile) -> ModelProfileCapabilityReport:
        unavailable = []
        verified = []
        reasons = []
        evidence: list[CapabilityProbeEvidence] = []
        if not profile.enabled: reasons.append("profile_disabled")
        if not profile.credentials_present and profile.provider not in LOCAL_PROVIDER_NAMES:
            reasons.append("credentials_missing")
        if profile.provider != "openai" and not profile.base_url_configured: reasons.append("endpoint_missing")
        if reasons:
            return ModelProfileCapabilityReport(provider=profile.provider, profile_id=profile.profile_id,
                model_id=profile.model_id, declared_capabilities=profile.declared_capabilities,
                unavailable_capabilities=profile.declared_capabilities, health_status="disabled",
                failure_reason=",".join(reasons), verified_at=datetime.now(timezone.utc),
                role=profile.role, verification_independence=profile.verification_independence)
        for capability in profile.declared_capabilities:
            request = _probe_request(profile, capability)
            try:
                payload = self.transport(profile, capability, request)
                passed = _probe_passed(capability, payload)
                if passed:
                    verified.append(capability)
                else:
                    unavailable.append(capability)
                    reasons.append(f"{capability.value}:probe_assertion_failed")
                evidence.append(CapabilityProbeEvidence(
                    capability=capability, trace_id=request["trace_id"], cache_key=request["cache_key"],
                    request_role=profile.role, passed=passed,
                    failure_reason=None if passed else "probe_assertion_failed",
                ))
            except Exception as exc:
                failure = type(exc).__name__
                unavailable.append(capability)
                reasons.append(f"{capability.value}:{failure}")
                evidence.append(CapabilityProbeEvidence(
                    capability=capability, trace_id=request["trace_id"], cache_key=request["cache_key"],
                    request_role=profile.role, passed=False, failure_reason=failure,
                ))
        health = "healthy" if verified and not unavailable else ("degraded" if verified else "unavailable")
        return ModelProfileCapabilityReport(provider=profile.provider, profile_id=profile.profile_id,
            model_id=profile.model_id, declared_capabilities=profile.declared_capabilities,
            verified_capabilities=verified, unavailable_capabilities=unavailable, health_status=health,
            failure_reason=";".join(reasons) or None, verified_at=datetime.now(timezone.utc),
            role=profile.role, verification_independence=profile.verification_independence,
            probe_evidence=evidence)

    def audit(self, profiles: list[ModelProfile]) -> ProviderAuditReport:
        reports = [self.validate(profile) for profile in profiles]
        return ProviderAuditReport(profiles=reports, configured_provider_count=len(profiles),
            verified_profile_count=sum(report.health_status == "healthy" for report in reports),
            credentials_present_count=sum(profile.credentials_present for profile in profiles),
            generated_at=datetime.now(timezone.utc))


class VerifiedCapabilityRegistry:
    """Only verified reports can announce models to the Phase 3 registry."""
    def __init__(self, reports: list[ModelProfileCapabilityReport]) -> None: self.reports = reports

    def announced_model_ids(self) -> set[str]:
        return {report.model_id for report in self.reports if report.health_status in {"healthy", "degraded"}
                and report.verified_capabilities}

    def profiles_for(self, capability: ModelCapability) -> list[ModelProfileCapabilityReport]:
        return [report for report in self.reports if capability in report.verified_capabilities]

    def activated_model_registry(self) -> ModelRegistry:
        specs_by_model: dict[str, ModelSpec] = {}
        for report in self.reports:
            if not report.verified_capabilities: continue
            roles = set()
            if ModelCapability.TEXT_EXTRACTION in report.verified_capabilities: roles.add(ModelRole.EXTRACTION_TEXT)
            if ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING in report.verified_capabilities: roles.add(ModelRole.EXTRACTION_VISION)
            if ModelCapability.ACCOUNTING_REASONING in report.verified_capabilities: roles.add(ModelRole.ACCOUNTING_REASONING)
            if not roles: continue
            previous = specs_by_model.get(report.model_id)
            combined_roles = roles | (set(previous.roles) if previous else set())
            specs_by_model[report.model_id] = ModelSpec(model_id=report.model_id, provider=report.provider,
                roles=frozenset(combined_roles),
                supports_json_schema=(ModelCapability.STRUCTURED_OUTPUT in report.verified_capabilities
                                      or bool(previous and previous.supports_json_schema)),
                supports_vision=(ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING in report.verified_capabilities
                                 or bool(previous and previous.supports_vision)),
                strong_reasoner=(ModelCapability.ACCOUNTING_REASONING in report.verified_capabilities
                                 or bool(previous and previous.strong_reasoner)))
        return ModelRegistry(specs_by_model.values())

    def activation_report(self) -> ProviderActivationReport:
        text = [
            report.profile_id for report in self.reports
            if report.role is ModelProfileRole.TEXT_EXTRACTION
            and {
                ModelCapability.TEXT_EXTRACTION,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        multimodal = [
            report.profile_id for report in self.reports
            if report.role is ModelProfileRole.MULTIMODAL_EXTRACTION
            and {
                ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                ModelCapability.HANDWRITING_INTERPRETATION,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        reasoning = [
            report.profile_id for report in self.reports
            if report.role is ModelProfileRole.ACCOUNTING_REASONING
            and {
                ModelCapability.ACCOUNTING_REASONING,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        verifiers = [
            report.profile_id for report in self.reports
            if report.role is ModelProfileRole.INDEPENDENT_VERIFICATION
            and {
                ModelCapability.INDEPENDENT_VERIFICATION,
                ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        reasons = []
        if not text:
            reasons.append("verified_text_profile_missing")
        if not multimodal:
            reasons.append("verified_multimodal_profile_missing")
        if not reasoning:
            reasons.append("verified_accounting_reasoning_profile_missing")
        if not verifiers:
            reasons.append("verified_independent_verifier_missing")
        return ProviderActivationReport(
            text_profile_ids=text,
            multimodal_profile_ids=multimodal,
            reasoning_profile_ids=reasoning,
            independent_verifier_profile_ids=verifiers,
            autonomous_gateway_enabled=not reasons,
            strong_reasoning_mode="shadow",
            blocking_reasons=reasons,
        )


class OpenAICompatibleProbeTransport:
    """Uses the existing hardened client; response must satisfy each probe assertion."""
    def __call__(self, profile: ModelProfile, capability: ModelCapability,
                 request: dict[str, Any]) -> Mapping[str, Any]:
        content: list[dict[str, Any]] | str = request["prompt"]
        vision = capability in {ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING, ModelCapability.HANDWRITING_INTERPRETATION}
        if vision:
            image = os.environ.get("AI_CAPABILITY_VISION_PROBE_IMAGE", "").strip()
            if not image: raise RuntimeError("private_vision_probe_image_missing")
            content = [{"type": "text", "text": request["prompt"]},
                       {"type": "image_url", "image_url": {"url": image}}]
        payload = {"model": profile.model_id, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": request["system"]}, {"role": "user", "content": content}]}
        if profile.provider == "openai":
            payload.update({"max_completion_tokens": 512, "reasoning_effort": "low"})
        else:
            payload.update({"max_tokens": 300, "temperature": 0})
        raw = ai_provider._send_chat_completion(
            provider=profile.provider,
            payload=payload,
            vision=vision,
            api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
            base_url_override=profile.base_url,
            timeout_seconds_override=profile.timeout_seconds,
            max_attempts_override=profile.max_retries + 1,
            capability_override=capability.value,
        )
        return ai_provider._extract_json_object(raw)


def _probe_request(profile: ModelProfile, capability: ModelCapability) -> dict[str, Any]:
    role_instructions = {
        ModelProfileRole.TEXT_EXTRACTION: "Extract observable source content only; do not make accounting decisions.",
        ModelProfileRole.MULTIMODAL_EXTRACTION: "Read visual source evidence only; preserve uncertainty.",
        ModelProfileRole.INDEPENDENT_VERIFICATION: "Verify supplied evidence independently; do not reuse a prior conclusion.",
        ModelProfileRole.ACCOUNTING_REASONING: "Reason over supplied facts without changing readiness or export authorization.",
    }
    system = (
        "Return strict JSON only. This is a capability health probe; do not include reasoning narrative. "
        + role_instructions[profile.role]
    )
    prompts = {
        ModelCapability.TEXT_EXTRACTION: 'Return {"probe":"IV39C_TEXT","value":"invoice 42"}.',
        ModelCapability.STRUCTURED_OUTPUT: 'Return exactly a JSON object with probe="IV39C_JSON" and ok=true.',
        ModelCapability.LONG_DOCUMENT_PROCESSING: ("Read to the final marker and return it as probe: " + "context " * 3000 + "IV39C_LONG"),
        ModelCapability.ACCOUNTING_REASONING: 'Return JSON {"probe":"IV39C_ACCOUNTING","balanced":true} for 8.00 + 2.00 = 10.00.',
        ModelCapability.INDEPENDENT_VERIFICATION: 'Independently verify candidate total 11 against evidence subtotal 8 and tax 2; return {"probe":"IV39C_VERIFY","corrected_total":"10.00"}.',
        ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING: 'Read the private probe image marker and return {"probe":"<marker>"}.',
        ModelCapability.HANDWRITING_INTERPRETATION: 'Read the handwritten marker and return {"probe":"<marker>"}.',
    }
    prompt = prompts[capability]
    trace_id = f"{profile.trace_namespace}:{uuid.uuid4().hex}"
    cache_material = json.dumps({
        "namespace": profile.cache_namespace,
        "profile_id": profile.profile_id,
        "role": profile.role.value,
        "model_id": profile.model_id,
        "capability": capability.value,
        "prompt": prompt,
    }, sort_keys=True).encode("utf-8")
    cache_key = f"{profile.cache_namespace}:{hashlib.sha256(cache_material).hexdigest()}"
    return {"system": system, "prompt": prompt, "trace_id": trace_id,
            "cache_key": cache_key, "role": profile.role.value}


def _probe_passed(capability: ModelCapability, payload: Mapping[str, Any]) -> bool:
    expected = {ModelCapability.TEXT_EXTRACTION: "IV39C_TEXT", ModelCapability.STRUCTURED_OUTPUT: "IV39C_JSON",
        ModelCapability.LONG_DOCUMENT_PROCESSING: "IV39C_LONG", ModelCapability.ACCOUNTING_REASONING: "IV39C_ACCOUNTING",
        ModelCapability.INDEPENDENT_VERIFICATION: "IV39C_VERIFY"}
    if capability in expected:
        if payload.get("probe") != expected[capability]: return False
        if capability is ModelCapability.STRUCTURED_OUTPUT: return payload.get("ok") is True
        if capability is ModelCapability.ACCOUNTING_REASONING: return payload.get("balanced") is True
        if capability is ModelCapability.INDEPENDENT_VERIFICATION: return str(payload.get("corrected_total")) == "10.00"
        return True
    marker = os.environ.get("AI_CAPABILITY_VISION_PROBE_EXPECTED", "").strip()
    return bool(marker and payload.get("probe") == marker)


def _int_env(name: str, default: int) -> int:
    try: return int(os.environ.get(name, default))
    except ValueError: return default


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _provider_family_profiles() -> list[ModelProfile]:
    """Load optional provider-specific profiles without guessing model IDs.

    A credential by itself never activates a model.  Each role requires an
    explicit model environment variable so capability probes can validate the
    exact deployment before routing production work to it.
    """
    definitions = (
        ("gemini", "GEMINI", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("deepseek", "DEEPSEEK", "https://api.deepseek.com"),
        ("anthropic", "ANTHROPIC", "https://api.anthropic.com/v1"),
    )
    profiles: list[ModelProfile] = []
    for provider, prefix, default_base in definitions:
        key = os.environ.get(f"{prefix}_API_KEY", "").strip()
        base = os.environ.get(f"{prefix}_BASE_URL", "").strip() or default_base
        timeout = _int_env(f"{prefix}_TIMEOUT_SECONDS", 45)
        family = os.environ.get(f"{prefix}_MODEL_FAMILY", "").strip() or None
        priority = _int_env(f"{prefix}_ROUTING_PRIORITY", 100)
        common = {
            "provider": provider,
            "api_key": key,
            "base_url": base,
            "timeout": timeout,
            "model_family": family,
            "routing_priority": priority,
        }
        role_specs = (
            ("text", "TEXT_MODEL", ModelProfileRole.TEXT_EXTRACTION,
             [ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT,
              ModelCapability.LONG_DOCUMENT_PROCESSING], False),
            ("vision", "VISION_MODEL", ModelProfileRole.MULTIMODAL_EXTRACTION,
             [ModelCapability.TEXT_EXTRACTION, ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
              ModelCapability.HANDWRITING_INTERPRETATION, ModelCapability.STRUCTURED_OUTPUT], True),
            ("verification", "VERIFICATION_MODEL", ModelProfileRole.INDEPENDENT_VERIFICATION,
             [ModelCapability.INDEPENDENT_VERIFICATION,
              ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
              ModelCapability.STRUCTURED_OUTPUT], True),
            ("accounting", "ACCOUNTING_REASONING_MODEL", ModelProfileRole.ACCOUNTING_REASONING,
             [ModelCapability.ACCOUNTING_REASONING, ModelCapability.STRUCTURED_OUTPUT], False),
        )
        for suffix, model_env, role, capabilities, vision in role_specs:
            model = (os.environ.get(f"{prefix}_{model_env}", "").strip()
                     or (os.environ.get(f"{prefix}_MODEL", "").strip() if suffix == "text" else ""))
            if not model:
                continue
            role_prefix = f"{prefix}_{suffix.upper()}"
            profiles.append(_environment_profile(
                profile_id=f"{provider}-{suffix}", model_id=model, role=role,
                capabilities=capabilities, vision=vision,
                input_cost=(_float_env(f"{role_prefix}_INPUT_COST_USD_PER_MILLION")
                            if os.environ.get(f"{role_prefix}_INPUT_COST_USD_PER_MILLION", "").strip()
                            else _float_env(f"{prefix}_INPUT_COST_USD_PER_MILLION")),
                output_cost=(_float_env(f"{role_prefix}_OUTPUT_COST_USD_PER_MILLION")
                             if os.environ.get(f"{role_prefix}_OUTPUT_COST_USD_PER_MILLION", "").strip()
                             else _float_env(f"{prefix}_OUTPUT_COST_USD_PER_MILLION")),
                **common,
            ))
    return profiles


def _local_environment_profiles() -> list[ModelProfile]:
    """Load loopback profiles only; remote environment profiles are ignored."""

    model = os.environ.get("LOCAL_MULTIMODAL_MODEL", "").strip()
    if not model:
        return []
    evaluation_profile_id = os.environ.get(
        "LOCAL_MULTIMODAL_PROFILE_ID", "",
    ).strip()
    base_url = os.environ.get(
        "LOCAL_MULTIMODAL_BASE_URL", "http://127.0.0.1:11434",
    ).strip()
    timeout = _int_env("LOCAL_MULTIMODAL_TIMEOUT_SECONDS", 180)
    common = {
        "provider": "local_ollama",
        "model_id": model,
        "api_key": "",
        "base_url": base_url,
        "timeout": timeout,
        "model_family": os.environ.get(
            "LOCAL_MULTIMODAL_MODEL_FAMILY", "qwen3-vl",
        ).strip(),
        "input_cost": 0.0,
        "output_cost": 0.0,
        "routing_priority": 0,
    }
    specs = [
        (
            (
                f"{evaluation_profile_id}-text"
                if evaluation_profile_id else "local-text"
            ),
            ModelProfileRole.TEXT_EXTRACTION,
            [ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT,
             ModelCapability.LONG_DOCUMENT_PROCESSING], False,
        ),
        (
            evaluation_profile_id or "local-vision",
            ModelProfileRole.MULTIMODAL_EXTRACTION,
            [ModelCapability.TEXT_EXTRACTION,
             ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
             ModelCapability.HANDWRITING_INTERPRETATION,
             ModelCapability.STRUCTURED_OUTPUT], True,
        ),
        (
            (
                f"{evaluation_profile_id}-verification"
                if evaluation_profile_id else "local-verification"
            ),
            ModelProfileRole.INDEPENDENT_VERIFICATION,
            [ModelCapability.INDEPENDENT_VERIFICATION,
             ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
             ModelCapability.STRUCTURED_OUTPUT], True,
        ),
        (
            (
                f"{evaluation_profile_id}-accounting"
                if evaluation_profile_id else "local-accounting"
            ),
            ModelProfileRole.ACCOUNTING_REASONING,
            [ModelCapability.ACCOUNTING_REASONING,
             ModelCapability.STRUCTURED_OUTPUT], False,
        ),
    ]
    profiles = [
        _environment_profile(
            profile_id=profile_id,
            role=role,
            capabilities=capabilities,
            vision=vision,
            **common,
        )
        for profile_id, role, capabilities, vision in specs
    ]
    # Ollama's loopback API has no credential. Marking this as present means
    # "no credential required", not that a secret exists.
    for profile in profiles:
        profile.credentials_present = True
        profile.base_url_configured = True
    _record_verification_independence(profiles)
    return profiles


def _environment_profile(*, provider: str, profile_id: str, model_id: str,
                         role: ModelProfileRole, capabilities: list[ModelCapability],
                         api_key: str, base_url: str, timeout: int,
                         vision: bool = False, model_family: str | None = None,
                         input_cost: float | None = None,
                         output_cost: float | None = None,
                         routing_priority: int = 100) -> ModelProfile:
    return ModelProfile(
        provider=provider,
        profile_id=profile_id,
        model_id=model_id,
        role=role,
        declared_capabilities=capabilities,
        credentials_present=bool(api_key),
        base_url_configured=bool(base_url or provider == "openai"),
        vision=vision,
        timeout_seconds=timeout,
        api_key=SecretStr(api_key) if api_key else None,
        base_url=base_url or None,
        trace_namespace=f"provider-trace:{profile_id}",
        cache_namespace=f"provider-cache:{profile_id}",
        model_family=model_family,
        input_cost_usd_per_million=input_cost,
        output_cost_usd_per_million=output_cost,
        routing_priority=routing_priority,
    )


def _record_verification_independence(profiles: list[ModelProfile]) -> None:
    verifier = next((item for item in profiles
                     if item.role is ModelProfileRole.INDEPENDENT_VERIFICATION), None)
    if verifier is None:
        return
    extraction = [item for item in profiles if item.role in {
        ModelProfileRole.TEXT_EXTRACTION,
        ModelProfileRole.MULTIMODAL_EXTRACTION,
    }]
    same_family = any(
        verifier.provider == item.provider
        and (
            verifier.model_id == item.model_id
            or bool(verifier.model_family and item.model_family
                    and verifier.model_family == item.model_family)
        )
        for item in extraction
    )
    verifier.verification_independence = (
        "isolated_same_family" if same_family else "isolated_unconfirmed_family"
    )


__all__ = ["CapabilityProbeEvidence", "ModelCapability", "ModelProfile", "ModelProfileCapabilityReport",
           "ModelProfileRole", "OpenAICompatibleProbeTransport",
           "ProfileLoader", "ProviderActivationReport", "ProviderAuditReport", "ProviderCapabilityValidator",
           "VerifiedCapabilityRegistry"]
