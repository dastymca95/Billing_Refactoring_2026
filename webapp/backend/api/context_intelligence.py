"""HTTP boundary for tenant-scoped ResMan Context Intelligence."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import context_intelligence as intelligence
from ..services import tenant_accounting_policies as tenants


router = APIRouter(prefix="/api/context-intelligence", tags=["context-intelligence"])


class ScanRequest(BaseModel):
    tenant_id: str | None = None
    actor: str = Field(default="local_operator", min_length=1, max_length=120)


class GovernanceRequest(intelligence.GovernanceUpdate):
    tenant_id: str | None = None


@router.get("/status")
def status(tenant_id: str | None = None) -> dict:
    return intelligence.status(_tenant(tenant_id))


@router.post("/scan")
def scan(body: ScanRequest) -> dict:
    try:
        report = intelligence.scan_resman(_tenant(body.tenant_id), actor=body.actor)
        return {
            "contract_version": intelligence.CONTRACT_VERSION,
            "state": "ready",
            "snapshot": report.model_dump(mode="json", exclude={"vendors", "properties", "audit"}),
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/matrix")
def matrix(
    dimension: Literal["vendors", "properties"] = "vendors",
    tenant_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=250),
    search: str = Query(default="", max_length=200),
    mode: str = Query(default="", max_length=80),
) -> dict:
    try:
        return intelligence.list_matrix(
            _tenant(tenant_id), dimension=dimension, page=page,
            page_size=page_size, search=search, mode=mode,
        ).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/vendors/{vendor_key}")
def vendor_detail(vendor_key: str, tenant_id: str | None = None) -> dict:
    try:
        return intelligence.vendor_detail(_tenant(tenant_id), vendor_key).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Vendor profile not found.")


@router.put("/vendors/{vendor_key}/governance")
def update_vendor(vendor_key: str, body: GovernanceRequest) -> dict:
    try:
        return intelligence.update_vendor_governance(
            _tenant(body.tenant_id), vendor_key, intelligence.GovernanceUpdate.model_validate(body.model_dump()),
        ).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Vendor profile not found.")


@router.get("/properties/{property_key}")
def property_detail(property_key: str, tenant_id: str | None = None) -> dict:
    try:
        return intelligence.property_detail(_tenant(tenant_id), property_key).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Property profile not found.")


def _tenant(value: str | None) -> str:
    try:
        return tenants.resolve_tenant_context(value)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
