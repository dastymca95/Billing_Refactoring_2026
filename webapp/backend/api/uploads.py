"""File-upload endpoint."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ..services import batch_store
from ..services.document_preview import pdf_page_count
from ..settings import ALLOWED_UPLOAD_EXTENSIONS


router = APIRouter(prefix="/api/batches", tags=["uploads"])

APPENDABLE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
COMBINED_PDF_CACHE_MAX_ITEMS = 8
UPLOAD_DETECTION_CACHE_VERSION = 3
_upload_metadata_lock = threading.Lock()
_combined_pdf_cache: OrderedDict[
    tuple[str, tuple[tuple[str, int, int], ...]], bytes
] = OrderedDict()


def _resolve_input_file(batch_id: str, filename: str) -> Path:
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


def _copy_upload_file_to_path(upload_file, dest: Path) -> None:
    try:
        upload_file.seek(0)
    except Exception:
        pass
    with open(dest, "wb") as out:
        shutil.copyfileobj(upload_file, out)


def _source_type_from_suffix(suffix: str) -> tuple[str, str, str, str]:
    suffix = suffix.lower()
    if suffix == ".pdf":
        return (
            "pdf_pending",
            "pending",
            "PDF",
            "Uploaded PDF; detailed detection runs outside the upload path.",
        )
    if suffix in APPENDABLE_IMAGE_EXTENSIONS or suffix in {".tif", ".tiff"}:
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


def _remember_uploaded_file_metadata(
    batch_id: str,
    dest: Path,
    *,
    page_count: int | None,
) -> None:
    """Persist cheap upload metadata so refreshes do not lose page counts.

    The viewer depends on accurate page totals for multi-document batches.
    Uploads run concurrently, so writes to batch_metadata.json are serialized
    in-process to avoid dropping cache entries under large drag/drop uploads.
    """

    try:
        batch_dir = batch_store.get_batch_dir(batch_id)
        meta_path = batch_dir / "batch_metadata.json"
        stat = dest.stat()
    except Exception:
        return

    source_type, support_status, support_label, support_reason = _source_type_from_suffix(
        dest.suffix
    )
    with _upload_metadata_lock:
        try:
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                meta = {}
        except Exception:
            meta = {}
        cache = meta.get("file_detection_cache")
        if not isinstance(cache, dict):
            cache = {}
        payload = dict(cache.get(dest.name)) if isinstance(cache.get(dest.name), dict) else {}
        payload.update(
            {
                "size_bytes": stat.st_size,
                "mtime": int(stat.st_mtime),
                "vendor_key": payload.get("vendor_key", "unknown"),
                "confidence": payload.get("confidence", 0.0),
                "reason": payload.get("reason", "Detection pending after upload."),
                "supported_in_phase_1": payload.get("supported_in_phase_1", False),
                "detector_version": UPLOAD_DETECTION_CACHE_VERSION,
                "source_type": payload.get("source_type") or source_type,
                "file_support_status": payload.get("file_support_status") or support_status,
                "file_support_label": payload.get("file_support_label") or support_label,
                "file_support_reason": payload.get("file_support_reason") or support_reason,
            }
        )
        if page_count is not None:
            payload["page_count"] = int(page_count)
        cache[dest.name] = payload
        meta["file_detection_cache"] = cache
        meta.setdefault("batch_id", batch_id)
        meta.setdefault(
            "created_at",
            datetime.fromtimestamp(batch_dir.stat().st_ctime).isoformat(timespec="seconds"),
        )
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass


def _unique_sibling_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _image_bytes_to_pdf_bytes(data: bytes) -> tuple[bytes, int]:
    try:
        from PIL import Image, ImageSequence  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Image-to-PDF support is not available on this backend.",
        )

    try:
        with Image.open(BytesIO(data)) as image:
            frames = [
                frame.convert("RGB").copy()
                for frame in ImageSequence.Iterator(image)
            ]
        if not frames:
            raise ValueError("Image has no frames")
        out = BytesIO()
        first, rest = frames[0], frames[1:]
        first.save(out, format="PDF", save_all=bool(rest), append_images=rest)
        return out.getvalue(), len(frames)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not convert image to PDF page: {type(exc).__name__}",
        )


def _image_to_pdf_reader(data: bytes):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(
            status_code=500, detail="PDF support is not available on this backend."
        )
    pdf_bytes, _page_count = _image_bytes_to_pdf_bytes(data)
    return PdfReader(BytesIO(pdf_bytes))


@router.post("/{batch_id}/upload")
async def upload_file_endpoint(
    batch_id: str,
    file: UploadFile = File(...),
    as_pdf: bool = Query(False),
) -> dict:
    try:
        in_dir = batch_store.get_input_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension: {suffix}. Allowed: {sorted(ALLOWED_UPLOAD_EXTENSIONS)}",
        )

    safe_name = Path(file.filename or "uploaded").name  # strip any path components
    convert_image_to_pdf = as_pdf and suffix in APPENDABLE_IMAGE_EXTENSIONS
    if convert_image_to_pdf:
        safe_name = f"{Path(safe_name).stem or 'uploaded'}.pdf"
        suffix = ".pdf"

    dest = in_dir / safe_name
    # If a file with the same name was uploaded before, suffix " (n)" to keep both.
    if dest.exists():
        stem, ext = dest.stem, dest.suffix
        n = 1
        while True:
            candidate = in_dir / f"{stem} ({n}){ext}"
            if not candidate.exists():
                dest = candidate
                break
            n += 1

    page_count: int | None = None
    if convert_image_to_pdf:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        pdf_bytes, page_count = await asyncio.to_thread(_image_bytes_to_pdf_bytes, data)
        await asyncio.to_thread(dest.write_bytes, pdf_bytes)
    else:
        await asyncio.to_thread(_copy_upload_file_to_path, file.file, dest)
        if dest.suffix.lower() == ".pdf":
            try:
                page_count = await asyncio.to_thread(pdf_page_count, dest)
            except Exception:
                page_count = None

    await asyncio.to_thread(
        _remember_uploaded_file_metadata,
        batch_id,
        dest,
        page_count=page_count,
    )

    return {
        "batch_id": batch_id,
        "filename": dest.name,
        "size_bytes": dest.stat().st_size,
        "extension": dest.suffix.lower(),
        "page_count": page_count,
        "converted_from": Path(file.filename or "uploaded").suffix.lower()
        if convert_image_to_pdf
        else "",
    }


@router.post("/{batch_id}/files/{filename}/append")
async def append_file_to_pdf_endpoint(
    batch_id: str,
    filename: str,
    file: UploadFile = File(...),
) -> dict:
    """Append a dropped/pasted document into the currently open document.

    This is intentionally different from `/upload`: the operator used the
    viewer's page rail, so the result should become more pages in that same
    visible document instead of another top-level file in the batch. If the
    open document is a screenshot/image, it is promoted to a PDF first so all
    document types converge on the same paginated viewer.
    """
    parent = _resolve_input_file(batch_id, filename)
    parent_suffix = parent.suffix.lower()
    if parent_suffix not in {".pdf", *APPENDABLE_IMAGE_EXTENSIONS}:
        raise HTTPException(
            status_code=415,
            detail="Documents can only be appended to an open PDF or screenshot/image.",
        )

    suffix = Path(file.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension: {suffix}. Allowed: {sorted(ALLOWED_UPLOAD_EXTENSIONS)}",
        )
    if suffix not in {".pdf", *APPENDABLE_IMAGE_EXTENSIONS}:
        raise HTTPException(
            status_code=415,
            detail="Only PDFs and screenshots/images can be appended as pages.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PDF append support is not available on this backend.",
        )

    try:
        if parent_suffix == ".pdf":
            parent_reader = PdfReader(BytesIO(parent.read_bytes()))
            output = parent
        else:
            parent_reader = _image_to_pdf_reader(parent.read_bytes())
            output = _unique_sibling_path(parent.with_suffix(".pdf"))

        appended_reader = (
            PdfReader(BytesIO(data))
            if suffix == ".pdf"
            else _image_to_pdf_reader(data)
        )
        if len(appended_reader.pages) == 0:
            raise HTTPException(status_code=400, detail="Uploaded PDF has no pages.")

        writer = PdfWriter()
        for page in parent_reader.pages:
            writer.add_page(page)
        appended_pages = 0
        for page in appended_reader.pages:
            writer.add_page(page)
            appended_pages += 1

        tmp = output.with_name(f".{output.name}.append.tmp")
        with open(tmp, "wb") as out:
            writer.write(out)
        tmp.replace(output)
        if output != parent:
            try:
                parent.unlink()
            except OSError:
                pass
        _remember_uploaded_file_metadata(batch_id, output, page_count=len(writer.pages))
        _combined_pdf_cache.clear()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not append document: {type(exc).__name__}",
        )

    return {
        "batch_id": batch_id,
        "filename": output.name,
        "original_filename": parent.name,
        "appended_filename": Path(file.filename or "uploaded").name,
        "appended_pages": appended_pages,
        "page_count": len(writer.pages),
        "size_bytes": output.stat().st_size,
        "extension": output.suffix.lower(),
    }


@router.get("/{batch_id}/combined/content")
def combined_pdf_content_endpoint(
    batch_id: str,
    files: list[str] = Query(default=[]),
) -> Response:
    """Return selected batch PDFs as one virtual PDF for viewer-only use."""
    if not files:
        raise HTTPException(status_code=400, detail="No files selected for combined preview.")
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Combined PDF preview is not available on this backend.",
        )

    sources: list[Path] = []
    try:
        for filename in files:
            source = _resolve_input_file(batch_id, filename)
            if source.suffix.lower() != ".pdf":
                raise HTTPException(
                    status_code=415,
                    detail=f"Only PDFs can be combined for preview: {source.name}",
                )
            sources.append(source)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not combine PDF preview: {type(exc).__name__}",
        )

    safe_names = [source.name for source in sources]
    cache_key = (
        batch_id,
        tuple(
            (source.name, source.stat().st_mtime_ns, source.stat().st_size)
            for source in sources
        ),
    )
    headers = {
        "Content-Disposition": 'inline; filename="combined-preview.pdf"',
        "X-Combined-Files": ",".join(safe_names),
    }
    cached = _combined_pdf_cache.get(cache_key)
    if cached is not None:
        _combined_pdf_cache.move_to_end(cache_key)
        return Response(cached, media_type="application/pdf", headers=headers)

    writer = PdfWriter()
    try:
        for source in sources:
            reader = PdfReader(BytesIO(source.read_bytes()))
            for page in reader.pages:
                writer.add_page(page)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not combine PDF preview: {type(exc).__name__}",
        )

    if len(writer.pages) == 0:
        raise HTTPException(status_code=400, detail="Combined PDF preview has no pages.")

    out = BytesIO()
    writer.write(out)
    data = out.getvalue()
    _combined_pdf_cache[cache_key] = data
    _combined_pdf_cache.move_to_end(cache_key)
    while len(_combined_pdf_cache) > COMBINED_PDF_CACHE_MAX_ITEMS:
        _combined_pdf_cache.popitem(last=False)
    return Response(data, media_type="application/pdf", headers=headers)


@router.delete("/{batch_id}/files/{filename}")
def delete_file_endpoint(batch_id: str, filename: str) -> dict:
    """Phase 1X — remove a single uploaded file from a batch.

    The web app's file-explorer sidebar offers a per-file trash icon
    so the operator can clean up individual mistakes without nuking
    the whole batch. The handler:

      * Resolves the file inside the batch's input directory and
        rejects any name that would escape that directory (path
        traversal).
      * Deletes only the matching upload from `input/`. Any vendor-
        specific staging copy under `input/<vendor>/` and any
        processed output stays intact — re-running Process will
        rebuild from the remaining files.
      * Returns 404 for a missing batch or a missing filename so the
        frontend can surface a friendly message.
    """
    try:
        in_dir = batch_store.get_input_dir(batch_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Batch not found: {batch_id}",
        )

    # Strip any path components defensively — the URL only carries
    # the basename but we can't trust the wire.
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target = in_dir / safe_name
    try:
        target_resolved = target.resolve()
        in_dir_resolved = in_dir.resolve()
        # Belt-and-braces traversal guard.
        target_resolved.relative_to(in_dir_resolved)
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not target_resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"File not found in batch: {safe_name}",
        )

    try:
        target_resolved.unlink()
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"Could not delete file: {type(e).__name__}",
        )

    return {
        "batch_id": batch_id,
        "filename": safe_name,
        "deleted": True,
    }
