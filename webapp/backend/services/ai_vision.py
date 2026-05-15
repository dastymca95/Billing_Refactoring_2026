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


class VisionRenderingUnavailable(RuntimeError):
    """Raised when PDF page rendering cannot be performed locally."""


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def render_pdf_pages_as_data_urls(
    *,
    batch_id: str,
    filename: str,
    page_numbers: list[int] | None = None,
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
                b64 = base64.b64encode(out_path.read_bytes()).decode("ascii")
                data_urls.append(f"data:image/png;base64,{b64}")
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
    "save_vision_trace_regions",
]
