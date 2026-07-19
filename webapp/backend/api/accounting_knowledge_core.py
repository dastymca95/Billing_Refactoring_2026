"""Tenant-authenticated Accounting Knowledge Core endpoints."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..services import batch_store, human_adjudication, tenant_accounting_policies
from ..services.accounting_knowledge_core import AccountingKnowledgeCore


router = APIRouter(prefix="/api/knowledge-core", tags=["accounting-knowledge-core"])
_CORE = AccountingKnowledgeCore()


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    edits: dict[int, dict[str, Any]]
    add_to_benchmark: bool = False
    approve_learning_example: bool = False
    propose_reusable_rule: bool = False


@router.get("/batches/{batch_id}/lines/{row_index}")
def line_context(batch_id: str, row_index: int) -> dict[str, Any]:
    actor = _actor()
    result = _load_result(batch_id)
    _assert_result_tenant(result, actor.tenant_id)
    rows = _rows(result)
    if row_index < 0 or row_index >= len(rows):
        raise HTTPException(status_code=404, detail=f"Row {row_index} not found.")
    try:
        return _CORE.line_context(tenant_id=actor.tenant_id, row=rows[row_index]).model_dump(mode="json")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/impact")
def impact(body: ImpactRequest) -> dict[str, Any]:
    actor = _actor()
    result = _load_result(body.batch_id)
    _assert_result_tenant(result, actor.tenant_id)
    rows = _rows(result)
    try:
        return _CORE.impact(
            tenant_id=actor.tenant_id, rows=rows, edits_by_index=body.edits,
            add_to_benchmark=body.add_to_benchmark,
            approve_learning=body.approve_learning_example,
            propose_rule=body.propose_reusable_rule,
        ).model_dump(mode="json")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/analytics")
def analytics() -> dict[str, Any]:
    actor = _actor()
    return _CORE.analytics(actor.tenant_id).model_dump(mode="json")


def _actor() -> human_adjudication.ActorContext:
    try:
        return human_adjudication.runtime_actor_context()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


def _load_result(batch_id: str) -> dict[str, Any]:
    try:
        path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No processed invoice preview is available.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Processed invoice cache is unreadable: {type(exc).__name__}")


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for invoice in result.get("all_invoices") or []
            for row in invoice.get("rows") or [] if isinstance(row, dict)]


def _assert_result_tenant(result: dict[str, Any], tenant_id: str) -> None:
    declared = str(result.get("tenant_id") or "").strip()
    if declared and declared != tenant_id:
        raise HTTPException(status_code=403, detail="Processed batch belongs to another tenant.")
    rows = _rows(result)
    if rows and any(
        tenant_accounting_policies.tenant_id_for_row(row) != tenant_id for row in rows
    ):
        raise HTTPException(status_code=403, detail="Processed batch belongs to another tenant.")
    if not rows and not declared:
        raise HTTPException(status_code=403, detail="Processed batch tenant cannot be verified.")
