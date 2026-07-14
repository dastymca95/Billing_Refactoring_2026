"""Phase 2L — Cross-vendor row normalization.

Applied at the webapp layer right after `process_batch()` returns and
BEFORE the result cache is written. Normalises three things across
EVERY vendor's rows so the operator sees consistent output regardless
of which processor produced them:

  1. **Vendor name** — replaced with the canonical spelling from
     ``Vendors/Vendor List.csv`` (matched by snake-cased vendor_key).
     The bill might say "Columbia Power & Water Systems" but the
     ResMan import wants "Columbia Power and Water System" exactly.

  2. **Description case** — "Invoice Description" and "Line Item
     Description" are forced to ResMan-facing Proper Case while preserving
     project acronyms and common street abbreviations.

  3. **Dates** — every column whose name ends in "Date" gets parsed
     into ``YYYY-MM-DD`` ISO format. The workbook writers then
     convert that to a real ``datetime.date`` cell (with a
     ``MM/DD/YYYY`` Excel number format) so the Excel file has true
     date cells instead of text.

Applied to BOTH `all_invoices[].rows[]` and
`by_vendor.<key>.invoices[].rows[]` because those lists are separate
list copies in the result dict (the webapp's preview reads from
`all_invoices`; the workbook export reads from the per-vendor stash).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional
from urllib.parse import quote

try:
    from utils.canonical_vendors import canonical_vendor_name
except Exception:  # pragma: no cover
    canonical_vendor_name = None  # type: ignore
try:
    from utils.text_normalization import proper_case_preserve_acronyms as _shared_proper_case
except Exception:  # pragma: no cover
    _shared_proper_case = None  # type: ignore


_LOG = logging.getLogger(__name__)
try:
    from . import output_contract_validator
except Exception:  # pragma: no cover
    output_contract_validator = None  # type: ignore


_DATE_COLUMNS = ("Invoice Date", "Accounting Date", "Due Date")
_DESCRIPTION_COLUMNS = ("Invoice Description", "Line Item Description")

# Phase 2L — used to fill the "Document Url" column when a vendor
# processor didn't already populate it (e.g. Dropbox not configured).
# The webapp serves the original file at this path; we wrap with a
# configurable base URL so the link is clickable from Excel.
_FILE_CONTENT_PATH_FMT = "/api/batches/{batch_id}/files/{filename}/content"


def _webapp_base_url() -> str:
    """Resolve the public URL prefix for ``Document Url`` fallback links.

    Order: ``WEBAPP_BASE_URL`` env var → ``http://localhost:8001`` (the
    default uvicorn port the project ships with). Trailing slashes are
    stripped so the path concatenation produces a well-formed URL."""
    base = (os.environ.get("WEBAPP_BASE_URL") or "http://localhost:8001").strip()
    return base.rstrip("/") or "http://localhost:8001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vendor_canonical(vendor_key: str, fallback: str) -> str:
    """Look up the canonical vendor name. Falls back to whatever the
    processor emitted if the lookup helper isn't available."""
    if canonical_vendor_name is None:
        return fallback or ""
    return canonical_vendor_name(vendor_key=vendor_key, fallback=fallback)


_DATE_INPUT_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y/%m/%d",
)


