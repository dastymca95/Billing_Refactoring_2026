"""Batch lifecycle endpoints: create (with optional name), list, status,
list-files, rename, delete. Each batch carries a small `batch_metadata.json`
sidecar in its root directory so the frontend can display human-friendly
names + summary stats without re-reading the result cache."""

from __future__ import annotations

import json
import hashlib
import re
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import batch_store
from ..services import document_ingestion
from ..services.document_preview import pdf_page_count
from ..services.vendor_detection import detect_vendor_for_file


router = APIRouter(prefix="/api/batches", tags=["batches"])


# ---------------------------------------------------------------------------
# Phase 1H — batch document mode + AI fallback fields
# Allowed values are mirrored on the frontend (src/types.ts:DocumentMode etc).
# ---------------------------------------------------------------------------
DOCUMENT_MODES = (
    "digital_pdf", "scanned_pdf", "screenshot_image", "mixed_pdf", "csv_excel", "auto_detect",
)
AI_FALLBACK_POLICIES = (
    "never", "only_low_confidence", "only_manual_review", "always_assist",
)
DEFAULT_DOCUMENT_MODE = "auto_detect"
DEFAULT_AI_FALLBACK_POLICY = "only_low_confidence"
DEFAULT_UNTITLED_BATCH_NAME = "Untitled batch"
DETECTION_CACHE_VERSION = 3
SUMMARY_CACHE_VERSION = 1
LIST_CACHE_WARM_LIMIT = 64
_SUMMARY_CACHE: dict[str, tuple[tuple, dict]] = {}


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


def _display_batch_name(meta: dict) -> str:
    return str(meta.get("batch_name") or "").strip() or DEFAULT_UNTITLED_BATCH_NAME


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _batch_summary_signature(batch_id: str, files: list[Path]) -> tuple:
    bdir = batch_store.get_batch_dir(batch_id)
    processed_dir = batch_store.get_processed_dir(batch_id)
    export_dir = batch_store.get_export_dir(batch_id)
    file_parts = []
    for p in files:
        sig = _stat_sig(p)
        if sig is not None:
            file_parts.append((p.name, sig[0], sig[1]))
    export_parts = []
    for p in sorted(export_dir.glob("*resman_import*.xlsx")):
        sig = _stat_sig(p)
        if sig is not None:
            export_parts.append((p.name, sig[0], sig[1]))
    return (
        SUMMARY_CACHE_VERSION,
        int(bdir.stat().st_ctime_ns),
        tuple(file_parts),
        _stat_sig(processed_dir / "_webapp_result.json"),
        tuple(export_parts),
    )


