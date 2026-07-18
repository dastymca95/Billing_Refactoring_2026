from webapp.backend.services.ai_invoice_processor import (
    _merge_page_vision_payloads,
    _matrix_payload_specificity,
    _payload_is_lossy_matrix_aggregate,
    _reconcile_high_confidence_vision_candidates,
    _select_more_specific_cross_page_payload,
    validate_ai_extraction,
)
import pytest

from webapp.backend.services.ai_provider import (
    AIProviderInvalidSchema,
    _build_vision_prompt,
    _coerce_invoice_schema,
    _expand_reconciled_matrix_rows,
    _validate_visual_line_structure,
)
from webapp.backend.services.ai_vision import _table_detail_bands, _table_detail_crop
from webapp.backend.services.accounting_contracts import LineItemFacts
from webapp.backend.services.semantic_classifier import classify_line


def test_vision_prompt_requires_matrix_cells_to_inherit_headers():
    prompt = _build_vision_prompt(
        vendor_hint="",
        document_text="",
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
    )

    assert "one line_item for every non-empty billable amount cell" in prompt
    assert "activity is the exact billable column header" in prompt
    assert "Unit Total, Row Total, Subtotal, or Invoice Total" in prompt
    assert "source_page" in prompt
    assert '"service_date": ""' in prompt
    assert "service_date" in prompt.split("Create vision_candidates", 1)[1]


def test_matrix_context_fields_survive_provider_schema_coercion():
    payload = _coerce_invoice_schema({
        "line_items": [{
            "page": 2,
            "groupHeader": "Surface refinishing",
            "rowLabel": "Apartment 12A",
            "columnHeader": "Bath Tub",
            "description": "Apartment 12A",
            "amount": 350,
        }]
    })

    item = payload["line_items"][0]
    assert item["source_page"] == 2
    assert item["section_header"] == "Surface refinishing"
    assert item["row_label"] == "Apartment 12A"
    assert item["activity"] == "Bath Tub"


def test_matrix_column_header_becomes_semantic_activity_without_overwriting_source_text():
    normalized = validate_ai_extraction(
        {
            "vendor_name": "Test Refinishing Company",
            "invoice_number": "MATRIX-1",
            "invoice_date": "2026-07-14",
            "total_amount": 350,
            "line_items": [{
                "source_page": 1,
                "section_header": "Refinishing",
                "row_label": "Apartment 12A",
                "activity": "Bath Tub",
                "description": "Apartment 12A",
                "amount": 350,
            }],
        },
        references={},
    )

    item = normalized["line_items"][0]
    assert item["activity"] == "Bath Tub"
    assert item["raw_description"] == "Apartment 12A"
    assert item["description"] == "Bath Tub - Apartment 12A"
    assert item["source_page"] == 1


def test_explicit_service_date_is_traceable_invoice_date_fallback_and_service_period():
    normalized = validate_ai_extraction(
        {
            "vendor_name": "Example Service Company",
            "invoice_number": "SERVICE-1",
            "invoice_date": "",
            "service_date": "02/21/2025",
            "due_date": "Upon Receipt",
            "payment_terms": "Upon Receipt",
            "total_amount": 350,
            "line_items": [{
                "activity": "Bath Tub",
                "description": "Tub refinishing",
                "raw_description": "Tub refinishing 350",
                "generated_description": "Bath tub refinishing",
                "amount": 350,
            }],
        },
        references={"vendors": [], "properties": [], "gl_accounts": []},
    )

    assert normalized["invoice_date"] == "02/21/2025"
    assert normalized["due_date"] == "02/21/2025"
    assert normalized["service_date"] == "02/21/2025"
    assert normalized["service_date_raw"] == "02/21/2025"
    assert normalized["payment_terms"] == "Upon Receipt"
    assert normalized["due_date_text"] == "Upon Receipt"
    assert normalized["service_period_start"] == "02/21/2025"
    assert normalized["service_period_end"] == "02/21/2025"
    provenance = {item["field"]: item for item in normalized["date_provenance"]}
    assert provenance["service_date"]["provenance"] == "document_observed"
    assert provenance["invoice_date"]["provenance"] == "tenant_policy_inference"
    assert provenance["invoice_date"]["source_field"] == "service_date"
    assert provenance["due_date_text"]["provenance"] == "document_observed"
    assert provenance["due_date"]["provenance"] == "tenant_policy_inference"
    assert provenance["due_date"]["source_field"] == "due_date_text"
    assert any(
        issue["code"] == "invoice_date_inferred_from_service_date"
        for issue in normalized["manual_review_issues"]
    )


