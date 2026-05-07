"""Batch processing facade — routes each batch to the right vendor processor.

Phase 1 supports Richmond Utilities only. The facade reads the detection
results, finds files that belong to a supported vendor, calls the
processor, and returns a unified result the API layer can return as JSON.
"""

from __future__ import annotations

import importlib
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import openpyxl

from ..settings import (
    PROJECT_ROOT, RESMAN_TEMPLATE, VENDORS_DIR, batch_dir,
)
from . import batch_store
from .vendor_detection import detect_vendor_for_file


# Phase 1H — declared timeline stages. The batch processor declares
# these up-front so the frontend can render the full list as
# `pending` placeholders before any work begins. Stage keys are stable
# and used by both the processor and the frontend renderer.
_DEFAULT_STAGES: list[tuple[str, str]] = [
    ("upload", "Uploading files"),
    ("vendor_detect", "Detecting vendor"),
    ("read_pdf", "Reading PDF text"),
    ("ocr", "Running OCR"),
    ("yaml_rules", "Applying vendor YAML rules"),
    ("address_match", "Matching service address"),
    ("unit_match", "Matching Unit Info Clean"),
    ("gl_evidence", "Using General Ledger evidence"),
    ("ai_fallback", "AI fallback (if enabled)"),
    ("reconcile", "Reconciling bill totals"),
    ("split_pdf", "Splitting support PDFs"),
    ("dropbox", "Uploading to Dropbox"),
    ("template", "Building ResMan template"),
    ("ready", "Ready for review"),
]


def _read_batch_metadata(batch_id: str) -> dict:
    """Read the batch_metadata.json sidecar (the same one batches.py
    writes). Returns an empty dict if missing."""
    bdir = batch_store.get_batch_dir(batch_id)
    p = bdir / "batch_metadata.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _read_region_hints(batch_id: str) -> list[dict]:
    """Read the region_hints.json sidecar (regions.py writes it). Returns
    an empty list if missing or malformed."""
    bdir = batch_store.get_batch_dir(batch_id)
    p = bdir / "region_hints.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return list(data.get("regions") or [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Vendor → processor module + entrypoint name. As new vendors come online,
# add a loader function below + register it in _PROCESSOR_LOADERS. Each
# loader returns the processor module; the registry tells `process_batch`
# which top-level function to call inside it.
# ---------------------------------------------------------------------------
def _import_richmond_processor():
    """Import the Richmond Utilities processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Water - Sewer"
                     / "Richmond Utilities").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_richmond_utilities")


def _import_hopkinsville_processor():
    """Import the Hopkinsville Water Environment Authority processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Water - Sewer"
                     / "Hopkinsville Water Environment Authority").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_hopkinsville_water_environment_authority")


def _import_columbia_processor():
    """Import the Columbia Power and Water System processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Electricity - Power"
                     / "Columbia Power and Water System").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_columbia_power_and_water_system")


def _import_atmos_processor():
    """Import the Atmos Energy Auto Pay processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Gas"
                     / "Atmos Energy Auto Pay").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_atmos_energy_auto_pay")


def _import_hardin_processor():
    """Import the Hardin County Water District No. 2 processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Water - Sewer"
                     / "Hardin County Water District No. 2").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_hardin_county_water_district_no_2")


def _import_shelbyville_processor():
    """Import the Shelbyville Power System processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Electricity - Power"
                     / "Shelbyville Power System").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_shelbyville_power_system")


def _import_zillow_processor():
    """Import the Zillow Rentals processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Marketing - Advertising"
                     / "Zillow Rentals").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_zillow_rentals")


def _import_mcminnville_processor():
    """Import the McMinnville Electric System processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Electricity - Power"
                     / "McMinnville Electric System").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_mcminnville_electric_system")


def _import_pennyrile_processor():
    """Import the Pennyrile Electric processor module."""
    vendor_folder = (PROJECT_ROOT / "Training Bills_Invoices" / "Electricity - Power"
                     / "Pennyrile Electric").resolve()
    if str(vendor_folder) not in sys.path:
        sys.path.insert(0, str(vendor_folder))
    return importlib.import_module("process_pennyrile_electric")


