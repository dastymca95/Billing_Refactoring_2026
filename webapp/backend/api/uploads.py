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