def test_misspelled_upon_receipt_is_observed_text_and_policy_inferred_due_date():
    normalized = validate_ai_extraction(
        {
            "vendor_name": "Example Service Company",
            "invoice_number": "SERVICE-2",
            "invoice_date": "",
            "service_date": "03/05/2025",
            "due_date": "",
            "due_date_text": "Upon Reciept",
            "total_amount": 350,
            "line_items": [{"activity": "Bath Tub", "description": "Tub refinishing", "amount": 350}],
        },
        references={"vendors": [], "properties": [], "gl_accounts": []},
    )

    assert normalized["invoice_date"] == "03/05/2025"
    assert normalized["due_date_text"] == "Upon Reciept"
    assert normalized["due_date"] == "03/05/2025"
    assert normalized["due_date_source"] == "tenant_policy_from_due_date_text"
    provenance = {item["field"]: item for item in normalized["date_provenance"]}
    assert provenance["due_date_text"]["provenance"] == "document_observed"
    assert provenance["due_date"]["provenance"] == "tenant_policy_inference"
    assert provenance["due_date"]["raw_value"] is None


def test_non_calendar_due_candidate_remains_visible_source_evidence():
    reconciled = _reconcile_high_confidence_vision_candidates({
        "payment_terms": "Upon Receipt",
        "vision_candidates": [{
            "field_key": "due_date",
            "value": "Upon Receipt",
            "page": 1,
            "confidence": 0.97,
            "validation_status": "candidate",
        }],
    })

    assert reconciled.get("due_date") in (None, "")
    assert reconciled["_unresolved_visual_field_candidates"] == [{
        "field": "due_date",
        "value": "Upon Receipt",
        "confidence": 0.97,
        "reason": "visible_text_is_not_a_normalized_calendar_date",
    }]


def test_visual_schema_rejects_total_column_as_billable_activity():
    with pytest.raises(AIProviderInvalidSchema, match="total column"):
        _validate_visual_line_structure({
            "total_amount": 350,
            "line_items": [{"activity": "Unit Total", "amount": 350}],
        })


def test_visual_schema_rejects_component_total_scope_mismatch():
    with pytest.raises(AIProviderInvalidSchema, match="do not reconcile"):
        _validate_visual_line_structure({
            "total_amount": 750,
            "line_items": [
                {"activity": "Bath Tub", "amount": 3500},
                {"activity": "Wall Tile", "amount": 3500},
            ],
        })


def test_visual_schema_requires_cent_exact_multi_page_reconciliation():
    with pytest.raises(AIProviderInvalidSchema, match="do not reconcile"):
        _validate_visual_line_structure({
            "total_amount": 8710,
            "line_items": [
                {
                    "source_page": 1,
                    "activity": "Surface work",
                    "raw_description": "Page 1 components",
                    "generated_description": "Surface refinishing work",
                    "amount": 7950,
                },
                {
                    "source_page": 2,
                    "activity": "Surface work",
                    "raw_description": "Page 2 components",
                    "generated_description": "Surface refinishing work",
                    "amount": 750,
                },
            ],
        })


def test_visual_schema_rejects_two_cent_difference():
    with pytest.raises(AIProviderInvalidSchema, match="do not reconcile"):
        _validate_visual_line_structure({
            "total_amount": 100.02,
            "line_items": [{
                "activity": "Service",
                "raw_description": "Service 100.00",
                "generated_description": "Service charge",
                "amount": 100.00,
            }],
        })


def test_visual_schema_rejects_matrix_when_zero_components_reconcile():
    with pytest.raises(AIProviderInvalidSchema, match="did not uniquely reconcile any component"):
        _validate_visual_line_structure({
            "total_amount": 1175,
            "line_items": [{
                "row_label": "05A",
                "activity": "Kitchen Counter, Bath Tub, Wall Tile, Tub Mat",
                "raw_description": "05A",
                "generated_description": "Apartment refinishing work",
                "amount": 1175,
            }],
        })


