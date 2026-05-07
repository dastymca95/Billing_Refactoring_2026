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
from pydantic import BaseModel, Field

from ..services import (
    batch_store,
    batch_processor,
    cancel_registry,
    learned_corrections as lc_service,
    processing_queue,
    revisions as revisions_service,
    row_normalizer,
)
from ..services.template_rules import get_template_rules
from ..services.vendor_detection import detect_vendors_for_files

# Phase 2J — Extraction Trace Overlay
try:
    from utils import extraction_trace
except Exception:  # pragma: no cover
    extraction_trace = None  # type: ignore


router = APIRouter(prefix="/api/batches", tags=["processing"])

_LOG = logging.getLogger("webapp.processing")

# In-memory tracker for background processing threads. Keyed by batch_id.
# Each entry is {"thread": Thread, "started_at": iso, "tracker": ProgressTracker}.
# The thread itself writes progress to disk; the dict is just so we can
# avoid double-starts AND so the cancel endpoint can flag a running
# tracker without going through the filesystem.
_RUNNING: dict[str, dict] = {}
_RUNNING_LOCK = threading.Lock()


def _register_running(batch_id: str, thread: threading.Thread, tracker) -> None:
    from datetime import datetime as _dt
    with _RUNNING_LOCK:
        _RUNNING[batch_id] = {
            "thread": thread,
            "started_at": _dt.now().isoformat(timespec="seconds"),
            "tracker": tracker,
        }


