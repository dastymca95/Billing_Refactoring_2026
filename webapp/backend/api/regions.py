"""Region hints endpoints — Phase 1H foundation.

Stores user-drawn rectangles per file inside a batch. Coordinates are
**normalized to [0, 1]** so the same hint applies regardless of the zoom
level / DPI / screen size at which the operator drew it.

Storage: `webapp_data/batches/<batch_id>/region_hints.json`. The file is
created lazily on first PUT; missing file = empty list.

The endpoints are deliberately small and CRUD-shaped so the frontend
PdfWorkspace can save changes per region (POST/DELETE) or replace the
whole list (PUT) without a complex protocol. Vendor processors consume
this file via `run_context["region_hints"]` (see batch_processor.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ..services import batch_store


router = APIRouter(prefix="/api/batches", tags=["regions"])


# Allowed region label values; mirrored on the frontend in
# `pdf_workspace/types.ts`.
REGION_LABELS = (
    "service_address",
    "account_number",
    "invoice_date",
    "due_date",
    "total_amount",
    "line_items",
    "notice_block",
    "ignore_zone",
    "custom",
)
REGION_SOURCES = ("user", "ai", "rules")


class BBox(BaseModel):
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    w: float = Field(..., gt=0.0, le=1.0)
    h: float = Field(..., gt=0.0, le=1.0)


class Region(BaseModel):
    id: str
    file_id: str
    page_number: int = Field(..., ge=1)
    bbox: BBox
    label: str
    color: Optional[str] = None
    notes: Optional[str] = None
    source: str = "user"
    confidence: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ReplaceRegionsBody(BaseModel):
    regions: list[Region]


def _regions_path(batch_id: str) -> Path:
    bdir = batch_store.get_batch_dir(batch_id)
    return bdir / "region_hints.json"


def _read_regions(batch_id: str) -> dict:
    p = _regions_path(batch_id)
    if not p.is_file():
        return {"schema_version": 1, "regions": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "regions": []}
    data.setdefault("schema_version", 1)
    data.setdefault("regions", [])
    return data


def _write_regions(batch_id: str, regions: list[dict]) -> dict:
    p = _regions_path(batch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "regions": regions,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _validate_label(label: str) -> str:
    v = (label or "").strip().lower()
    if v not in REGION_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"label must be one of {list(REGION_LABELS)}; got {label!r}",
        )
    return v


def _validate_source(source: str) -> str:
    v = (source or "user").strip().lower()
    if v not in REGION_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of {list(REGION_SOURCES)}; got {source!r}",
        )
    return v


@router.get("/{batch_id}/regions")
def list_regions(batch_id: str) -> dict:
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    return _read_regions(batch_id)


@router.put("/{batch_id}/regions")
def replace_regions(batch_id: str, body: ReplaceRegionsBody) -> dict:
    """Replace the whole region list for the batch. Used on initial save
    after a workspace session."""
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    cleaned = []
    for r in body.regions:
        rd = r.model_dump()
        rd["label"] = _validate_label(rd.get("label", ""))
        rd["source"] = _validate_source(rd.get("source") or "user")
        rd["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if not rd.get("created_at"):
            rd["created_at"] = rd["updated_at"]
        cleaned.append(rd)
    return _write_regions(batch_id, cleaned)


@router.post("/{batch_id}/regions")
def add_region(batch_id: str, body: Region = Body(...)) -> dict:
    """Append a single region. The frontend may also use PUT for bulk
    replacement; POST is convenient when adding one rectangle at a time."""
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    data = _read_regions(batch_id)
    rd = body.model_dump()
    rd["label"] = _validate_label(rd.get("label", ""))
    rd["source"] = _validate_source(rd.get("source") or "user")
    now = datetime.now().isoformat(timespec="seconds")
    rd.setdefault("created_at", now)
    rd["updated_at"] = now
    # Replace any prior region with the same id; otherwise append.
    regions = [r for r in (data.get("regions") or []) if r.get("id") != rd["id"]]
    regions.append(rd)
    return _write_regions(batch_id, regions)


@router.delete("/{batch_id}/regions/{region_id}")
def delete_region(batch_id: str, region_id: str) -> dict:
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    data = _read_regions(batch_id)
    before = len(data.get("regions") or [])
    regions = [r for r in (data.get("regions") or []) if r.get("id") != region_id]
    after = len(regions)
    payload = _write_regions(batch_id, regions)
    return {"deleted": before - after, **payload}