def _summary_signature_token(signature: tuple) -> str:
    raw = json.dumps(signature, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _write_summary_cache(batch_id: str, live: dict, signature: tuple | None = None) -> None:
    payload = {
        "version": SUMMARY_CACHE_VERSION,
        "signature_token": _summary_signature_token(signature) if signature is not None else None,
        "files_count": live.get("files_count", 0),
        "invoices_count": live.get("invoices_count", 0),
        "rows_count": live.get("rows_count", 0),
        "manual_review_count": live.get("manual_review_count", 0),
        "export_available": live.get("export_available", False),
        "last_export_file": live.get("last_export_file"),
        "export_filenames": live.get("export_filenames") or [],
        "supported_vendor_summary": live.get("supported_vendor_summary") or {},
        "summary": live.get("summary") or {},
        "preview_available": live.get("preview_available", False),
        "created_at": live.get("created_at"),
    }
    try:
        p = _metadata_path(batch_id)
        meta = _read_metadata(batch_id)
        if meta.get("summary_cache") == payload:
            return
        meta["summary_cache"] = payload
        p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 1U — cached vendor detection.
#
# Vendor detection opens each PDF with pdfplumber to sample text. For a
# 10-file batch that's ~1–3 s of disk + parser work on EVERY GET
# /api/batches/<id>. The result is deterministic for an unchanged file
# (same name + size + mtime), so we cache it inside batch_metadata.json
# under a `file_detection_cache` key. The cache is keyed by filename and
# invalidated whenever the file's size or mtime changes, or when the
# detector version changes after a routing bug fix.
# ---------------------------------------------------------------------------
def _detect_files_cached(batch_id: str, files: list) -> list[dict]:
    """Return file entries with vendor detection populated.

    Uses the cached detection in `batch_metadata.json` when the file
    matches the cached `(size, mtime)` tuple; falls back to running
    detect_vendor_for_file and persisting the result. Persistence is
    best-effort — a write failure simply means the next call re-detects.
    """
    meta = _read_metadata(batch_id)
    cache = meta.get("file_detection_cache")
    if not isinstance(cache, dict):
        cache = {}
    fresh_cache: dict[str, dict] = {}
    cache_dirty = False
    entries: list[dict] = []
    for p in files:
        try:
            stat = p.stat()
        except FileNotFoundError:
            continue
        size_bytes = stat.st_size
        mtime = int(stat.st_mtime)
        cache_key = p.name
        cached = cache.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("size_bytes") == size_bytes
            and cached.get("mtime") == mtime
            and cached.get("detector_version") == DETECTION_CACHE_VERSION
            and "vendor_key" in cached
        ):
            det = {
                "vendor_key": cached["vendor_key"],
                "confidence": cached.get("confidence", 0.0),
                "reason": cached.get("reason", ""),
                "supported_in_phase_1": cached.get("supported_in_phase_1", False),
            }
            support = {
                "source_type": cached.get("source_type") or "unknown",
                "file_support_status": cached.get("file_support_status") or "supported",
                "file_support_label": cached.get("file_support_label") or "",
                "file_support_reason": cached.get("file_support_reason") or "",
            }
            page_count = cached.get("page_count")
            if p.suffix.lower() == ".pdf" and page_count is None:
                page_count = pdf_page_count(p)
                cache_dirty = True
        else:
            det = detect_vendor_for_file(p)
            support = document_ingestion.detect_file_support(p)
            page_count = pdf_page_count(p)
            cache_dirty = True
        fresh_cache[cache_key] = {
            "size_bytes": size_bytes,
            "mtime": mtime,
            "vendor_key": det["vendor_key"],
            "confidence": det["confidence"],
            "reason": det["reason"],
            "supported_in_phase_1": det["supported_in_phase_1"],
            "detector_version": DETECTION_CACHE_VERSION,
            "source_type": support.get("source_type"),
            "file_support_status": support.get("file_support_status"),
            "file_support_label": support.get("file_support_label"),
            "file_support_reason": support.get("file_support_reason"),
        }
        if page_count is not None:
            fresh_cache[cache_key]["page_count"] = page_count
        entry = {
            "filename": p.name,
            "size_bytes": size_bytes,
            "extension": p.suffix.lower(),
            "vendor_key": det["vendor_key"],
            "vendor_confidence": det["confidence"],
            "vendor_detection_reason": det["reason"],
            "supported_in_phase_1": det["supported_in_phase_1"],
            "source_type": support.get("source_type"),
            "file_support_status": support.get("file_support_status"),
            "file_support_label": support.get("file_support_label"),
            "file_support_reason": support.get("file_support_reason"),
        }
        if page_count is not None:
            entry["page_count"] = page_count
        entries.append(entry)

    # Detect deletions: if any cached filename is no longer present in
    # `files`, the cache shrunk and we need to persist the smaller view.
    if set(fresh_cache.keys()) != set(cache.keys()):
        cache_dirty = True

    if cache_dirty:
        try:
            _write_metadata(batch_id, file_detection_cache=fresh_cache)
        except Exception:
            # Persistence is non-fatal; we still return the entries we
            # computed for this request.
            pass
    return entries


