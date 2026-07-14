"""Versioned model roles and capability discovery for Phase 3.

Registry entries describe policy. Discovery describes what the configured
runtime has actually advertised. Keeping both separate prevents a model name
from being treated as proof of availability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


class ModelRole(str, Enum):
    EXTRACTION_TEXT = "extraction_text"
    EXTRACTION_VISION = "extraction_vision"
    ACCOUNTING_REASONING = "accounting_reasoning"


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    provider: str
    roles: frozenset[ModelRole]
    supports_json_schema: bool = True
    supports_vision: bool = False
    strong_reasoner: bool = False
    input_cost_per_million_usd: float | None = None
    output_cost_per_million_usd: float | None = None
    max_latency_ms: int = 45_000


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    provider: str
    available: bool
    source: str
    roles: frozenset[ModelRole] = field(default_factory=frozenset)
    reason: str = ""


class ModelRegistry:
    def __init__(self, specs: Iterable[ModelSpec]) -> None:
        items = list(specs)
        self._specs = {item.model_id: item for item in items}
        if len(self._specs) != len(items):
            raise ValueError("model_id values must be unique")

    def get(self, model_id: str | None) -> ModelSpec | None:
        return self._specs.get((model_id or "").strip())

    def for_role(self, role: ModelRole) -> list[ModelSpec]:
        return [item for item in self._specs.values() if role in item.roles]


def default_registry() -> ModelRegistry:
    """Build policy from explicit environment configuration, not guesses."""
    provider = os.environ.get("AI_PROVIDER", "").strip().lower() or "unconfigured"
    specs: dict[str, ModelSpec] = {}

    def add(model_id: str, model_provider: str, role: ModelRole, *,
            supports_vision: bool = False, strong_reasoner: bool = False) -> None:
        previous = specs.get(model_id)
        roles = set(previous.roles) if previous else set()
        roles.add(role)
        specs[model_id] = ModelSpec(
            model_id, model_provider, frozenset(roles),
            supports_vision=supports_vision or bool(previous and previous.supports_vision),
            strong_reasoner=strong_reasoner or bool(previous and previous.strong_reasoner),
        )
    text = os.environ.get("AI_MODEL", "").strip()
    vision = os.environ.get("AI_VISION_MODEL", "").strip()
    strong = os.environ.get("AI_ACCOUNTING_REASONING_MODEL", "").strip()
    if text:
        add(text, provider, ModelRole.EXTRACTION_TEXT)
    if vision:
        add(vision, os.environ.get("AI_VISION_PROVIDER", "").strip().lower() or provider,
            ModelRole.EXTRACTION_VISION, supports_vision=True)
    if strong:
        add(strong, os.environ.get("AI_ACCOUNTING_REASONING_PROVIDER", "").strip().lower() or provider,
            ModelRole.ACCOUNTING_REASONING, strong_reasoner=True)
    return ModelRegistry(specs.values())


class CapabilityDiscovery:
    """Conservative discovery based on provider-advertised model identifiers.

    ``AI_AVAILABLE_MODELS`` must be populated by deployment discovery or an
    approved startup probe. Presence of credentials or a configured name alone
    is deliberately insufficient.
    """
    def __init__(self, registry: ModelRegistry, advertised: Iterable[str] | None = None) -> None:
        self.registry = registry
        raw = advertised if advertised is not None else os.environ.get("AI_AVAILABLE_MODELS", "").split(",")
        self.advertised = {item.strip() for item in raw if item.strip()}

    def discover(self, model_id: str | None) -> ModelCapability:
        spec = self.registry.get(model_id)
        if spec is None:
            return ModelCapability(model_id or "", "unconfigured", False, "registry", reason="not_registered")
        available = spec.model_id in self.advertised
        return ModelCapability(spec.model_id, spec.provider, available, "provider_advertisement",
                               spec.roles, "advertised" if available else "not_advertised")

    def strong_accounting_model(self) -> ModelCapability | None:
        for spec in self.registry.for_role(ModelRole.ACCOUNTING_REASONING):
            capability = self.discover(spec.model_id)
            if capability.available and spec.strong_reasoner:
                return capability
        return None
