"""
Reusable PDF text extractor for utility-bill processors.

Two extraction paths:
  1. Digital text via `pdfplumber.extract_text(...)` (works for PDFs with an
     embedded text layer).
  2. OCR via `pdf2image` + `pytesseract` for scanned / image-only PDFs.

Both paths produce the same `PdfExtractionResult` so vendor parsers don't
need to care which one fired.

Design goals:
  * Single entry point (`extract_pdf_text`) that always returns a result —
    never raises on missing OCR dependencies.
  * Per-page word-level position data when OCR is used, so vendor parsers
    can do layout-aware grouping (rows by `top` Y-coordinate) instead of
    fragile regex on plain text.
  * Configurable: Tesseract binary path and Poppler binary path can be
    overridden by environment variables or kwargs (the YAML layer can
    surface these to the operator).
  * Manual-review propagation: when OCR is required but unavailable, the
    result carries `requires_manual_review=True` and a reason string the
    caller can attach to its manual-review report.

Dependencies:
  * `pdfplumber` (always required — already used by the webapp preview)
  * `pdf2image` + `pytesseract` (only if OCR is needed)
  * Tesseract binary + Poppler binary (only if OCR is needed). On Windows
    the standard locations are
        C:\\Program Files\\Tesseract-OCR\\tesseract.exe
        C:\\poppler\\bin\\pdftoppm.exe
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore

# Importing pytesseract / pdf2image is deferred to the OCR helper so that
# missing dependencies do not break the digital-text path.


EXTRACTION_DIGITAL = "digital_text"
EXTRACTION_OCR = "ocr"
EXTRACTION_OCR_UNAVAILABLE = "ocr_required_but_unavailable"
EXTRACTION_FAILED = "failed"


# Heuristic: a page with fewer than this many embedded characters is treated
# as scanned and routed to OCR.
DEFAULT_DIGITAL_TEXT_MIN_CHARS = 30


@dataclass
class PdfPage:
    """One page's worth of extracted content."""
    page_number: int                        # 1-indexed
    text: str = ""
    extraction_method: str = ""             # digital_text | ocr
    width: int = 0
    height: int = 0
    # OCR-only: list of {text, conf, left, top, width, height, line_num}
    words: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PdfExtractionResult:
    """Result of an `extract_pdf_text(...)` call.

    `text` is the per-page text joined by "\f". `pages` keeps each page's
    word boxes for layout-aware parsing. `extraction_method` reflects the
    method used for the BULK of the PDF (digital_text wins if any page used
    it; ocr if every page was OCR'd; ocr_required_but_unavailable / failed
    when nothing worked).
    """
    pdf_path: Path
    text: str = ""
    pages: list[PdfPage] = field(default_factory=list)
    pages_count: int = 0
    extraction_method: str = ""
    confidence: float = 0.0                # 0.0–1.0
    warnings: list[str] = field(default_factory=list)
    requires_manual_review: bool = False
    manual_review_reasons: list[str] = field(default_factory=list)

    def page_text(self, n: int) -> str:
        """Return the text for page `n` (1-indexed) or empty string."""
        for p in self.pages:
            if p.page_number == n:
                return p.text
        return ""


# ---------------------------------------------------------------------------
# Digital-text path
# ---------------------------------------------------------------------------
def _try_digital_text(pdf_path: Path, logger: logging.Logger) -> list[PdfPage]:
    """Attempt to read each page's embedded text via pdfplumber. Returns an
    empty list if pdfplumber is unavailable or the PDF can't be opened."""
    pages: list[PdfPage] = []
    if pdfplumber is None:
        return pages
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                txt = page.extract_text() or ""
                pages.append(PdfPage(
                    page_number=i,
                    text=txt,
                    extraction_method=EXTRACTION_DIGITAL,
                    width=int(getattr(page, "width", 0) or 0),
                    height=int(getattr(page, "height", 0) or 0),
                ))
    except Exception as e:  # pragma: no cover
        logger.warning("pdfplumber failed on %s: %s", pdf_path.name, e)
        return []
    return pages


