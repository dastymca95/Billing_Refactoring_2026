"""Invoice Processor human-adjudication and governance endpoints."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..services import batch_store, human_adjudication as adjudication, tenant_accounting_policies


router = APIRouter(prefix="/api", tags=["human-adjudication"])


class DecisionRequest(BaseModel):
    approve: bool = True


@router.get("/human-adjudication/context")
def context() -> dict[str, Any]:
    try:
        actor = adjudication.runtime_actor_context()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {
        "contract_version": adjudication.ADJUDICATION_CONTRACT_VERSION,
        "reviewer_id": actor.reviewer_id,
        "role": actor.role.value,
        "tenant_id": actor.tenant_id,
        "permissions": {
            "invoice_correction": _allowed(actor, adjudication.AuthorizationScope.INVOICE_CORRECTION),
            "benchmark_submission": _allowed(actor, adjudication.AuthorizationScope.BENCHMARK_SUBMISSION),
            "learning_approval": _allowed(actor, adjudication.AuthorizationScope.LEARNING_APPROVAL),
            "rule_proposal": _allowed(actor, adjudication.AuthorizationScope.RULE_PROPOSAL),
            "rule_approval": _allowed(actor, adjudication.AuthorizationScope.RULE_APPROVAL),
            "shared_knowledge_promotion": _allowed(
                actor, adjudication.AuthorizationScope.SHARED_KNOWLEDGE_PROMOTION,
            ),
        },
    }


@router.get("/batches/{batch_id}/adjudications")
def list_batch_adjudications(
    batch_id: str, invoice_group_id: str | None = None,
) -> dict[str, Any]:
    actor = _actor()
    items = adjudication.list_revisions(
        actor.tenant_id, batch_id=batch_id, invoice_group_id=invoice_group_id,
    )
    events = adjudication.list_governance_events(actor.tenant_id)
    revision_ids = {item.revision_id for item in items}
    return {
        "contract_version": adjudication.ADJUDICATION_CONTRACT_VERSION,
        "tenant_id": actor.tenant_id,
        "items": [item.model_dump(mode="json") for item in items],
        "governance_events": [item.model_dump(mode="json") for item in events
                              if item.revision_id in revision_ids],
    }


@router.post("/human-adjudication/revisions/{revision_id}/benchmark/decision")
def decide_benchmark(revision_id: str, body: DecisionRequest) -> dict[str, Any]:
    try:
        actor = _actor()
        event = adjudication.decide_benchmark(
            revision_id, approve=body.approve, actor=actor,
        )
        _record_governance_activity(event, actor)
        return event.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Adjudication revision not found.")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/human-adjudication/revisions/{revision_id}/learning/approve")
def approve_learning(revision_id: str) -> dict[str, Any]:
    try:
        actor = _actor()
        event = adjudication.approve_learning(revision_id, actor=actor)
        _record_governance_activity(event, actor)
        return event.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Adjudication revision not found.")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/human-adjudication/revisions/{revision_id}/rule/approve")
def approve_rule(revision_id: str) -> dict[str, Any]:
    try:
        actor = _actor()
        event = adjudication.approve_rule(revision_id, actor=actor)
        _record_governance_activity(event, actor)
        return event.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Adjudication revision not found.")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/batches/{batch_id}/adjudications/evidence/{row_index}/{field}/crop")
def evidence_crop(batch_id: str, row_index: int, field: str) -> Response:
    """Render only the persisted source bbox; never expose filesystem paths."""
    actor = _actor()
    result = _load_result(batch_id)
    row = _row_at(result, row_index)
    if tenant_accounting_policies.tenant_id_for_row(row) != actor.tenant_id:
        raise HTTPException(status_code=403, detail="Evidence cannot cross tenant boundaries.")
    evidence = adjudication.source_evidence_for_cell(
        batch_id=batch_id, row=row, field=field,
    )
    if not evidence.source_file or not evidence.bounding_boxes or not evidence.page:
        raise HTTPException(status_code=404, detail="No evidence crop is available for this cell.")
    source = batch_store.get_input_dir(batch_id) / Path(evidence.source_file).name
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Source document is unavailable.")
    try:
        content = _render_crop(source, evidence.page, evidence.bounding_boxes)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"Evidence crop could not be rendered: {type(exc).__name__}")
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _actor() -> adjudication.ActorContext:
    try:
        return adjudication.runtime_actor_context()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


def _allowed(actor: adjudication.ActorContext, scope: adjudication.AuthorizationScope) -> bool:
    try:
        actor.require(scope)
        return True
    except PermissionError:
        return False


def _record_governance_activity(
    event: adjudication.GovernanceEvent, actor: adjudication.ActorContext,
) -> None:
    revision = next(
        (item for item in adjudication.list_revisions(actor.tenant_id)
         if item.revision_id == event.revision_id),
        None,
    )
    if revision is None:
        return
    from ..services import operator_activity_log

    operator_activity_log.record(
        batch_id=revision.batch_id,
        invoice_group_id=revision.invoice_group_id,
        event_type=event.event_type,
        source="rule" if event.event_type.startswith("rule_") else "manual",
        actor=actor.reviewer_id,
        summary=f"{event.event_type.replace('_', ' ').capitalize()}: {revision.field}.",
        details={
            "adjudication_revision_id": revision.revision_id,
            "status": event.status,
            "tenant_id": actor.tenant_id,
        },
    )
    _refresh_batch_overlay(revision.batch_id, actor.tenant_id)


def _refresh_batch_overlay(batch_id: str, tenant_id: str) -> None:
    """Make governance badges visible without re-running extraction."""
    try:
        result = _load_result(batch_id)
    except HTTPException:
        return
    adjudication.apply_to_result(result, batch_id=batch_id, tenant_id=tenant_id)
    path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    temp.replace(path)
    from ..services import revisions as revisions_service

    current = revisions_service.current_revision_id(batch_id)
    if current:
        try:
            revisions_service.overwrite_snapshot(batch_id, current, result=result)
        except (FileNotFoundError, ValueError):
            pass


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


def _row_at(result: dict[str, Any], row_index: int) -> dict[str, Any]:
    current = 0
    for invoice in result.get("all_invoices") or []:
        for row in invoice.get("rows") or []:
            if current == row_index:
                return row
            current += 1
    raise HTTPException(status_code=404, detail=f"Row {row_index} not found.")


def _render_crop(
    source: Path, page_number: int, boxes: list[adjudication.EvidenceBox],
) -> bytes:
    left = max(0.0, min(box.x for box in boxes) - 0.025)
    top = max(0.0, min(box.y for box in boxes) - 0.025)
    right = min(1.0, max(box.x + box.w for box in boxes) + 0.025)
    bottom = min(1.0, max(box.y + box.h for box in boxes) + 0.025)
    if right <= left or bottom <= top:
        raise ValueError("invalid evidence bounds")
    if source.suffix.casefold() == ".pdf":
        import fitz

        with fitz.open(source) as document:
            if page_number < 1 or page_number > len(document):
                raise ValueError("source page is outside the document")
            page = document[page_number - 1]
            rect = page.rect
            clip = fitz.Rect(
                rect.x0 + left * rect.width,
                rect.y0 + top * rect.height,
                rect.x0 + right * rect.width,
                rect.y0 + bottom * rect.height,
            )
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=clip, alpha=False)
            return pixmap.tobytes("png")
    from PIL import Image

    with Image.open(source) as image:
        width, height = image.size
        cropped = image.crop((int(left * width), int(top * height),
                              int(right * width), int(bottom * height)))
        output = io.BytesIO()
        cropped.convert("RGB").save(output, format="PNG")
        return output.getvalue()


__all__ = ["router"]
