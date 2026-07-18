"""Vision-assist utilities for AI invoice extraction.

All helpers are opt-in and batch-local. They never modify source training
files, never log image bytes, and clean up rendered page images immediately
after converting them to provider-safe data URLs.
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

from .. import settings
from . import batch_store
from .local_processing_guard import serialized_local_document_operation


class VisionRenderingUnavailable(RuntimeError):
    """Raised when PDF page rendering cannot be performed locally."""


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@serialized_local_document_operation
def render_pdf_pages_as_data_urls(
    *,
    batch_id: str,
    filename: str,
    page_numbers: list[int] | None = None,
    include_detail_crop: bool = False,
    include_table_bands: bool = False,
) -> list[str]:
    """Render selected PDF pages into capped PNG data URLs.

    Pages are 1-based. The output count is capped by AI_VISION_MAX_PAGES and
    width by AI_VISION_MAX_IMAGE_WIDTH.
    """
    input_dir = batch_store.get_input_dir(batch_id).resolve()
    safe_name = Path(filename or "").name
    if not safe_name:
        raise FileNotFoundError("File not found.")
    pdf_path = (input_dir / safe_name).resolve()
    if input_dir not in pdf_path.parents:
        raise FileNotFoundError("File not found.")
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise VisionRenderingUnavailable("Vision assist currently supports PDF files only.")

    renderer = "fitz"
    try:
        import fitz  # type: ignore
    except Exception:
        fitz = None  # type: ignore[assignment]
        renderer = "pdfium"
        try:
            import pypdfium2 as pdfium  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on local optional dep
            raise VisionRenderingUnavailable("Vision rendering unavailable.") from exc

    max_pages = max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2))
    max_width = max(400, int(getattr(settings, "AI_VISION_MAX_IMAGE_WIDTH", 1600) or 1600))
    temp_dir = batch_store.get_batch_dir(batch_id) / "temp" / "ai_vision"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_files: list[Path] = []
    data_urls: list[str] = []
    try:
        doc = fitz.open(str(pdf_path)) if renderer == "fitz" else pdfium.PdfDocument(str(pdf_path))  # type: ignore[name-defined]
        try:
            requested = page_numbers or [1]
            cleaned_pages = []
            for n in requested:
                try:
                    pn = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= pn <= len(doc) and pn not in cleaned_pages:
                    cleaned_pages.append(pn)
            if not cleaned_pages:
                cleaned_pages = [1]
            for page_number in cleaned_pages[:max_pages]:
                page = doc[page_number - 1]
                width = (
                    float(page.rect.width or 612)
                    if renderer == "fitz"
                    else float(page.get_width() or 612)
                )
                scale = min(2.5, max(1.0, max_width / width))
                out_path = temp_dir / f"{_safe_stem(safe_name)}_p{page_number}.png"
                if renderer == "fitz":
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)  # type: ignore[union-attr]
                    pix.save(str(out_path))
                else:
                    pil_image = page.render(scale=scale).to_pil()
                    pil_image.save(str(out_path), format="PNG")
                temp_files.append(out_path)
                data_urls.append(_provider_safe_page_data_url(out_path))
                if include_detail_crop:
                    try:
                        from PIL import Image, ImageEnhance, ImageOps  # type: ignore

                        with Image.open(out_path) as image:
                            image = image.convert("RGB")
                            crop = _table_detail_crop(image, max_width=max_width)
                            buffer = io.BytesIO()
                            crop.save(buffer, format="JPEG", quality=94, optimize=True)
                            data_urls.append(
                                "data:image/jpeg;base64,"
                                + base64.b64encode(buffer.getvalue()).decode("ascii")
                            )
                    except Exception:
                        # Detail crops improve small handwriting but are never
                        # a prerequisite for the normal full-page visual path.
                        pass
                if include_table_bands:
                    try:
                        from PIL import Image  # type: ignore

                        with Image.open(out_path) as image:
                            image = image.convert("RGB")
                            for band in _table_detail_bands(image, max_width=max_width):
                                buffer = io.BytesIO()
                                band.save(buffer, format="JPEG", quality=94, optimize=True)
                                data_urls.append(
                                    "data:image/jpeg;base64,"
                                    + base64.b64encode(buffer.getvalue()).decode("ascii")
                                )
                    except Exception:
                        # Bands are a bounded escalation for dense matrices;
                        # the full-page evidence remains available on failure.
                        pass
            if include_detail_crop and len(cleaned_pages) == 1:
                # Also retain the historical header/detail crop for a
                # single-page document, after the table crop above.
                try:
                    from PIL import Image, ImageEnhance, ImageOps  # type: ignore

                    with Image.open(temp_files[0]) as image:
                        image = image.convert("RGB")
                        width, height = image.size
                        # Focused header-facts band: sold-to/job-site and the
                        # date/terms row.  The former crop extended deep into
                        # the financial table, shrinking faint handwriting in
                        # the provider view and causing digit substitutions.
                        crop = image.crop((
                            int(width * 0.04),
                            int(height * 0.225),
                            int(width * 0.96),
                            int(height * 0.425),
                        ))
                        crop = ImageOps.autocontrast(crop)
                        crop = ImageEnhance.Contrast(crop).enhance(1.35)
                        crop = ImageEnhance.Sharpness(crop).enhance(2.0)
                        if crop.width < max_width:
                            ratio = max_width / float(crop.width)
                            crop = crop.resize((max_width, max(1, int(crop.height * ratio))))
                        buffer = io.BytesIO()
                        crop.save(buffer, format="JPEG", quality=96, optimize=True)
                        data_urls.append(
                            "data:image/jpeg;base64,"
                            + base64.b64encode(buffer.getvalue()).decode("ascii")
                        )
                except Exception:
                    # Detail crops improve small handwriting but are never a
                    # prerequisite for the normal full-page visual path.
                    pass
        finally:
            close = getattr(doc, "close", None)
            if callable(close):
                close()
    finally:
        for p in temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    return data_urls


@serialized_local_document_operation
def render_pdf_apt_column_crop(
    *,
    batch_id: str,
    filename: str,
    page_number: int = 1,
    render_dpi: int = 600,
) -> tuple[str, dict[str, int]]:
    """Render only the left-hand Apt./Unit column of a matrix invoice.

    This is a bounded fallback for handwritten allocation tables. It does not
    mutate the source and deliberately excludes financial/accounting columns.
    Returned coordinates are pixels on the full page at ``render_dpi``.
    """

    input_dir = batch_store.get_input_dir(batch_id).resolve()
    safe_name = Path(filename or "").name
    pdf_path = (input_dir / safe_name).resolve()
    if input_dir not in pdf_path.parents or not pdf_path.is_file():
        raise FileNotFoundError("File not found.")
    if pdf_path.suffix.lower() != ".pdf":
        raise VisionRenderingUnavailable("Apt. column verification supports PDF files only.")
    try:
        from PIL import Image, ImageEnhance, ImageOps  # type: ignore
    except Exception as exc:  # pragma: no cover - optional runtime dependencies
        raise VisionRenderingUnavailable("Apt. column rendering unavailable.") from exc
    try:
        import fitz  # type: ignore
        renderer = "fitz"
        doc = fitz.open(str(pdf_path))
    except Exception:
        try:
            import pypdfium2 as pdfium  # type: ignore
            renderer = "pdfium"
            doc = pdfium.PdfDocument(str(pdf_path))
        except Exception as exc:  # pragma: no cover - optional runtime dependencies
            raise VisionRenderingUnavailable("Apt. column rendering unavailable.") from exc
    try:
        if not 1 <= int(page_number) <= len(doc):
            raise VisionRenderingUnavailable("Requested PDF page is unavailable.")
        page = doc[int(page_number) - 1]
        page_width = (
            float(page.rect.width or 612)
            if renderer == "fitz" else float(page.get_width() or 612)
        )
        page_height = (
            float(page.rect.height or 792)
            if renderer == "fitz" else float(page.get_height() or 792)
        )
        # Matrix forms place the row identifier in the left-most table column.
        # Keep a margin around the header and all visible rows; a downstream
        # verifier must still report ambiguity rather than infer from catalogs.
        normalized = (0.055, 0.355, 0.190, 0.690)
        scale = max(1.0, float(render_dpi) / 72.0)
        if renderer == "fitz":
            clip = fitz.Rect(
                page_width * normalized[0],
                page_height * normalized[1],
                page_width * normalized[2],
                page_height * normalized[3],
            )
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        else:
            full = page.render(scale=scale).to_pil().convert("RGB")
            image = full.crop((
                int(full.width * normalized[0]),
                int(full.height * normalized[1]),
                int(full.width * normalized[2]),
                int(full.height * normalized[3]),
            ))
        image = ImageOps.autocontrast(image)
        image = ImageEnhance.Contrast(image).enhance(1.35)
        image = ImageEnhance.Sharpness(image).enhance(2.0)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=96, optimize=True)
        coordinates = {
            "page": int(page_number),
            "x": int(round(page_width * scale * normalized[0])),
            "y": int(round(page_height * scale * normalized[1])),
            "width": int(round(page_width * scale * (normalized[2] - normalized[0]))),
            "height": int(round(page_height * scale * (normalized[3] - normalized[1]))),
            "render_dpi": int(render_dpi),
            "source_page_width": int(round(page_width * scale)),
            "source_page_height": int(round(page_height * scale)),
        }
        return (
            "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
            coordinates,
        )
    finally:
        doc.close()


def _provider_safe_page_data_url(path: Path) -> str:
    """Encode a rendered page compactly without changing the source file.

    Full-page PNG scans can exceed multimodal gateway request limits even when
    their visible information is modest.  A high-quality JPEG preserves the
    receipt text while substantially reducing the request envelope.  PNG is a
    dependency-safe fallback only.
    """
    try:
        from PIL import Image, ImageOps  # type: ignore

        with Image.open(path) as image:
            image = ImageOps.autocontrast(image.convert("RGB"))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=92, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def _table_detail_crop(image: Any, *, max_width: int) -> Any:
    """Return a sharpened central invoice-table crop without source mutation."""
    from PIL import ImageEnhance, ImageOps  # type: ignore

    width, height = image.size
    crop = image.crop((
        int(width * 0.035),
        int(height * 0.30),
        int(width * 0.965),
        int(height * 0.76),
    ))
    crop = ImageOps.autocontrast(crop)
    crop = ImageEnhance.Contrast(crop).enhance(1.25)
    crop = ImageEnhance.Sharpness(crop).enhance(1.8)
    if crop.width < max_width:
        ratio = max_width / float(crop.width)
        crop = crop.resize((max_width, max(1, int(crop.height * ratio))))
    return crop


def _table_detail_bands(image: Any, *, max_width: int) -> list[Any]:
    """Return three overlapping high-resolution bands of the central charge table."""
    from PIL import ImageEnhance, ImageOps  # type: ignore

    width, height = image.size
    left = int(width * 0.025)
    right = int(width * 0.975)
    table_top = int(height * 0.27)
    table_bottom = int(height * 0.82)
    table_height = max(1, table_bottom - table_top)
    band_height = max(1, int(table_height * 0.42))
    starts = (
        table_top,
        table_top + int(table_height * 0.29),
        max(table_top, table_bottom - band_height),
    )
    bands: list[Any] = []
    for start in starts:
        bottom = min(table_bottom, start + band_height)
        band = image.crop((left, start, right, bottom))
        band = ImageOps.autocontrast(band)
        band = ImageEnhance.Contrast(band).enhance(1.35)
        band = ImageEnhance.Sharpness(band).enhance(2.0)
        if band.width < max_width:
            ratio = max_width / float(band.width)
            band = band.resize((max_width, max(1, int(band.height * ratio))))
        bands.append(band)
    return bands


def image_path_as_data_url(path: Path) -> str:
    """Convert an uploaded screenshot/photo to a capped data URL."""
    p = path.resolve()
    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        raise VisionRenderingUnavailable("Vision assist currently supports PDF or image files only.")
    max_width = max(400, int(getattr(settings, "AI_VISION_MAX_IMAGE_WIDTH", 1600) or 1600))
    mime = _mime_for_suffix(p.suffix.lower())
    try:
        from PIL import Image  # type: ignore

        with Image.open(p) as img:
            img = img.convert("RGB")
            if img.width > max_width:
                ratio = max_width / float(img.width)
                img = img.resize((max_width, max(1, int(img.height * ratio))))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=88, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except ImportError:
        # Pillow is optional. For normal snipping-tool screenshots the raw file
        # is usually small enough; still avoid logging or persisting bytes.
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as exc:
        raise VisionRenderingUnavailable("Vision image preparation unavailable.") from exc


def save_vision_trace_regions(
    *,
    batch_id: str,
    source_file: str,
    candidates: list[dict[str, Any]],
    feeds_rows: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Persist vision candidate bboxes into the normal trace overlay file."""
    if not candidates:
        return []
    batch_dir = batch_store.get_batch_dir(batch_id)
    trace_dir = batch_dir / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{_safe_filename_for_trace(source_file)}.json"
    items: list[dict[str, Any]] = []
    if trace_path.is_file():
        try:
            payload = json.loads(trace_path.read_text(encoding="utf-8"))
            items = list(payload.get("items") or [])
        except (OSError, ValueError):
            items = []

    # Vision traces are observations from the current provider call. Reusing
    # old regions makes the overlay lie after reprocessing, so replace them
    # while preserving any deterministic/OCR trace entries in the same file.
    items = [item for item in items if str(item.get("source_type") or "") != "ai_vision"]
    existing_ids = {str(item.get("trace_id") or "") for item in items}
    added: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, start=1):
        bbox = candidate.get("bbox")
        if not isinstance(bbox, dict):
            continue
        field_key = str(candidate.get("field_key") or "vision_candidate")
        trace_id = f"vision:{field_key}:{candidate.get('page') or 1}:{idx}"
        if trace_id in existing_ids:
            continue
        item = {
            "trace_id": trace_id,
            "source_file": source_file,
            "page": max(1, int(candidate.get("page") or 1)),
            "bbox": {
                "x": float(bbox.get("x") or 0),
                "y": float(bbox.get("y") or 0),
                "w": float(bbox.get("w") or 0),
                "h": float(bbox.get("h") or 0),
            },
            "field_key": field_key,
            "field_label": str(candidate.get("field_label") or field_key),
            "source_type": "ai_vision",
            "rule_id": "ai_vision_candidate",
            "match_strategy": str(candidate.get("validation_status") or "candidate"),
            "confidence": float(candidate.get("confidence") or 0),
            "feeds_rows": feeds_rows or [],
            "feeds_columns": [_field_to_column(field_key)],
            "detected_text": "" if candidate.get("value") is None else str(candidate.get("value")),
        }
        items.append(item)
        added.append(item)

    payload = {
        "source_file": source_file,
        "trace_count": len(items),
        "items": items,
    }
    trace_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return added


def _safe_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:120] or "document"


def _safe_filename_for_trace(name: str) -> str:
    s = (name or "unknown").strip().replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:200] or "unknown"


def _field_to_column(field_key: str) -> str:
    mapping = {
        "vendor_name": "Vendor",
        "invoice_number": "Invoice Number",
        "invoice_date": "Invoice Date",
        "due_date": "Due Date",
        "total_amount": "Amount",
        "line_items_table": "Line Item Description",
    }
    return mapping.get(field_key, field_key.replace("_", " ").title())


def _mime_for_suffix(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(suffix.lower(), "application/octet-stream")


__all__ = [
    "VisionRenderingUnavailable",
    "IMAGE_EXTENSIONS",
    "image_path_as_data_url",
    "render_pdf_pages_as_data_urls",
    "render_pdf_apt_column_crop",
    "save_vision_trace_regions",
]
