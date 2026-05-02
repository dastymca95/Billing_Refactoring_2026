"""Export / download endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services import batch_store, batch_processor


router = APIRouter(prefix="/api/batches", tags=["export"])


class ExportRequest(BaseModel):
    """Optional JSON body for the export endpoint.

    When the operator hasn't edited anything in the browser, the frontend
    POSTs an empty body (or no body at all) and the backend uses the most
    recent processed workbook. When the operator has edited cells in the
    preview table, the frontend sends the full edited rows and the backend
    writes a fresh workbook from `Output/Template.xlsx`.
    """
    edited_rows: Optional[list[dict[str, Any]]] = None


@router.post("/{batch_id}/export")
def export_endpoint(batch_id: str, body: Optional[ExportRequest] = None) -> dict:
    edited_rows = body.edited_rows if (body and body.edited_rows is not None) else None
    try:
        return batch_processor.export_batch(batch_id, edited_rows=edited_rows)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{batch_id}/download")
def download_endpoint(batch_id: str, filename: str | None = None):
    try:
        export_dir = batch_store.get_export_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    # Match BOTH the legacy `<vendor>_resman_import_<TS>.xlsx` AND the new
    # `resman_import_edited_<TS>.xlsx` patterns.
    files = sorted(export_dir.glob("*resman_import*.xlsx"))
    if filename:
        target = export_dir / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"Export file not found: {filename}")
    else:
        if not files:
            raise HTTPException(
                status_code=404,
                detail="No export file available yet — run POST /export first.",
            )
        target = files[-1]
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
