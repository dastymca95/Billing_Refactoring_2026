import hashlib
import json

import pytest

from webapp.backend import settings
from webapp.backend.services import ai_provider, batch_store
from webapp.backend.services.ai_provider import AIProviderStatus
from webapp.backend.services.native_pdf_evidence import (
    NativePdfEvidence,
    NativePdfEvidenceError,
    load_native_pdf_evidence,
)
from webapp.backend.services.ai_invoice_processor import (
    _merge_critical_header_verification,
    _merge_row_identity_verification,
    _native_pdf_model_sequence,
    _requires_critical_header_verification,
    _should_use_native_pdf_for_candidate,
)
from webapp.backend.services.document_ingestion import DocumentCandidate


def _vision_status(_context=None) -> AIProviderStatus:
    return AIProviderStatus(
        enabled=True,
        provider="openai",
        model="reasoning-model",
        configured=True,
        supports_vision=True,
        vision_enabled=True,
        vision_provider="openai",
        vision_model="vision-model",
        vision_mode="fallback_only",
        message="configured",
    )


def _valid_visual_payload() -> dict:
    return {
        "vendor_name": "Example Refinishing",
        "invoice_number": "INV-100",
        "invoice_date": None,
        "service_date": "2025-02-21",
        "due_date": None,
        "payment_terms": "Upon Receipt",
        "bill_or_credit": "Bill",
        "account_number": None,
        "service_address": None,
        "address_role": "sold_to",
        "location_candidate": "12A",
        "service_period_start": None,
        "service_period_end": None,
        "service_period": None,
        "property_candidate": "Example Apartments",
        "property_abbreviation": None,
        "invoice_description": "Surface refinishing",
        "line_items": [{
            "source_page": 1,
            "section_header": "Refinishing",
            "row_label": "12A",
            "location_candidate": "12A",
            "activity": "Bath Tub",
            "description": "12A Bath Tub 350.00",
            "raw_description": "12A Bath Tub 350.00",
            "normalized_description": "12A Bath Tub 350.00",
            "generated_description": "Bath tub refinishing",
            "quantity": 1,
            "unit_price": 350,
            "amount": 350,
            "gl_account_candidate": None,
            "expense_type": None,
            "is_replacement_reserve": None,
            "confidence": 0.95,
            "reason": "Visible matrix cell",
        }],
        "subtotal": 350,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": 350,
        "visual_extraction_status": "complete",
        "unresolved_visual_regions": [],
        "page_reconciliations": [{
            "page": 1,
            "printed_page_total": 350,
            "extracted_component_total": 350,
            "difference": 0,
            "status": "reconciled",
        }],
        "vision_candidates": [],
        "warnings": [],
        "needs_manual_review": False,
        "confidence": 0.95,
    }


def test_private_pdf_loader_preserves_source_and_never_exposes_absolute_path(
    tmp_path, monkeypatch
):
    batch_id = "batch_20260101_120000_001"
    batches = tmp_path / "batches"
    input_dir = batches / batch_id / "input"
    input_dir.mkdir(parents=True)
    payload = b"%PDF-1.4\nprivate-source\n%%EOF"
    source = input_dir / "sample.pdf"
    source.write_bytes(payload)
    monkeypatch.setattr(settings, "BATCHES_ROOT", batches)
    monkeypatch.setattr(batch_store, "BATCHES_ROOT", batches)

    evidence = load_native_pdf_evidence(
        batch_id=batch_id,
        filename="sample.pdf",
        max_bytes=1024,
    )

    assert source.read_bytes() == payload
    assert evidence.filename == "sample.pdf"
    assert evidence.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert evidence.byte_count == len(payload)
    assert str(tmp_path) not in repr(evidence)
    assert evidence.data_url.startswith("data:application/pdf;base64,")


def test_private_pdf_loader_cannot_escape_batch_input(tmp_path, monkeypatch):
    batch_id = "batch_20260101_120000_001"
    batches = tmp_path / "batches"
    (batches / batch_id / "input").mkdir(parents=True)
    (batches / batch_id / "outside.pdf").write_bytes(b"%PDF-private")
    monkeypatch.setattr(settings, "BATCHES_ROOT", batches)
    monkeypatch.setattr(batch_store, "BATCHES_ROOT", batches)

    with pytest.raises(NativePdfEvidenceError, match="not found"):
        load_native_pdf_evidence(
            batch_id=batch_id,
            filename="../outside.pdf",
            max_bytes=1024,
        )


