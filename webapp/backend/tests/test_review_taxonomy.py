from webapp.backend.services.review_taxonomy import (
    ReviewCategory, categorize_warning, migrate_invoice_review_codes,
)
from webapp.backend.services.ai_invoice_processor import validate_ai_extraction


def test_review_code_is_stable_when_warning_prose_changes():
    first = categorize_warning("The payable row identity is ambiguous; verify the apartment.")
    second = categorize_warning("Apt number could not be read, so row identity needs review.")
    assert first.category is ReviewCategory.ROW_IDENTITY_AMBIGUOUS
    assert second.category is ReviewCategory.ROW_IDENTITY_AMBIGUOUS
    assert first.original_warning != second.original_warning


def test_window_sill_tub_mat_disagreement_is_visual_component_conflict():
    result = categorize_warning("Visual conflict: Window Sill versus Tub Mat")
    assert result.category is ReviewCategory.VISUAL_COMPONENT_CONFLICT


def test_unrecognized_warning_keeps_stable_generic_identity_and_original_text():
    warning = "The handwriting has an unusual flourish."
    result = categorize_warning(warning)
    assert result.category is ReviewCategory.VISUAL_EXTRACTION_WARNING
    assert result.original_warning == warning


def test_legacy_free_text_code_is_migrated_while_original_warning_is_preserved():
    warning = "The handwritten service date is ambiguous."
    invoice = {
        "manual_review_codes": ["ai_warning_the_handwritten_service_date_is_ambiguous", "required_gl_account"],
        "rows": [{"_meta": {"ai_warnings": [warning]}}],
    }
    migrate_invoice_review_codes(invoice)
    assert invoice["manual_review_codes"] == [
        "required_gl_account", "handwritten_date_ambiguous",
    ]
    assert invoice["typed_review_evidence"][0]["original_warning"] == warning


def test_legacy_code_without_raw_warning_remains_a_typed_blocking_signal():
    invoice = {"manual_review_codes": ["ai_warning_unrecoverable_old_text"], "rows": [{"_meta": {}}]}
    migrate_invoice_review_codes(invoice)
    assert invoice["manual_review_codes"] == ["visual_extraction_warning"]
    assert invoice["typed_review_evidence"][0]["original_warning"] == "ai_warning_unrecoverable_old_text"


def test_new_extraction_emits_typed_warning_code_not_prose_derived_identity():
    normalized = validate_ai_extraction({
        "vendor_name": "Test Vendor", "invoice_number": "INV-1",
        "invoice_date": "2026-07-17", "due_date": "2026-07-17",
        "property_abbreviation": "TEST", "total_amount": 10,
        "warnings": ["The payable row identity is ambiguous; verify the apartment."],
        "line_items": [{"description": "Observed service", "amount": 10}],
    }, references={})
    assert "row_identity_ambiguous" in normalized["manual_review_codes"]
    assert not any(code.startswith("ai_warning_") for code in normalized["manual_review_codes"])
    assert normalized["typed_review_evidence"][0]["original_warning"].startswith("The payable")