# vendor_key → (loader, entrypoint_name)
_PROCESSOR_LOADERS: dict[str, tuple[Any, str]] = {
    "richmond_utilities": (_import_richmond_processor,
                           "process_richmond_utilities_batch"),
    "hopkinsville_water_environment_authority": (
        _import_hopkinsville_processor,
        "process_hopkinsville_water_environment_authority_batch",
    ),
    "columbia_power_and_water_system": (
        _import_columbia_processor,
        "process_columbia_power_and_water_system_batch",
    ),
    "atmos_energy_auto_pay": (
        _import_atmos_processor,
        "process_atmos_energy_auto_pay_batch",
    ),
    "hardin_county_water_district_no_2": (
        _import_hardin_processor,
        "process_hardin_county_water_district_no_2_batch",
    ),
    "shelbyville_power_system": (
        _import_shelbyville_processor,
        "process_shelbyville_power_system_batch",
    ),
    "zillow_rentals": (
        _import_zillow_processor,
        "process_zillow_rentals_batch",
    ),
    "mcminnville_electric_system": (
        _import_mcminnville_processor,
        "process_mcminnville_electric_system_batch",
    ),
    "pennyrile_electric": (
        _import_pennyrile_processor,
        "process_pennyrile_electric_batch",
    ),
}