def test_native_pdf_request_uses_responses_strict_schema_and_private_cache_fingerprint(
    monkeypatch,
):
    captured = {}
    visual_payload = _valid_visual_payload()

    def send(*, payload, api_key, base_url, timeout_seconds, max_attempts=3):
        captured["payload"] = payload
        captured["api_key_present"] = bool(api_key)
        captured["base_url"] = base_url
        captured["timeout"] = timeout_seconds
        return json.dumps(visual_payload), {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
        }

    monkeypatch.setattr(ai_provider, "_require_vision_configured", _vision_status)
    monkeypatch.setattr(ai_provider, "_send_openai_response", send)
    monkeypatch.setattr(ai_provider, "_load_extraction_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(ai_provider, "_save_extraction_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(ai_provider, "_reserve_cost_budget", lambda *_a, **_k: None)
    monkeypatch.setattr(settings, "AI_VISION_API_KEY", "private-test-key")
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "AI_VISION_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_BASE_URL", "")
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_DETAIL", "high")
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_REASONING_EFFORT", "high")
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_MAX_RESPONSE_TOKENS", 32768)
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_TIMEOUT_SECONDS", 240)

    raw_data_url = "data:application/pdf;base64,JVBERi0xLjQ="
    result = ai_provider.extract_invoice_native_pdf_structured(
        vendor_hint="",
        document_text="",
        pdf_evidence=NativePdfEvidence(
            filename="invoice.pdf",
            content_sha256="a" * 64,
            byte_count=12,
            data_url=raw_data_url,
        ),
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
        model_override="reasoning-model",
        cost_scope_id="test",
    )

    request = captured["payload"]
    file_part = request["input"][1]["content"][0]
    assert file_part == {
        "type": "input_file",
        "filename": "invoice.pdf",
        "file_data": raw_data_url,
        "detail": "high",
    }
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] == 32768
    frozen = ai_provider._frozen_cache_payload("native", request)
    serialized = json.dumps(frozen)
    assert raw_data_url not in serialized
    assert "data_url_sha256" in serialized
    assert result["_provider_request_surface"] == "responses_native_pdf"
    assert result["_provider_usage"]["total_tokens"] == 1500
    assert "selected_gl" not in result


def test_native_pdf_aggregate_fallback_is_rejected_before_accounting():
    payload = _valid_visual_payload()
    payload["visual_extraction_status"] = "aggregate_fallback"

    with pytest.raises(ai_provider.AIProviderInvalidSchema, match="aggregate fallback"):
        ai_provider._validate_visual_line_structure(payload)