def _unregister_running(batch_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(batch_id, None)


def _get_running_tracker(batch_id: str):
    with _RUNNING_LOCK:
        entry = _RUNNING.get(batch_id)
        return entry.get("tracker") if entry else None


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


def _int_or_none(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _invoice_source_file(inv: dict) -> str:
    debug = inv.get("debug_info") if isinstance(inv.get("debug_info"), dict) else {}
    for key in ("source_file", "file_name", "filename"):
        value = inv.get(key) or debug.get(key)
        if value:
            return str(value)
    return ""


def _invoice_source_page(inv: dict) -> int | None:
    debug = inv.get("debug_info") if isinstance(inv.get("debug_info"), dict) else {}
    for key in ("source_page", "source_page_number", "pdf_page_number", "page_number"):
        page = _int_or_none(inv.get(key) or debug.get(key))
        if page is not None:
            return page
    return None


def _preview_rows_with_navigation(result: dict, columns: list[str]) -> list[dict]:
    """Flatten invoice rows and attach stable document-navigation metadata.

    Vendor processors already return invoice-level `source_file` for the web
    cache. Some processors also carry page information in debug metadata; when
    that is absent in older cached results, we provide a deterministic backend
    fallback: one invoice maps to page 1, multiple invoices from the same PDF
    map by invoice order within that source file. This keeps existing batches
    navigable without changing vendor extraction or export columns.
    """
    invoices = list(result.get("all_invoices", []) or [])
    source_counts: dict[str, int] = {}
    for inv in invoices:
        source = _invoice_source_file(inv)
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1

    source_seen: dict[str, int] = {}
    out: list[dict] = []
    for invoice_index, inv in enumerate(invoices):
        raw_rows = list(inv.get("rows", []) or [])
        source_file = _invoice_source_file(inv)
        explicit_page = _invoice_source_page(inv)
        if source_file:
            source_seen[source_file] = source_seen.get(source_file, 0) + 1
        fallback_page = (
            source_seen.get(source_file, 1)
            if source_file and source_counts.get(source_file, 0) > 1
            else 1
        )
        source_page = explicit_page or fallback_page
        invoice_number = str(inv.get("invoice_number") or "").strip()
        invoice_group_id = "::".join(
            [
                source_file or "unknown-file",
                f"page-{source_page}",
                invoice_number or f"invoice-{invoice_index + 1}",
            ]
        )

        for invoice_row_index, row in enumerate(raw_rows):
            padded = _pad_row_to_template(row, columns)
            meta = padded.get("_meta") if isinstance(padded.get("_meta"), dict) else {}
            meta = dict(meta)
            meta.setdefault("source_file", source_file or None)
            meta.setdefault("source_page", source_page)
            meta.setdefault("invoice_group_id", invoice_group_id)
            meta.setdefault("invoice_number", invoice_number or None)
            meta["invoice_index"] = invoice_index
            meta["invoice_row_index"] = invoice_row_index
            meta["row_index"] = len(out)
            padded["_meta"] = meta
            padded["_row_index"] = len(out)
            out.append(padded)
    return out


def _read_export_name_from_metadata(batch_id: str) -> str | None:
    try:
        meta_path = batch_store.get_batch_dir(batch_id) / "batch_metadata.json"
        if not meta_path.is_file():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        v = data.get("export_name")
        return v if isinstance(v, str) and v.strip() else None
    except Exception:
        return None


def _apply_learned_corrections_to_result(result: dict) -> int:
    """Phase 2K — apply persisted learned corrections to a fresh
    process_batch result before it gets cached.

    For each vendor that produced rows, we walk the corrections
    sidecar and rewrite cell values in place. Region-remap corrections
    are NOT applied here — they need word boxes from the PDF and must
    be consumed inside the vendor processor itself; the loader on the
    HWEA side reads them directly. This step is purely the
    ``value_override`` layer.

    ``all_invoices`` and ``by_vendor.<key>.invoices`` are separate
    lists (the processor builds them as parallel copies) so we apply
    overrides to BOTH. Vendor-key derivation for ``all_invoices``
    falls back to the single-vendor case.
    """
    total = 0
    try:
        by_vendor = result.get("by_vendor") or {}
        # Apply per-vendor stash.
        for vendor_key, payload in by_vendor.items():
            total += _apply_to_invoices(
                (payload or {}).get("invoices") or [], vendor_key
            )
        # Apply top-level stash too. When there's exactly one vendor
        # we know the key; otherwise we'd need to derive it per-row,
        # which is more involved. Single-vendor batches are the
        # common case today.
        all_inv = result.get("all_invoices") or []
        if all_inv and len(by_vendor) == 1:
            only_vendor_key = next(iter(by_vendor.keys()))
            total += _apply_to_invoices(all_inv, only_vendor_key)
    except Exception:  # pragma: no cover — never poison a real run
        _LOG.exception("learned corrections application failed")
    return total


def _apply_to_invoices(invs: list[dict], vendor_key: str) -> int:
    flat_rows: list[dict] = []
    acct_lookup: dict[int, str] = {}
    r_idx = 0
    for inv in invs:
        acct = (inv or {}).get("account_number") or ""
        for r in (inv or {}).get("rows") or []:
            flat_rows.append(r)
            if acct:
                acct_lookup[r_idx] = acct
            r_idx += 1
    return lc_service.apply_value_overrides_to_rows(
        flat_rows, vendor_key, inv_account_lookup=acct_lookup,
    )


def _record_revision_for_result(batch_id: str, result: dict) -> None:
    """Phase 2D — every successful run produces a frozen snapshot in
    `revisions/<rev_id>.json` and a manifest entry. Best-effort: a
    failure to record the revision must never poison the live preview
    cache the operator already has."""
    try:
        revisions_service.record_revision(
            batch_id,
            result=result,
            export_name=_read_export_name_from_metadata(batch_id),
            status="completed",
        )
    except Exception:  # pragma: no cover - belt-and-braces
        _LOG.exception("Could not record revision for %s", batch_id)


def _was_cancelled(batch_id: str) -> bool:
    """Phase 2E — decide if a run finished cancelled.

    Two signals, in order:
      1. The cancel_registry flag set by the /cancel endpoint. This is
         the authoritative source — the operator clicked Stop, the
         vendor processor saw the flag and broke out of its loops.
      2. The progress.json snapshot's last status (``cancelled`` or
         ``cancelling``). Belt-and-braces in case the registry was
         already cleaned up by the time we check.
    """
    if cancel_registry.is_cancel_requested(batch_id):
        return True
    try:
        progress_path = batch_store.get_batch_dir(batch_id) / "progress.json"
        if not progress_path.is_file():
            return False
        snap = json.loads(progress_path.read_text(encoding="utf-8")) or {}
        status = (snap.get("status") or "").lower()
        return status in ("cancelled", "cancelling")
    except Exception:
        return False


def _stamp_cancelled(batch_id: str) -> None:
    """Phase 2E — make absolutely sure progress.json reads
    ``status=cancelled`` so the frontend's poller sees a terminal state.
    Called after the worker returns when we've decided the run was
    cancelled. Uses the tracker's ``cancelled()`` API (preferred, marks
    stages as skipped) and falls back to a raw JSON write."""
    try:
        from utils.progress_tracker import ProgressTracker
        progress_path = batch_store.get_batch_dir(batch_id) / "progress.json"
        t = ProgressTracker(progress_path, batch_id=batch_id)
        t.cancelled()
        return
    except Exception:
        pass
    try:
        progress_path = batch_store.get_batch_dir(batch_id) / "progress.json"
        snap: dict = {}
        if progress_path.is_file():
            snap = json.loads(progress_path.read_text(encoding="utf-8")) or {}
        snap["batch_id"] = batch_id
        snap["status"] = "cancelled"
        snap["percent"] = 100.0
        snap["current_step"] = "Processing cancelled"
        progress_path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run_batch_in_background(batch_id: str) -> None:
    """Worker that runs `batch_processor.process_batch` and writes the
    cache JSON. Errors are logged + flushed to progress.json as
    `status=failed`. Always cleans up the running registry on exit.

    Phase 2E — when the run is cancelled mid-flight we DO NOT:
      * record a revision (operator's history must not contain partials)
      * overwrite the existing _webapp_result.json cache (so any prior
        successful preview stays browsable until a fresh run completes).
    Whatever the vendor processor managed to produce before the cancel
    checkpoint is logged but not persisted as a workspace state.
    """
    try:
        if extraction_trace is not None:
            extraction_trace.start_batch(batch_id)
        try:
            result = batch_processor.process_batch(batch_id)
        finally:
            # Persist whatever traces were captured, even on failure —
            # partial traces are useful when triaging a bad run.
            if extraction_trace is not None:
                try:
                    extraction_trace.flush_batch(
                        batch_id,
                        batch_store.get_batch_dir(batch_id) / "trace",
                    )
                finally:
                    extraction_trace.end_batch(batch_id)
                    extraction_trace.clear_batch(batch_id)
        if _was_cancelled(batch_id):
            _LOG.info(
                "Batch %s was cancelled; skipping cache + revision write.",
                batch_id,
            )
            _stamp_cancelled(batch_id)
            return
        # Phase 2L — cross-vendor row normalisation: canonical Vendor
        # name from Vendor List.csv, sentence-case descriptions, and
        # dates parsed into ISO strings (the workbook writer turns
        # those into real Excel date cells). Run BEFORE the learned
        # corrections layer so an operator-saved override always wins
        # over the defaults.
        try:
            row_normalizer.normalize_result(result)
        except Exception:  # pragma: no cover
            _LOG.exception("row normalizer failed")
        # Phase 2K — apply learned-correction value overrides BEFORE
        # writing the cache so the preview reflects the user's curated
        # state on first paint (no flicker, no separate refresh).
        _apply_learned_corrections_to_result(result)
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
        _record_revision_for_result(batch_id, result)
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
        if extraction_trace is not None:
            extraction_trace.start_batch(batch_id)
        try:
            try:
                result = batch_processor.process_batch(batch_id)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
        finally:
            if extraction_trace is not None:
                try:
                    extraction_trace.flush_batch(
                        batch_id,
                        batch_store.get_batch_dir(batch_id) / "trace",
                    )
                finally:
                    extraction_trace.end_batch(batch_id)
                    extraction_trace.clear_batch(batch_id)
        # Phase 2L — cross-vendor row normalisation: canonical Vendor
        # name from Vendor List.csv, sentence-case descriptions, and
        # dates parsed into ISO strings (the workbook writer turns
        # those into real Excel date cells). Run BEFORE the learned
        # corrections layer so an operator-saved override always wins
        # over the defaults.
        try:
            row_normalizer.normalize_result(result)
        except Exception:  # pragma: no cover
            _LOG.exception("row normalizer failed")
        # Phase 2K — apply learned-correction value overrides BEFORE
        # writing the cache so the preview reflects the user's curated
        # state on first paint (no flicker, no separate refresh).
        _apply_learned_corrections_to_result(result)
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
        # Phase 2D — sync runs (used by tests + CLI-style smokes) also
        # produce a revision so the UI sees them.
        _record_revision_for_result(batch_id, result)
        return result

    # Phase 2D — submit to the cross-batch queue. Only one batch processes
    # at a time globally; subsequent submissions enter a FIFO queue and
    # start automatically when the running batch finishes. The legacy
    # per-batch `_RUNNING` dict is kept for compatibility (cancel paths
    # and tests still touch it), but the queue is the source of truth.
    submission = processing_queue.submit(
        batch_id, runner=_run_batch_in_background
    )
    queue_state = processing_queue.state_for(batch_id)
    return {
        "batch_id": batch_id,
        "status": "accepted",
        "queue": {
            "state": queue_state["state"],
            "position": queue_state["position"],
            "running": submission.get("running"),
            "queued": submission.get("queued", []),
        },
        "polling_url": f"/api/batches/{batch_id}/progress",
    }


@router.get("/{batch_id}/preview")
def preview_endpoint(batch_id: str) -> dict:
    try:
        cache_path = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No preview available — run POST /process first.",
        )
    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    rules = get_template_rules()
    columns = rules["columns"]
    rows = _preview_rows_with_navigation(result, columns)

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


@router.post("/{batch_id}/cancel")
def cancel_endpoint(batch_id: str) -> dict:
    """Phase 1N — request cooperative cancellation of an active batch
    processing run. Phase 2D — also drops the batch from the queue if
    it hasn't started yet.

    Returns:
      * 200 + `{status: "cancelling"}` if a tracker is registered (the
        worker thread will stop at its next checkpoint and call
        `tracker.cancelled()` to finalise).
      * 200 + `{status: "removed_from_queue"}` if the batch was queued
        but had not started yet.
      * 200 + `{status: "no_active_run"}` if neither applies.
    """
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    # Phase 2D — first try the queue (covers the still-queued case);
    # then fall through to the legacy in-process tracker flag.
    q_result = processing_queue.cancel(batch_id)
    if q_result.get("result") == "removed_from_queue":
        return {
            "batch_id": batch_id,
            "status": "removed_from_queue",
            "message": "Removed from the processing queue.",
        }
    if q_result.get("result") == "cancel_requested":
        return {
            "batch_id": batch_id,
            "status": "cancelling",
            "message": "Cancellation requested. Processing will stop at the next safe checkpoint.",
        }

    flagged = cancel_registry.request_cancel(batch_id)
    if not flagged:
        return {
            "batch_id": batch_id,
            "status": "no_active_run",
            "message": "No active processing thread for this batch.",
        }
    return {
        "batch_id": batch_id,
        "status": "cancelling",
        "message": "Cancellation requested. Processing will stop at the next safe checkpoint.",
    }


@router.get("/{batch_id}/manual-review")
def manual_review_endpoint(batch_id: str) -> dict:
    try:
        cache_path = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No manual-review data available — run POST /process first.",
        )
    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    return {"batch_id": batch_id, "items": result.get("all_manual_review", [])}


