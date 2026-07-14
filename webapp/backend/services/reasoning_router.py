"""Deterministic Phase 3 routing state machine; never decides readiness."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model_registry import CapabilityDiscovery


class Route(str, Enum):
    DETERMINISTIC = "deterministic"
    AI_TEXT = "ai_text"
    AI_VISION = "ai_vision"
    STRONG_REASONING_SHADOW = "strong_reasoning_shadow"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class RoutingSignals:
    deterministic_parser_succeeded: bool
    facts_complete: bool
    image_document: bool = False
    ocr_quality: float | None = None
    accounting_ambiguity: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    route: Route
    reason_code: str
    model_id: str | None = None
    shadow_only: bool = False


class RoutingStateMachine:
    def __init__(self, discovery: CapabilityDiscovery, *, text_available: bool, vision_available: bool) -> None:
        self.discovery = discovery
        self.text_available = text_available
        self.vision_available = vision_available

    def decide_extraction(self, signals: RoutingSignals) -> RoutingDecision:
        if signals.deterministic_parser_succeeded and signals.facts_complete:
            return RoutingDecision(Route.DETERMINISTIC, "deterministic_complete")
        weak_ocr = signals.ocr_quality is not None and signals.ocr_quality < 0.65
        if (signals.image_document or weak_ocr) and self.vision_available:
            return RoutingDecision(Route.AI_VISION, "visual_or_low_ocr")
        if self.text_available:
            return RoutingDecision(Route.AI_TEXT, "incomplete_deterministic_facts")
        return RoutingDecision(Route.MANUAL_REVIEW, "no_extraction_capability")

    def decide_accounting_shadow(self, signals: RoutingSignals) -> RoutingDecision:
        capability = self.discovery.strong_accounting_model()
        if signals.accounting_ambiguity and capability:
            return RoutingDecision(Route.STRONG_REASONING_SHADOW, "ambiguous_accounting_decision",
                                   capability.model_id, shadow_only=True)
        return RoutingDecision(Route.DETERMINISTIC, "central_engine_authoritative")