# ---------------------------------------------------------------------------
# Per-batch processing entrypoint
# ---------------------------------------------------------------------------
def process_batch(
    batch_id: str,
    *,
    dry_run: bool = False,
    rules_override_paths: dict[str, "Path"] | None = None,
) -> dict[str, Any]:
    """Run the appropriate vendor processor over every file in the batch.

    Phase 2A — adds two opt-in flags for the Vendor Rules Studio's
    "Test against batch" feature:

      * ``dry_run=True``   — propagated into ``run_context["dry_run"]``.
        Vendor processors that honour the flag skip Dropbox uploads and
        ResMan workbook writes; everything else runs identically. The
        webapp's preview cache (``_webapp_result.json``) is the caller's
        responsibility — this function never writes it (the API layer
        does, and skips it for dry-run calls).
      * ``rules_override_paths``  — ``{vendor_key: Path}`` mapping. When a
        vendor has an entry, that path is used as ``config_path`` instead
        of ``config/vendors/<vendor_key>.yaml``. The processor still calls
        ``yaml.safe_load()`` exactly the way the CLI does, so the rule
        loader is unchanged. Caller is responsible for cleaning up the
        temp files.

    Returns a dict shaped roughly like:
        {
            "batch_id": ...,
            "summary": {...},
            "by_vendor": {
                "richmond_utilities": ProcessBatchResult-as-dict,
            },
            "unsupported_files": [...]
        }

    Side-effect: writes a per-batch progress snapshot at
    `webapp_data/batches/<id>/progress.json` so the frontend can poll
    `GET /api/batches/<id>/progress` while the batch runs. (The progress
    file is written even on dry-run calls; nothing else on disk changes.)
    """
    rules_override_paths = rules_override_paths or {}
    bdir = batch_store.get_batch_dir(batch_id)
    in_dir = batch_store.get_input_dir(batch_id)
    processed_dir = batch_store.get_processed_dir(batch_id)
    files = batch_store.list_files_in_batch(batch_id)

    # Phase 1H: pull batch-level mode + AI policy + region hints from disk.
    batch_meta = _read_batch_metadata(batch_id)
    document_mode = batch_meta.get("document_mode") or "auto_detect"
    ai_fallback_policy = batch_meta.get("ai_fallback_policy") or "only_low_confidence"
    ai_fallback_enabled_meta = bool(batch_meta.get("ai_fallback_enabled", True))
    region_hints = _read_region_hints(batch_id)

    # AI fallback service — disabled by default. The service is the
    # single gate that decides whether any provider call ever fires.
    ai_service = None
    try:
        from .ai_fallback import get_service
        ai_service = get_service()
    except Exception:
        ai_service = None

    # Progress tracker — defensive import. CLI doesn't use this; the
    # webapp always wires it up.
    progress_path = bdir / "progress.json"
    tracker = None
    progress_callback = None
    try:
        from utils.progress_tracker import ProgressTracker, make_callback
        tracker = ProgressTracker(progress_path, batch_id=batch_id)
        tracker.declare_stages(_DEFAULT_STAGES)
        tracker.update(status="processing", files_total=len(files),
                       current_step="Detecting vendors…", percent=1.0)
        # Phase 1N — register the tracker so the /cancel endpoint can
        # flag it without round-tripping through the filesystem.
        try:
            from . import cancel_registry
            cancel_registry.register(batch_id, tracker)
        except Exception:
            pass
        # Upload stage was actually completed before we got here (the
        # files are already on disk), so flip it green right away.
        tracker.complete_stage("upload", detail=f"{len(files)} file(s)")
        tracker.start_stage("vendor_detect", detail=f"{len(files)} file(s)")
        progress_callback = make_callback(tracker)
    except Exception:
        tracker = None
        progress_callback = None

    # Phase 1N — cooperative cancellation hook. Vendor processors that
    # accept `should_cancel_callback` poll this between files / pages.
    # CLI runs (no tracker) get a no-op that always returns False so
    # behaviour is identical to today.
    def should_cancel() -> bool:
        return tracker is not None and tracker.is_cancel_requested()

    # Group files by detected vendor.
    grouped: dict[str, list[Path]] = {}
    detection: dict[str, dict] = {}
    for f in files:
        det = detect_vendor_for_file(f)
        detection[f.name] = det
        grouped.setdefault(det["vendor_key"], []).append(f)

    if tracker is not None:
        tracker.complete_stage(
            "vendor_detect",
            detail=f"{len(grouped)} vendor(s); document_mode={document_mode}",
        )

    by_vendor: dict[str, dict[str, Any]] = {}
    unsupported: list[dict] = []
    overall_invoices: list[dict] = []
    overall_review: list[dict] = []

    if tracker is not None:
        tracker.update(current_step=f"Routing files to {len(grouped)} vendor(s)…", percent=3.0)

    for vendor_key, vfiles in grouped.items():
        # Phase 1N — bail before each vendor if cancellation was requested
        # while a previous vendor was running.
        if should_cancel():
            break
        if vendor_key not in _PROCESSOR_LOADERS:
            for f in vfiles:
                unsupported.append({
                    "filename": f.name,
                    "vendor_key": vendor_key,
                    "detection": detection.get(f.name),
                    "reason": "no_processor_for_vendor_in_phase_1",
                })
            continue

        # Per-vendor working dirs inside the batch.
        vendor_in = bdir / "input" / vendor_key
        vendor_out = processed_dir / vendor_key
        vendor_in.mkdir(parents=True, exist_ok=True)
        vendor_out.mkdir(parents=True, exist_ok=True)
        # Stage files into the per-vendor input folder so the processor sees
        # only its files. We HARDLINK / copy — never move — so the original
        # batch input folder still has every uploaded file.
        for f in vfiles:
            target = vendor_in / f.name
            if not target.exists():
                try:
                    shutil.copy2(f, target)
                except Exception as e:
                    unsupported.append({
                        "filename": f.name,
                        "vendor_key": vendor_key,
                        "reason": f"failed_to_stage_input:{type(e).__name__}",
                    })

        # Call the processor via the registry tuple (loader, entrypoint_name).
        loader, entrypoint_name = _PROCESSOR_LOADERS[vendor_key]
        mod = loader()
        process_func = getattr(mod, entrypoint_name, None)
        if process_func is None:
            unsupported.extend([
                {"filename": f.name, "vendor_key": vendor_key,
                 "reason": f"vendor_processor_function_missing:{entrypoint_name}"}
                for f in vfiles
            ])
            continue

        if tracker is not None:
            tracker.update(current_step=f"Processing {len(vfiles)} {vendor_key} file(s)…",
                           percent=5.0)

        # Phase 2A — Rules Studio impact preview: caller can pass an
        # alternate YAML path per vendor so we test draft rules without
        # touching the on-disk file.
        config_path = rules_override_paths.get(vendor_key) or (
            VENDORS_DIR / f"{vendor_key}.yaml"
        )
        # Pass progress_callback when the processor accepts it. Older
        # processors that don't will get a TypeError at call time — we
        # introspect the signature to stay backwards compatible.
        import inspect
        # Phase 1H — enrich `run_context` with batch-level mode, AI
        # policy, region hints, and a reference to the AI service.
        # Vendor processors that don't read these keys ignore them; the
        # CLI sets `run_context=None` and behaves identically to today.
        run_context: dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "source": "webapp",
            "batch_id": batch_id,
            "document_mode": document_mode,
            "ai_fallback_enabled": (
                ai_fallback_enabled_meta
                and ai_service is not None
                and ai_service.is_enabled()
            ),
            "ai_fallback_policy": ai_fallback_policy,
            "ai_fallback_service": ai_service,
            "region_hints": [
                r for r in region_hints
                if (r.get("file_id") or "") in {f.name for f in vfiles}
            ],
            # Phase 2A — vendor processors that honour this skip Dropbox
            # uploads and Excel writes. Default False keeps CLI + normal
            # webapp runs identical to today.
            "dry_run": dry_run,
        }
        kwargs: dict[str, Any] = {
            "input_folder": vendor_in,
            "output_folder": vendor_out,
            "template_path": RESMAN_TEMPLATE,
            "config_path": config_path,
            "run_context": run_context,
        }
        try:
            sig = inspect.signature(process_func)
            if "progress_callback" in sig.parameters and progress_callback is not None:
                kwargs["progress_callback"] = progress_callback
            # Phase 1N — pass should_cancel_callback when supported.
            if "should_cancel_callback" in sig.parameters:
                kwargs["should_cancel_callback"] = should_cancel
        except (TypeError, ValueError):
            pass

        # Phase 1N — also expose the cancel callable inside run_context
        # so processors that read `run_context` directly can poll it.
        run_context["should_cancel"] = should_cancel

        if tracker is not None:
            # The vendor processor is going to read text, optionally OCR,
            # apply YAML rules, match addresses, etc. — without a
            # processor-level hook we can't drive each individual stage,
            # but we can mark the broader stages as we enter / leave the
            # vendor call.
            for k in ("read_pdf", "ocr", "yaml_rules", "address_match",
                      "unit_match", "gl_evidence"):
                tracker.start_stage(k, detail=f"{vendor_key}: {len(vfiles)} file(s)")
        result = process_func(**kwargs)
        if tracker is not None:
            for k in ("read_pdf", "ocr", "yaml_rules", "address_match",
                      "unit_match", "gl_evidence"):
                tracker.complete_stage(k)
            if run_context["ai_fallback_enabled"]:
                tracker.start_stage("ai_fallback",
                                    detail=f"policy={ai_fallback_policy}")
                tracker.complete_stage("ai_fallback")
            else:
                tracker.skip_stage(
                    "ai_fallback",
                    detail="disabled or not configured",
                )
            tracker.start_stage("reconcile")
            tracker.complete_stage("reconcile")
            tracker.start_stage("split_pdf")
            tracker.complete_stage("split_pdf")
            tracker.start_stage("dropbox")
            tracker.complete_stage("dropbox")
            tracker.start_stage("template")
            tracker.complete_stage("template")
        # ProcessBatchResult is a dataclass — convert to plain dict.
        from dataclasses import asdict
        result_dict = asdict(result)
        by_vendor[vendor_key] = result_dict
        overall_invoices.extend(result.invoices)
        overall_review.extend(result.manual_review_rows)

    # Top-level summary
    summary = {
        "files_total": len(files),
        "files_supported": sum(len(v) for k, v in grouped.items() if k in _PROCESSOR_LOADERS),
        "files_unsupported": len(unsupported),
        "invoices_total": len(overall_invoices),
        "manual_review_total": len(overall_review),
    }

    if tracker is not None:
        if should_cancel():
            # Phase 1N — finalise as cancelled rather than completed.
            tracker.cancelled(
                files_total=len(files),
                files_done=sum(len(v) for k, v in grouped.items() if k in by_vendor),
                invoices_created=len(overall_invoices),
                rows_created=sum(
                    len(inv.get("rows", [])) for inv in overall_invoices
                ),
                warnings_count=len(overall_review),
            )
            summary["cancelled"] = True
        else:
            if not by_vendor:
                for k in (
                    "read_pdf",
                    "ocr",
                    "yaml_rules",
                    "address_match",
                    "unit_match",
                    "gl_evidence",
                    "ai_fallback",
                    "reconcile",
                    "split_pdf",
                    "dropbox",
                    "template",
                ):
                    tracker.skip_stage(k, detail="No supported files in batch")
            tracker.start_stage("ready")
            tracker.complete_stage(
                "ready",
                detail=f"{len(overall_invoices)} invoice(s), {len(overall_review)} flagged",
            )
            tracker.complete(
                files_total=len(files),
                files_done=len(files),
                invoices_created=len(overall_invoices),
                rows_created=sum(len(inv.get("rows", [])) for inv in overall_invoices),
                warnings_count=len(overall_review),
            )
        # Phase 1N — release the registry entry whether we completed
        # normally or cancelled.
        try:
            from . import cancel_registry
            cancel_registry.unregister(batch_id)
        except Exception:
            pass

    return {
        "batch_id": batch_id,
        "summary": summary,
        "by_vendor": by_vendor,
        "detection": detection,
        "unsupported_files": unsupported,
        "all_invoices": overall_invoices,
        "all_manual_review": overall_review,
    }


