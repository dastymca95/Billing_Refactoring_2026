"""File-upload endpoint."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..services import batch_store
from ..settings import ALLOWED_UPLOAD_EXTENSIONS


router = APIRouter(prefix="/api/batches", tags=["uploads"])


@router.post("/{batch_id}/upload")
async def upload_file_endpoint(batch_id: str, file: UploadFile = File(...)) -> dict:
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

    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)

    return {
        "batch_id": batch_id,
        "filename": dest.name,
        "size_bytes": dest.stat().st_size,
        "extension": dest.suffix.lower(),
    }


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