def _source_type_from_suffix(suffix: str) -> tuple[str, str, str, str]:
    suffix = suffix.lower()
    if suffix == ".pdf":
        return (
            "pdf_pending",
            "pending",
            "PDF",
            "Uploaded PDF; detailed detection runs outside the upload path.",
        )
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}:
        return (
            "image",
            "limited",
            "Image",
            "Uploaded image; OCR or vision may be recommended during processing.",
        )
    if suffix in {".xlsx", ".xls"}:
        return ("excel", "supported", "Excel", "Spreadsheet uploaded.")
    if suffix == ".csv":
        return ("csv", "supported", "CSV", "CSV file uploaded.")
    if suffix in {".docx", ".doc"}:
        return ("word", "limited", "Word", "Word document uploaded.")
    return ("unknown", "unsupported", "Unsupported", "This file type is not supported.")


def _list_files_light(
    batch_id: str,
    files: list[Path],
    *,
    detect_support: bool = False,
    include_pdf_page_count: bool = False,
) -> list[dict]:
    """Latency-safe file list for upload UI refreshes.

    This must not open PDFs or run vendor detectors. It may reuse cached
    metadata from a prior full detection, but new uploads are returned as
    immediately visible "pending" entries.
    """
    meta = _read_metadata(batch_id)
    cache = meta.get("file_detection_cache")
    if not isinstance(cache, dict):
        cache = {}

    entries: list[dict] = []
    cache_dirty = False
    for p in files:
        try:
            stat = p.stat()
        except FileNotFoundError:
            continue
        size_bytes = stat.st_size
        mtime = int(stat.st_mtime)
        cached = cache.get(p.name)
        cached_valid = (
            isinstance(cached, dict)
            and cached.get("size_bytes") == size_bytes
            and cached.get("mtime") == mtime
            and cached.get("detector_version") == DETECTION_CACHE_VERSION
        )
        page_count = cached.get("page_count") if cached_valid else None
        counted_pdf_pages = False
        source_type, support_status, support_label, support_reason = _source_type_from_suffix(
            p.suffix
        )
        if detect_support and not cached_valid:
            try:
                support = document_ingestion.detect_file_support(p)
                source_type = support.get("source_type") or source_type
                support_status = support.get("file_support_status") or support_status
                support_label = support.get("file_support_label") or support_label
                support_reason = support.get("file_support_reason") or support_reason
            except Exception:
                pass
        if p.suffix.lower() == ".pdf" and page_count is None and (
            detect_support or include_pdf_page_count
        ):
            try:
                page_count = pdf_page_count(p)
                counted_pdf_pages = page_count is not None
            except Exception:
                page_count = None
        if counted_pdf_pages:
            cached_payload = dict(cached) if isinstance(cached, dict) else {}
            cached_payload.update(
                {
                    "size_bytes": size_bytes,
                    "mtime": mtime,
                    "detector_version": DETECTION_CACHE_VERSION,
                    "page_count": page_count,
                }
            )
            cache[p.name] = cached_payload
            cache_dirty = True
        entry = {
            "filename": p.name,
            "size_bytes": size_bytes,
            "extension": p.suffix.lower(),
            "vendor_key": "unknown",
            "vendor_confidence": 0.0,
            "vendor_detection_reason": "Detection pending after upload.",
            "supported_in_phase_1": False,
            "source_type": source_type,
            "file_support_status": support_status,
            "file_support_label": support_label,
            "file_support_reason": support_reason,
        }
        if page_count is not None:
            entry["page_count"] = page_count
        if cached_valid:
            entry.update(
                {
                    "vendor_key": cached.get("vendor_key", "unknown"),
                    "vendor_confidence": cached.get("confidence", 0.0),
                    "vendor_detection_reason": cached.get("reason", ""),
                    "supported_in_phase_1": cached.get("supported_in_phase_1", False),
                    "source_type": cached.get("source_type") or source_type,
                    "file_support_status": cached.get("file_support_status") or support_status,
                    "file_support_label": cached.get("file_support_label") or support_label,
                    "file_support_reason": cached.get("file_support_reason") or support_reason,
                }
            )
        entries.append(entry)
    if cache_dirty:
        try:
            _write_metadata(batch_id, file_detection_cache=cache)
        except Exception:
            pass
    return entries


