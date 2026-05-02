"""Batch lifecycle endpoints: create (with optional name), list, status,
list-files, rename, delete. Each batch carries a small `batch_metadata.json`
sidecar in its root directory so the frontend can display human-friendly
names + summary stats without re-reading the result cache."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ..services import batch_store
from ..services.vendor_detection import detect_vendor_for_file


router = APIRouter(prefix="/api/batches", tags=["batches"])


# ---------------------------------------------------------------------------
# Phase 1H — batch document mode + AI fallback fields
# Allowed values are mirrored on the frontend (src/types.ts:DocumentMode etc).
# ---------------------------------------------------------------------------
DOCUMENT_MODES = (
    "digital_pdf", "scanned_pdf", "mixed_pdf", "csv_excel", "auto_detect",
)
AI_FALLBACK_POLICIES = (
    "never", "only_low_confidence", "only_manual_review", "always_assist",
)
DEFAULT_DOCUMENT_MODE = "auto_detect"
DEFAULT_AI_FALLBACK_POLICY = "only_low_confidence"


# ---------------------------------------------------------------------------
# Metadata sidecar (`batch_metadata.json` inside each batch dir).
# Created on every create/update; kept small so reads are fast.
# ---------------------------------------------------------------------------
def _metadata_path(batch_id: str) -> Path:
    return batch_store.get_batch_dir(batch_id) / "batch_metadata.json"


def _read_metadata(batch_id: str) -> dict:
    p = _metadata_path(batch_id)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_metadata(batch_id: str, **fields) -> dict:
    """Merge `fields` into the existing metadata and persist. Always
    bumps `updated_at` and ensures `batch_id` + `created_at` are present."""
    p = _metadata_path(batch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = _read_metadata(batch_id)
    if "batch_id" not in current:
        current["batch_id"] = batch_id
    if "created_at" not in current:
        current["created_at"] = datetime.fromtimestamp(
            batch_store.get_batch_dir(batch_id).stat().st_ctime
        ).isoformat(timespec="seconds")
    current.update({k: v for k, v in fields.items() if v is not None})
    current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def _summary_for_batch(batch_id: str) -> dict:
    """Inspect the batch folder + cached result + export folder to
    compute the live counts the metadata sidecar may be missing."""
    bdir = batch_store.get_batch_dir(batch_id)
    files = batch_store.list_files_in_batch(batch_id)

    processed_dir = batch_store.get_processed_dir(batch_id)
    cache_path = processed_dir / "_webapp_result.json"
    invoices_count = 0
    rows_count = 0
    manual_review_count = 0
    summary: dict = {}
    by_vendor: dict[str, dict] = {}
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            summary = cached.get("summary") or {}
            invs = cached.get("all_invoices") or []
            invoices_count = len(invs)
            rows_count = sum(len(inv.get("rows") or []) for inv in invs)
            manual_review_count = len(cached.get("all_manual_review") or [])
            by_vendor = {
                k: v.get("summary", {})
                for k, v in (cached.get("by_vendor") or {}).items()
            }
        except Exception:
            pass

    export_dir = batch_store.get_export_dir(batch_id)
    export_files = sorted(export_dir.glob("*resman_import*.xlsx"))
    return {
        "files_count": len(files),
        "invoices_count": invoices_count,
        "rows_count": rows_count,
        "manual_review_count": manual_review_count,
        "export_available": bool(export_files),
        "last_export_file": export_files[-1].name if export_files else None,
        "supported_vendor_summary": by_vendor,
        "summary": summary,
        "preview_available": cache_path.is_file(),
        "created_at": datetime.fromtimestamp(bdir.stat().st_ctime).isoformat(timespec="seconds"),
        "export_filenames": [p.name for p in export_files],
    }


# ---------------------------------------------------------------------------
# Pydantic schemas (request bodies)
# ---------------------------------------------------------------------------
class CreateBatchBody(BaseModel):
    batch_name: str | None = None
    # Phase 1H: optional batch-level mode + AI policy. All have safe
    # defaults so older clients (no body, or `{batch_name}` only) stay
    # byte-identical to the legacy behaviour.
    document_mode: Optional[str] = None
    ai_fallback_enabled: Optional[bool] = None
    ai_fallback_policy: Optional[str] = None


class UpdateBatchBody(BaseModel):
    batch_name: str | None = None
    document_mode: Optional[str] = None
    ai_fallback_enabled: Optional[bool] = None
    ai_fallback_policy: Optional[str] = None


def _validate_document_mode(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in DOCUMENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"document_mode must be one of {list(DOCUMENT_MODES)}; got {value!r}",
        )
    return v


def _validate_ai_policy(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in AI_FALLBACK_POLICIES:
        raise HTTPException(
            status_code=400,
            detail=f"ai_fallback_policy must be one of {list(AI_FALLBACK_POLICIES)}; got {value!r}",
        )
    return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("")
def create_batch_endpoint(body: CreateBatchBody | None = Body(default=None)) -> dict:
    """Create a new batch. Optional `batch_name` lets the operator name
    the batch up-front; defaults to `"Batch <YYYY-MM-DD HH:MM>"`.
    Phase 1H: optionally accepts `document_mode`, `ai_fallback_enabled`,
    `ai_fallback_policy`. All have safe defaults; older clients keep
    working unchanged."""
    bid = batch_store.create_batch()
    name = (body.batch_name if body else None) or \
        f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    document_mode = _validate_document_mode(
        body.document_mode if body else None
    ) or DEFAULT_DOCUMENT_MODE
    ai_policy = _validate_ai_policy(
        body.ai_fallback_policy if body else None
    ) or DEFAULT_AI_FALLBACK_POLICY
    ai_enabled = (body.ai_fallback_enabled if body else None)
    if ai_enabled is None:
        ai_enabled = True  # default; the *service* level switch decides
                            # whether AI actually fires.
    meta = _write_metadata(
        bid,
        batch_name=name,
        status="idle",
        document_mode=document_mode,
        ai_fallback_enabled=bool(ai_enabled),
        ai_fallback_policy=ai_policy,
    )
    return {"batch_id": bid, "batch_name": name, "metadata": meta}


@router.get("")
def list_batches_endpoint() -> dict:
    """List every batch on disk with its metadata + live counts. Sorted
    most-recent first by `created_at`."""
    out = []
    for entry in batch_store.list_batches():
        bid = entry["batch_id"]
        try:
            meta = _read_metadata(bid)
            live = _summary_for_batch(bid)
        except FileNotFoundError:
            continue
        out.append({
            "batch_id": bid,
            "batch_name": meta.get("batch_name") or bid,
            "created_at": live.get("created_at") or meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "status": meta.get("status") or "idle",
            "files_count": live["files_count"],
            "invoices_count": live["invoices_count"],
            "rows_count": live["rows_count"],
            "manual_review_count": live["manual_review_count"],
            "export_available": live["export_available"],
            "last_export_file": live["last_export_file"],
            "supported_vendor_summary": live["supported_vendor_summary"],
        })
    out.sort(key=lambda b: b.get("created_at") or "", reverse=True)
    return {"batches": out}


@router.patch("/{batch_id}")
def update_batch_endpoint(batch_id: str, body: UpdateBatchBody) -> dict:
    """Rename a batch (and update metadata only — does not touch the
    underlying batch_id directory)."""
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    fields: dict = {}
    if body.batch_name is not None:
        clean = body.batch_name.strip()
        if not clean:
            raise HTTPException(status_code=400, detail="batch_name cannot be empty")
        if len(clean) > 200:
            raise HTTPException(status_code=400, detail="batch_name too long (max 200)")
        fields["batch_name"] = clean
    if body.document_mode is not None:
        fields["document_mode"] = _validate_document_mode(body.document_mode)
    if body.ai_fallback_enabled is not None:
        fields["ai_fallback_enabled"] = bool(body.ai_fallback_enabled)
    if body.ai_fallback_policy is not None:
        fields["ai_fallback_policy"] = _validate_ai_policy(body.ai_fallback_policy)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    meta = _write_metadata(batch_id, **fields)
    return {"batch_id": batch_id, "metadata": meta}


@router.get("/{batch_id}")
def get_batch_endpoint(batch_id: str) -> dict:
    """Return batch metadata + live counts. Used by the frontend to
    rehydrate after a page refresh and as the source of truth for the
    batch selector. Returns 404 if the batch folder is gone."""
    try:
        bdir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    files = batch_store.list_files_in_batch(batch_id)
    file_entries = []
    for p in files:
        det = detect_vendor_for_file(p)
        file_entries.append({
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "extension": p.suffix.lower(),
            "vendor_key": det["vendor_key"],
            "vendor_confidence": det["confidence"],
            "vendor_detection_reason": det["reason"],
            "supported_in_phase_1": det["supported_in_phase_1"],
        })

    meta = _read_metadata(batch_id)
    live = _summary_for_batch(batch_id)

    return {
        "batch_id": batch_id,
        "batch_name": meta.get("batch_name") or batch_id,
        "created_at": live["created_at"],
        "updated_at": meta.get("updated_at"),
        "files": file_entries,
        "files_total": len(file_entries),
        "preview_available": live["preview_available"],
        "export_available": live["export_available"],
        "export_filenames": live["export_filenames"],
        "summary": live["summary"],
        "metadata": meta,
    }


@router.get("/{batch_id}/files")
def list_files_endpoint(batch_id: str) -> dict:
    try:
        files = batch_store.list_files_in_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    out = []
    for p in files:
        det = detect_vendor_for_file(p)
        out.append({
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "extension": p.suffix.lower(),
            "vendor_key": det["vendor_key"],
            "vendor_confidence": det["confidence"],
            "vendor_detection_reason": det["reason"],
            "supported_in_phase_1": det["supported_in_phase_1"],
        })
    return {"batch_id": batch_id, "files": out}


@router.get("/{batch_id}/progress")
def get_batch_progress_endpoint(batch_id: str) -> dict:
    """Return the on-disk progress snapshot for a batch. Frontend polls
    this endpoint while `Process Batch` is running. Returns a JSON object
    with at minimum `{batch_id, status, percent, current_step}`. If the
    batch has no progress.json yet (e.g. processing hasn't started), the
    snapshot returns `status="idle"`."""
    try:
        bdir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    progress_path = bdir / "progress.json"

    # Defensive import — if utils.progress_tracker is missing for any reason,
    # fall back to reading the JSON directly.
    try:
        from utils.progress_tracker import load_snapshot
        snap = load_snapshot(progress_path)
    except Exception:
        if progress_path.is_file():
            import json as _json
            try:
                snap = _json.loads(progress_path.read_text(encoding="utf-8"))
            except Exception:
                snap = None
        else:
            snap = None

    if snap is None:
        return {
            "batch_id": batch_id,
            "status": "idle",
            "percent": 0.0,
            "current_step": "",
            "current_file": "",
            "files_total": 0,
            "files_done": 0,
            "invoices_created": 0,
            "rows_created": 0,
            "warnings_count": 0,
            "error_message": "",
        }
    snap.setdefault("batch_id", batch_id)
    return snap


@router.delete("/{batch_id}")
def delete_batch_endpoint(batch_id: str) -> dict:
    batch_store.delete_batch(batch_id)
    return {"batch_id": batch_id, "deleted": True}