# ---------------------------------------------------------------------------
# Helper: write edited preview rows into a fresh copy of Output/Template.xlsx
# ---------------------------------------------------------------------------
# Keys the frontend may include but which aren't real ResMan columns. Anything
# starting with "_" is also stripped.
_NON_TEMPLATE_KEYS = {"_meta", "_edited", "_row_index"}


def _write_edited_rows_to_template(template_path: Path, dest: Path,
                                   rows: list[dict[str, Any]]) -> int:
    """Copy the official ResMan template and write `rows` into it. Each
    dict in `rows` is matched to template columns by exact header name;
    missing columns are left blank; extra keys (like `_meta`) are ignored.
    Returns the number of rows actually written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, dest)
    wb = openpyxl.load_workbook(dest)
    ws = wb["Sheet 1"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    header_to_col: dict[str, int] = {}
    for i, h in enumerate(headers, start=1):
        if h is None:
            continue
        key = str(h).strip()
        if key:
            header_to_col[key] = i

    # Phase 2L — Date columns are written as real Excel dates so
    # ResMan accepts them as date cells, not text.
    try:
        from utils.excel_helpers import set_cell as _set_cell
    except Exception:  # pragma: no cover
        _set_cell = None  # type: ignore

    written = 0
    for r_idx, row in enumerate(rows, start=2):
        for key, value in row.items():
            if key in _NON_TEMPLATE_KEYS or key.startswith("_"):
                continue
            col = header_to_col.get(key)
            if not col:
                continue
            cell = ws.cell(row=r_idx, column=col)
            if _set_cell is not None:
                _set_cell(cell, _coerce_cell_value(value), key)
            else:
                cell.value = _coerce_cell_value(value)
        written += 1
    wb.save(dest)
    return written


def _coerce_cell_value(value: Any) -> Any:
    """Light coercion so the resulting xlsx renders nicely:
    - strings that look like numbers stay as strings (we don't want to
      autoformat invoice numbers)
    - ``None`` and empty string render as blank cells
    - everything else passes through
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    return value


