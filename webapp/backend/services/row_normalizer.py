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
try:
    from .description_builder import polish_accounting_description_pair
except Exception:  # pragma: no cover
    polish_accounting_description_pair = None  # type: ignore


_LOG = logging.getLogger(__name__)
try:
    from . import output_contract_validator
except Exception:  # pragma: no cover
    output_contract_validator = None  # type: ignore
try:
    from . import accounting_pipeline_v2
except Exception:  # pragma: no cover
    accounting_pipeline_v2 = None  # type: ignore


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
    "%d-%b-%Y",
    "%d-%b-%y",
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
        emitted_vendor = str((rows[0] or {}).get("Vendor") or "") if rows else ""
        canonical_name = _vendor_canonical(vendor_key, emitted_vendor)
    base_url: Optional[str] = _webapp_base_url() if batch_id else None

    document_semantic_context = " | ".join(dict.fromkeys(
        str(value).strip()
        for row in rows if isinstance(row, dict)
        for value in (
            ((row.get("_meta") or {}).get("source_text") or {}).get("raw_invoice_description")
            if isinstance(row.get("_meta"), dict) else None,
            row.get("Invoice Description"),
            ((row.get("_meta") or {}).get("source_line_description")
             if isinstance(row.get("_meta"), dict) else None),
            row.get("Line Item Description"),
        )
        if value and str(value).strip()
    ))
    touched = 0
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        line_item_id = str(meta.get("line_item_id") or meta.get("invoice_row_index") or row.get("Line Item Number") or row_index)
        document_id = str(meta.get("source_file") or source_file or batch_id or "unknown-document")
        if accounting_pipeline_v2 is not None:
            accounting_pipeline_v2.capture_source_fields(
                row, document_id=document_id, line_item_id=line_item_id,
            )
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

        if polish_accounting_description_pair is not None:
            invoice_desc, line_desc = polish_accounting_description_pair(
                row.get("Invoice Description"),
                row.get("Line Item Description"),
                gl_account=row.get("GL Account"),
                vendor_name=row.get("Vendor"),
            )
            if "Invoice Description" in row and invoice_desc != (row.get("Invoice Description") or ""):
                row["Invoice Description"] = invoice_desc
                changed = True
            if "Line Item Description" in row and line_desc != (row.get("Line Item Description") or ""):
                row["Line Item Description"] = line_desc
                changed = True

        # 3) Dates → ISO strings (the workbook writer flips these into
        #    real datetime.date cells with a MM/DD/YYYY format).
        for col in _DATE_COLUMNS:
            if col in row and row[col] not in (None, ""):
                iso = _parse_to_iso_date(row[col])
                if iso and iso != row[col]:
                    row[col] = iso
                    changed = True

        # 4) Document Url should still be usable from the web console even
        #    when a deterministic processor did not upload the support doc to
        #    Dropbox. Use the original batch file content endpoint as a local
        #    fallback so export validation does not fail on an otherwise valid
        #    preview. Real Dropbox links from processors are left untouched.
        if (
            base_url
            and source_file
            and "Document Url" in row
            and not str(row.get("Document Url") or "").strip()
        ):
            safe_name = quote(source_file, safe="")
            row["Document Url"] = (
                base_url
                + _FILE_CONTENT_PATH_FMT.format(batch_id=quote(batch_id, safe=""), filename=safe_name)
            )
            meta = row.setdefault("_meta", {})
            if isinstance(meta, dict):
                meta.setdefault("support_document_status", "local_webapp_link")
            changed = True

        meta = row.setdefault("_meta", {})
        if isinstance(meta, dict):
            source_text = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
            if not meta.get("normalized_source_description"):
                meta["normalized_source_description"] = to_sentence_case(source_text.get("raw_description")) if source_text.get("raw_description") else None
            meta["generated_line_description"] = row.get("Line Item Description")
            meta["generated_invoice_description"] = row.get("Invoice Description")
        if changed:
            touched += 1
    from .accounting_integration_bridges import RowAccountingV2Adapter
    RowAccountingV2Adapter().enrich_rows(rows, {
        "document_id": source_file or batch_id or "normalized-row",
        "extraction_route": vendor_key or "row_normalizer",
        "document_context": document_semantic_context,
    })
    if output_contract_validator is not None:
        output_contract_validator.annotate_rows(rows)
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
    from .review_taxonomy import migrate_invoice_review_codes
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
            migrate_invoice_review_codes(inv)
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
        migrate_invoice_review_codes(inv)
    if only_key:
        counts[only_key] = counts.get(only_key, 0) + extra
    return counts
