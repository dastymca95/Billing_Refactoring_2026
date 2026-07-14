"""Typed, probe-backed provider capability discovery for Phase 3.9C."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from pydantic import BaseModel, Field

from . import ai_provider
from .model_registry import ModelRegistry, ModelRole, ModelSpec


class ModelCapability(str, Enum):
    TEXT_EXTRACTION = "text_extraction"
    VISUAL_DOCUMENT_UNDERSTANDING = "visual_document_understanding"
    HANDWRITING_INTERPRETATION = "handwriting_interpretation"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_DOCUMENT_PROCESSING = "long_document_processing"
    ACCOUNTING_REASONING = "accounting_reasoning"
    INDEPENDENT_VERIFICATION = "independent_verification"


class ModelProfile(BaseModel):
    provider: str
    profile_id: str
    model_id: str
    declared_capabilities: list[ModelCapability]
    base_url_configured: bool = False
    credentials_present: bool = False
    enabled: bool = True
    vision: bool = False
    timeout_seconds: int = Field(default=45, ge=1, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)


class ModelProfileCapabilityReport(BaseModel):
    provider: str
    profile_id: str
    model_id: str
    declared_capabilities: list[ModelCapability]
    verified_capabilities: list[ModelCapability] = Field(default_factory=list)
    unavailable_capabilities: list[ModelCapability] = Field(default_factory=list)
    health_status: str
    failure_reason: str | None = None
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
        provider = os.environ.get("AI_PROVIDER", "").strip().lower()
        key = os.environ.get("AI_API_KEY", "").strip()
        base = os.environ.get("AI_BASE_URL", "").strip()
        timeout = _int_env("AI_TIMEOUT_SECONDS", 45); profiles = []
        text = os.environ.get("AI_MODEL", "").strip()
        if provider and text:
            profiles.append(ModelProfile(provider=provider, profile_id="runtime-text", model_id=text,
                declared_capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT,
                    ModelCapability.LONG_DOCUMENT_PROCESSING], credentials_present=bool(key),
                base_url_configured=bool(base or provider == "openai"), timeout_seconds=timeout))
        vision_model = os.environ.get("AI_VISION_MODEL", "").strip()
        vision_provider = os.environ.get("AI_VISION_PROVIDER", "").strip().lower() or provider
        vision_key = os.environ.get("AI_VISION_API_KEY", "").strip() or key
        vision_base = os.environ.get("AI_VISION_BASE_URL", "").strip() or base
        if vision_provider and vision_model:
            profiles.append(ModelProfile(provider=vision_provider, profile_id="runtime-vision", model_id=vision_model,
                declared_capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                    ModelCapability.STRUCTURED_OUTPUT], credentials_present=bool(vision_key),
                base_url_configured=bool(vision_base or vision_provider == "openai"), vision=True, timeout_seconds=timeout))
        reasoner = os.environ.get("AI_ACCOUNTING_REASONING_MODEL", "").strip()
        reason_provider = os.environ.get("AI_ACCOUNTING_REASONING_PROVIDER", "").strip().lower() or provider
        if reason_provider and reasoner:
            profiles.append(ModelProfile(provider=reason_provider, profile_id="runtime-accounting", model_id=reasoner,
                declared_capabilities=[ModelCapability.ACCOUNTING_REASONING, ModelCapability.INDEPENDENT_VERIFICATION,
                    ModelCapability.STRUCTURED_OUTPUT], credentials_present=bool(key),
                base_url_configured=bool(base or reason_provider == "openai"), timeout_seconds=timeout))
        return profiles


class ProviderCapabilityValidator:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or OpenAICompatibleProbeTransport()

    def validate(self, profile: ModelProfile) -> ModelProfileCapabilityReport:
        unavailable = []; verified = []; reasons = []
        if not profile.enabled: reasons.append("profile_disabled")
        if not profile.credentials_present: reasons.append("credentials_missing")
        if profile.provider != "openai" and not profile.base_url_configured: reasons.append("endpoint_missing")
        if reasons:
            return ModelProfileCapabilityReport(provider=profile.provider, profile_id=profile.profile_id,
                model_id=profile.model_id, declared_capabilities=profile.declared_capabilities,
                unavailable_capabilities=profile.declared_capabilities, health_status="disabled",
                failure_reason=",".join(reasons), verified_at=datetime.now(timezone.utc))
        for capability in profile.declared_capabilities:
            try:
                payload = self.transport(profile, capability, _probe_request(capability))
                if _probe_passed(capability, payload): verified.append(capability)
                else: unavailable.append(capability); reasons.append(f"{capability.value}:probe_assertion_failed")
            except Exception as exc:
                unavailable.append(capability); reasons.append(f"{capability.value}:{type(exc).__name__}")
        health = "healthy" if verified and not unavailable else ("degraded" if verified else "unavailable")
        return ModelProfileCapabilityReport(provider=profile.provider, profile_id=profile.profile_id,
            model_id=profile.model_id, declared_capabilities=profile.declared_capabilities,
            verified_capabilities=verified, unavailable_capabilities=unavailable, health_status=health,
            failure_reason=";".join(reasons) or None, verified_at=datetime.now(timezone.utc))

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
        specs = []
        for report in self.reports:
            if not report.verified_capabilities: continue
            roles = set()
            if ModelCapability.TEXT_EXTRACTION in report.verified_capabilities: roles.add(ModelRole.EXTRACTION_TEXT)
            if ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING in report.verified_capabilities: roles.add(ModelRole.EXTRACTION_VISION)
            if ModelCapability.ACCOUNTING_REASONING in report.verified_capabilities: roles.add(ModelRole.ACCOUNTING_REASONING)
            if not roles: continue
            specs.append(ModelSpec(model_id=report.model_id, provider=report.provider, roles=frozenset(roles),
                supports_json_schema=ModelCapability.STRUCTURED_OUTPUT in report.verified_capabilities,
                supports_vision=ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING in report.verified_capabilities,
                strong_reasoner=ModelCapability.ACCOUNTING_REASONING in report.verified_capabilities))
        return ModelRegistry(specs)

    def activation_report(self) -> ProviderActivationReport:
        multimodal = [
            report.profile_id for report in self.reports
            if {
                ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        reasoning = [
            report.profile_id for report in self.reports
            if {
                ModelCapability.ACCOUNTING_REASONING,
                ModelCapability.STRUCTURED_OUTPUT,
            }.issubset(set(report.verified_capabilities))
        ]
        verifiers = [
            report.profile_id for report in self.reports
            if ModelCapability.INDEPENDENT_VERIFICATION in report.verified_capabilities
        ]
        reasons = []
        if not multimodal:
            reasons.append("verified_multimodal_profile_missing")
        if not reasoning:
            reasons.append("verified_accounting_reasoning_profile_missing")
        if not verifiers:
            reasons.append("verified_independent_verifier_missing")
        return ProviderActivationReport(
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
        payload = {"model": profile.model_id, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": request["system"]}, {"role": "user", "content": content}],
            "max_tokens": 300}
        raw = ai_provider._send_chat_completion(provider=profile.provider, payload=payload, vision=vision)
        return json.loads(raw)


def _probe_request(capability: ModelCapability) -> dict[str, Any]:
    system = "Return strict JSON only. This is a capability health probe; do not include reasoning narrative."
    prompts = {
        ModelCapability.TEXT_EXTRACTION: 'Return {"probe":"IV39C_TEXT","value":"invoice 42"}.',
        ModelCapability.STRUCTURED_OUTPUT: 'Return exactly a JSON object with probe="IV39C_JSON" and ok=true.',
        ModelCapability.LONG_DOCUMENT_PROCESSING: ("Read to the final marker and return it as probe: " + "context " * 3000 + "IV39C_LONG"),
        ModelCapability.ACCOUNTING_REASONING: 'Return JSON {"probe":"IV39C_ACCOUNTING","balanced":true} for 8.00 + 2.00 = 10.00.',
        ModelCapability.INDEPENDENT_VERIFICATION: 'Independently verify candidate total 11 against evidence subtotal 8 and tax 2; return {"probe":"IV39C_VERIFY","corrected_total":"10.00"}.',
        ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING: 'Read the private probe image marker and return {"probe":"<marker>"}.',
        ModelCapability.HANDWRITING_INTERPRETATION: 'Read the handwritten marker and return {"probe":"<marker>"}.',
    }
    return {"system": system, "prompt": prompts[capability]}


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


__all__ = ["ModelCapability", "ModelProfile", "ModelProfileCapabilityReport", "OpenAICompatibleProbeTransport",
           "ProfileLoader", "ProviderActivationReport", "ProviderAuditReport", "ProviderCapabilityValidator",
           "VerifiedCapabilityRegistry"]
