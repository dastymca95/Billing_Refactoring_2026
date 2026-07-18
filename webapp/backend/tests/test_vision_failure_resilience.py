from decimal import Decimal
from webapp.backend.services import ai_invoice_processor, ai_provider
from webapp.backend.services.accounting_contracts import LineItemFacts
from webapp.backend.services.semantic_classifier import classify_line


def test_reduced_visual_payload_is_retried_before_text_fallback(monkeypatch):
    calls = []

    def fake_extract(**kwargs):
        calls.append(list(kwargs["page_images_or_refs"]))
        if len(calls) == 1:
            raise ai_provider.AIProviderUnavailable(
                "safe failure",
                failure_code="vision_http_error",
                http_status=400,
            )
        return {"line_items": [{"description": "visible hardware", "amount": 10}]}

    monkeypatch.setattr(ai_provider, "extract_invoice_vision_structured", fake_extract)
    result = ai_invoice_processor._extract_vision_with_reduced_retry(
        vendor_hint="",
        document_text="",
        page_images_or_refs=["full-page", "detail-crop"],
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
    )

    assert [len(call) for call in calls] == [2, 1]
    assert result["line_items"][0]["description"] == "visible hardware"


def test_invalid_economy_vision_result_escalates_to_configured_runtime_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_provider.settings, "AI_VISION_ENABLED", True)

    def fake_extract(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ai_provider.AIProviderInvalidSchema("collapsed matrix")
        return {"line_items": [{"description": "Bath Tub", "amount": 350}], "total_amount": 350}

    monkeypatch.setattr(ai_provider, "extract_invoice_vision_structured", fake_extract)
    monkeypatch.setattr(
        ai_provider,
        "provider_status",
        lambda: ai_provider.AIProviderStatus(
            enabled=True,
            provider="openai",
            model="configured-text",
            configured=True,
            supports_vision=True,
            vision_enabled=True,
            vision_provider="openai",
            vision_model="configured-strong-vision",
            vision_mode="fallback_only",
            message="configured",
        ),
    )

    result = ai_invoice_processor._extract_vision_with_reduced_retry(
        vendor_hint="",
        document_text="",
        page_images_or_refs=["full-page", "detail-crop"],
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
    )

    assert result["total_amount"] == 350
    assert calls[1]["model_override"] == "configured-strong-vision"
    assert calls[1]["force_model_override"] is True


def test_provider_diagnostic_is_allow_listed_and_secret_free():
    error = ai_provider.AIProviderUnavailable(
        "body must not be serialized",
        failure_code="vision_http_error",
        http_status=400,
        provider_error_type="invalid_request_error",
        provider_error_code="invalid_image",
        provider_error_param="messages.1.content.1",
    )
    diagnostic = error.safe_diagnostic()

    assert diagnostic == {
        "failure_code": "vision_http_error",
        "http_status": 400,
        "provider_error_type": "invalid_request_error",
        "provider_error_code": "invalid_image",
        "provider_error_param": "messages.1.content.1",
    }
    assert "body" not in str(diagnostic)


def test_failed_vision_total_fallback_is_not_source_evidence():
    assert ai_invoice_processor._is_unresolved_visual_total_fallback(
        {
            "row_label": "Invoice total fallback",
            "reason": "The explicit invoice total is used as a payable fallback.",
        },
        warnings=["ai_vision_failed_text_fallback_used"],
        line_item_count=1,
    )
    assert not ai_invoice_processor._is_unresolved_visual_total_fallback(
        {"description": "Door stop", "amount": 22.96},
        warnings=[],
        line_item_count=1,
    )


def test_maintenance_supplies_are_materials_without_performed_labor():
    semantics = classify_line(
        LineItemFacts(
            line_item_id="line-1",
            raw_description="Purchase building repair and maintenance supplies",
            amount=Decimal("530.73"),
        ),
        document_id="document-1",
    )

    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"
    assert "mixed_material_and_service_indicators" in semantics.contradictions


def test_explicit_labor_remains_service_when_parts_are_also_present():
    semantics = classify_line(
        LineItemFacts(
            line_item_id="line-1",
            raw_description="Technician labor and installation of repair parts",
            amount=Decimal("300.00"),
        ),
        document_id="document-1",
    )

    assert semantics.line_family == "labor_service"
    assert semantics.work_mode == "labor_service"


def test_repair_kit_is_a_material_not_repair_labor():
    semantics = classify_line(
        LineItemFacts(
            line_item_id="line-1",
            raw_description="Bi-fold repair kit with pivot bracket",
            amount=Decimal("10.48"),
        ),
        document_id="document-1",
    )

    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"


def test_line_description_layers_remain_distinct():
    payload = ai_provider._coerce_invoice_schema({
        "line_items": [{
            "description": "ETN 1G DUP RECEPT PLATE",
            "raw_description": "ETN 1G DUP RECEPT PLATE",
            "normalized_description": "Eaton one-gang duplex receptacle plate",
            "generated_description": "Duplex electrical wall plate",
            "amount": 8.50,
        }],
    })

    line = payload["line_items"][0]
    assert line["raw_description"] == "ETN 1G DUP RECEPT PLATE"
    assert line["normalized_description"] == "Eaton one-gang duplex receptacle plate"
    assert line["generated_description"] == "Duplex electrical wall plate"


def test_visual_line_requires_separate_plain_language_description():
    try:
        ai_provider._validate_visual_line_structure({
            "line_items": [{
                "description": "ETN 1G DUP RECEPT PLATE",
                "raw_description": "ETN 1G DUP RECEPT PLATE",
                "generated_description": "",
                "amount": 8.50,
            }],
            "total_amount": 8.50,
        })
    except ai_provider.AIProviderInvalidSchema as exc:
        assert "generated_description" in str(exc)
    else:
        raise AssertionError("Visual extraction accepted a line without reviewer description")


def test_empty_visual_invoice_is_rejected_before_accounting():
    try:
        ai_provider._validate_visual_line_structure({"line_items": [], "total_amount": 0})
    except ai_provider.AIProviderInvalidSchema as exc:
        assert "no payable line items" in str(exc)
    else:
        raise AssertionError("Visual extraction accepted an empty invoice")