# ---------------------------------------------------------------------------
# Phase 2D — revisions
# ---------------------------------------------------------------------------


@router.get("/{batch_id}/revisions")
def list_revisions_endpoint(batch_id: str) -> dict:
    """List the per-batch template revision history (newest first)."""
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    items = revisions_service.list_revisions(batch_id)
    return {
        "batch_id": batch_id,
        "current_revision_id": revisions_service.current_revision_id(batch_id),
        "revisions": items,
    }


@router.post("/{batch_id}/revisions/{revision_id}/activate")
def activate_revision_endpoint(batch_id: str, revision_id: str) -> dict:
    """Make a previous revision the active one. Copies the snapshot
    over `_webapp_result.json` so `/preview` and `/manual-review`
    return the chosen revision's rows."""
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    try:
        entry = revisions_service.activate_revision(batch_id, revision_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "batch_id": batch_id,
        "current_revision_id": entry["revision_id"],
        "activated": entry,
    }


class SaveEditsRequest(BaseModel):
    """Body for POST /save-edits.

    `edits` is keyed by the row's index in the flattened preview list
    (the same index ResManTemplatePreview uses in its `edits` prop).
    Each value is a column → new-value map.
    """

    edits: dict[str, dict[str, object]] = Field(default_factory=dict)


@router.post("/{batch_id}/save-edits")
def save_edits_endpoint(batch_id: str, body: SaveEditsRequest) -> dict:
    """Persist operator cell edits into the active cache AND, when there
    is a current revision, into that revision's snapshot file too.

    Why both: the active cache (`_webapp_result.json`) drives the live
    preview; the revision snapshot is what gets copied back when the
    operator re-activates the revision later. Mirroring keeps both in
    lock-step so switching revisions and coming back shows the saved
    edits. Saving is idempotent and never creates a new revision —
    explicit "Process" is the only path that produces revisions.
    """
    try:
        cache_path = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No preview to edit — run Process first.",
        )

    edits = body.edits or {}
    if not edits:
        return {
            "batch_id": batch_id,
            "applied": 0,
            "skipped": 0,
            "current_revision_id": revisions_service.current_revision_id(batch_id),
        }

    # Normalize keys to ints; drop malformed entries silently.
    edits_by_idx: dict[int, dict[str, object]] = {}
    for k, v in edits.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict) and v:
            edits_by_idx[idx] = v

    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    applied = 0
    skipped = 0
    flat_index = 0
    for inv in result.get("all_invoices", []) or []:
        rows = inv.get("rows") or []
        for row in rows:
            patch = edits_by_idx.get(flat_index)
            if patch:
                for col, val in patch.items():
                    if not isinstance(col, str):
                        skipped += 1
                        continue
                    row[col] = val
                    applied += 1
            flat_index += 1

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, default=str, indent=2)

    current = revisions_service.current_revision_id(batch_id)
    if current:
        try:
            revisions_service.overwrite_snapshot(
                batch_id, current, result=result
            )
        except (FileNotFoundError, ValueError):
            # Snapshot vanished or was malformed — the active cache is
            # still saved, so the operator's changes aren't lost.
            _LOG.warning(
                "save-edits: could not mirror into snapshot %s for batch %s",
                current,
                batch_id,
            )

    return {
        "batch_id": batch_id,
        "applied": applied,
        "skipped": skipped,
        "current_revision_id": current,
    }


