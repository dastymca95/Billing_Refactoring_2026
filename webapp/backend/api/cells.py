"""Phase 2K — Cell-level endpoints.

Powers the operator-facing "Cell Explain / Correct / Learn" flow:

  * ``GET /api/batches/{id}/cells/{row}/{col}/explain`` — synthesise
    a human-readable explanation of why a specific template cell
    holds its current value, citing trace items, vendor regex rules,
    fallbacks, and missing fields.
  * ``POST /api/batches/{id}/cells/{row}/{col}/override`` — record a
    one-off cell value override OR a reusable learned correction
    (depending on `scope`).
  * ``POST /api/batches/{id}/cells/{row}/{col}/remap-source`` — store a
    bbox-based hint that a future processing run can read text from
    instead of relying on the regex extraction.
  * ``GET /api/learned-corrections`` and friends — list / delete the
    persisted learned corrections.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import batch_store
from ..services import learned_corrections as lc_service


_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/batches", tags=["cells"])
learned_router = APIRouter(prefix="/api/learned-corrections", tags=["learned"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_cache_path(batch_id: str) -> Path:
    return batch_store.get_processed_dir(batch_id) / "_webapp_result.json"


def _load_result(batch_id: str) -> dict[str, Any]:
    try:
        p = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not p.is_file():
        raise HTTPException(
            status_code=404,
            detail="No preview cached — run Process first.",
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"Cache read failed: {e}")


def _row_at(result: dict[str, Any], row_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the (invoice, row_dict) at flat ``row_index``."""
    flat = 0
    for inv in result.get("all_invoices", []) or []:
        for r in inv.get("rows") or []:
            if flat == row_index:
                return inv, r
            flat += 1
    raise HTTPException(status_code=404, detail=f"Row {row_index} not found")


def _trace_items_for(batch_id: str, source_file: str) -> list[dict[str, Any]]:
    safe = batch_store.get_batch_dir(batch_id) / "trace"
    # Mirror of utils.extraction_trace._safe_filename, kept local.
    import re as _re
    name = (source_file or "unknown").strip().replace("\\", "/").split("/")[-1]
    safe_name = _re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200] or "unknown"
    fp = safe / f"{safe_name}.json"
    if not fp.is_file():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return list(data.get("items") or [])
    except (OSError, ValueError):
        return []


def _vendor_key_for(result: dict[str, Any], invoice: dict[str, Any]) -> str:
    """Best-effort vendor key derivation.

    ResMan rows carry a "Vendor" display name; ``by_vendor`` keys are
    the vendor_key slugs (e.g. "hopkinsville_water_environment_authority").
    We prefer the slug when we can find it.
    """
    by = result.get("by_vendor") or {}
    if isinstance(by, dict) and len(by) == 1:
        return list(by.keys())[0]
    # Fallback: derive from the first row's Vendor column.
    name = (invoice.get("rows") or [{}])[0].get("Vendor") or ""
    if not name:
        return ""
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:120]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CellExplainResponse(BaseModel):
    batch_id: str
    row_index: int
    column: str
    current_value: Any = None
    summary: str = ""
    cell_kind: str = ""              # "extracted" | "derived" | "fallback" | "user_edited"
    fallback_used: bool = False
    missing_components: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    source_file: Optional[str] = None
    source_page: Optional[int] = None
    vendor_key: str = ""
    ai_generated: bool = False
    ai_confidence: Optional[float] = None
    ai_validation_flags: list[str] = Field(default_factory=list)
    ai_warnings: list[str] = Field(default_factory=list)


class OverrideRequest(BaseModel):
    new_value: Any = None
    scope: str = "cell"              # "cell" | "vendor"
    note: str = ""
    contains_text: Optional[str] = None  # for vendor-scope triggers


class OverrideResponse(BaseModel):
    batch_id: str
    row_index: int
    column: str
    saved: str                       # "edit" | "learned_correction"
    correction_id: Optional[str] = None
    new_value: Any = None


class RemapSourceRequest(BaseModel):
    field_key: str                   # "service_address", etc.
    page: int
    bbox: dict[str, float]           # normalized 0..1
    scope: str = "vendor"
    note: str = ""


class RemapSourceResponse(BaseModel):
    batch_id: str
    correction_id: str
    saved: str = "region_remap"


# ---------------------------------------------------------------------------
# Endpoints — explain
# ---------------------------------------------------------------------------


