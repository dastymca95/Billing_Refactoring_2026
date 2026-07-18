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
import os
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import (
    batch_store,
    batch_processor,
    cancel_registry,
    human_adjudication,
    learned_corrections as lc_service,
    perf_timer,
    processing_queue,
    revisions as revisions_service,
    row_normalizer,
)
from ..services.template_rules import get_template_rules
from ..services.vendor_detection import detect_vendors_for_files
from ..services.invoice_identity import (
    build_invoice_identities,
    invoice_source_file,
    invoice_source_page,
)

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


def _load_cached_result(batch_id: str, missing_detail: str) -> dict:
    try:
        cache_path = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not cache_path.is_file():
        raise HTTPException(status_code=404, detail=missing_detail)
    with open(cache_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    try:
        row_normalizer.normalize_result(result)
    except Exception:  # pragma: no cover - preview should stay available
        _LOG.exception("Could not normalize cached preview for %s", batch_id)
    return result


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


def _float_or_none(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        n = float(value)
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def _invoice_source_file(inv: dict) -> str:
    """Compatibility adapter for callers of the former local helper."""
    return invoice_source_file(inv)


def _invoice_source_page(inv: dict) -> int | None:
    """Compatibility adapter for callers of the former local helper."""
    return invoice_source_page(inv)


def _invoice_total_metadata(inv: dict, raw_rows: list[dict]) -> dict:
    """Return invoice-level totals for preview-only AI review metadata.

    Older AI-assisted cached results stored `total_amount` and validation
    summary on the invoice object, but not on each flattened row's
    `_meta.ai_provenance`. The Single Invoice UI is row-driven, so attach a
    non-export metadata copy during preview flattening. This does not change
    vendor business logic or the workbook rows.
    """
    validation = inv.get("validation_summary")
    validation = validation if isinstance(validation, dict) else {}
    invoice_total = (
        _float_or_none(inv.get("total_amount"))
        or _float_or_none(validation.get("invoice_total"))
        or _float_or_none(validation.get("reconciled_total"))
    )
    subtotal = _float_or_none(inv.get("subtotal"))
    tax_amount = _float_or_none(inv.get("tax_amount"))
    shipping_amount = _float_or_none(inv.get("shipping_amount"))
    fees_amount = _float_or_none(inv.get("fees_amount"))
    row_total = 0.0
    for row in raw_rows:
        amount = _float_or_none(row.get("Amount"))
        if amount is not None:
            row_total += amount
    if subtotal is None and row_total > 0:
        subtotal = round(row_total, 2)
    if tax_amount is None and invoice_total is not None and subtotal is not None:
        inferred_tax = round(invoice_total - subtotal, 2)
        if inferred_tax > 0:
            tax_amount = inferred_tax
    out: dict = {}
    if invoice_total is not None:
        out["invoice_total"] = round(invoice_total, 2)
    if subtotal is not None:
        out["subtotal"] = round(subtotal, 2)
    if tax_amount is not None:
        out["tax_amount"] = round(tax_amount, 2)
    if shipping_amount is not None:
        out["shipping_amount"] = round(shipping_amount, 2)
    if fees_amount is not None:
        out["fees_amount"] = round(fees_amount, 2)
    return out


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
    identities = build_invoice_identities(invoices)
    out: list[dict] = []
    for identity, inv in zip(identities, invoices):
        invoice_index = identity.invoice_index
        raw_rows = list(inv.get("rows", []) or [])
        invoice_totals = _invoice_total_metadata(inv, raw_rows)
        source_file = identity.source_file
        source_page = identity.source_page
        invoice_number = identity.invoice_number
        invoice_group_id = identity.group_id

        for invoice_row_index, row in enumerate(raw_rows):
            padded = _pad_row_to_template(row, columns)
            meta = padded.get("_meta") if isinstance(padded.get("_meta"), dict) else {}
            meta = dict(meta)
            meta.setdefault("source_file", source_file or None)
            meta.setdefault("source_page", source_page)
            meta.setdefault("invoice_group_id", invoice_group_id)
            meta.setdefault("invoice_number", invoice_number or None)
            if invoice_totals:
                provenance = meta.get("ai_provenance")
                if not isinstance(provenance, dict):
                    provenance = {}
                provenance = {**invoice_totals, **provenance}
                meta["ai_provenance"] = provenance
            validation = inv.get("validation_summary") if isinstance(inv.get("validation_summary"), dict) else {}
            if "total_reconciliation_passed" in validation:
                meta["total_reconciliation_passed"] = validation.get("total_reconciliation_passed")
            meta["invoice_index"] = invoice_index
            meta["invoice_row_index"] = invoice_row_index
            meta["row_index"] = len(out)
            padded["_meta"] = meta
            padded["_row_index"] = len(out)
            out.append(padded)
    return out


def _row_contract_manual_review_items(result: dict) -> list[dict]:
    """Expose row-contract blockers as review rows for cached/fresh previews."""

    items: list[dict] = []
    for inv in result.get("all_invoices") or []:
        invoice_number = str(inv.get("invoice_number") or "").strip()
        account_number = str(inv.get("account_number") or "").strip()
        invoice_date = str(inv.get("billing_date") or inv.get("invoice_date") or "").strip()
        service_address = str(inv.get("service_address") or "").strip()
        total_amount = inv.get("total_amount") or 0
        for row in inv.get("rows") or []:
            if not isinstance(row, dict):
                continue
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            reasons = list(meta.get("contract_blocking_reasons") or [])
            if not reasons:
                continue
            items.append(
                {
                    "source_file": meta.get("source_file") or inv.get("source_file") or "",
                    "account_number": account_number,
                    "invoice_number": row.get("Invoice Number") or invoice_number,
                    "invoice_date": row.get("Invoice Date") or invoice_date,
                    "property_abbreviation": row.get("Property Abbreviation") or "",
                    "location": row.get("Location") or "",
                    "service_address": meta.get("service_address") or service_address,
                    "total_amount": float(total_amount or row.get("Amount") or 0),
                    "line_count": len(inv.get("rows") or []),
                    "reasons": reasons,
                    "match_strategy": "row_contract_validator",
                    "match_confidence": "",
                    "service_period_source": meta.get("service_period_source") or "",
                }
            )
    return items


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
        with perf_timer.perf_step("revision.write", batch_id=batch_id):
            revisions_service.record_revision(
                batch_id,
                result=result,
                export_name=_read_export_name_from_metadata(batch_id),
                status="completed",
            )
    except Exception:  # pragma: no cover - belt-and-braces
        _LOG.exception("Could not record revision for %s", batch_id)


def _source_file_name(item: dict | None) -> str:
    """Return the source filename carried by an invoice/review payload."""
    if not isinstance(item, dict):
        return ""
    debug = item.get("debug_info") if isinstance(item.get("debug_info"), dict) else {}
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    for payload in (item, debug, meta):
        for key in ("source_file", "file_name", "filename"):
            value = payload.get(key)
            if value:
                return Path(str(value)).name
    return ""


def _source_page_number(item: dict | None) -> int | None:
    """Return the source page carried by an invoice/review payload."""
    if not isinstance(item, dict):
        return None
    debug = item.get("debug_info") if isinstance(item.get("debug_info"), dict) else {}
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    for payload in (item, debug, meta):
        for key in ("source_page", "source_page_number", "pdf_page_number", "page_number"):
            page = _int_or_none(payload.get(key))
            if page is not None:
                return page
    return None


def _source_file_count(items: list[dict]) -> int:
    return len({
        src for src in (_source_file_name(item) for item in items) if src
    })


def _is_payload_for_file(item: dict | None, filename: str) -> bool:
    return _source_file_name(item) == Path(filename).name


def _is_payload_for_file_page(item: dict | None, filename: str, page: int) -> bool:
    return (
        _source_file_name(item) == Path(filename).name
        and _source_page_number(item) == page
    )


def _row_count(invoices: list[dict]) -> int:
    return sum(len((inv or {}).get("rows") or []) for inv in invoices)


def _dedupe_repeated_source_blocks(items: list[dict]) -> list[dict]:
    """Collapse duplicate source-file blocks, preserving latest results."""
    blocks: list[tuple[str, list[dict]]] = []
    current_key: str | None = None
    current_block: list[dict] = []
    anonymous_index = 0

    for item in items:
        source = _source_file_name(item)
        key = source or f"__anonymous_{anonymous_index}"
        if not source:
            anonymous_index += 1
        if current_key is None or key != current_key:
            if current_block:
                blocks.append((current_key or "", current_block))
            current_key = key
            current_block = [item]
        else:
            current_block.append(item)
    if current_block:
        blocks.append((current_key or "", current_block))

    last_index_by_key: dict[str, int] = {}
    for idx, (key, _block) in enumerate(blocks):
        if key and not key.startswith("__anonymous_"):
            last_index_by_key[key] = idx

    deduped: list[dict] = []
    for idx, (key, block) in enumerate(blocks):
        if key.startswith("__anonymous_") or last_index_by_key.get(key) == idx:
            deduped.extend(block)
    return deduped


def _dedupe_unsupported_files(items: list[dict]) -> list[dict]:
    deduped_reversed: list[dict] = []
    seen: set[str] = set()
    for item in reversed(items):
        name = Path(str((item or {}).get("filename") or "")).name
        if name:
            if name in seen:
                continue
            seen.add(name)
        deduped_reversed.append(item)
    return list(reversed(deduped_reversed))


def _refresh_vendor_summary(payload: dict) -> dict:
    invoices = list(payload.get("invoices") or [])
    review = list(payload.get("manual_review_rows") or [])
    summary = dict(payload.get("summary") or {})
    source_count = _source_file_count(invoices)
    if source_count:
        summary["files_processed"] = source_count
    summary["invoices_produced"] = len(invoices)
    rows_total = _row_count(invoices)
    summary["rows_total"] = rows_total
    summary["line_items"] = rows_total
    summary["invoices_flagged_for_review"] = len(review)
    payload["summary"] = summary
    return payload


def _recompute_template_summary(result: dict, *, scope: str = "template") -> None:
    invoices = list(result.get("all_invoices") or [])
    review = list(result.get("all_manual_review") or [])
    unsupported = _dedupe_unsupported_files(list(result.get("unsupported_files") or []))
    result["unsupported_files"] = unsupported
    source_count = _source_file_count(invoices)
    summary = dict(result.get("summary") or {})
    summary.update({
        "scope": scope,
        "files_total": max(1 if invoices or unsupported else 0, source_count + len(unsupported)),
        "files_supported": source_count,
        "files_unsupported": len(unsupported),
        "invoices_total": len(invoices),
        "manual_review_total": len(review),
    })
    result["summary"] = summary


def _merge_single_file_result(batch_id: str, filename: str, fresh: dict) -> dict:
    """Add/replace one file's extraction in the active template preview."""
    target = Path(filename).name
    try:
        cache_path = _result_cache_path(batch_id)
        if not cache_path.is_file():
            return _mark_single_file_result(batch_id, target, fresh)
        with open(cache_path, "r", encoding="utf-8") as f:
            existing = json.load(f) or {}
    except Exception:
        return _mark_single_file_result(batch_id, target, fresh)

    merged = dict(existing)
    merged["batch_id"] = batch_id
    merged["scope"] = {
        "type": "template",
        "last_added_file": target,
    }
    merged["all_invoices"] = _dedupe_repeated_source_blocks([
        *[
            inv for inv in (existing.get("all_invoices") or [])
            if not _is_payload_for_file(inv, target)
        ],
        *list(fresh.get("all_invoices") or []),
    ])
    merged["all_manual_review"] = _dedupe_repeated_source_blocks([
        *[
            item for item in (existing.get("all_manual_review") or [])
            if not _is_payload_for_file(item, target)
        ],
        *list(fresh.get("all_manual_review") or []),
    ])
    merged["unsupported_files"] = _dedupe_unsupported_files([
        *[
            item for item in (existing.get("unsupported_files") or [])
            if Path(str((item or {}).get("filename") or "")).name != target
        ],
        *list(fresh.get("unsupported_files") or []),
    ])
    detection = dict(existing.get("detection") or {})
    detection.update(fresh.get("detection") or {})
    merged["detection"] = detection

    by_vendor: dict[str, dict] = {}
    for vendor_key, payload in (existing.get("by_vendor") or {}).items():
        payload = dict(payload or {})
        payload["invoices"] = _dedupe_repeated_source_blocks([
            inv for inv in (payload.get("invoices") or [])
            if not _is_payload_for_file(inv, target)
        ])
        payload["manual_review_rows"] = _dedupe_repeated_source_blocks([
            item for item in (payload.get("manual_review_rows") or [])
            if not _is_payload_for_file(item, target)
        ])
        if payload["invoices"] or payload["manual_review_rows"]:
            by_vendor[vendor_key] = _refresh_vendor_summary(payload)

    for vendor_key, fresh_payload in (fresh.get("by_vendor") or {}).items():
        fresh_payload = dict(fresh_payload or {})
        payload = dict(by_vendor.get(vendor_key) or {})
        for key, value in fresh_payload.items():
            if key not in {"invoices", "manual_review_rows", "summary"}:
                payload[key] = value
        payload["invoices"] = _dedupe_repeated_source_blocks([
            *list(payload.get("invoices") or []),
            *list(fresh_payload.get("invoices") or []),
        ])
        payload["manual_review_rows"] = _dedupe_repeated_source_blocks([
            *list(payload.get("manual_review_rows") or []),
            *list(fresh_payload.get("manual_review_rows") or []),
        ])
        by_vendor[vendor_key] = _refresh_vendor_summary(payload)

    merged["by_vendor"] = by_vendor
    _recompute_template_summary(merged, scope="template")
    return merged


def _merge_single_page_result(
    batch_id: str,
    filename: str,
    page: int,
    fresh: dict,
) -> dict:
    """Add/replace one extracted PDF page without discarding sibling pages."""
    target = Path(filename).name
    try:
        cache_path = _result_cache_path(batch_id)
        if not cache_path.is_file():
            return _mark_single_page_result(batch_id, target, page, fresh)
        with open(cache_path, "r", encoding="utf-8") as f:
            existing = json.load(f) or {}
    except Exception:
        return _mark_single_page_result(batch_id, target, page, fresh)

    merged = dict(existing)
    merged["batch_id"] = batch_id
    merged["scope"] = {
        "type": "template",
        "last_added_file": target,
        "last_added_page": page,
    }
    merged["all_invoices"] = [
        *[
            inv for inv in (existing.get("all_invoices") or [])
            if not _is_payload_for_file_page(inv, target, page)
        ],
        *list(fresh.get("all_invoices") or []),
    ]
    merged["all_manual_review"] = [
        *[
            item for item in (existing.get("all_manual_review") or [])
            if not _is_payload_for_file_page(item, target, page)
        ],
        *list(fresh.get("all_manual_review") or []),
    ]
    merged["unsupported_files"] = _dedupe_unsupported_files([
        *[
            item for item in (existing.get("unsupported_files") or [])
            if not _is_payload_for_file_page(item, target, page)
        ],
        *list(fresh.get("unsupported_files") or []),
    ])

    detection = dict(existing.get("detection") or {})
    detection.update(fresh.get("detection") or {})
    merged["detection"] = detection

    by_vendor: dict[str, dict] = {}
    for vendor_key, payload in (existing.get("by_vendor") or {}).items():
        payload = dict(payload or {})
        payload["invoices"] = [
            inv for inv in (payload.get("invoices") or [])
            if not _is_payload_for_file_page(inv, target, page)
        ]
        payload["manual_review_rows"] = [
            item for item in (payload.get("manual_review_rows") or [])
            if not _is_payload_for_file_page(item, target, page)
        ]
        if payload["invoices"] or payload["manual_review_rows"]:
            by_vendor[vendor_key] = _refresh_vendor_summary(payload)

    for vendor_key, fresh_payload in (fresh.get("by_vendor") or {}).items():
        fresh_payload = dict(fresh_payload or {})
        payload = dict(by_vendor.get(vendor_key) or {})
        for key, value in fresh_payload.items():
            if key not in {"invoices", "manual_review_rows", "summary"}:
                payload[key] = value
        payload["invoices"] = [
            *list(payload.get("invoices") or []),
            *list(fresh_payload.get("invoices") or []),
        ]
        payload["manual_review_rows"] = [
            *list(payload.get("manual_review_rows") or []),
            *list(fresh_payload.get("manual_review_rows") or []),
        ]
        by_vendor[vendor_key] = _refresh_vendor_summary(payload)

    merged["by_vendor"] = by_vendor
    _recompute_template_summary(merged, scope="template")
    return merged


def _mark_single_file_result(batch_id: str, filename: str, result: dict) -> dict:
    """Stamp a single-file run as an isolated template revision."""
    target = Path(filename).name
    result["batch_id"] = batch_id
    result["scope"] = {
        "type": "file",
        "source_file": target,
    }
    summary = dict(result.get("summary") or {})
    invoices = list(result.get("all_invoices") or [])
    review = list(result.get("all_manual_review") or [])
    unsupported = list(result.get("unsupported_files") or [])
    source_count = _source_file_count(invoices) or (0 if unsupported else 1)
    summary.update({
        "scope": "file",
        "source_file": target,
        "files_total": max(1, source_count + len(unsupported)),
        "files_supported": source_count,
        "files_unsupported": len(unsupported),
        "invoices_total": len(invoices),
        "manual_review_total": len(review),
    })
    result["summary"] = summary
    return result


def _mark_single_page_result(batch_id: str, filename: str, page: int, result: dict) -> dict:
    """Stamp a page-only run as an isolated template revision."""
    target = Path(filename).name
    result["batch_id"] = batch_id
    result["scope"] = {
        "type": "page",
        "source_file": target,
        "source_page": page,
    }
    summary = dict(result.get("summary") or {})
    invoices = list(result.get("all_invoices") or [])
    review = list(result.get("all_manual_review") or [])
    unsupported = list(result.get("unsupported_files") or [])
    source_count = _source_file_count(invoices) or (0 if unsupported else 1)
    summary.update({
        "scope": "page",
        "source_file": target,
        "source_page": page,
        "files_total": max(1, source_count + len(unsupported)),
        "files_supported": source_count,
        "files_unsupported": len(unsupported),
        "invoices_total": len(invoices),
        "manual_review_total": len(review),
    })
    result["summary"] = summary
    return result


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
        perf_timer.record(batch_id, "cancel.settled", 0.0)
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
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
        perf_timer.record(batch_id, "cancel.settled", 0.0, meta={"fallback": True})
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
    except Exception:
        pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    with tempfile.NamedTemporaryFile(
        "w",
        dir=str(path.parent),
        prefix=".progress_",
        suffix=".tmp",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_name = tmp.name
    try:
        os.replace(tmp_name, path)
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def _invalidate_batch_summary_cache(batch_id: str) -> None:
    """Drop list-view summary counts after a preview cache rewrite.

    `GET /api/batches/{id}` rebuilds this from `_webapp_result.json`. If we
    leave the old metadata cache around after reprocessing an appended PDF, the
    batch dropdown can keep showing the previous invoice/row counts.
    """

    try:
        meta_path = batch_store.get_batch_dir(batch_id) / "batch_metadata.json"
        if not meta_path.is_file():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        if "summary_cache" not in meta:
            return
        meta.pop("summary_cache", None)
        _atomic_write_json(meta_path, meta)
    except Exception:
        _LOG.debug("Could not invalidate summary cache for %s", batch_id, exc_info=True)


def _stamp_completed_after_cache(batch_id: str, result: dict) -> None:
    """Mark progress completed only after preview cache/revision writes land."""
    try:
        progress_path = batch_store.get_batch_dir(batch_id) / "progress.json"
        snap: dict = {}
        if progress_path.is_file():
            try:
                snap = json.loads(progress_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                snap = {}
        summary = result.get("summary") or {}
        invoices = result.get("all_invoices") or []
        review = result.get("all_manual_review") or []
        now = datetime.now().isoformat(timespec="seconds")
        rows_created = sum(len((inv or {}).get("rows") or []) for inv in invoices)
        files_total = int(summary.get("files_total") or snap.get("files_total") or 0)
        for stage in snap.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            if stage.get("status") == "running":
                stage["status"] = "completed"
                stage["completed_at"] = now
            if stage.get("key") == "ready":
                stage["status"] = "completed"
                stage["percent"] = 100.0
                stage["completed_at"] = stage.get("completed_at") or now
        snap.update(
            {
                "batch_id": batch_id,
                "status": "completed",
                "percent": 100.0,
                "current_step": "Done",
                "current_file": "",
                "files_total": files_total,
                "files_done": files_total,
                "invoices_created": len(invoices),
                "rows_created": rows_created,
                "warnings_count": len(review),
                "error_message": "",
                "updated_at": now,
            }
        )
        snap.setdefault("started_at", now)
        _atomic_write_json(progress_path, snap)
    except Exception:
        _LOG.exception("Could not stamp completed progress for %s", batch_id)


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
    perf_timer.record(batch_id, "queue.runner_start", 0.0)
    try:
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
    except Exception:
        pass
    try:
        if extraction_trace is not None:
            extraction_trace.start_batch(batch_id)
        try:
            result = batch_processor.process_batch(
                batch_id,
                finalize_progress=False,
            )
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
            with perf_timer.perf_step("validation.row_normalizer", batch_id=batch_id):
                row_normalizer.normalize_result(result)
        except Exception:  # pragma: no cover
            _LOG.exception("row normalizer failed")
        # Phase 2K — apply learned-correction value overrides BEFORE
        # writing the cache so the preview reflects the user's curated
        # state on first paint (no flicker, no separate refresh).
        with perf_timer.perf_step("validation.learned_corrections", batch_id=batch_id):
            _apply_learned_corrections_to_result(result)
        # Human-approved assistant corrections are private runtime overlays.
        # They are replayed after fresh extraction and re-enter V2 as candidates;
        # they never bypass AccountingDecisionEngine or readiness.
        from ..services import approved_invoice_corrections
        with perf_timer.perf_step("validation.approved_invoice_corrections", batch_id=batch_id):
            approved_invoice_corrections.apply_to_result(result, batch_id=batch_id)
        from ..services import human_adjudication
        with perf_timer.perf_step("validation.human_adjudication", batch_id=batch_id):
            human_adjudication.apply_to_result(result, batch_id=batch_id)
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with perf_timer.perf_step("preview.cache_write", batch_id=batch_id):
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, default=str, indent=2)
        _invalidate_batch_summary_cache(batch_id)
        _record_revision_for_result(batch_id, result)
        with perf_timer.perf_step("progress.final_stamp", batch_id=batch_id):
            _stamp_completed_after_cache(batch_id, result)
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
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


def _resolve_batch_input_file(batch_id: str, filename: str) -> Path:
    try:
        in_dir = batch_store.get_input_dir(batch_id).resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    target = (in_dir / safe_name).resolve()
    try:
        target.relative_to(in_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal blocked")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found in batch: {safe_name}")
    return target


def _create_single_page_input(batch_id: str, filename: str, page: int) -> Path:
    source = _resolve_batch_input_file(batch_id, filename)
    if source.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=415,
            detail="Page-level processing is only available for PDFs.",
        )
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PDF page processing is not available on this backend.",
        )

    try:
        reader = PdfReader(str(source))
        page_count = len(reader.pages)
        if page < 1 or page > page_count:
            raise HTTPException(
                status_code=400,
                detail=f"Page {page} is outside this document's {page_count} pages.",
            )
        writer = PdfWriter()
        writer.add_page(reader.pages[page - 1])
        temp_name = f".__page_process__{uuid4().hex}_{source.stem[:64]}_p{page}.pdf"
        temp_path = source.with_name(temp_name)
        with open(temp_path, "wb") as out:
            writer.write(out)
        return temp_path
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not isolate PDF page: {type(exc).__name__}",
        )


