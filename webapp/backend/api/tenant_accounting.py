"""Tenant-governed vendor entities and accounting policy endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import tenant_accounting_policies as policies


router = APIRouter(prefix="/api/tenant-accounting", tags=["tenant-accounting"])


class VendorEntityCreateRequest(BaseModel):
    tenant_id: str | None = None
    draft: policies.VendorEntityDraft
    actor: str = "local_operator"


class PolicyCreateRequest(BaseModel):
    tenant_id: str | None = None
    draft: policies.TenantPolicyDraft
    source_interaction_id: str | None = None
    actor: str = "local_operator"


class PolicyUpdateRequest(BaseModel):
    tenant_id: str | None = None
    draft: policies.TenantPolicyDraft
    actor: str = "local_operator"


class PolicySimulationRequest(BaseModel):
    tenant_id: str | None = None
    lines: list[policies.PolicySimulationLine] = Field(max_length=10000)
    actor: str = "local_operator"


class PolicyDecisionRequest(BaseModel):
    tenant_id: str | None = None
    approve: bool
    actor: str = "local_operator"


class PolicyStatusRequest(BaseModel):
    tenant_id: str | None = None
    enabled: bool
    actor: str = "local_operator"


@router.get("/context")
def tenant_context() -> dict:
    try:
        tenant_id = policies.default_tenant_id()
        return {
            "tenant_id": tenant_id,
            "context_source": "environment_adapter",
            "production_auth_required": True,
        }
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/vendors")
def list_vendor_entities(tenant_id: str | None = None) -> dict:
    try:
        resolved = _tenant(tenant_id)
        items = policies.list_vendor_entities(resolved)
        return {
            "contract_version": policies.VENDOR_ENTITY_CONTRACT_VERSION,
            "tenant_id": resolved,
            "items": [item.model_dump(mode="json") for item in items],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/vendors")
def create_vendor_entity(body: VendorEntityCreateRequest) -> dict:
    try:
        return policies.create_vendor_entity(
            _tenant(body.tenant_id), body.draft, actor=body.actor,
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/policies")
def list_policies(tenant_id: str | None = None) -> dict:
    try:
        resolved = _tenant(tenant_id)
        items = policies.list_policies(resolved)
        return {
            "contract_version": policies.TENANT_POLICY_CONTRACT_VERSION,
            "tenant_id": resolved,
            "items": [item.model_dump(mode="json") for item in items],
            "active_count": sum(
                item.status is policies.TenantPolicyStatus.ACTIVE for item in items
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/policies")
def create_policy(body: PolicyCreateRequest) -> dict:
    try:
        return policies.create_policy_draft(
            _tenant(body.tenant_id),
            body.draft,
            source_interaction_id=body.source_interaction_id,
            actor=body.actor,
        ).model_dump(mode="json")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/policies/{policy_id}")
def update_policy(policy_id: str, body: PolicyUpdateRequest) -> dict:
    try:
        return policies.update_policy_draft(
            _tenant(body.tenant_id), policy_id, body.draft, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Tenant policy not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/policies/{policy_id}/simulate")
def simulate_policy(policy_id: str, body: PolicySimulationRequest) -> dict:
    try:
        return policies.simulate_policy(
            _tenant(body.tenant_id), policy_id, body.lines, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Tenant policy not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/policies/{policy_id}/decision")
def decide_policy(policy_id: str, body: PolicyDecisionRequest) -> dict:
    try:
        return policies.decide_policy(
            _tenant(body.tenant_id), policy_id,
            approve=body.approve, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Tenant policy not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/policies/{policy_id}/status")
def set_policy_status(policy_id: str, body: PolicyStatusRequest) -> dict:
    try:
        return policies.set_policy_enabled(
            _tenant(body.tenant_id), policy_id,
            enabled=body.enabled, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Tenant policy not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _tenant(value: str | None) -> str:
    try:
        return policies.resolve_tenant_context(value)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=str(exc),
        )
