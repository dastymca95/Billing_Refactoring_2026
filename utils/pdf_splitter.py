"""
Reusable per-page PDF splitter for multi-bill PDFs.

Many utility vendors mail one PDF per billing month with one bill per page.
The Richmond Utilities multi-bill PDF is the first user — but the splitter
itself stays vendor-agnostic so future vendor processors can reuse it.

Public API:
    split_pdf_pages(pdf_path, output_folder, page_metadata) -> list[SplitPdfResult]

`page_metadata` is a list of dicts (one per page). Each dict carries:
    {
        "page_number":    int,    # 1-indexed
        "vendor_name":    str,    # e.g. "Richmond Utilities"
        "account_number": str,    # may be empty
        "month_abbrev":   str,    # "Apr"
        "year_2digit":    str,    # "26"
    }

Each `SplitPdfResult` carries:
    source_pdf, page_number, output_pdf_path, account_number, invoice_number,
    status ("ok" | "skipped_invalid_page" | "failed"), warnings.

Design notes:
  * Uses `pypdf` (already a project dependency).
  * Never modifies the source PDF — opens read-only and writes new files
    via PdfWriter.
  * Filenames are normalised to a Windows-safe slug, with a `_pageN` suffix
    only when the account number is unknown (so two different unknown pages
    don't collide).
  * `output_folder` is created if it doesn't exist; existing files at the
    same path are overwritten so re-runs don't accumulate stale copies.
  * If `pypdf` can't be imported (vendor's bundled environment is missing
    it), the function returns one `failed` result per page rather than
    raising — the caller flags `support_pdf_split_failed` and falls back.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pypdf  # type: ignore
except Exception:  # pragma: no cover
    pypdf = None  # type: ignore


SLUG_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\s]+')


def _long_path(p: Path) -> str:
    """Return a path string Windows file APIs will accept even when the
    absolute path exceeds MAX_PATH (260). On Windows, prefix with
    `\\\\?\\` (and `\\\\?\\UNC\\` for UNC paths). Non-Windows: return str(p).
    The prefix bypasses the Win32 path-length parser, so paths up to ~32k
    chars are usable. We only apply it to absolute paths."""
    if os.name != "nt":
        return str(p)
    s = str(p.resolve())
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):  # UNC path
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


@dataclass
class SplitPdfResult:
    source_pdf: Path
    page_number: int
    output_pdf_path: Optional[Path] = None
    account_number: str = ""
    invoice_number: str = ""
    status: str = ""               # "ok" | "skipped_invalid_page" | "failed"
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status == "ok" and self.output_pdf_path is not None


def _slug(text: str) -> str:
    """Make a string safe to use in a Windows filename. Replace any of
    [ \\ / : * ? " < > | ] (plus runs of whitespace) with a single underscore.
    Trim trailing dots/underscores."""
    cleaned = SLUG_BAD_CHARS_RE.sub("_", (text or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or ""


def _build_split_pdf_name(
    vendor_name: str,
    account_number: str,
    month_abbrev: str,
    year_2digit: str,
    page_number: int,
    *,
    used_account: bool,
) -> str:
    """Compose `<vendor>_<account>_<Mon>_<YY>.pdf`. When the account number
    is empty/unknown, fall back to `<vendor>_page<N>_<Mon>_<YY>.pdf` so two
    unknown-account pages don't collide."""
    parts = [_slug(vendor_name) or "support"]
    if used_account:
        parts.append(_slug(account_number))
    else:
        parts.append(f"page{page_number:02d}")
    if month_abbrev:
        parts.append(_slug(month_abbrev))
    if year_2digit:
        parts.append(_slug(year_2digit))
    return "_".join(p for p in parts if p) + ".pdf"


def split_pdf_pages(
    pdf_path: Path,
    output_folder: Path,
    page_metadata: list[dict],
    *,
    logger: Optional[logging.Logger] = None,
) -> list[SplitPdfResult]:
    """Split `pdf_path` into one PDF per page. Each page's `SplitPdfResult`
    carries the new file path, the account/invoice it belongs to, and any
    warnings. Pages with `page_number` outside the source PDF range are
    returned with status="skipped_invalid_page" and no output file.

    The source PDF is opened read-only and never modified.
    """
    log = logger or logging.getLogger("pdf_splitter")
    pdf_path = Path(pdf_path)
    output_folder = Path(output_folder)

    if pypdf is None:
        log.warning("pypdf is not importable; cannot split %s", pdf_path.name)
        return [
            SplitPdfResult(
                source_pdf=pdf_path,
                page_number=int(meta.get("page_number") or 0),
                account_number=str(meta.get("account_number") or ""),
                status="failed",
                warnings=["pypdf_not_available"],
            )
            for meta in page_metadata
        ]

    if not pdf_path.is_file():
        log.warning("Source PDF not found: %s", pdf_path)
        return [
            SplitPdfResult(
                source_pdf=pdf_path,
                page_number=int(meta.get("page_number") or 0),
                account_number=str(meta.get("account_number") or ""),
                status="failed",
                warnings=["source_pdf_not_found"],
            )
            for meta in page_metadata
        ]

    try:
        output_folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        os.makedirs(_long_path(output_folder), exist_ok=True)
    results: list[SplitPdfResult] = []

    try:
        reader = pypdf.PdfReader(_long_path(pdf_path))
        total_pages = len(reader.pages)
    except Exception as e:
        log.warning("Failed to open %s with pypdf: %s", pdf_path.name, e)
        return [
            SplitPdfResult(
                source_pdf=pdf_path,
                page_number=int(meta.get("page_number") or 0),
                account_number=str(meta.get("account_number") or ""),
                status="failed",
                warnings=[f"pypdf_open_failed:{type(e).__name__}"],
            )
            for meta in page_metadata
        ]

    for meta in page_metadata:
        page_number = int(meta.get("page_number") or 0)
        account_number = str(meta.get("account_number") or "").strip()
        vendor_name = str(meta.get("vendor_name") or "")
        month_abbrev = str(meta.get("month_abbrev") or "")
        year_2digit = str(meta.get("year_2digit") or "")

        result = SplitPdfResult(
            source_pdf=pdf_path,
            page_number=page_number,
            account_number=account_number,
            invoice_number=str(meta.get("invoice_number") or ""),
        )

        if page_number < 1 or page_number > total_pages:
            result.status = "skipped_invalid_page"
            result.warnings.append(
                f"page_out_of_range:{page_number}/{total_pages}"
            )
            results.append(result)
            continue

        used_account = bool(account_number)
        if not used_account:
            result.warnings.append("split_pdf_account_unknown")

        out_name = _build_split_pdf_name(
            vendor_name=vendor_name,
            account_number=account_number,
            month_abbrev=month_abbrev,
            year_2digit=year_2digit,
            page_number=page_number,
            used_account=used_account,
        )
        out_path = output_folder / out_name

        try:
            # Defensive: ensure the parent directory exists immediately
            # before opening for write. We've seen rare antivirus / sync
            # interactions that drop the freshly-mkdir'd folder before
            # the per-page write fires. On Windows, the resolved path
            # frequently exceeds MAX_PATH (260) — wrap in `\\?\` so the
            # Win32 layer accepts it (paths up to ~32k chars).
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                os.makedirs(_long_path(out_path.parent), exist_ok=True)
            writer = pypdf.PdfWriter()
            writer.add_page(reader.pages[page_number - 1])
            with open(_long_path(out_path), "wb") as f:
                writer.write(f)
            result.output_pdf_path = out_path
            result.status = "ok"
        except Exception as e:
            log.warning(
                "Failed to write split page %d of %s: %s",
                page_number, pdf_path.name, e,
            )
            result.status = "failed"
            result.warnings.append(f"pypdf_write_failed:{type(e).__name__}")

        results.append(result)

    return results
