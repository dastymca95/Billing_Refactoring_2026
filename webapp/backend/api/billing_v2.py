"""Billing V2 endpoints.

These endpoints are additive and do not alter the legacy batch workflow.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..services import billing_v2


router = APIRouter(prefix="/api/billing-v2", tags=["billing_v2"])


@router.get("/audit")
def audit_endpoint() -> dict:
    return billing_v2.deterministic_processor_audit()


@router.post("/batches/{batch_id}/prepare-links")
def prepare_links_endpoint(batch_id: str) -> dict:
    try:
        return billing_v2.prepare_document_links(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}") from exc