@router.get("/{batch_id}/documents/{filename:path}/trace")
def document_trace_endpoint(batch_id: str, filename: str) -> dict:
    """Phase 2J — return the extraction trace for one source document.

    Reads the persisted `trace/<safe_name>.json` produced at the end of
    a processing run. Returns an empty trace list (HTTP 200) when no
    trace was recorded — the frontend treats that as "feature available
    but the active vendor doesn't emit traces yet" rather than an
    error. Path traversal is prevented by sanitising `filename` the
    same way the writer does.
    """
    try:
        bdir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    safe = _safe_filename_for_trace(filename)
    trace_path = bdir / "trace" / f"{safe}.json"
    if not trace_path.is_file():
        return {
            "batch_id": batch_id,
            "source_file": filename,
            "trace_count": 0,
            "items": [],
        }
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"Trace read failed: {e}")
    return {
        "batch_id": batch_id,
        "source_file": payload.get("source_file") or filename,
        "trace_count": int(payload.get("trace_count") or 0),
        "items": payload.get("items") or [],
    }


def _safe_filename_for_trace(name: str) -> str:
    """Mirror of utils.extraction_trace._safe_filename. Kept local to
    avoid pulling the optional module into request handling at import
    time (the trace module may be absent in stripped-down deploys)."""
    import re as _re
    s = (name or "unknown").strip().replace("\\", "/").split("/")[-1]
    return _re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:200] or "unknown"


@router.delete("/{batch_id}/revisions/{revision_id}")
def delete_revision_endpoint(batch_id: str, revision_id: str) -> dict:
    """Delete a stored revision (snapshot + manifest entry).

    The active cache is left untouched — if the deleted revision was
    the newest one, the preview keeps showing it until a new run or
    activation overwrites the cache.
    """
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    try:
        deleted = revisions_service.delete_revision(batch_id, revision_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "batch_id": batch_id,
        "deleted": deleted,
        "current_revision_id": revisions_service.current_revision_id(batch_id),
    }


# ---------------------------------------------------------------------------
# Phase 2D — global queue status
# ---------------------------------------------------------------------------


# This endpoint sits under a different prefix; we expose it via a small
# separate router so the URL is `/api/processing/queue` rather than
# `/api/batches/.../queue`.
queue_router = APIRouter(prefix="/api/processing", tags=["processing-queue"])


@queue_router.get("/queue")
def queue_status_endpoint() -> dict:
    """Return the current cross-batch processing queue snapshot."""
    return processing_queue.status()
