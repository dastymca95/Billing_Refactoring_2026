"""AI fallback status endpoint — Phase 1H foundation.

The frontend pings `GET /api/ai/status` once on app load (and again
after settings changes) to decide whether to show the AI badge as
"AI: off", "AI: not configured", or "AI: ready". Never returns API keys
or other secrets — only operator-safe metadata."""

from __future__ import annotations

from fastapi import APIRouter

from ..services import ai_provider


router = APIRouter(prefix="/api/ai", tags=["ai_fallback"])


@router.get("/status")
def get_ai_status() -> dict:
    payload = ai_provider.status_payload()
    # Keep legacy keys used by the existing badge while adding the
    # Phase AI-1 provider-neutral fields.
    return {
        **payload,
        "reason": payload["message"],
        "policy": "invoice_extraction_candidates",
        "max_cost_per_batch_usd": None,
        "allowed_tasks": [
            "variable vendor invoice extraction",
            "line item reading",
            "vendor matching",
            "GL mapping suggestions",
            "total validation",
        ],
    }
