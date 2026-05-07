"""Vendor Rules Studio — FastAPI routes.

Phase 1Z. Frontend at /webapp/frontend/src/components/VendorRulesStudio.tsx
calls these:

  GET    /api/vendor-rules                 — list editable vendors
  GET    /api/vendor-rules/{vendor_key}    — current rules + UI groups
  POST   /api/vendor-rules/{vendor_key}/validate  — dry-run a patch
  PATCH  /api/vendor-rules/{vendor_key}    — apply a patch (atomic + backup)
  POST   /api/vendor-rules/{vendor_key}/restore   — restore most recent backup

The patch body is a *dotted-flat* mapping:
    {"vendor_identity.vendor_name": "...", "amount_rules.tolerance": 0.05}
or a *nested* mapping. Both are accepted.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import vendor_rules as svc
from ..services import rules_impact


router = APIRouter(prefix="/api/vendor-rules", tags=["vendor-rules"])


class PatchBody(BaseModel):
    patch: dict[str, Any]


class PreviewImpactBody(BaseModel):
    batch_id: str
    draft_rules: dict[str, Any] = {}
    # Reserved for forward-compat. The current implementation always
    # compares against the batch's saved preview cache; future versions
    # could compare against an in-memory snapshot.
    compare_against_saved: bool = True


@router.get("")
def list_vendors() -> dict[str, Any]:
    return {"vendors": svc.list_editable_vendors()}


@router.get("/{vendor_key}")
def get_vendor(vendor_key: str) -> dict[str, Any]:
    try:
        groups = svc.editable_groups(vendor_key)
    except svc.VendorRulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "vendor_key": vendor_key,
        "groups": groups,
    }


@router.post("/{vendor_key}/validate")
def validate(vendor_key: str, body: PatchBody) -> dict[str, Any]:
    try:
        issues = svc.validate_patch(vendor_key, body.patch or {})
    except svc.VendorRulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"vendor_key": vendor_key, "ok": len(issues) == 0, "issues": issues}


@router.patch("/{vendor_key}")
def patch(vendor_key: str, body: PatchBody) -> dict[str, Any]:
    try:
        result = svc.apply_patch(vendor_key, body.patch or {})
    except svc.VendorRulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Re-read so the UI shows the canonical state after save.
    groups = svc.editable_groups(vendor_key)
    return {
        "vendor_key": vendor_key,
        "result": result,
        "groups": groups,
    }


@router.post("/{vendor_key}/preview-impact")
def preview_impact(vendor_key: str, body: PreviewImpactBody) -> dict[str, Any]:
    """Phase 2A — dry-run a draft patch against an existing batch and return
    a row-level diff vs. the batch's saved preview.

    No YAML is written, no Dropbox call is made, no Excel file is touched.
    See `services/rules_impact.py` for the full safety description.
    """
    try:
        return rules_impact.preview_rule_impact(
            vendor_key=vendor_key,
            batch_id=body.batch_id,
            patch=body.draft_rules or {},
        )
    except svc.VendorRulesError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{vendor_key}/restore")
def restore(vendor_key: str) -> dict[str, Any]:
    try:
        result = svc.restore_latest_backup(vendor_key)
    except svc.VendorRulesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    groups = svc.editable_groups(vendor_key)
    return {
        "vendor_key": vendor_key,
        "result": result,
        "groups": groups,
    }
