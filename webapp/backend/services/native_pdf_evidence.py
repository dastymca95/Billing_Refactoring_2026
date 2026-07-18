"""Private-safe loading of original PDF evidence for multimodal extraction.

The source file is never modified.  Only the basename, content fingerprint,
byte count, and an in-memory data URL leave this boundary.  The data URL is
excluded from ``repr`` so normal diagnostics cannot serialize document bytes.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import batch_store


class NativePdfEvidenceError(ValueError):
    """Raised when a batch PDF cannot be loaded inside the private boundary."""


@dataclass(frozen=True)
class NativePdfEvidence:
    filename: str
    content_sha256: str
    byte_count: int
    media_type: str = "application/pdf"
    data_url: str = field(default="", repr=False)


def load_native_pdf_evidence(
    *,
    batch_id: str,
    filename: str,
    max_bytes: int,
) -> NativePdfEvidence:
    """Load one batch-local PDF without exposing an absolute filesystem path."""

    input_dir = batch_store.get_input_dir(batch_id).resolve()
    safe_name = Path(filename or "").name
    if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
        raise NativePdfEvidenceError("Native document evidence requires a PDF file.")
    source_path = (input_dir / safe_name).resolve()
    if input_dir not in source_path.parents or not source_path.is_file():
        raise NativePdfEvidenceError("Native document evidence was not found in the batch.")
    byte_count = source_path.stat().st_size
    bounded_max = max(1, int(max_bytes or 1))
    if byte_count <= 0 or byte_count > bounded_max:
        raise NativePdfEvidenceError("Native document evidence exceeds the configured size limit.")
    payload = source_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    return NativePdfEvidence(
        filename=safe_name,
        content_sha256=digest,
        byte_count=byte_count,
        data_url=f"data:application/pdf;base64,{encoded}",
    )


__all__ = [
    "NativePdfEvidence",
    "NativePdfEvidenceError",
    "load_native_pdf_evidence",
]