def _parse_to_iso_date(value: Any) -> Optional[str]:
    """Best-effort conversion to YYYY-MM-DD. Returns None if the value
    couldn't be parsed as a date — caller leaves it untouched."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATE_INPUT_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


_WORD_CHAR_RE = re.compile(r"[A-Za-z]")


def to_sentence_case(value: Any) -> Any:
    """Backward-compatible re-export. The implementation lives in
    ``utils.text_normalize`` so vendor processors can use it without
    importing from the webapp package."""
    if _shared_proper_case is not None:
        return _shared_proper_case(value)
    if value in (None, ""):
        return value
    s = str(value)
    return " ".join(word[:1].upper() + word[1:].lower() for word in s.split(" "))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_rows(
    rows: list[dict[str, Any]],
    *,
    vendor_key: str = "",
    batch_id: str = "",
    source_file: str = "",
) -> int:
    """Apply the four normalizations in place. Returns the number
    of rows touched. Best-effort — silently skips malformed entries.

    Steps:
      1. Canonical vendor name from Vendor List.csv.
      2. Sentence-case descriptions.
      3. Dates → ISO strings (workbook writer turns into real cells).
      4. Document Url fallback to ``/api/batches/.../content`` when
         the vendor processor didn't populate one (no Dropbox).
    """
    if not rows:
        return 0
    canonical_name: Optional[str] = None
    if vendor_key and vendor_key != "ai_assisted":
        canonical_name = _vendor_canonical(vendor_key, "")
    base_url: Optional[str] = _webapp_base_url() if batch_id else None

    touched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        changed = False

        # 1) Vendor name
        if canonical_name:
            cur = row.get("Vendor")
            if cur != canonical_name:
                row["Vendor"] = canonical_name
                changed = True

        # 2) Description case
        for col in _DESCRIPTION_COLUMNS:
            if col in row and row[col] not in (None, ""):
                new_val = to_sentence_case(row[col])
                if new_val != row[col]:
                    row[col] = new_val
                    changed = True

        # 3) Dates → ISO strings (the workbook writer flips these into
        #    real datetime.date cells with a MM/DD/YYYY format).
        for col in _DATE_COLUMNS:
            if col in row and row[col] not in (None, ""):
                iso = _parse_to_iso_date(row[col])
                if iso and iso != row[col]:
                    row[col] = iso
                    changed = True

        # 4) Document Url is owned by the vendor processor (Dropbox
        #    upload). When the upload fails or Dropbox isn't configured
        #    the cell is left blank and the row is already flagged
        #    `dropbox_upload_failed` / `dropbox_credentials_missing`
        #    in manual_review_reasons. No fallback here.

        if changed:
            touched += 1
    if output_contract_validator is not None:
        output_contract_validator.annotate_rows(rows)
    from .accounting_integration_bridges import RowAccountingV2Adapter
    RowAccountingV2Adapter().enrich_rows(rows, {
        "document_id": source_file or batch_id or "normalized-row",
        "extraction_route": vendor_key or "row_normalizer",
    })
    return touched


def normalize_result(result: dict[str, Any]) -> dict[str, int]:
    """Walk a process_batch result dict and normalize every row in
    place. Returns a {vendor_key: rows_touched} count for telemetry.

    The result has two parallel views (`all_invoices` AND
    `by_vendor.<key>.invoices`); we walk both. The two lists carry
    DIFFERENT row dicts (the processor builds them as parallel
    copies), so both must be updated to keep the preview and the
    export workbook in sync."""
    counts: dict[str, int] = {}
    batch_id = str(result.get("batch_id") or "")
    by_vendor = result.get("by_vendor") or {}

    # Per-vendor stash: walk per-invoice so we can pass the invoice's
    # source_file down to normalize_rows (which fills the Document
    # Url fallback when empty).
    for vendor_key, payload in by_vendor.items():
        n = 0
        for inv in (payload or {}).get("invoices") or []:
            src = ((inv or {}).get("source_file")
                   or ((inv or {}).get("debug_info") or {}).get("source_file")
                   or "")
            n += normalize_rows(
                list((inv or {}).get("rows") or []),
                vendor_key=vendor_key,
                batch_id=batch_id,
                source_file=src,
            )
        counts[vendor_key] = n

    # Top-level all_invoices: same per-invoice walk so the source_file
    # propagates through the parallel copy too.
    all_inv = result.get("all_invoices") or []
    if not all_inv:
        return counts
    only_key = next(iter(by_vendor.keys())) if len(by_vendor) == 1 else ""
    extra = 0
    for inv in all_inv:
        src = ((inv or {}).get("source_file")
               or ((inv or {}).get("debug_info") or {}).get("source_file")
               or "")
        extra += normalize_rows(
            list((inv or {}).get("rows") or []),
            vendor_key=only_key,
            batch_id=batch_id,
            source_file=src,
        )
    if only_key:
        counts[only_key] = counts.get(only_key, 0) + extra
    return counts