def _summary_for_batch(batch_id: str) -> dict:
    """Inspect the batch folder + cached result + export folder to
    compute the live counts the metadata sidecar may be missing."""
    bdir = batch_store.get_batch_dir(batch_id)
    files = batch_store.list_files_in_batch(batch_id)
    signature = _batch_summary_signature(batch_id, files)
    cached_entry = _SUMMARY_CACHE.get(batch_id)
    if cached_entry and cached_entry[0] == signature:
        return deepcopy(cached_entry[1])
    signature_token = _summary_signature_token(signature)
    meta = _read_metadata(batch_id)
    persistent_cache = meta.get("summary_cache")
    if (
        isinstance(persistent_cache, dict)
        and persistent_cache.get("version") == SUMMARY_CACHE_VERSION
        and persistent_cache.get("signature_token") == signature_token
    ):
        live = {
            "files_count": int(persistent_cache.get("files_count") or 0),
            "invoices_count": int(persistent_cache.get("invoices_count") or 0),
            "rows_count": int(persistent_cache.get("rows_count") or 0),
            "manual_review_count": int(persistent_cache.get("manual_review_count") or 0),
            "export_available": bool(persistent_cache.get("export_available")),
            "last_export_file": persistent_cache.get("last_export_file"),
            "supported_vendor_summary": persistent_cache.get("supported_vendor_summary") or {},
            "summary": persistent_cache.get("summary") or {},
            "preview_available": bool(persistent_cache.get("preview_available")),
            "created_at": persistent_cache.get("created_at")
            or datetime.fromtimestamp(bdir.stat().st_ctime).isoformat(timespec="seconds"),
            "export_filenames": persistent_cache.get("export_filenames") or [],
        }
        _SUMMARY_CACHE[batch_id] = (signature, deepcopy(live))
        return deepcopy(live)

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
    export_files = sorted(
        export_dir.glob("*resman_import*.xlsx"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    live = {
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
    _SUMMARY_CACHE[batch_id] = (signature, deepcopy(live))
    _write_summary_cache(batch_id, live, signature=signature)
    return live


def _summary_for_batch_light(batch_id: str, meta: dict) -> dict:
    """Fast list-view summary.

    The batch dropdown is opened constantly, so it must not parse every
    historical `_webapp_result.json`. Exact counts are refreshed by
    `GET /api/batches/{id}` and stored as `summary_cache`; list view can
    safely use the last known values plus live file/export existence.
    """
    bdir = batch_store.get_batch_dir(batch_id)
    files_count = len(batch_store.list_files_in_batch(batch_id))
    processed_dir = batch_store.get_processed_dir(batch_id)
    cache_path = processed_dir / "_webapp_result.json"
    export_dir = batch_store.get_export_dir(batch_id)
    export_files = sorted(
        export_dir.glob("*resman_import*.xlsx"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    cached = meta.get("summary_cache") if isinstance(meta.get("summary_cache"), dict) else {}
    return {
        "files_count": files_count,
        "invoices_count": int(cached.get("invoices_count") or 0),
        "rows_count": int(cached.get("rows_count") or 0),
        "manual_review_count": int(cached.get("manual_review_count") or 0),
        "export_available": bool(export_files) or bool(cached.get("export_available")),
        "last_export_file": export_files[-1].name if export_files else cached.get("last_export_file"),
        "supported_vendor_summary": cached.get("supported_vendor_summary") or {},
        "preview_available": cache_path.is_file(),
        "created_at": datetime.fromtimestamp(bdir.stat().st_ctime).isoformat(timespec="seconds"),
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
    # Phase 2C — display name for the export workbook ("Richmond_3.xlsx").
    # Optional; if not set the UI falls back to a generated default.
    export_name: Optional[str] = None


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


# Phase 2C — characters allowed in an export display name. We strip
# anything path-traversal-flavored ('/', '\\', '..') and replace it with
# '_' so the operator can paste loose text without breaking downloads.
_EXPORT_NAME_ILLEGAL = re.compile(r'[\\/:\*\?"<>\|]+')


def _sanitize_export_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        raise HTTPException(status_code=400, detail="export_name cannot be empty")
    # Strip path components defensively.
    s = Path(s).name
    s = _EXPORT_NAME_ILLEGAL.sub("_", s)
    s = s.strip(". ")
    if not s:
        raise HTTPException(
            status_code=400,
            detail="export_name contains only invalid characters",
        )
    if len(s) > 120:
        raise HTTPException(status_code=400, detail="export_name too long (max 120)")
    # Ensure .xlsx so downloads are valid Excel files. If the operator
    # typed another extension, replace it.
    stem = Path(s).stem or s
    return f"{stem}.xlsx"


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
    supplied_name = (body.batch_name.strip() if body and body.batch_name else "")
    if len(supplied_name) > 200:
        raise HTTPException(status_code=400, detail="batch_name too long (max 200)")
    name = supplied_name or f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}"
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
    entries = batch_store.list_batches()

    def build(index_entry: tuple[int, dict]) -> dict | None:
        index, entry = index_entry
        bid = entry["batch_id"]
        try:
            meta = _read_metadata(bid)
            summary_cache = meta.get("summary_cache")
            has_valid_summary_cache = (
                isinstance(summary_cache, dict)
                and summary_cache.get("version") == SUMMARY_CACHE_VERSION
            )
            result_cache = batch_store.get_processed_dir(bid) / "_webapp_result.json"
            live = (
                _summary_for_batch_light(bid, meta)
                if has_valid_summary_cache
                or not result_cache.is_file()
                or index >= LIST_CACHE_WARM_LIMIT
                else _summary_for_batch(bid)
            )
        except FileNotFoundError:
            return None
        return {
            "batch_id": bid,
            "batch_name": _display_batch_name(meta),
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
        }

    if len(entries) > 24:
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = [item for item in pool.map(build, enumerate(entries)) if item is not None]
    else:
        out = [item for item in (build(item) for item in enumerate(entries)) if item is not None]
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
    if body.export_name is not None:
        fields["export_name"] = _sanitize_export_name(body.export_name)
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
    # Keep restore/switching latency-safe. Full vendor detection stays out of
    # the navigation/upload path; cached results are reused when available.
    file_entries = _list_files_light(batch_id, files, include_pdf_page_count=False)

    meta = _read_metadata(batch_id)
    live = _summary_for_batch(batch_id)

    return {
        "batch_id": batch_id,
        "batch_name": _display_batch_name(meta),
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
def list_files_endpoint(
    batch_id: str,
    detail: Literal["light", "full"] = Query("light"),
) -> dict:
    try:
        files = batch_store.list_files_in_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    # Phase PERF-1 hotfix — `listFiles` is on the upload critical path
    # and must NEVER trigger a fresh image-OCR pass (the Weakley image
    # detector alone takes 10-50 seconds on a screenshot). Fast-mode
    # skips heavy OCR; if the cache is empty the file shows as
    # vendor=unknown and the real detection runs at process time.
    if detail == "light":
        return {
            "batch_id": batch_id,
            "files": _list_files_light(batch_id, files, detect_support=False),
        }
    from ..services.vendor_detection import fast_detection_context
    with fast_detection_context():
        out = _detect_files_cached(batch_id, files)
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
    try:
        batch_store.delete_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    return {"batch_id": batch_id, "deleted": True}
