"""HTTP boundary for the tenant-scoped ResMan Context Data Hub."""
from __future__ import annotations
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..services import resman_context_data as hub
from ..services import tenant_accounting_policies as tenants


router = APIRouter(prefix="/api/resman-context", tags=["resman-context"])


class PublishRequest(BaseModel):
    tenant_id: str | None = None
    actor: str = Field(default="local_operator", min_length=1, max_length=120)


class MutationRequest(hub.RecordMutation):
    pass


@router.get("/status")
def status(tenant_id: str | None = None) -> dict:
    resolved = _tenant(tenant_id)
    return {
        "contract_version": hub.CONTRACT_VERSION,
        "tenant_id": resolved,
        "datasets": [item.model_dump(mode="json") for item in hub.all_statuses(resolved)],
    }


@router.post("/{dataset}/imports/preview")
async def preview_import(
    dataset: hub.DatasetKind,
    file: UploadFile = File(...),
    tenant_id: str | None = Query(default=None),
) -> dict:
    try:
        content = await file.read(hub.MAX_UPLOAD_BYTES + 1)
        return hub.stage_import(
            _tenant(tenant_id), dataset, file.filename or "resman-report.csv", content,
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{dataset}/imports/{import_id}/publish")
def publish_import(
    dataset: hub.DatasetKind, import_id: str, body: PublishRequest,
) -> dict:
    try:
        return hub.publish_import(_tenant(body.tenant_id), dataset, import_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Staged import not found.")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{dataset}/snapshots")
def snapshots(dataset: hub.DatasetKind, tenant_id: str | None = None) -> dict:
    resolved = _tenant(tenant_id)
    return {
        "contract_version": hub.CONTRACT_VERSION,
        "tenant_id": resolved,
        "dataset": dataset.value,
        "items": [item.model_dump(mode="json") for item in hub.list_snapshots(resolved, dataset)],
    }


@router.post("/{dataset}/snapshots/{snapshot_id}/activate")
def activate_snapshot(
    dataset: hub.DatasetKind, snapshot_id: str, body: PublishRequest,
) -> dict:
    try:
        return hub.activate_snapshot(
            _tenant(body.tenant_id), dataset, snapshot_id, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Snapshot not found.")


@router.get("/{dataset}/records")
def records(
    dataset: hub.DatasetKind,
    tenant_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=250),
    search: str = Query(default="", max_length=200),
) -> dict:
    return hub.list_records(
        _tenant(tenant_id), dataset, page=page, page_size=page_size, search=search,
    ).model_dump(mode="json")


@router.post("/{dataset}/records")
def create_record(dataset: hub.DatasetKind, body: MutationRequest) -> dict:
    try:
        return hub.create_record(
            _tenant(body.tenant_id), dataset, body.payload, actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{dataset}/records/{natural_key}")
def update_record(
    dataset: hub.DatasetKind, natural_key: str, body: MutationRequest,
) -> dict:
    try:
        return hub.update_record(
            _tenant(body.tenant_id), dataset, natural_key, body.payload, actor=body.actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Record not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{dataset}/records/{natural_key}")
def delete_record(
    dataset: hub.DatasetKind,
    natural_key: str,
    tenant_id: str | None = None,
    actor: str = Query(default="local_operator", min_length=1, max_length=120),
) -> dict:
    try:
        return hub.delete_record(_tenant(tenant_id), dataset, natural_key, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Record not found.")


def _tenant(value: str | None) -> str:
    try:
        return tenants.resolve_tenant_context(value)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