# ---------------------------------------------------------------------------
# OCR path (scanned PDFs)
# ---------------------------------------------------------------------------
def _resolve_tesseract_cmd(override: Optional[str] = None) -> Optional[str]:
    """Find the Tesseract binary. Order:
       1. explicit override
       2. `TESSERACT_CMD` env var
       3. `shutil.which("tesseract")`
       4. common Windows install path
    """
    candidates: list[Optional[str]] = [
        override,
        os.environ.get("TESSERACT_CMD"),
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _resolve_poppler_path(override: Optional[str] = None) -> Optional[str]:
    """Find the Poppler bin folder (where `pdftoppm.exe` lives).
    Returned value is the *folder*, not the binary, since that's what
    `pdf2image.convert_from_path(poppler_path=...)` expects."""
    candidates: list[Optional[str]] = [
        override,
        os.environ.get("POPPLER_PATH"),
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return c
    # Fallback: dirname of pdftoppm if it's on PATH.
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        return str(Path(pdftoppm).parent)
    common = Path(r"C:\poppler\bin")
    if common.is_dir():
        return str(common)
    return None


def _ocr_page(image, *, logger: logging.Logger) -> tuple[str, list[dict[str, Any]], float]:
    """OCR a single PIL Image. Returns (text, words, mean_confidence)."""
    import pytesseract  # type: ignore
    text = pytesseract.image_to_string(image) or ""
    words: list[dict[str, Any]] = []
    mean_conf = 0.0
    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        confs: list[float] = []
        n = len(data.get("text", []))
        for i in range(n):
            t = (data["text"][i] or "").strip()
            try:
                c = float(data["conf"][i])
            except (TypeError, ValueError):
                c = -1.0
            if not t:
                continue
            words.append({
                "text": t,
                "conf": c,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
                "line_num": int(data.get("line_num", [0] * n)[i] or 0),
                "block_num": int(data.get("block_num", [0] * n)[i] or 0),
            })
            if c >= 0:
                confs.append(c)
        if confs:
            mean_conf = sum(confs) / len(confs) / 100.0
    except Exception as e:  # pragma: no cover
        logger.debug("image_to_data failed: %s", e)
    return text, words, mean_conf


def _try_ocr(
    pdf_path: Path,
    *,
    dpi: int,
    tesseract_cmd: Optional[str],
    poppler_path: Optional[str],
    logger: logging.Logger,
) -> tuple[list[PdfPage], list[str], float, bool]:
    """Run OCR over every page of `pdf_path`. Returns
    (pages, warnings, mean_confidence_0_1, ocr_actually_ran)."""
    warnings: list[str] = []
    try:
        import pytesseract  # type: ignore
        import pdf2image  # type: ignore
    except Exception:
        return ([], [
            "ocr_dependencies_missing: pip install pytesseract pdf2image",
        ], 0.0, False)

    cmd = _resolve_tesseract_cmd(tesseract_cmd)
    poppler = _resolve_poppler_path(poppler_path)
    if not cmd:
        return ([], ["tesseract_binary_not_found"], 0.0, False)
    if not poppler:
        return ([], ["poppler_binary_not_found"], 0.0, False)
    pytesseract.pytesseract.tesseract_cmd = cmd

    try:
        images = pdf2image.convert_from_path(pdf_path, dpi=dpi, poppler_path=poppler)
    except Exception as e:
        logger.warning("pdf2image failed on %s: %s", pdf_path.name, e)
        return ([], [f"pdf2image_error:{type(e).__name__}"], 0.0, False)

    pages: list[PdfPage] = []
    confs: list[float] = []
    for i, img in enumerate(images, start=1):
        try:
            txt, words, conf = _ocr_page(img, logger=logger)
        except Exception as e:
            logger.warning("OCR failed on page %d of %s: %s", i, pdf_path.name, e)
            txt, words, conf = "", [], 0.0
            warnings.append(f"ocr_page_{i}_failed:{type(e).__name__}")
        pages.append(PdfPage(
            page_number=i,
            text=txt,
            extraction_method=EXTRACTION_OCR,
            width=img.width,
            height=img.height,
            words=words,
        ))
        if conf > 0:
            confs.append(conf)
    mean = (sum(confs) / len(confs)) if confs else 0.0
    return pages, warnings, mean, True


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def extract_pdf_text(
    pdf_path: Path,
    *,
    digital_text_first: bool = True,
    ocr_if_text_missing: bool = True,
    digital_min_chars_per_page: int = DEFAULT_DIGITAL_TEXT_MIN_CHARS,
    ocr_dpi: int = 200,
    tesseract_cmd: Optional[str] = None,
    poppler_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> PdfExtractionResult:
    """Extract text from a PDF, falling back from digital-text to OCR.

    `digital_text_first`  — try pdfplumber first.
    `ocr_if_text_missing` — if a page returns < `digital_min_chars_per_page`
                            characters, route the *whole* PDF to OCR.
    `tesseract_cmd`       — explicit Tesseract binary path (overrides env).
    `poppler_path`        — explicit Poppler bin folder (overrides env).
    """
    log = logger or logging.getLogger("pdf_text_extractor")
    pdf_path = Path(pdf_path)
    result = PdfExtractionResult(pdf_path=pdf_path)
    if not pdf_path.is_file():
        result.extraction_method = EXTRACTION_FAILED
        result.warnings.append("file_not_found")
        result.requires_manual_review = True
        result.manual_review_reasons.append("pdf_text_extraction_failed")
        return result

    digital_pages: list[PdfPage] = []
    if digital_text_first:
        digital_pages = _try_digital_text(pdf_path, log)

    insufficient_text = (
        not digital_pages
        or any(len((p.text or "").strip()) < digital_min_chars_per_page for p in digital_pages)
    )

    if digital_pages and not insufficient_text:
        result.pages = digital_pages
        result.pages_count = len(digital_pages)
        result.text = "\f".join(p.text for p in digital_pages)
        result.extraction_method = EXTRACTION_DIGITAL
        result.confidence = 1.0
        return result

    if not ocr_if_text_missing:
        if digital_pages:
            result.pages = digital_pages
            result.pages_count = len(digital_pages)
            result.text = "\f".join(p.text for p in digital_pages)
            result.extraction_method = EXTRACTION_DIGITAL
            result.confidence = 0.5
            result.warnings.append("digital_text_partial_ocr_disabled")
        else:
            result.extraction_method = EXTRACTION_FAILED
            result.warnings.append("no_digital_text_and_ocr_disabled")
            result.requires_manual_review = True
            result.manual_review_reasons.append("pdf_text_extraction_failed")
        return result

    # ---- OCR path ----
    ocr_pages, ocr_warnings, mean_conf, ocr_ran = _try_ocr(
        pdf_path,
        dpi=ocr_dpi,
        tesseract_cmd=tesseract_cmd,
        poppler_path=poppler_path,
        logger=log,
    )
    result.warnings.extend(ocr_warnings)
    if not ocr_ran:
        # Fall back to whatever digital text we have, with a manual-review flag.
        if digital_pages:
            result.pages = digital_pages
            result.pages_count = len(digital_pages)
            result.text = "\f".join(p.text for p in digital_pages)
            result.extraction_method = EXTRACTION_OCR_UNAVAILABLE
            result.confidence = 0.3
        else:
            result.extraction_method = EXTRACTION_OCR_UNAVAILABLE
            result.confidence = 0.0
        result.requires_manual_review = True
        result.manual_review_reasons.append("ocr_required_but_unavailable")
        return result

    result.pages = ocr_pages
    result.pages_count = len(ocr_pages)
    result.text = "\f".join(p.text for p in ocr_pages)
    result.extraction_method = EXTRACTION_OCR
    result.confidence = mean_conf
    if mean_conf < 0.5:
        result.warnings.append(f"low_ocr_confidence:{mean_conf:.2f}")
    return result


# ---------------------------------------------------------------------------
# Layout helper: group word boxes into rows by Y-coordinate
# ---------------------------------------------------------------------------
def group_words_into_rows(
    words: list[dict[str, Any]], *, y_tolerance: int = 12,
) -> list[list[dict[str, Any]]]:
    """Bin word boxes into visual rows.

    Each word's vertical *centre* (top + height/2) is compared to the
    running row's centre; words within `y_tolerance` pixels join the same
    row. Using the centre instead of the top makes the grouping robust to
    fonts with slightly different cap-heights (e.g. "Gas" vs "Meter
    Charge" rendered at the same baseline can have `top` values that
    differ by a few pixels).

    Returns rows sorted top-to-bottom; words within a row sorted left-to-right.
    """
    if not words:
        return []
    items = []
    for w in words:
        cy = w["top"] + (w.get("height", 0) // 2)
        items.append((cy, w))
    items.sort(key=lambda x: (x[0], x[1]["left"]))

    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_centre: float = -10**9
    for cy, w in items:
        if not current or abs(cy - current_centre) <= y_tolerance:
            current.append(w)
            current_centre = sum(
                cw["top"] + (cw.get("height", 0) // 2) for cw in current
            ) / len(current)
        else:
            rows.append(sorted(current, key=lambda x: x["left"]))
            current = [w]
            current_centre = cy
    if current:
        rows.append(sorted(current, key=lambda x: x["left"]))
    return rows


def row_text(row: list[dict[str, Any]]) -> str:
    """Render a row's words as a single space-joined line."""
    return " ".join(w["text"] for w in row).strip()
