"""Process / preview / manual-review endpoints.

The actual run happens in a **background thread** so the HTTP request
returns 202 Accepted immediately. Vendor processors stream progress to
`webapp_data/batches/<id>/progress.json`; the frontend polls
`GET /api/batches/<id>/progress` for live updates. The full processing
result is cached at `processed/_webapp_result.json` and read by GET
preview / manual-review endpoints.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..services import batch_store, batch_processor
from ..services.template_rules import get_template_rules
from ..services.vendor_detection import detect_vendors_for_files


router = APIRouter(prefix="/api/batches", tags=["processing"])

_LOG = logging.getLogger("webapp.processing")

# In-memory tracker for background processing threads. Keyed by batch_id.
# Each entry is {"thread": Thread, "started_at": iso}. The thread itself
# writes progress to disk; the dict is just so we can avoid double-starts.
_RUNNING: dict[str, dict] = {}
_RUNNING_LOCK = threading.Lock()


def _result_cache_path(batch_id: str) -> Path:
    return batch_store.get_processed_dir(batch_id) / "_webapp_result.json"


def _pad_row_to_template(row: dict, columns: list[str]) -> dict:
    """Ensure every template column appears in the row dict, with `None`
    for any column the vendor processor didn't populate. Preserves the
    `_meta` key (and any other underscore-prefixed key) untouched."""
    padded = {}
    for c in columns:
        padded[c] = row.get(c, None)
    for k, v in row.items():
        if k.startswith("_"):
            padded[k] = v
    return padded


def _run_batch_in_background(batch_id: str) -> None:
    """Worker that runs `batch_processor.process_batch` and writes the
    cache JSON. Errors are logged + flushed to progress.json as
    `status=failed`. Always cleans up the running registry on exit."""
    try:
        result = batch_processor.process_batch(batch_id)
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
    except Exception as e:
        _LOG.exception("Background batch processing failed for %s", batch_id)
        # Best-effort: stamp progress.json with failure so the polling
        # frontend sees it.
        try:
            from utils.progress_tracker import ProgressTracker
            progress_path = batch_store.get_batch_dir(batch_id) / "progress.json"
            t = ProgressTracker(progress_path, batch_id=batch_id)
            t.fail(f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(batch_id, None)


@router.post("/{batch_id}/detect")
def detect_endpoint(batch_id: str) -> dict:
    try:
        files = batch_store.list_files_in_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    return {"batch_id": batch_id, "detection": detect_vendors_for_files(files)}


@router.post("/{batch_id}/process")
def process_endpoint(batch_id: str, sync: bool = False) -> dict:
    """Kick off batch processing.

    By default the run happens in a background thread; the response is
    `{status: "accepted", batch_id, polling_url}`. The frontend polls
    `GET /progress` to track the run. Pass `?sync=1` to force a blocking
    request — used by tests and the CLI smoke tests where we want the
    final summary back in one call.
    """
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    if sync:
        try:
            result = batch_processor.process_batch(batch_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
        return result

    with _RUNNING_LOCK:
        if batch_id in _RUNNING:
            return {
                "batch_id": batch_id,
                "status": "already_running",
                "polling_url": f"/api/batches/{batch_id}/progress",
            }
        thread = threading.Thread(
            target=_run_batch_in_background,
            args=(batch_id,),
            name=f"batch-{batch_id}",
            daemon=True,
        )
        from datetime import datetime
        _RUNNING[batch_id] = {
            "thread": thread,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        thread.start()
    return {
        "batch_id": batch_id,
        "status": "accepted",
        "polling_url": f"/api/batches/{batch_id}/progress",
    }


@router.get("/{batch_id}/preview")
def preview_endpoint(batch_id: str) -> dict:
    cache_path = _result_cache_path(batch_id)
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No preview available — run POST /process first.",
        )
    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    rules = get_template_rules()
    columns = rules["columns"]
    rows = [
        _pad_row_to_template(row, columns)
        for inv in result.get("all_invoices", [])
        for row in inv.get("rows", [])
    ]

    return {
        "batch_id": batch_id,
        "summary": result.get("summary", {}),
        "by_vendor_summaries": {
            k: v.get("summary", {}) for k, v in result.get("by_vendor", {}).items()
        },
        "columns": columns,
        "required_columns": rules["required_columns"],
        "recommended_columns": rules["recommended_columns"],
        "optional_columns": rules["optional_columns"],
        "optional_columns_collapsible": rules["optional_columns_collapsible"],
        "optional_columns_hidden_by_default": rules["optional_columns_hidden_by_default"],
        "rows": rows,
        "invoice_count": len(result.get("all_invoices", [])),
        "row_count": len(rows),
        "unsupported_files": result.get("unsupported_files", []),
    }


@router.get("/{batch_id}/manual-review")
def manual_review_endpoint(batch_id: str) -> dict:
    cache_path = _result_cache_path(batch_id)
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No manual-review data available — run POST /process first.",
        )
    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    return {"batch_id": batch_id, "items": result.get("all_manual_review", [])}