@router.get("/{batch_id}/cells/{row_index}/{column}/explain")
def explain_cell_endpoint(
    batch_id: str, row_index: int, column: str
) -> CellExplainResponse:
    """Build a friendly explanation for one cell.

    Pulls the row from the result cache, finds matching trace items
    (via `feeds_columns` ∩ row `_meta.trace_ids`), and synthesises a
    short summary that flags missing components or fallbacks. The
    frontend renders the modal directly from this payload.
    """
    result = _load_result(batch_id)
    invoice, row = _row_at(result, row_index)
    meta = row.get("_meta") or {}
    raw_trace_ids = list(meta.get("trace_ids") or [])
    source_file = meta.get("source_file") or invoice.get("debug_info", {}).get("source_file")
    source_page = meta.get("source_page")
    items_all = _trace_items_for(batch_id, source_file or "")
    by_id = {it.get("trace_id"): it for it in items_all}

    # Cell-scoped traces: keep only those whose feeds_columns include
    # the column AND whose trace_id is on this row's _meta.
    cell_traces: list[dict[str, Any]] = []
    for tid in raw_trace_ids:
        it = by_id.get(tid)
        if not it:
            continue
        feeds = it.get("feeds_columns") or []
        if not feeds or column in feeds:
            cell_traces.append(it)

    current_value = row.get(column)

    # Heuristic for cell_kind / missing_components: each known column
    # has expected components. We list the expected ones and which
    # ones we found in the trace. Anything expected-but-absent is a
    # "missing component" → fallback was used.
    expected = _expected_components_for(column)
    found_keys = [t.get("field_key") for t in cell_traces]
    missing = [k for k in expected if not _trace_key_present(k, found_keys)]

    fallback_used = False
    cell_kind = "extracted"
    if not cell_traces and current_value not in (None, ""):
        cell_kind = "derived"
    if missing:
        fallback_used = True
        cell_kind = "fallback"
    if (meta.get("learned_corrections_applied") or []):
        cell_kind = "user_edited"
    elif meta.get("ai_generated"):
        cell_kind = "ai_extracted"

    summary = _humanise_summary(
        column=column,
        current_value=current_value,
        cell_traces=cell_traces,
        missing=missing,
        meta=meta,
    )

    vendor_key = _vendor_key_for(result, invoice)

    return CellExplainResponse(
        batch_id=batch_id,
        row_index=row_index,
        column=column,
        current_value=current_value,
        summary=summary,
        cell_kind=cell_kind,
        fallback_used=fallback_used,
        missing_components=missing,
        trace_ids=[t.get("trace_id") for t in cell_traces],
        traces=cell_traces,
        source_file=source_file,
        source_page=source_page if isinstance(source_page, int) else None,
        vendor_key=vendor_key,
        ai_generated=bool(meta.get("ai_generated")),
        ai_confidence=(
            meta.get("ai_confidence")
            if isinstance(meta.get("ai_confidence"), (int, float))
            else None
        ),
        ai_validation_flags=list(meta.get("ai_validation_flags") or []),
        ai_warnings=list(meta.get("ai_warnings") or []),
    )


def _expected_components_for(column: str) -> list[str]:
    """What field_keys SHOULD have contributed to this column,
    according to how the ResMan template is composed today.

    These are the columns we explain in detail today. Anything not
    listed gets an empty expected set, which keeps the explanation
    truthful (we won't claim a component is missing when we don't
    actually know what should be there)."""
    table: dict[str, list[str]] = {
        "Invoice Number": ["account_number"],
        "Invoice Date": ["due_date", "invoice_date"],
        "Accounting Date": ["due_date", "invoice_date"],
        "Due Date": ["due_date"],
        "Invoice Description": ["service_address", "service_period"],
        "Property Abbreviation": ["service_address"],
        "Location": ["service_address"],
        "Amount": ["total_amount_due"],
        "Line Item Description": ["line_item"],
        "GL Account": ["line_item"],
    }
    return table.get(column, [])


def _trace_key_present(needle: str, found_keys: list[Optional[str]]) -> bool:
    """A field_key matches if it equals or starts-with the expected
    name (so ``line_item_WAF`` counts as ``line_item``)."""
    n = (needle or "").strip().lower()
    if not n:
        return False
    for fk in found_keys:
        if not fk:
            continue
        s = str(fk).strip().lower()
        if s == n or s.startswith(n + "_") or n in s:
            return True
    return False


def _humanise_summary(
    *, column: str, current_value: Any, cell_traces: list[dict[str, Any]],
    missing: list[str], meta: dict[str, Any],
) -> str:
    """Operator-friendly one-paragraph explanation."""
    cur = current_value if current_value not in (None, "") else "(blank)"
    parts: list[str] = [f"The cell value is {cur!r}."]
    if cell_traces:
        names = ", ".join(
            sorted({t.get("field_label") or t.get("field_key") or "?"
                    for t in cell_traces})
        )
        parts.append(f"It was built from these source regions: {names}.")
    else:
        parts.append("No source regions were attached to this cell.")
    if missing:
        nice = ", ".join(missing)
        parts.append(
            f"One or more expected components were missing ({nice}); "
            "the processor used a fallback instead."
        )
    if meta.get("learned_corrections_applied"):
        parts.append(
            "A previously-saved learned correction was applied to this row."
        )
    if meta.get("manual_review_reasons"):
        reasons = ", ".join(meta["manual_review_reasons"][:3])
        parts.append(f"Manual review flags on this row: {reasons}.")
    if meta.get("ai_generated"):
        conf = meta.get("ai_confidence")
        if isinstance(conf, (int, float)):
            parts.append(
                f"This value came from AI extraction with {conf * 100:.0f}% confidence."
            )
        else:
            parts.append("This value came from AI extraction.")
        flags = meta.get("ai_validation_flags") or []
        if flags:
            parts.append("AI validation flags: " + ", ".join(map(str, flags[:4])) + ".")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Endpoints — override (one-off edit OR vendor-wide learned correction)