def _remap_single_page_result_sources(
    payload: dict,
    *,
    temp_filename: str,
    source_filename: str,
    source_page: int,
) -> dict:
    target = Path(source_filename).name
    temp = Path(temp_filename).name

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            matched_source = False
            for key in ("source_file", "file_name", "filename"):
                if Path(str(value.get(key) or "")).name == temp:
                    value[key] = target
                    matched_source = True
            if matched_source or _source_file_name(value) == target:
                value["source_page"] = source_page
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    detection = payload.get("detection")
    if isinstance(detection, dict) and temp in detection:
        page_detection = detection.pop(temp)
        detection[target] = page_detection
    return payload


@router.post("/{batch_id}/detect")
def detect_endpoint(batch_id: str) -> dict:
    try:
        files = batch_store.list_files_in_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    return {"batch_id": batch_id, "detection": detect_vendors_for_files(files)}


@router.post("/{batch_id}/process")
def process_endpoint(
    batch_id: str,
    sync: bool = False,
    file: str | None = None,
    file_mode: str = "replace",
    page: int | None = None,
) -> dict:
    """Kick off batch processing.

    By default the run happens in a background thread; the response is
    `{status: "accepted", batch_id, polling_url}`. The frontend polls
    `GET /progress` to track the run. Pass `?sync=1` to force a blocking
    request — used by tests and the CLI smoke tests where we want the
    final summary back in one call.

    Phase 2M — pass ``?file=<filename>`` to process a single file
    inside the batch instead of the whole batch. Single-file runs are
    always synchronous (they're fast and the caller wants the result
    back immediately), write an isolated one-file preview/revision,
    and never touch the cross-batch queue, so a queued full-batch run
    stays in line.
    """
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    if page is not None:
        if not file:
            raise HTTPException(
                status_code=400,
                detail="Page processing requires a filename.",
            )
        if page < 1:
            raise HTTPException(status_code=400, detail="Invalid page number.")

    # Single-file/page path forces sync regardless of the ``sync`` flag.
    if file:
        sync = True
        if file_mode not in {"replace", "merge"}:
            raise HTTPException(
                status_code=400,
                detail="Invalid file processing mode",
            )

    if sync:
        processing_file = file
        page_temp_path: Path | None = None
        if file and page is not None:
            page_temp_path = _create_single_page_input(batch_id, file, page)
            processing_file = page_temp_path.name

        if extraction_trace is not None:
            extraction_trace.start_batch(batch_id)
        try:
            try:
                result = batch_processor.process_batch(
                    batch_id,
                    only_filename=processing_file,
                    route_filename=file,
                    route_page=page,
                )
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
            if page_temp_path is not None:
                try:
                    page_temp_path.unlink(missing_ok=True)
                except OSError:
                    _LOG.warning("Could not remove temporary page file %s", page_temp_path)
        # Phase 2L — cross-vendor row normalisation: canonical Vendor
        # name from Vendor List.csv, sentence-case descriptions, and
        # dates parsed into ISO strings (the workbook writer turns
        # those into real Excel date cells). Run BEFORE the learned
        # corrections layer so an operator-saved override always wins
        # over the defaults.
        try:
            with perf_timer.perf_step("validation.row_normalizer", batch_id=batch_id):
                row_normalizer.normalize_result(result)
        except Exception:  # pragma: no cover
            _LOG.exception("row normalizer failed")
        # Phase 2K — apply learned-correction value overrides BEFORE
        # writing the cache so the preview reflects the user's curated
        # state on first paint (no flicker, no separate refresh).
        with perf_timer.perf_step("validation.learned_corrections", batch_id=batch_id):
            _apply_learned_corrections_to_result(result)
        if file and page is not None and page_temp_path is not None:
            result = _remap_single_page_result_sources(
                result,
                temp_filename=page_temp_path.name,
                source_filename=file,
                source_page=page,
            )
            result = (
                _merge_single_page_result(batch_id, file, page, result)
                if file_mode == "merge"
                else _mark_single_page_result(batch_id, file, page, result)
            )
        elif file:
            result = (
                _merge_single_file_result(batch_id, file, result)
                if file_mode == "merge"
                else _mark_single_file_result(batch_id, file, result)
            )
        from ..services import approved_invoice_corrections
        with perf_timer.perf_step("validation.approved_invoice_corrections", batch_id=batch_id):
            approved_invoice_corrections.apply_to_result(result, batch_id=batch_id)
        from ..services import human_adjudication
        with perf_timer.perf_step("validation.human_adjudication", batch_id=batch_id):
            human_adjudication.apply_to_result(result, batch_id=batch_id)
        cache_path = _result_cache_path(batch_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with perf_timer.perf_step("preview.cache_write", batch_id=batch_id):
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, default=str, indent=2)
        _invalidate_batch_summary_cache(batch_id)
        # Phase 2D — sync runs (used by tests + CLI-style smokes) also
        # produce a revision so the UI sees them.
        _record_revision_for_result(batch_id, result)
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
        return result

    # Phase 2D — submit to the cross-batch queue. Only one batch processes
    # at a time globally; subsequent submissions enter a FIFO queue and
    # start automatically when the running batch finishes. The legacy
    # per-batch `_RUNNING` dict is kept for compatibility (cancel paths
    # and tests still touch it), but the queue is the source of truth.
    perf_timer.record(batch_id, "queue.submitted", 0.0)
    perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
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


