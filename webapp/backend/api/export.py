"""Export / download endpoints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services import accounting_readiness, batch_store, batch_processor


router = APIRouter(prefix="/api/batches", tags=["export"])


# Phase 2D — same illegal-character class used by the PATCH metadata
# sanitizer in api/batches.py. Kept local to keep this module
# self-contained; the canonical sanitizer still lives in batches.py and
# is what's persisted to disk. This second pass is defensive: even if a
# batch's metadata was hand-edited or written by an older version, the
# download endpoint refuses to emit an unsafe Content-Disposition.
_EXPORT_NAME_ILLEGAL = re.compile(r'[\\/:\*\?"<>\|]+')


def _resolve_display_filename(batch_id: str, batch_dir: Path) -> str:
    """Return the operator-visible download filename for this batch.

    Resolution order:
      1. `batch_metadata.json::export_name` if present (already
         sanitized when it was saved).
      2. `batch_metadata.json::batch_name` slugged into
         `<batch_name>_ResMan_Import.xlsx`.
      3. Last-resort default `ResMan_Import.xlsx`.

    The result is always a single .xlsx basename — never a path, never
    contains illegal characters, never empty.
    """
    meta_path = batch_dir / "batch_metadata.json"
    raw_export_name = ""
    raw_batch_name = ""
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            raw_export_name = str(data.get("export_name") or "").strip()
            raw_batch_name = str(data.get("batch_name") or "").strip()
        except (OSError, ValueError):
            pass

    candidate = raw_export_name or _slug_for_default(raw_batch_name)
    return _sanitize(candidate)


def _slug_for_default(batch_name: str) -> str:
    base = batch_name.strip() or "ResMan_Import"
    # Spaces -> underscores so the default looks like a workbook filename
    # rather than a folder, but only for the default; the operator's
    # explicit export_name preserves their spaces.
    base = re.sub(r"\s+", "_", base)
    return f"{base}_ResMan_Import.xlsx"


def _sanitize(value: str) -> str:
    s = value.strip()
    if not s:
        return "ResMan_Import.xlsx"
    s = Path(s).name  # strip path components
    s = _EXPORT_NAME_ILLEGAL.sub("_", s)
    s = s.strip(". ")
    if not s:
        return "ResMan_Import.xlsx"
    if len(s) > 120:
        s = s[:120].rstrip(". ")
    stem = Path(s).stem or s
    return f"{stem}.xlsx"


class ExportRequest(BaseModel):
    """Optional JSON body for the export endpoint.

    When the operator hasn't edited anything in the browser, the frontend
    POSTs an empty body (or no body at all) and the backend uses the most
    recent processed workbook. When the operator has edited cells in the
    preview table, the frontend sends the full edited rows and the backend
    writes a fresh workbook from `Output/Template.xlsx`.
    """
    edited_rows: Optional[list[dict[str, Any]]] = None


class ReadinessRequest(BaseModel):
    rows: Optional[list[dict[str, Any]]] = None


@router.post("/{batch_id}/readiness")
def readiness_endpoint(batch_id: str, body: ReadinessRequest | None = None) -> dict[str, Any]:
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    rows = body.rows if body and body.rows is not None else batch_processor.cached_preview_rows_for_readiness(batch_id)
    return accounting_readiness.as_dict(accounting_readiness.evaluate_and_record(batch_id, rows))


@router.post("/{batch_id}/export")
def export_endpoint(batch_id: str, body: Optional[ExportRequest] = None) -> dict:
    edited_rows = body.edited_rows if (body and body.edited_rows is not None) else None
    try:
        return batch_processor.export_batch(batch_id, edited_rows=edited_rows)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{batch_id}/download")
def download_endpoint(batch_id: str, filename: str | None = None):
    try:
        export_dir = batch_store.get_export_dir(batch_id)
        batch_dir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    # Match BOTH the legacy `<vendor>_resman_import_<TS>.xlsx` AND the new
    # `resman_import_edited_<TS>.xlsx` patterns. The `?filename=` query
    # param is still used to pick a SPECIFIC on-disk file when the
    # operator wants to download an older export; the *display* filename
    # the browser saves under always comes from batch_metadata.export_name
    # (Phase 2D — see _resolve_display_filename above).
    files = sorted(export_dir.glob("*resman_import*.xlsx"))
    if filename:
        safe_name = Path(filename).name
        if not safe_name or safe_name in (".", "..") or safe_name != filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        target = (export_dir / safe_name).resolve()
        try:
            target.relative_to(export_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Path traversal blocked")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"Export file not found: {filename}")
    else:
        if not files:
            raise HTTPException(
                status_code=404,
                detail="No export file available yet — run POST /export first.",
            )
        target = files[-1]

    # Phase 2D — Content-Disposition uses the operator-chosen export
    # name (sanitized) so the browser saves the workbook under the name
    # shown in the Template Workspace title, not the on-disk timestamp.
    display_name = _resolve_display_filename(batch_id, batch_dir)
    return FileResponse(
        target,
        filename=display_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