def test_only_difficult_scanned_pdfs_route_to_native_document_vision(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_ENABLED", True)
    monkeypatch.setattr(settings, "AI_VISION_MAX_PAGES", 4)
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF")
    scan = DocumentCandidate(
        source_file=source.name,
        source_type="pdf_scanned",
        page_count=2,
        document_text="unreadable table",
        text_quality_score=0.05,
        extraction_quality={"text_quality_score": 0.05},
    )

    assert _should_use_native_pdf_for_candidate(scan, _vision_status(), source) is True
    digital = DocumentCandidate(
        source_file=source.name,
        source_type="pdf_digital",
        page_count=2,
        document_text="Invoice Date 02/21/2025 Total Due 350.00",
        text_quality_score=0.99,
    )
    assert _should_use_native_pdf_for_candidate(digital, _vision_status(), source) is False

    non_openai = AIProviderStatus(
        **{**_vision_status().__dict__, "provider": "gemini", "vision_provider": "gemini"}
    )
    assert _should_use_native_pdf_for_candidate(scan, non_openai, source) is False


def test_single_page_scan_with_missing_critical_ocr_routes_to_native_pdf(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_ENABLED", True)
    monkeypatch.setattr(settings, "AI_VISION_MAX_PAGES", 2)
    source = tmp_path / "single-page-form.pdf"
    source.write_bytes(b"%PDF")
    scan = DocumentCandidate(
        source_file=source.name,
        source_type="pdf_scanned",
        page_count=1,
        document_text="INVOICE vendor and number only",
        text_quality_score=0.31,
        extraction_quality={"text_quality_score": 0.31},
    )

    assert _should_use_native_pdf_for_candidate(scan, _vision_status(), source) is True


def test_single_page_high_quality_scan_stays_on_economic_rendered_route(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "AI_VISION_NATIVE_PDF_ENABLED", True)
    source = tmp_path / "readable.pdf"
    source.write_bytes(b"%PDF")
    scan = DocumentCandidate(
        source_file=source.name,
        source_type="pdf_scanned",
        page_count=1,
        document_text="Invoice Date 02/21/2025 Total Due 350.00",
        text_quality_score=0.91,
        extraction_quality={"text_quality_score": 0.91},
    )

    assert _should_use_native_pdf_for_candidate(scan, _vision_status(), source) is False


def test_row_identity_uses_pixels_before_catalog_and_corrects_payable_label(monkeypatch):
    payload = _valid_visual_payload()
    payload["line_items"][0]["row_label"] = "53B"
    payload["line_items"][0]["location_candidate"] = "53B"
    payload["line_items"][0]["description"] = "Apt. # 53B | Bath Tub"
    payload["line_items"][0]["normalized_description"] = "Apt. # 53B | Bath Tub"
    payload["line_items"][0]["raw_description"] = "Apt. # 53B | Bath Tub"
    payload["warnings"] = [
        "Handwritten apartment suffixes are interpreted as letters: 53B.",
        "The SOLD TO property name is faint.",
    ]
    monkeypatch.setattr(
        "webapp.backend.services.ai_invoice_processor._catalog_units_for_row_identity",
        lambda _payload: ("TGAP", ["3053B", "3057B"]),
    )
    verification = {
        "crop_coordinates": {
            "page": 1, "x": 300, "y": 2400, "width": 650, "height": 2000,
            "render_dpi": 600, "source_page_width": 5100, "source_page_height": 6600,
        },
        "visible_rows": [{
            "row_index": 1,
            "raw_value": "57B",
            "alternatives": [{"value": "53B", "confidence": 0.40}],
            "confidence": 0.94,
            "bbox": {"x": 0.10, "y": 0.30, "w": 0.60, "h": 0.08},
            "selection_marker": "circled",
            "status": "confirmed",
        }],
    }

    merged = _merge_row_identity_verification(payload, verification)

    assert merged["line_items"][0]["row_label"] == "57B"
    assert merged["line_items"][0]["location_candidate"] == "3057B"
    evidence = merged["line_items"][0]["row_identity_evidence"]
    assert evidence["resolved_unit"] == "3057B"
    assert evidence["resolution_basis"] == "handwriting_confirmed_before_unique_catalog_validation"
    assert evidence["crop_coordinates"]["x"] == 365
    assert merged["line_items"][0]["description"] == "Apt. # 57B | Bath Tub"
    assert merged["line_items"][0]["normalized_description"] == "Apt. # 57B | Bath Tub"
    assert merged["line_items"][0]["raw_description"] == "Apt. # 53B | Bath Tub"
    assert merged["warnings"] == ["The SOLD TO property name is faint."]
    assert merged["_row_identity_verification"]["superseded_primary_warnings"] == [
        "Handwritten apartment suffixes are interpreted as letters: 53B."
    ]


def test_excluded_paid_rows_never_match_crossed_out_row_by_position(monkeypatch):
    payload = _valid_visual_payload()
    payload["excluded_paid_rows"] = [{
        "raw_apartment_number": "15B",
        "component_amounts": [{"label": "Bath Tub", "amount": 350}],
        "row_total": 350,
        "paid_marker_evidence": [{
            "page": 1, "text": "PAID", "confidence": 0.97,
            "bbox": {"x": 0.9, "y": 0.5, "w": 0.08, "h": 0.03},
        }],
        "exclusion_reason": "visible_paid_marker",
    }]
    monkeypatch.setattr(
        "webapp.backend.services.ai_invoice_processor._catalog_units_for_row_identity",
        lambda _payload: ("TGAP", ["3065B"]),
    )
    verification = {
        "crop_coordinates": {
            "page": 1, "x": 300, "y": 2400, "width": 650, "height": 2000,
            "render_dpi": 600, "source_page_width": 5100, "source_page_height": 6600,
        },
        "visible_rows": [
            {
                "row_index": 0, "raw_value": "21C", "alternatives": [],
                "confidence": 0.7, "bbox": {"x": .1, "y": .2, "w": .5, "h": .05},
                "selection_marker": "crossed_out", "status": "needs_confirmation",
            },
            {
                "row_index": 1, "raw_value": "65B", "alternatives": [],
                "confidence": 0.94, "bbox": {"x": .1, "y": .5, "w": .5, "h": .05},
                "selection_marker": "unmarked", "status": "confirmed",
            },
        ],
    }

    merged = _merge_row_identity_verification(payload, verification)

    excluded = merged["excluded_paid_rows"][0]
    assert excluded["raw_apartment_number"] == "65B"
    assert excluded["apartment_identity"]["resolved_unit"] == "3065B"
    assert excluded["apartment_identity"]["raw_value"] != "21C"


def test_row_identity_catalog_cannot_resolve_ambiguous_handwriting(monkeypatch):
    payload = _valid_visual_payload()
    payload["line_items"][0]["row_label"] = "53B"
    monkeypatch.setattr(
        "webapp.backend.services.ai_invoice_processor._catalog_units_for_row_identity",
        lambda _payload: ("TGAP", ["3053B", "3057B"]),
    )
    verification = {
        "crop_coordinates": {
            "page": 1, "x": 300, "y": 2400, "width": 650, "height": 2000,
            "render_dpi": 600,
        },
        "visible_rows": [{
            "row_index": 1,
            "raw_value": "57B",
            "alternatives": [{"value": "53B", "confidence": 0.88}],
            "confidence": 0.91,
            "bbox": {"x": 0.10, "y": 0.30, "w": 0.60, "h": 0.08},
            "selection_marker": "circled",
            "status": "needs_confirmation",
        }],
    }

    merged = _merge_row_identity_verification(payload, verification)

    evidence = merged["line_items"][0]["row_identity_evidence"]
    assert evidence["resolved_unit"] is None
    assert evidence["status"] == "needs_confirmation"
    assert merged["_row_identity_verification"]["payable_needs_confirmation"] is True


def test_ambiguous_header_verification_corrects_only_source_facts():
    candidate = DocumentCandidate(
        source_file="form.pdf",
        source_type="pdf_scanned",
        page_count=1,
        document_text="",
        text_quality_score=0.31,
    )
    primary = {
        "service_date": "2-2-25",
        "payment_terms": "Upon Receipt",
        "property_candidate": "Example at Unclear",
        "line_items": [{"activity": "Bath Tub", "amount": 350}],
        "warnings": ["The handwritten date is faint."],
    }
    verification = {
        "service_date": "3-5-25",
        "property_candidate": "Example at Pinson",
        "payment_terms": "30 Days",
        "sold_to_raw_text": "Example at Pinson",
        "job_site_raw_text": "June",
        "confidence": 0.96,
        "warnings": [],
        "_provider_profile_id": "runtime-vision:critical-fields",
        "_provider_name": "openai",
        "_provider_model_id": "economic-vision",
        "_estimated_cost_usd": 0.001,
    }

    assert _requires_critical_header_verification(primary, candidate) is True
    merged = _merge_critical_header_verification(primary, verification)

    assert merged["service_date"] == "3-5-25"
    assert merged["property_candidate"] == "Example at Pinson"
    assert merged["payment_terms"] == "Upon Receipt"
    assert merged["sold_to_raw_text"] == "Example at Pinson"
    assert merged["job_site_raw_text"] == "June"
    assert merged["line_items"] == primary["line_items"]
    assert merged["_critical_header_verification"]["superseded_primary_warnings"] == [
        "The handwritten date is faint."
    ]
    assert "The handwritten date is faint." not in merged["warnings"]
    assert len(merged["_critical_header_verification"]["conflicts"]) == 3
    payment_conflict = next(
        item for item in merged["_critical_header_verification"]["conflicts"]
        if item["field"] == "payment_terms"
    )
    assert payment_conflict["selected"] == "Upon Receipt"


def test_low_confidence_header_disagreement_never_overwrites_primary_fact():
    primary = {
        "service_date": "2-2-25",
        "warnings": ["The handwritten date is faint."],
        "line_items": [{"activity": "Bath Tub", "amount": 350}],
    }
    merged = _merge_critical_header_verification(primary, {
        "service_date": "3-5-25",
        "confidence": 0.60,
    })

    assert merged["service_date"] == "2-2-25"
    assert merged["_critical_header_verification"]["conflicts"][0]["selected"] == "2-2-25"


def test_native_pdf_uses_configured_economic_profile_before_strong_escalation(
    monkeypatch,
):
    status = AIProviderStatus(
        **{**_vision_status().__dict__, "vision_model": "economic-vision"}
    )
    monkeypatch.setenv("AI_VISION_ESCALATION_MODEL", "strong-vision")
    moderate_scan = DocumentCandidate(
        source_file="invoice.pdf",
        source_type="pdf_scanned",
        page_count=2,
        document_text="Invoice Date 02/21/2025 Total Due 350.00",
        text_quality_score=0.55,
        extraction_quality={"text_quality_score": 0.55},
    )

    assert _native_pdf_model_sequence(status, moderate_scan) == (
        "economic-vision",
        "strong-vision",
    )
    monkeypatch.setenv("AI_VISION_ESCALATION_MODEL", "economic-vision")
    assert _native_pdf_model_sequence(status, moderate_scan) == ("economic-vision", "")


def test_hard_native_pdf_goes_directly_to_strong_profile_without_double_charge(
    monkeypatch,
):
    status = AIProviderStatus(
        **{**_vision_status().__dict__, "vision_model": "economic-vision"}
    )
    monkeypatch.setenv("AI_VISION_ESCALATION_MODEL", "strong-vision")
    hard_scan = DocumentCandidate(
        source_file="invoice.pdf",
        source_type="pdf_scanned",
        page_count=2,
        document_text="INVOICE handwritten allocation matrix",
        text_quality_score=0.2,
        extraction_quality={"text_quality_score": 0.2},
    )

    assert _native_pdf_model_sequence(status, hard_scan) == ("strong-vision", "")
