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
from typing import Any, Callable, Optional

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore

# Phase PERF-1 — optional OCR cache + perf timer. Imports are
# defensive so legacy CLI invocations (which don't have a webapp
# context) keep working when the modules are absent.
try:
    from utils import ocr_cache as _ocr_cache  # type: ignore
except Exception:  # pragma: no cover
    _ocr_cache = None  # type: ignore
try:
    from webapp.backend.services import perf_timer as _perf_timer  # type: ignore
except Exception:  # pragma: no cover
    _perf_timer = None  # type: ignore


def _perf(step: str, batch_id: Optional[str], meta: Optional[dict] = None):
    """Best-effort perf wrapper that no-ops when batch_id or the
    perf_timer module is absent (CLI mode)."""
    if _perf_timer is None or not batch_id:
        from contextlib import nullcontext
        return nullcontext()
    return _perf_timer.perf_step(step, batch_id=batch_id, meta=meta)

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
    empty list if pdfplumber is unavailable or the PDF can't be opened.

    Phase 2J — also captures word-level bounding boxes via
    `page.extract_words()` so downstream extraction code can build trace
    overlays that point back at the source region for each emitted
    field. Coordinates are PDF points with a top-left origin (pdfplumber
    normalizes from the PDF's native bottom-left). Failure to extract
    words is non-fatal; we keep the text and leave `words` empty."""
    pages: list[PdfPage] = []
    if pdfplumber is None:
        return pages
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                txt = page.extract_text() or ""
                w = float(getattr(page, "width", 0) or 0)
                h = float(getattr(page, "height", 0) or 0)
                words: list[dict[str, Any]] = []
                try:
                    raw = page.extract_words(
                        keep_blank_chars=False,
                        use_text_flow=True,
                    ) or []
                    for wd in raw:
                        try:
                            x0 = float(wd.get("x0") or 0)
                            x1 = float(wd.get("x1") or 0)
                            top = float(wd.get("top") or 0)
                            bottom = float(wd.get("bottom") or 0)
                        except (TypeError, ValueError):
                            continue
                        words.append({
                            "text": str(wd.get("text") or ""),
                            "left": x0,
                            "top": top,
                            "width": max(0.0, x1 - x0),
                            "height": max(0.0, bottom - top),
                            # Position metadata helps consumers group rows.
                            "line_num": int(wd.get("line_num") or 0)
                            if wd.get("line_num") is not None else None,
                        })
                except Exception as we:  # pragma: no cover
                    logger.debug(
                        "extract_words failed on %s page %d: %s",
                        pdf_path.name, i, we,
                    )
                pages.append(PdfPage(
                    page_number=i,
                    text=txt,
                    extraction_method=EXTRACTION_DIGITAL,
                    width=int(w),
                    height=int(h),
                    words=words,
                ))
    except Exception as e:  # pragma: no cover
        logger.warning("pdfplumber failed on %s: %s", pdf_path.name, e)
        # Phase 2L — fallback to PyPDF2 / pypdf for malformed PDFs that
        # pdfplumber refuses to open (Shelbyville Power bills ship with
        # missing MediaBox metadata). PyPDF2 is more forgiving and
        # gives us text — we lose word-level bboxes (no trace overlay
        # for these vendors) but the row data still flows through.
        return _try_digital_text_fallback_pypdf(pdf_path, logger)
    return pages


def _try_digital_text_fallback_pypdf(
    pdf_path: Path, logger: logging.Logger,
) -> list[PdfPage]:
    """Last-ditch text extraction using PyPDF2 (or its successor pypdf).
    Used when pdfplumber chokes on malformed PDF metadata. Produces
    text + page count but NO word-level bboxes — trace-overlay regions
    won't be available for these documents."""
    try:
        import PyPDF2  # type: ignore
        reader_cls = PyPDF2.PdfReader  # type: ignore
    except Exception:
        try:
            import pypdf  # type: ignore
            reader_cls = pypdf.PdfReader  # type: ignore
        except Exception:
            logger.warning(
                "PyPDF2/pypdf not available; cannot recover %s",
                pdf_path.name,
            )
            return []
    try:
        with open(pdf_path, "rb") as fh:
            reader = reader_cls(fh)
            out: list[PdfPage] = []
            for i, page in enumerate(reader.pages, start=1):
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                # MediaBox / page size is best-effort; default to US
                # Letter (612 × 792) when missing.
                width, height = 612, 792
                try:
                    box = page.mediabox  # type: ignore[attr-defined]
                    if box and len(box) >= 4:
                        width = int(float(box[2]) - float(box[0]))
                        height = int(float(box[3]) - float(box[1]))
                except Exception:
                    pass
                out.append(PdfPage(
                    page_number=i,
                    text=txt,
                    extraction_method=EXTRACTION_DIGITAL,
                    width=width,
                    height=height,
                    words=[],   # no bboxes via PyPDF2
                ))
            return out
    except Exception as e:  # pragma: no cover
        logger.warning("PyPDF2 fallback failed on %s: %s", pdf_path.name, e)
        return []


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
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[list[PdfPage], list[str], float, bool]:
    """Run OCR over every page of `pdf_path`. Returns
    (pages, warnings, mean_confidence_0_1, ocr_actually_ran).

    Phase 1O: optional `progress_callback(done, total, label)` is fired
    after each page so callers can drive smooth per-page progress
    instead of one big jump at the end. The callback is best-effort —
    exceptions inside it never abort OCR.

    Phase 2E: optional ``should_cancel()`` is polled before every page
    so an in-progress OCR loop can stop at the next safe checkpoint
    when the operator clicks Stop. The function returns whatever pages
    completed before cancellation; the warning ``ocr_cancelled`` is
    appended so callers can distinguish a cancelled OCR from a clean
    one. Default is None which preserves the legacy behaviour.
    """
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

    def _progress(done: int, total: int, label: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(done, total, label)
        except Exception:
            pass

    _progress(0, 0, f"Rasterising {pdf_path.name} for OCR…")
    try:
        images = pdf2image.convert_from_path(pdf_path, dpi=dpi, poppler_path=poppler)
    except Exception as e:
        logger.warning("pdf2image failed on %s: %s", pdf_path.name, e)
        return ([], [f"pdf2image_error:{type(e).__name__}"], 0.0, False)

    def _check_cancel() -> bool:
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    total_pages = len(images)
    pages: list[PdfPage] = []
    confs: list[float] = []
    cancelled = False
    for i, img in enumerate(images, start=1):
        # Phase 2E — poll cancellation BEFORE the heavy Tesseract call
        # so a Stop click during OCR stops at the next page boundary
        # (the previous behaviour was to run all pages regardless).
        if _check_cancel():
            cancelled = True
            warnings.append("ocr_cancelled")
            logger.info(
                "OCR cancelled on %s after %d/%d page(s)",
                pdf_path.name, i - 1, total_pages,
            )
            break
        _progress(i - 1, total_pages, f"OCR page {i} of {total_pages}…")
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
        _progress(i, total_pages, f"OCR page {i} of {total_pages} done")
    mean = (sum(confs) / len(confs)) if confs else 0.0
    if cancelled:
        # Returning the pages that *did* complete keeps partial-result
        # debugging honest; the caller can read the ``ocr_cancelled``
        # warning to decide whether to stop or keep parsing.
        return pages, warnings, mean, True
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
    ocr_progress_callback: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    batch_id: Optional[str] = None,
) -> PdfExtractionResult:
    """Extract text from a PDF, falling back from digital-text to OCR.

    `digital_text_first`  — try pdfplumber first.
    `ocr_if_text_missing` — if a page returns < `digital_min_chars_per_page`
                            characters, route the *whole* PDF to OCR.
    `tesseract_cmd`       — explicit Tesseract binary path (overrides env).
    `poppler_path`        — explicit Poppler bin folder (overrides env).
    `ocr_progress_callback`— Phase 1O. Called as `(done, total, label)`
                             after each OCR page so the web app's
                             progress bar can move smoothly through long
                             OCR runs instead of sitting at one percent.
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
        with _perf("pdf.digital_text", batch_id, {"file": pdf_path.name}):
            digital_pages = _try_digital_text(pdf_path, log)

    # Phase 2L — relaxed text-availability check.
    #
    # Some vendors (Shelbyville Power) ship multi-page PDFs where
    # only the first page carries the bill data and the remaining
    # pages are inserts / blanks / scanned artwork that produce
    # near-zero text. Forcing the whole PDF to OCR in that case is
    # slow and lossier than the digital text we already have. We
    # keep digital extraction as long as AT LEAST ONE page met the
    # threshold; the per-vendor parsers already tolerate empty pages.
    digital_total_chars = sum(len((p.text or "").strip()) for p in digital_pages)
    digital_pages_with_text = sum(
        1 for p in digital_pages
        if len((p.text or "").strip()) >= digital_min_chars_per_page
    )
    insufficient_text = (
        not digital_pages
        or (digital_pages_with_text == 0
            and digital_total_chars < digital_min_chars_per_page)
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
    # Phase PERF-1 — short-circuit OCR when we already cached the
    # exact same file's results at this DPI. Saves the entire
    # Tesseract + pdf2image latency on re-processing (single-file
    # mode, retries, repeated demos).
    if _ocr_cache is not None:
        try:
            cached = _ocr_cache.lookup(pdf_path, ocr_dpi)
        except Exception:
            cached = None
        if cached:
            with _perf("ocr.cache_hit", batch_id, {"file": pdf_path.name}):
                cached_pages = [
                    PdfPage(
                        page_number=int(pg.get("page_number") or i + 1),
                        text=str(pg.get("text") or ""),
                        extraction_method=EXTRACTION_OCR,
                        width=int(pg.get("width") or 0),
                        height=int(pg.get("height") or 0),
                        words=list(pg.get("words") or []),
                    )
                    for i, pg in enumerate(cached.get("pages") or [])
                ]
            if cached_pages:
                result.pages = cached_pages
                result.pages_count = len(cached_pages)
                result.text = "\f".join(p.text for p in cached_pages)
                result.extraction_method = EXTRACTION_OCR
                result.confidence = float(cached.get("confidence") or 0.0)
                result.warnings.extend(cached.get("warnings") or [])
                result.warnings.append("ocr_cache_hit")
                return result

    with _perf("ocr.tesseract", batch_id,
               {"file": pdf_path.name, "dpi": ocr_dpi}):
        ocr_pages, ocr_warnings, mean_conf, ocr_ran = _try_ocr(
            pdf_path,
            dpi=ocr_dpi,
            tesseract_cmd=tesseract_cmd,
            poppler_path=poppler_path,
            logger=log,
            progress_callback=ocr_progress_callback,
            should_cancel=should_cancel,
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
    # Phase PERF-1 — persist successful (non-cancelled) OCR runs so a
    # follow-up single-file re-process is instant.
    if (_ocr_cache is not None and ocr_pages
            and "ocr_cancelled" not in ocr_warnings):
        try:
            _ocr_cache.store(pdf_path, ocr_dpi, {
                "pages": [
                    {
                        "page_number": p.page_number,
                        "text": p.text,
                        "width": p.width,
                        "height": p.height,
                        "words": p.words,
                    }
                    for p in ocr_pages
                ],
                "extraction_method": EXTRACTION_OCR,
                "confidence": mean_conf,
                "warnings": ocr_warnings,
            })
        except Exception as e:  # pragma: no cover
            log.debug("ocr_cache store failed for %s: %s", pdf_path.name, e)
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
