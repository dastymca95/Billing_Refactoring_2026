from webapp.backend.services.ai_invoice_processor import (
    _distribute_invoice_difference,
    validate_ai_extraction,
)


def test_large_scope_mismatch_is_not_distributed_into_source_lines():
    normalized = {
        "total_amount": 750,
        "manual_review_codes": [],
        "manual_review_reasons": [],
    }
    items = [
        {"description": "Bath Tub - Apt 1A", "amount": 3500},
        {"description": "Wall Tile - Apt 1A", "amount": 3500},
    ]

    result = _distribute_invoice_difference(items, normalized)

    assert [item["amount"] for item in result] == [3500, 3500]
    assert "unsafe_distribution_blocked" in normalized["manual_review_codes"]


def test_small_unexplained_difference_is_not_used_to_rewrite_source_amounts():
    normalized = {
        "total_amount": 8710,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "manual_review_codes": [],
        "manual_review_reasons": [],
    }
    items = [
        {"description": "Page one services", "amount": 7960},
        {"description": "Page two services", "amount": 750},
    ]
    # Simulate the dangerous case: a page-scoped candidate replaced the true
    # document total with 7,960.  The 750 difference is only 8.6%, but it is not
    # tax/freight/fees and therefore must never be spread into the source rows.
    normalized["total_amount"] = 7960

    result = _distribute_invoice_difference(items, normalized)

    assert [item["amount"] for item in result] == [7960, 750]
    assert "unsafe_distribution_blocked" in normalized["manual_review_codes"]


def test_small_unexplained_difference_remains_invalid_and_export_blocked():
    normalized = validate_ai_extraction({
        "vendor_name": "Example Contractor",
        "invoice_number": "INV-STRUCTURAL-MISMATCH",
        "invoice_date": "2026-07-17",
        "due_date": "2026-08-16",
        "property_abbreviation": "TEST",
        "total_amount": 7235,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "line_items": [{
            "description": "Observed source components",
            "amount": 7185,
            "gl_account_candidate": "6669",
        }],
    }, references={})

    summary = normalized["validation_summary"]
    assert summary["total_reconciliation_passed"] is False
    assert summary["reconciled_total"] == 7185
    assert summary["invoice_total"] == 7235
    assert summary["valid"] is False
    assert summary["export_blocked"] is True
    assert "total_reconciliation_failed" in normalized["manual_review_codes"]
    assert "unexplained_invoice_difference" in normalized["manual_review_codes"]


def test_residual_difference_after_explicit_components_is_still_unexplained():
    normalized = validate_ai_extraction({
        "vendor_name": "Example Contractor",
        "invoice_number": "INV-RESIDUAL-MISMATCH",
        "invoice_date": "2026-07-17",
        "due_date": "2026-08-16",
        "property_abbreviation": "TEST",
        "total_amount": 100,
        "tax_amount": 5,
        "shipping_amount": 2,
        "fees_amount": 1,
        "line_items": [{
            "description": "Observed source components",
            "amount": 90,
            "gl_account_candidate": "6669",
        }],
    }, references={})

    summary = normalized["validation_summary"]
    assert summary["reconciled_total"] == 98
    assert summary["total_reconciliation_passed"] is False
    assert summary["export_blocked"] is True
    assert "total_reconciliation_failed" in normalized["manual_review_codes"]
    assert "unexplained_invoice_difference" in normalized["manual_review_codes"]
