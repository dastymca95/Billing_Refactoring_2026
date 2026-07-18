"""Support-document link generation for webapp-created invoice rows.

Deterministic vendor processors already upload their source bills to Dropbox
and write the resulting shared link into the ResMan ``Document Url`` column.
AI-assisted invoices are produced inside the webapp service layer, so they need
the same behavior here instead of returning a localhost-only preview URL.
"""

from __future__ import annotations

import json
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
    cached = _cached_support_document_link(
        batch_id=batch_id,
        local_path=local_path,
        source_file=safe_name,
        dropbox_path=dropbox_path,
    )
    if cached:
        return cached
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
    link = SupportDocumentLink(
        success=True,
        url=result.shared_link,
        status="dropbox_uploaded",
        dropbox_path=result.dropbox_path,
    )
    _remember_support_document_link(
        batch_id=batch_id,
        local_path=local_path,
        source_file=safe_name,
        link=link,
    )
    return link


def _support_link_cache_path(batch_id: str) -> Path:
    return batch_store.get_batch_dir(batch_id) / "audit" / "support_document_links.json"


def _support_link_key(local_path: Path, source_file: str, dropbox_path: str) -> str:
    stat = local_path.stat()
    return "|".join((source_file, str(stat.st_size), str(stat.st_mtime_ns), dropbox_path))


def _cached_support_document_link(
    *,
    batch_id: str,
    local_path: Path,
    source_file: str,
    dropbox_path: str,
) -> SupportDocumentLink | None:
    cache_path = _support_link_cache_path(batch_id)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    key = _support_link_key(local_path, source_file, dropbox_path)
    entry = data.get(key) if isinstance(data, dict) else None
    if isinstance(entry, dict) and str(entry.get("url") or "").startswith("https://"):
        return SupportDocumentLink(
            success=True,
            url=str(entry["url"]),
            status="dropbox_cached",
            dropbox_path=str(entry.get("dropbox_path") or dropbox_path),
        )

    # Seed the cache from a prior completed revision. This makes the first
    # run after an application upgrade fast as well; no Dropbox request is
    # needed when the exact batch source already has a verified shared link.
    revision_dir = batch_store.get_batch_dir(batch_id) / "revisions"
    try:
        index = json.loads((revision_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        index = []
    for revision in index[:10] if isinstance(index, list) else []:
        snapshot_name = str((revision or {}).get("snapshot_filename") or "")
        if not snapshot_name:
            continue
        try:
            snapshot = json.loads((revision_dir / snapshot_name).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        for invoice in snapshot.get("all_invoices") or []:
            if str(invoice.get("source_file") or "") != source_file:
                continue
            for row in invoice.get("rows") or []:
                url = str(row.get("Document Url") or "")
                meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
                stored_path = str(meta.get("support_document_dropbox_path") or "")
                if url.startswith("https://") and (not stored_path or stored_path == dropbox_path):
                    link = SupportDocumentLink(
                        success=True,
                        url=url,
                        status="dropbox_cached",
                        dropbox_path=stored_path or dropbox_path,
                    )
                    _remember_support_document_link(
                        batch_id=batch_id,
                        local_path=local_path,
                        source_file=source_file,
                        link=link,
                    )
                    return link
    return None


def _remember_support_document_link(
    *,
    batch_id: str,
    local_path: Path,
    source_file: str,
    link: SupportDocumentLink,
) -> None:
    if not link.success or not link.url.startswith("https://"):
        return
    cache_path = _support_link_cache_path(batch_id)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[_support_link_key(local_path, source_file, link.dropbox_path)] = {
        "url": link.url,
        "dropbox_path": link.dropbox_path,
    }
    tmp = cache_path.with_suffix(".tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


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
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%m-%d-%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