def test_exact_matrix_arithmetic_expands_components_and_preserves_raw_source():
    raw_row = "05A 275 350 350 3@150 1@50"
    payload = {
        "total_amount": 1175,
        "warnings": [],
        "line_items": [{
            "row_label": "05A",
            "activity": "Kitchen Counter, Bath Tub, Wall Tile, Other, Tub Mat",
            "raw_description": raw_row,
            "generated_description": "Apartment surface refinishing components",
            "amount": 1175,
            "gl_account_candidate": "6669",
        }],
    }

    expanded = _expand_reconciled_matrix_rows(payload)

    assert [item["activity"] for item in expanded["line_items"]] == [
        "Kitchen Counter", "Bath Tub", "Wall Tile", "Other", "Tub Mat",
    ]
    assert [item["amount"] for item in expanded["line_items"]] == [275, 350, 350, 150, 50]
    assert [item["quantity"] for item in expanded["line_items"]] == [1, 1, 1, 3, 1]
    assert expanded["line_items"][3]["unit_price"] == 50
    assert all(item["raw_description"] == raw_row for item in expanded["line_items"])
    assert [item["source_component_token"] for item in expanded["line_items"]] == [
        "275", "350", "350", "3@150", "1@50",
    ]
    assert all(item["gl_account_candidate"] == "" for item in expanded["line_items"])
    assert all(len(item["generated_description"].split()) <= 8 for item in expanded["line_items"])
    assert sum(item["amount"] for item in expanded["line_items"]) == 1175


def test_matrix_expansion_isolates_non_reconciled_row_without_losing_source():
    payload = {
        "total_amount": 1175,
        "line_items": [{
            "row_label": "05A",
            "activity": "Kitchen Counter, Bath Tub",
            "raw_description": "05A 275 350",
            "generated_description": "Apartment refinishing work",
            "amount": 1175,
        }],
    }

    expanded = _expand_reconciled_matrix_rows(payload)

    assert len(expanded["line_items"]) == 1
    assert expanded["line_items"][0]["activity"] == "Unresolved Matrix Components"
    assert expanded["line_items"][0]["raw_description"] == "05A 275 350"
    assert expanded["line_items"][0]["amount"] == 1175
    assert expanded["line_items"][0]["gl_account_candidate"] == ""
    assert expanded["line_items"][0]["matrix_component_headers"] == [
        "Kitchen Counter", "Bath Tub",
    ]
    assert expanded["needs_manual_review"] is True
    assert "matrix_rows_with_unresolved_component_arithmetic:1" in expanded["warnings"]
    with pytest.raises(AIProviderInvalidSchema, match="did not uniquely reconcile any component"):
        _validate_visual_line_structure(payload)


def test_visual_schema_accepts_partial_matrix_recovery_with_one_unresolved_row():
    recovered = _validate_visual_line_structure({
        "total_amount": 1525,
        "line_items": [
            {
                "row_label": "01A",
                "activity": "Bath Tub, Wall Tile",
                "raw_description": "01A 350 350",
                "generated_description": "Apartment refinishing components",
                "amount": 700,
            },
            {
                "row_label": "02A",
                "activity": "Kitchen Counter, Bath Tub",
                "raw_description": "02A illegible",
                "generated_description": "Apartment refinishing components",
                "amount": 825,
            },
        ],
    })

    assert len(recovered["line_items"]) == 3
    assert sum(item["amount"] for item in recovered["line_items"]) == 1525
    assert sum(
        item.get("matrix_expansion_status") == "unresolved_arithmetic"
        for item in recovered["line_items"]
    ) == 1


def test_table_detail_crop_enlarges_the_central_table_without_mutating_source():
    from PIL import Image

    source = Image.new("RGB", (1000, 1600), "white")
    crop = _table_detail_crop(source, max_width=1600)

    assert source.size == (1000, 1600)
    assert crop.width == 1600
    assert crop.height > 700


def test_table_detail_bands_create_three_overlapping_views_without_source_mutation():
    from PIL import Image

    source = Image.new("RGB", (1000, 1600), "white")
    bands = _table_detail_bands(source, max_width=1600)

    assert source.size == (1000, 1600)
    assert len(bands) == 3
    assert all(band.width == 1600 for band in bands)
    assert all(band.height > 250 for band in bands)


def test_independently_reconciled_pages_merge_without_losing_page_context():
    merged = _merge_page_vision_payloads([
        (1, {"invoice_number": "A-1", "line_items": [{"activity": "Bath Tub", "amount": 350}], "total_amount": 350, "confidence": 0.9}),
        (2, {"invoice_number": "A-1", "line_items": [{"activity": "Kitchen Counter", "amount": 275}], "total_amount": 275, "confidence": 0.8}),
    ])

    assert merged["total_amount"] == 625
    assert [item["source_page"] for item in merged["line_items"]] == [1, 2]
    assert merged["confidence"] == 0.8
    assert "multi_page_invoice_merged_from_independently_reconciled_page_facts" in merged["warnings"]


