"""Developer-safe AI invoice helper endpoints.

The processing pipeline calls the AI invoice service internally. This API file
exposes only non-secret status/validation helpers for smoke tests and future
operator tooling; it never returns provider credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import ai_invoice_processor, ai_provider, ai_vision, batch_store
from ..services.template_rules import get_template_rules


router = APIRouter(prefix="/api/ai/invoice", tags=["ai_invoice"])
test_router = APIRouter(prefix="/api/ai-invoice", tags=["ai_invoice"])
batch_router = APIRouter(prefix="/api/batches", tags=["ai_invoice"])


class ValidatePayload(BaseModel):
    extraction: dict[str, Any] = Field(default_factory=dict)


class TestExtractPayload(BaseModel):
    vendor_hint: str = ""
    document_text: str = Field(default="", max_length=200_000)
    dry_run: bool = True


class VisionAssistPayload(BaseModel):
    filename: str = ""
    page_numbers: list[int] = Field(default_factory=lambda: [1])
    vendor_hint: str = ""
    document_text: str = Field(default="", max_length=200_000)
    current_extraction: dict[str, Any] | None = None
    dry_run: bool = True


@router.get("/status")
def get_invoice_ai_status() -> dict[str, Any]:
    return ai_provider.status_payload()


@router.post("/validate")
def validate_invoice_payload(payload: ValidatePayload) -> dict[str, Any]:
    try:
        normalized = ai_invoice_processor.validate_ai_extraction(payload.extraction)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="AI extraction JSON is invalid.") from exc
    return {
        "valid": True,
        "manual_review_reasons": normalized.get("manual_review_reasons", []),
        "manual_review_codes": normalized.get("manual_review_codes", []),
        "validation_summary": normalized.get("validation_summary", {}),
        "normalized": normalized,
    }


@test_router.post("/test-extract")
def test_extract_invoice(payload: TestExtractPayload) -> dict[str, Any]:
    """Dry-run a configured AI provider without touching batches or exports."""
    if not payload.dry_run:
        raise HTTPException(
            status_code=400,
            detail="Only dry_run=true is supported by this endpoint.",
        )
    status = ai_provider.provider_status()
    if not status.enabled:
        raise HTTPException(status_code=400, detail="AI invoice processing is disabled.")
    if not status.configured:
        raise HTTPException(status_code=400, detail=status.message)
    if not payload.document_text.strip():
        raise HTTPException(status_code=422, detail="document_text is required.")

    references = ai_invoice_processor.load_references()
    template_schema = {
        "columns": get_template_rules().get("columns", []),
        "required_columns": get_template_rules().get("required_columns", []),
        "recommended_columns": get_template_rules().get("recommended_columns", []),
    }
    try:
        extraction = ai_provider.extract_invoice_structured(
            vendor_hint=payload.vendor_hint,
            document_text=payload.document_text,
            page_images_or_refs=[],
            template_schema=template_schema,
            property_reference=references["properties"],
            gl_reference=references["gl_accounts"],
            vendor_reference=references["vendors"],
        )
        normalized = ai_invoice_processor.validate_ai_extraction(
            extraction,
            references=references,
        )
    except ai_provider.AIProviderNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ai_provider.AIProviderInvalidJSON, ai_provider.AIProviderInvalidSchema) as exc:
        raise HTTPException(status_code=422, detail="AI response was not valid JSON.") from exc
    except ai_provider.AIProviderUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ai_provider.AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail="AI extraction validation failed.") from exc

    return {
        "dry_run": True,
        "provider": status.provider,
        "model": status.model,
        "extraction": extraction,
        "validation": {
            "valid": True,
            "manual_review_reasons": normalized.get("manual_review_reasons", []),
            "manual_review_codes": normalized.get("manual_review_codes", []),
            "warnings": normalized.get("warnings", []),
            "row_count": len(normalized.get("line_items", [])),
            "total_amount": normalized.get("total_amount"),
            **(normalized.get("validation_summary") or {}),
        },
        "normalized": normalized,
    }


@batch_router.post("/{batch_id}/ai-invoice/vision-assist")
def vision_assist_invoice(batch_id: str, payload: VisionAssistPayload) -> dict[str, Any]:
    """Run opt-in vision assist for one batch document.

    This endpoint does not export, create revisions, trigger Dropbox, or mutate
    template rows. The only persisted side effect is a batch-local trace overlay
    file when the provider returns candidate bounding boxes.
    """
    if not payload.dry_run:
        raise HTTPException(
            status_code=400,
            detail="Only dry_run=true is supported by this endpoint.",
        )
    status = ai_provider.provider_status()
    if not status.enabled:
        raise HTTPException(status_code=400, detail="AI invoice processing is disabled.")
    if not status.configured:
        raise HTTPException(status_code=400, detail=status.message)
    if not status.vision_enabled:
        raise HTTPException(
            status_code=400,
            detail="Vision assist is not enabled. Configure AI_VISION_ENABLED and a vision-capable model.",
        )
    safe_filename = Path(payload.filename or "").name
    if not safe_filename:
        raise HTTPException(status_code=422, detail="filename is required.")

    try:
        batch_store.get_batch_dir(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}") from exc

    references = ai_invoice_processor.load_references()
    template_schema = {
        "columns": get_template_rules().get("columns", []),
        "required_columns": get_template_rules().get("required_columns", []),
        "recommended_columns": get_template_rules().get("recommended_columns", []),
    }
    document_text = payload.document_text
    if not document_text.strip():
        try:
            document_text = ai_invoice_processor.extract_document_text(
                batch_store.get_input_dir(batch_id) / safe_filename
            )
        except Exception:
            document_text = ""

    try:
        if status.provider == "mock":
            page_images = ["mock://page/1"]
        elif Path(safe_filename).suffix.lower() in ai_vision.IMAGE_EXTENSIONS:
            image_path = (batch_store.get_input_dir(batch_id) / safe_filename).resolve()
            page_images = [ai_vision.image_path_as_data_url(image_path)]
        else:
            page_images = ai_vision.render_pdf_pages_as_data_urls(
                batch_id=batch_id,
                filename=safe_filename,
                page_numbers=payload.page_numbers,
            )
        raw = ai_provider.extract_invoice_vision_structured(
            vendor_hint=payload.vendor_hint,
            document_text=document_text,
            page_images_or_refs=page_images,
            template_schema=template_schema,
            property_reference=references["properties"],
            gl_reference=references["gl_accounts"],
            vendor_reference=references["vendors"],
        )
        vision_normalized = ai_invoice_processor.validate_ai_extraction(
            raw,
            references=references,
        )
        text_normalized = None
        if payload.current_extraction:
            try:
                text_normalized = ai_invoice_processor.validate_ai_extraction(
                    payload.current_extraction,
                    references=references,
                )
            except Exception:
                text_normalized = None
        merged = ai_invoice_processor.merge_text_and_vision_results(
            text_normalized,
            vision_normalized,
        )
        trace_items = ai_vision.save_vision_trace_regions(
            batch_id=batch_id,
            source_file=safe_filename,
            candidates=list(raw.get("vision_candidates") or []),
            feeds_rows=[],
        )
    except ai_vision.VisionRenderingUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ai_provider.AIProviderNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ai_provider.AIProviderInvalidJSON, ai_provider.AIProviderInvalidSchema) as exc:
        raise HTTPException(status_code=422, detail="AI vision response was not valid JSON.") from exc
    except ai_provider.AIProviderUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ai_provider.AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail="AI vision extraction validation failed.") from exc

    return {
        "dry_run": True,
        "provider": status.provider,
        "model": status.vision_model,
        "vision_enabled": status.vision_enabled,
        "vision_mode": status.vision_mode,
        "extraction": raw,
        "validation": {
            "valid": True,
            "manual_review_reasons": merged.get("manual_review_reasons", []),
            "manual_review_codes": merged.get("manual_review_codes", []),
            "warnings": merged.get("warnings", []),
            "row_count": len(merged.get("line_items", [])),
            "total_amount": merged.get("total_amount"),
            **(merged.get("validation_summary") or {}),
        },
        "normalized": merged,
        "trace_regions": trace_items,
    }