# ---------------------------------------------------------------------------
# Final export — copy the per-vendor ResMan workbook to the batch export
# folder, OR, if `edited_rows` is supplied by the frontend, write a fresh
# workbook from the official template using those edited values.
# ---------------------------------------------------------------------------
def export_batch(batch_id: str, edited_rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    bdir = batch_store.get_batch_dir(batch_id)
    processed_dir = bdir / "processed"
    export_dir = batch_store.get_export_dir(batch_id)

    # ---- Path A: edited export (frontend sent the table state) ----
    if edited_rows is not None:
        if not RESMAN_TEMPLATE.is_file():
            return {
                "batch_id": batch_id, "exported": [],
                "reason": "template_missing", "template_path": str(RESMAN_TEMPLATE),
            }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = export_dir / f"resman_import_edited_{ts}.xlsx"
        rows_written = _write_edited_rows_to_template(RESMAN_TEMPLATE, dest, edited_rows)
        return {
            "batch_id": batch_id,
            "exported": [{
                "vendor_key": "edited",
                "source_path": str(RESMAN_TEMPLATE),
                "export_path": str(dest),
                "filename": dest.name,
            }],
            "export_used_edited_rows": True,
            "edited_rows_count": len(edited_rows),
            "rows_written": rows_written,
        }

    # ---- Path B: legacy export (copy latest per-vendor processed xlsx) ----
    if not processed_dir.is_dir():
        return {"batch_id": batch_id, "exported": [], "reason": "no_processed_output_yet",
                "export_used_edited_rows": False}

    exported: list[dict] = []
    for vendor_dir in sorted(processed_dir.iterdir()):
        if not vendor_dir.is_dir():
            continue
        candidates = sorted(vendor_dir.glob("*_resman_import_*.xlsx"))
        if not candidates:
            continue
        latest = candidates[-1]
        dest = export_dir / latest.name
        shutil.copy2(latest, dest)
        exported.append({
            "vendor_key": vendor_dir.name,
            "source_path": str(latest),
            "export_path": str(dest),
            "filename": dest.name,
        })

    return {
        "batch_id": batch_id,
        "exported": exported,
        "export_used_edited_rows": False,
    }