@router.get("/{batch_id}/performance")
def performance_endpoint(batch_id: str) -> dict:
    """Phase PERF-1 — return a step-by-step timing summary for the last
    batch run plus, if present, the OCR cache stats and any audit
    warnings (e.g. AI call > 8s, OCR > 5s).

    The summary is built from:
      * In-memory ``perf_timer`` state for the currently-running batch
        (so the operator can see a live breakdown without waiting for
        the run to finish).
      * The persisted ``audit/performance.json`` file written when the
        batch finishes.

    Never echoes invoice content or API keys — only step names, ms,
    counts, and small metadata fields the timer sanitises.
    """
    try:
        bdir = batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    from ..services import perf_timer  # local import keeps cold-load cheap

    # Live in-memory timings take priority — they reflect the active run.
    live_summary = perf_timer.summarize(batch_id)
    persisted: dict[str, Any] | None = None
    audit_dir = bdir / "audit"
    persisted_path = audit_dir / "performance.json"
    persisted_jsonl_path = audit_dir / "performance.jsonl"
    if persisted_path.is_file():
        try:
            with open(persisted_path, "r", encoding="utf-8") as f:
                persisted = json.load(f)
        except Exception:
            persisted = None

    out: dict[str, Any] = {
        "batch_id": batch_id,
        "live": live_summary if live_summary["step_count"] else None,
        "persisted": persisted,
        "events": perf_timer.read_jsonl(persisted_jsonl_path, limit=500),
        "paths": {
            "json": str(persisted_path) if persisted_path.is_file() else None,
            "jsonl": str(persisted_jsonl_path) if persisted_jsonl_path.is_file() else None,
        },
    }
    # OCR cache stats — useful to know how often the cache is hitting.
    try:
        from utils import ocr_cache  # type: ignore
        out["ocr_cache"] = ocr_cache.cache_stats()
    except Exception:
        out["ocr_cache"] = None
    return out


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
    with perf_timer.perf_step("preview.cache_read", batch_id=batch_id):
        with open(cache_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    try:
        with perf_timer.perf_step("preview.normalize", batch_id=batch_id):
            row_normalizer.normalize_result(result)
    except Exception:  # pragma: no cover - preview should stay available
        _LOG.exception("Could not normalize cached preview for %s", batch_id)

    with perf_timer.perf_step("preview.template_rules", batch_id=batch_id):
        rules = get_template_rules()
    columns = rules["columns"]
    with perf_timer.perf_step(
        "preview.build_rows",
        batch_id=batch_id,
        meta={"invoices": len(result.get("all_invoices", []) or [])},
    ):
        rows = _preview_rows_with_navigation(result, columns)
    from ..services import accounting_readiness
    readiness = accounting_readiness.evaluate_and_record(batch_id, rows)
    invoice_readiness: dict[str, dict] = {}
    invoice_groups: dict[str, list[dict]] = {}
    for row in rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        group_id = str(meta.get("invoice_group_id") or row.get("Invoice Number") or "unknown")
        invoice_groups.setdefault(group_id, []).append(row)
    for group_id, group_rows in invoice_groups.items():
        decision = accounting_readiness.evaluate_rows(group_rows)
        invoice_readiness[group_id] = accounting_readiness.as_dict(decision)
        for row in group_rows:
            meta = row.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta["readiness_snapshot_id"] = decision.snapshot_id
                meta["readiness_status"] = decision.status.value
    perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")

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
        "accounting_readiness": accounting_readiness.as_dict(readiness),
        "invoice_readiness": invoice_readiness,
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
        perf_timer.record(batch_id, "cancel.removed_from_queue", 0.0)
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
        return {
            "batch_id": batch_id,
            "status": "removed_from_queue",
            "message": "Removed from the processing queue.",
        }
    if q_result.get("result") == "cancel_requested":
        perf_timer.record(batch_id, "cancel.requested", 0.0, meta={"source": "queue"})
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
        return {
            "batch_id": batch_id,
            "status": "cancelling",
            "message": "Cancellation requested. Processing will stop at the next safe checkpoint.",
        }

    flagged = cancel_registry.request_cancel(batch_id)
    if not flagged:
        perf_timer.record(batch_id, "cancel.no_active_run", 0.0)
        perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
        return {
            "batch_id": batch_id,
            "status": "no_active_run",
            "message": "No active processing thread for this batch.",
        }
    perf_timer.record(batch_id, "cancel.requested", 0.0, meta={"source": "registry"})
    perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
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
    with perf_timer.perf_step("manual_review.cache_read", batch_id=batch_id):
        with open(cache_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    try:
        with perf_timer.perf_step("manual_review.normalize", batch_id=batch_id):
            row_normalizer.normalize_result(result)
    except Exception:  # pragma: no cover - review should stay available
        _LOG.exception("Could not normalize cached manual review for %s", batch_id)
    with perf_timer.perf_step("manual_review.build_items", batch_id=batch_id):
        items = list(result.get("all_manual_review") or [])
        items.extend(_row_contract_manual_review_items(result))
    perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
    return {"batch_id": batch_id, "items": items}


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
    with perf_timer.perf_step("revision.list", batch_id=batch_id):
        items = revisions_service.list_revisions(batch_id)
        current_revision_id = revisions_service.current_revision_id(batch_id)
    perf_timer.flush_to_disk(batch_id, batch_store.get_batch_dir(batch_id) / "audit")
    return {
        "batch_id": batch_id,
        "current_revision_id": current_revision_id,
        "revisions": items,
    }


@router.get("/{batch_id}/activity")
def list_activity_endpoint(batch_id: str, invoice_group_id: str | None = None, limit: int = 500) -> dict:
    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    from ..services import operator_activity_log

    events = operator_activity_log.list_events(
        batch_id=batch_id, invoice_group_id=invoice_group_id, limit=limit,
    )
    return {
        "contract_version": operator_activity_log.ACTIVITY_CONTRACT_VERSION,
        "items": [event.model_dump(mode="json") for event in events],
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
    adjudication: human_adjudication.AdjudicationOptions | None = None


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
    audit_changes: list[dict[str, object]] = []
    adjudication_report: human_adjudication.AdjudicationApplyReport | None = None
    if body.adjudication is not None:
        try:
            actor = human_adjudication.runtime_actor_context()
            adjudication_report = human_adjudication.record_manual_edits(
                result=result,
                batch_id=batch_id,
                edits_by_index=edits_by_idx,
                options=body.adjudication,
                actor=actor,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        applied = adjudication_report.recorded
        revisions_by_id = {
            item.revision_id: item
            for item in human_adjudication.list_revisions(actor.tenant_id, batch_id=batch_id)
        }
        for revision_id in adjudication_report.revision_ids[:250]:
            revision = revisions_by_id.get(revision_id)
            if revision is None:
                continue
            audit_changes.append({
                "row_index": revision.global_row_index,
                "invoice_group_id": revision.invoice_group_id,
                "field": revision.field,
                "before": revision.previous_value,
                "after": revision.corrected_value,
                "adjudication_revision_id": revision.revision_id,
            })
    else:
        # Backward-compatible adapter for older clients. The current Invoice
        # Processor always sends the adjudication contract; legacy callers
        # retain invoice-only save behavior until migrated.
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
                        if col == "GL Account":
                            meta = row.setdefault("_meta", {})
                            if isinstance(meta, dict):
                                meta["approved_operator_gl_candidate"] = str(val or "").strip()
                        before = row.get(col)
                        row[col] = val
                        applied += 1
                        if len(audit_changes) < 250:
                            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
                            audit_changes.append({
                                "row_index": flat_index,
                                "invoice_group_id": str(meta.get("invoice_group_id") or "") or None,
                                "field": col,
                                "before": before if isinstance(before, (str, int, float, bool)) or before is None else str(before),
                                "after": val if isinstance(val, (str, int, float, bool)) or val is None else str(val),
                            })
                flat_index += 1

    # Any persisted edit can change semantic context or invalidate a previous
    # GL decision. Re-run only the accounting bridge over existing extracted
    # facts; this does not repeat OCR/Vision extraction and only the bounded
    # unknown-semantics gateway may call AI.
    if applied and body.adjudication is None:
        from ..services.accounting_integration_bridges import RowAccountingV2Adapter

        for invoice_index, inv in enumerate(result.get("all_invoices", []) or []):
            rows = inv.get("rows") or []
            if not rows:
                continue
            source_file = str(inv.get("source_file") or ((rows[0].get("_meta") or {}).get("source_file")
                              if isinstance(rows[0].get("_meta"), dict) else "") or batch_id)
            RowAccountingV2Adapter().enrich_rows(rows, {
                "document_id": source_file,
                "extraction_route": "operator_edit_accounting_refresh",
            })

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

    if applied:
        from ..services import operator_activity_log
        operator_activity_log.record(
            batch_id=batch_id,
            event_type=("human_adjudication_saved" if adjudication_report else "manual_edits_saved"),
            source="manual",
            actor=(actor.reviewer_id if adjudication_report else "local_operator"),
            summary=(
                f"Saved {applied} evidence-backed human adjudication"
                f"{'s' if applied != 1 else ''}."
                if adjudication_report else
                f"Saved {applied} manual cell change{'s' if applied != 1 else ''}."
            ),
            details={
                "changes": audit_changes,
                "skipped": skipped,
                "adjudication": (
                    adjudication_report.model_dump(mode="json")
                    if adjudication_report else None
                ),
            },
        )
        if adjudication_report:
            scoped_events = (
                ("benchmark_submission", "manual", adjudication_report.benchmark_submissions,
                 "Submitted human corrections for benchmark approval."),
                ("learning_examples_approved", "manual", adjudication_report.learning_approvals,
                 "Approved tenant-private learning examples."),
                ("reusable_rule_proposals_created", "rule", adjudication_report.rule_proposals,
                 "Created reusable accounting rule proposals; none were activated."),
            )
            for event_type, source, count, summary in scoped_events:
                if not count:
                    continue
                operator_activity_log.record(
                    batch_id=batch_id,
                    event_type=event_type,
                    source=source,
                    actor=actor.reviewer_id,
                    summary=summary,
                    details={
                        "count": count,
                        "revision_ids": adjudication_report.revision_ids,
                        "tenant_id": actor.tenant_id,
                    },
                )

    return {
        "batch_id": batch_id,
        "applied": applied,
        "skipped": skipped,
        "current_revision_id": current,
        "accounting_refreshed": bool(applied),
        "adjudication": (
            adjudication_report.model_dump(mode="json")
            if adjudication_report else None
        ),
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
