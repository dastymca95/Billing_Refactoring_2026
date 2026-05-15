"""Support-document link generation for webapp-created invoice rows.

Deterministic vendor processors already upload their source bills to Dropbox
and write the resulting shared link into the ResMan ``Document Url`` column.
AI-assisted invoices are produced inside the webapp service layer, so they need
the same behavior here instead of returning a localhost-only preview URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import batch_store

_LOG = logging.getLogger(__name__)


@dataclass
class SupportDocumentLink:
    success: bool
    url: str = ""
    status: str = ""
    dropbox_path: str = ""
    review_code: str = ""
    review_message: str = ""


def upload_source_document_to_dropbox(
    *,
    batch_id: str,
    source_file: str | Path,
    vendor_name: str,
    invoice_date: Any = None,
    dry_run: bool = False,
) -> SupportDocumentLink:
    """Upload a batch input file to Dropbox and return the shared link.

    The source file is resolved strictly inside ``webapp_data/<batch>/input``.
    No secrets are logged or returned. ``dry_run`` is honored so rules impact
    previews and tests never make an external Dropbox call.
    """
    safe_name = Path(str(source_file or "")).name
    if not safe_name:
        return SupportDocumentLink(
            success=False,
            status="missing_source_file",
            review_code="dropbox_upload_failed",
            review_message="Source document name was missing, so no Dropbox link was generated.",
        )
    try:
        input_dir = batch_store.get_input_dir(batch_id).resolve()
    except FileNotFoundError:
        return SupportDocumentLink(
            success=False,
            status="batch_not_found",
            review_code="dropbox_upload_failed",
            review_message="Batch input folder was not found, so no Dropbox link was generated.",
        )
    local_path = (input_dir / safe_name).resolve()
    try:
        local_path.relative_to(input_dir)
    except ValueError:
        return SupportDocumentLink(
            success=False,
            status="invalid_source_path",
            review_code="dropbox_upload_failed",
            review_message="Source document path was invalid, so no Dropbox link was generated.",
        )
    if not local_path.is_file():
        return SupportDocumentLink(
            success=False,
            status="source_file_missing",
            review_code="dropbox_upload_failed",
            review_message=f"Source document '{safe_name}' was not found, so no Dropbox link was generated.",
        )
    if dry_run:
        return SupportDocumentLink(success=False, status="dry_run_skipped")

    try:
        from utils.dropbox_uploader import DropboxUploader, build_dropbox_path
    except Exception:
        return SupportDocumentLink(
            success=False,
            status="sdk_missing",
            review_code="dropbox_upload_failed",
            review_message="Dropbox support is not installed for this backend Python environment.",
        )

    uploader = DropboxUploader.from_env(logger=_LOG)
    if not uploader.is_configured:
        auth_mode = getattr(uploader, "auth_mode", "credentials_missing")
        code = (
            "dropbox_upload_failed"
            if auth_mode == "sdk_missing"
            else "dropbox_credentials_missing"
        )
        message = (
            "Dropbox support is not installed for this backend Python environment."
            if auth_mode == "sdk_missing"
            else "Dropbox credentials are not configured, so no shared document link was generated."
        )
        return SupportDocumentLink(
            success=False,
            status=auth_mode,
            review_code=code,
            review_message=message,
        )

    billing_date = _parse_date(invoice_date)
    dropbox_path = build_dropbox_path(
        base_folder=uploader.base_folder,
        vendor_name=_safe_vendor_folder(vendor_name),
        billing_date=billing_date,
        filename=safe_name,
    )
    result = uploader.upload(
        local_path=local_path,
        dropbox_path=dropbox_path,
        overwrite=True,
    )
    if not result.success:
        code = (
            "dropbox_credentials_missing"
            if result.error_kind == "credentials_missing"
            else "dropbox_upload_failed"
        )
        return SupportDocumentLink(
            success=False,
            status=result.error_kind or "upload_failed",
            dropbox_path=result.dropbox_path or dropbox_path,
            review_code=code,
            review_message=(
                "Dropbox could not create a shared document link. "
                f"{result.error_message or 'Review Dropbox configuration.'}"
            ),
        )
    return SupportDocumentLink(
        success=True,
        url=result.shared_link,
        status="dropbox_uploaded",
        dropbox_path=result.dropbox_path,
    )


def _safe_vendor_folder(vendor_name: str) -> str:
    name = str(vendor_name or "").strip() or "AI Assisted Invoices"
    for ch in '<>:"\\|?*':
        name = name.replace(ch, "-")
    return " ".join(name.split()) or "AI Assisted Invoices"


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
