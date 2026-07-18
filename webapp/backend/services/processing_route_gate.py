"""Authoritative execution gate for deterministic versus AI processing.

The persisted policy records what the operator authorized.  This module joins
that request with runtime processor availability and produces the only route
decision the batch orchestrator may execute.  It does not inspect accounting
fields and cannot authorize export.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from .processing_route_policy import (
    CONTRACT_VERSION as POLICY_CONTRACT_VERSION,
    ProcessingRouteMode,
    ProcessingRouteResolution,
)


CONTRACT_VERSION = "processing-route-decision/1.0"


class EffectiveProcessingRoute(str, Enum):
    DETERMINISTIC = "deterministic"
    AI = "ai"
    BLOCKED = "blocked"


class ProcessingRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    policy_contract_version: str = POLICY_CONTRACT_VERSION
    batch_id: str
    filename: str | None = None
    page: int | None = None
    requested_mode: ProcessingRouteMode
    inherited_from: str
    effective_route: EffectiveProcessingRoute
    deterministic_available: bool
    vendor_key: str | None = None
    processor_id: str | None = None
    ai_fallback_authorized: bool = False
    reason_code: str


def decide_processing_route(
    resolution: ProcessingRouteResolution,
    *,
    vendor_key: str | None,
    deterministic_available: bool,
    processor_id: str | None = None,
) -> ProcessingRouteDecision:
    """Resolve one executable route without ambiguity or silent escalation."""

    normalized_vendor = str(vendor_key or "").strip() or None
    available = bool(deterministic_available and normalized_vendor)
    mode = resolution.requested_mode

    if mode == ProcessingRouteMode.DETERMINISTIC_ONLY:
        if available:
            route = EffectiveProcessingRoute.DETERMINISTIC
            reason = "operator_locked_deterministic"
        else:
            route = EffectiveProcessingRoute.BLOCKED
            reason = "deterministic_processor_unavailable"
        ai_authorized = False
    elif mode == ProcessingRouteMode.AI_FALLBACK_ALLOWED:
        if available:
            route = EffectiveProcessingRoute.DETERMINISTIC
            reason = "deterministic_first_ai_fallback_authorized"
            ai_authorized = True
        else:
            route = EffectiveProcessingRoute.AI
            reason = "ai_authorized_no_deterministic_processor"
            ai_authorized = True
    else:
        # Cost-safe auto is intentionally stronger than the legacy behavior:
        # a recognized deterministic processor is locked and may not silently
        # escalate to a paid provider.  Unknown documents remain eligible for
        # the universal AI route because no deterministic route exists.
        if available:
            route = EffectiveProcessingRoute.DETERMINISTIC
            reason = "cost_safe_deterministic_default"
            ai_authorized = False
        else:
            route = EffectiveProcessingRoute.AI
            reason = "cost_safe_ai_no_deterministic_processor"
            ai_authorized = True

    return ProcessingRouteDecision(
        batch_id=resolution.batch_id,
        filename=resolution.filename,
        page=resolution.page,
        requested_mode=mode,
        inherited_from=resolution.inherited_from,
        effective_route=route,
        deterministic_available=available,
        vendor_key=normalized_vendor,
        processor_id=(processor_id if available else None),
        ai_fallback_authorized=ai_authorized,
        reason_code=reason,
    )


__all__ = [
    "CONTRACT_VERSION",
    "EffectiveProcessingRoute",
    "ProcessingRouteDecision",
    "decide_processing_route",
]