def test_multi_page_merge_preserves_explicit_totals_and_exposes_component_mismatch():
    merged = _merge_page_vision_payloads([
        (1, {"invoice_number": "A-1", "line_items": [{"amount": 7950}], "total_amount": 7960, "confidence": 0.9}),
        (2, {"invoice_number": "A-1", "line_items": [{"amount": 750}], "total_amount": 750, "confidence": 0.9}),
    ])

    assert merged["total_amount"] == 8710
    assert sum(item["amount"] for item in merged["line_items"]) == 8700
    assert merged["unexplained_invoice_difference"] == 10
    assert merged["needs_manual_review"] is True
    assert "multi_page_component_total_mismatch_preserved_for_review" in merged["warnings"]


def test_page_total_candidate_cannot_override_reconciled_multi_page_total():
    payload = {
        "total_amount": 8710,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "line_items": [
            {"source_page": 1, "amount": 7960},
            {"source_page": 2, "amount": 750},
        ],
        "vision_candidates": [{
            "field_key": "total_amount",
            "value": "7960.00",
            "page": 1,
            "confidence": 0.99,
            "validation_status": "candidate",
        }],
    }

    reconciled = _reconcile_high_confidence_vision_candidates(payload)

    assert reconciled["total_amount"] == 8710
    assert reconciled["_vision_candidate_conflicts"][0]["candidate"] == 7960
    assert "page_scoped_visual_total_did_not_override_reconciled_document_total" in reconciled["warnings"]


def test_reconciled_aggregate_is_lossy_when_source_exposes_matrix_headers():
    assert _payload_is_lossy_matrix_aggregate({
        "invoice_description": "Kitchen Counter, Bath Tub, Wall Tile refinishing",
        "total_amount": 7960,
        "line_items": [{
            "description": "Refinishing services by apartment and item",
            "amount": 7960,
        }],
    })


def test_cross_page_recovery_requires_same_identity_total_and_more_specificity():
    merged = {
        "invoice_number": "22-1",
        "total_amount": 750,
        "line_items": [{"description": "Aggregate services", "amount": 750}],
    }
    recovery = {
        "invoice_number": "22-1",
        "total_amount": 750,
        "warnings": [],
        "line_items": [
            {"row_label": "1A", "activity": "Bath Tub", "amount": 350},
            {"row_label": "1A", "activity": "Wall Tile", "amount": 400},
        ],
    }

    selected = _select_more_specific_cross_page_payload(merged, recovery)

    assert selected is not merged
    assert len(selected["line_items"]) == 2
    assert "cross_page_visual_recovery_selected_for_matrix_specificity" in selected["warnings"]
    band_selected = _select_more_specific_cross_page_payload(
        merged,
        recovery,
        selection_warning="matrix_band_visual_recovery_selected_for_specificity",
    )
    assert "matrix_band_visual_recovery_selected_for_specificity" in band_selected["warnings"]
    assert _select_more_specific_cross_page_payload(
        merged, {**recovery, "total_amount": 700}
    ) == merged
    assert _select_more_specific_cross_page_payload(
        merged, {**recovery, "invoice_number": "different"}
    ) == merged


def test_row_totals_without_semantic_activity_do_not_count_as_specific_recovery():
    row_totals_only = {
        "line_items": [
            {"row_label": "01A", "description": "01A", "amount": 100},
            {"row_label": "02A", "description": "02A", "amount": 200},
        ],
    }

    assert _matrix_payload_specificity(row_totals_only) == 0


@pytest.mark.parametrize("activity, family", [
    ("Kitchen Counter", "countertop"),
    ("Bath Counter", "countertop"),
    ("Bath Tub", "tub_refinishing"),
    ("Tub Mat", "tub_refinishing"),
    ("Wall Tile", "tub_refinishing"),
    ("Floor Tile", "flooring"),
])
def test_matrix_charge_headers_classify_by_specific_asset_context(activity, family):
    facts = LineItemFacts(line_item_id="1", raw_activity=activity, raw_description="Apartment 1A")

    classification = classify_line(facts, document_id="doc")
    assert classification.trade_family == family
    if family == "tub_refinishing":
        assert classification.work_mode == "labor_service"
