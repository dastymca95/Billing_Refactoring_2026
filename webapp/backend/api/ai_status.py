"""AI fallback status endpoint — Phase 1H foundation.

The frontend pings `GET /api/ai/status` once on app load (and again
after settings changes) to decide whether to show the AI badge as
"AI: off", "AI: not configured", or "AI: ready". Never returns API keys
or other secrets — only operator-safe metadata."""

from __future__ import annotations

from fastapi import APIRouter

from ..services.ai_fallback import get_service


router = APIRouter(prefix="/api/ai", tags=["ai_fallback"])


@router.get("/status")
def get_ai_status() -> dict:
    svc = get_service()
    s = svc.status()
    return {
        "enabled": s.enabled,
        "provider": s.provider,
        "configured": s.configured,
        "reason": s.reason,
        "policy": s.policy,
        "max_cost_per_batch_usd": s.max_cost_per_batch_usd,
        "allowed_tasks": s.allowed_tasks,
    }