# ---------------------------------------------------------------------------


@router.post("/{batch_id}/cells/{row_index}/{column}/override")
def override_cell_endpoint(
    batch_id: str, row_index: int, column: str, body: OverrideRequest
) -> OverrideResponse:
    """Save a cell override.

    ``scope=cell`` — one-off edit. We do NOT mutate the cache here
    (the existing `/save-edits` flow handles that); we just confirm
    the request and return so the frontend can surface a toast and
    rely on the existing edit pipeline for persistence.

    ``scope=vendor`` — store a reusable ``value_override`` in the
    learned-corrections sidecar so future processing runs apply it.
    """
    result = _load_result(batch_id)
    invoice, row = _row_at(result, row_index)
    if column not in row:
        raise HTTPException(status_code=400, detail=f"Unknown column: {column!r}")
    scope = (body.scope or "cell").lower()
    if scope == "cell":
        return OverrideResponse(
            batch_id=batch_id,
            row_index=row_index,
            column=column,
            saved="edit",
            new_value=body.new_value,
        )
    if scope == "vendor":
        vendor_key = _vendor_key_for(result, invoice)
        if not vendor_key:
            raise HTTPException(status_code=400, detail="Could not derive vendor key.")
        trigger: dict[str, Any] = {"column": column}
        if body.contains_text:
            trigger["contains_text"] = body.contains_text
        else:
            # Default trigger: match invoices with this account number.
            acct = invoice.get("account_number") or ""
            if acct:
                trigger["account_number"] = acct
        entry = lc_service.add_correction(
            vendor_key=vendor_key,
            kind="value_override",
            scope="vendor",
            trigger=trigger,
            action={"set_column": column, "set_value": body.new_value},
            created_from={
                "batch_id": batch_id,
                "row_index": row_index,
                "source_file": (row.get("_meta") or {}).get("source_file"),
            },
            note=body.note,
        )
        return OverrideResponse(
            batch_id=batch_id,
            row_index=row_index,
            column=column,
            saved="learned_correction",
            correction_id=entry.get("correction_id"),
            new_value=body.new_value,
        )
    raise HTTPException(status_code=400, detail=f"Unknown scope: {scope!r}")


# ---------------------------------------------------------------------------
# Endpoints — remap source (bbox-based)
# ---------------------------------------------------------------------------


@router.post("/{batch_id}/cells/{row_index}/{column}/remap-source")
def remap_source_endpoint(
    batch_id: str, row_index: int, column: str, body: RemapSourceRequest
) -> RemapSourceResponse:
    """Save a region-remap correction.

    The operator drew a box on the document viewer and tagged it with
    a field_key (e.g. ``service_address``). We persist the bbox + the
    field_key in the learned-corrections sidecar so the next
    processing run can read text from that region instead of using
    the regex-based extraction. Application happens in the vendor
    processor (HWEA today)."""
    result = _load_result(batch_id)
    invoice, row = _row_at(result, row_index)
    vendor_key = _vendor_key_for(result, invoice)
    if not vendor_key:
        raise HTTPException(status_code=400, detail="Could not derive vendor key.")
    bbox = body.bbox or {}
    for k in ("x", "y", "w", "h"):
        if k not in bbox:
            raise HTTPException(status_code=400, detail=f"bbox missing '{k}'")
    trigger = {"column": column}
    acct = invoice.get("account_number") or ""
    if acct:
        trigger["account_number"] = acct
    action = {
        "field_key": body.field_key,
        "bbox": {
            "x": float(bbox.get("x", 0)),
            "y": float(bbox.get("y", 0)),
            "w": float(bbox.get("w", 0)),
            "h": float(bbox.get("h", 0)),
        },
        "page": int(body.page or 1),
    }
    entry = lc_service.add_correction(
        vendor_key=vendor_key,
        kind="region_remap",
        scope=body.scope or "vendor",
        trigger=trigger,
        action=action,
        created_from={
            "batch_id": batch_id,
            "row_index": row_index,
            "source_file": (row.get("_meta") or {}).get("source_file"),
        },
        note=body.note,
    )
    return RemapSourceResponse(
        batch_id=batch_id,
        correction_id=entry.get("correction_id"),
    )


# ---------------------------------------------------------------------------
# Endpoints — list / delete learned corrections
# ---------------------------------------------------------------------------


@learned_router.get("")
def list_learned_endpoint(vendor_key: str = "") -> dict:
    """Return the persisted learned corrections."""
    return {
        "vendor_key": vendor_key,
        "items": lc_service.list_corrections(vendor_key),
    }


@learned_router.delete("/{vendor_key}/{correction_id}")
def delete_learned_endpoint(vendor_key: str, correction_id: str) -> dict:
    """Drop one correction by id."""
    ok = lc_service.delete_correction(vendor_key, correction_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Correction not found")
    return {"vendor_key": vendor_key, "correction_id": correction_id, "deleted": True}
