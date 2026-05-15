"""File preview + ResMan preview + manual review endpoints."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services import batch_store
from ..services import document_ingestion
from ..services.document_preview import preview_file


router = APIRouter(prefix="/api/batches", tags=["preview"])


# Map of extensions we want to render inline in the browser. Anything else
# falls through to FileResponse's auto-detected type with a default
# Content-Disposition that the browser may or may not render inline.
INLINE_PREVIEW_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}


def _resolve_input_file(batch_id: str, filename: str) -> Path:
    """Look up `<batch>/input/<filename>`, defending against path traversal.

    `filename` is reduced to its bare basename via `Path(...).name` so the
    final path is always inside the batch's input folder. We also resolve
    both sides and assert containment as a belt-and-suspenders check."""
    try:
        in_dir = batch_store.get_input_dir(batch_id).resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    target = (in_dir / safe_name).resolve()
    try:
        target.relative_to(in_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal blocked")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    return target


def _file_response(target: Path, *, inline: bool) -> FileResponse:
    """Build a FileResponse with explicit Content-Type and the requested
    Content-Disposition. `inline=True` tells the browser to render in-page
    (used by the document preview); `inline=False` is for downloads."""
    ext = target.suffix.lower()
    media_type = INLINE_PREVIEW_TYPES.get(ext) or mimetypes.guess_type(target.name)[0] \
        or "application/octet-stream"
    disposition_type = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f'{disposition_type}; filename="{target.name}"',
        # Defense-in-depth: stop browsers from re-sniffing PDFs into HTML etc.
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(target, media_type=media_type, headers=headers)


@router.get("/{batch_id}/files/{filename}/preview")
def file_preview_endpoint(batch_id: str, filename: str) -> dict:
    target = _resolve_input_file(batch_id, filename)
    return preview_file(target)


@router.get("/{batch_id}/files/{filename}/ingestion-preview")
def file_ingestion_preview_endpoint(batch_id: str, filename: str) -> dict:
    """Inspect normalized ingestion metadata without processing the invoice.

    This endpoint is intentionally diagnostic-only: no AI calls, no Dropbox,
    no output writes, and no full document dump. It lets the frontend and QA
    see whether a file looks like a digital PDF, scanned PDF, image,
    spreadsheet, Word document, or unsupported source before it reaches the
    reasoner.
    """
    target = _resolve_input_file(batch_id, filename)
    candidate = document_ingestion.ingest_document(target, max_pages=3)
    text = candidate.document_text or ""
    tables_preview = []
    for table in candidate.tables[:3]:
        tables_preview.append(
            {
                "source": table.source,
                "sheet_name": table.sheet_name,
                "page_number": table.page_number,
                "table_index": table.table_index,
                "headers": table.headers or table.columns,
                "rows": table.rows[:8],
                "confidence": table.confidence,
                "warnings": table.warnings,
            }
        )
    return {
        "batch_id": batch_id,
        "filename": target.name,
        "source_type": candidate.source_type,
        "mime_type": candidate.mime_type,
        "file_size_bytes": candidate.file_size_bytes,
        "page_count": candidate.page_count,
        "sheet_count": candidate.sheet_count,
        "table_count": len(candidate.tables),
        "text_quality_score": candidate.text_quality_score,
        "extraction_quality": candidate.extraction_quality.get("label"),
        "needs_ocr": candidate.needs_ocr,
        "needs_vision": candidate.needs_vision,
        "warnings": candidate.warnings[:20],
        "vendor_hint": candidate.vendor_hint,
        "category_hint": candidate.category_hint,
        "text_preview": text[:1400],
        "tables_preview": tables_preview,
    }


@router.get("/{batch_id}/files/{filename}/raw")
def file_raw_endpoint(batch_id: str, filename: str):
    """Legacy raw endpoint (defaults to inline so existing <embed src=...>
    tags keep working; older clients that pass `?inline=0` get a download)."""
    target = _resolve_input_file(batch_id, filename)
    return _file_response(target, inline=True)


@router.get("/{batch_id}/files/{filename}/content")
def file_content_endpoint(batch_id: str, filename: str):
    """Stream a batch input file with `Content-Disposition: inline` and the
    correct Content-Type so the browser renders PDFs / images directly in an
    `<iframe>` / `<embed>` instead of triggering a download.

    Path-traversal-safe: `filename` is reduced to its basename before being
    joined to the batch's input directory, and the resolved target is
    asserted to live inside that directory.
    """
    target = _resolve_input_file(batch_id, filename)
    return _file_response(target, inline=True)
