from webapp.backend.services.ai_invoice_processor import (
    ai_result_to_invoice,
    validate_ai_extraction,
)


def test_empty_ai_line_items_remain_visible_as_blocked_review_row():
    normalized = validate_ai_extraction(
        {
            "vendor_name": "Unmapped Test Vendor",
            "invoice_number": "INV-REVIEW-1",
            "invoice_date": "2026-07-14",
            "invoice_description": "Unreadable source document",
            "total_amount": 0,
            "line_items": [],
        },
        references={},
    )

    assert len(normalized["line_items"]) == 1
    assert normalized["line_items"][0]["amount"] == 0
    assert "line_items_missing" in normalized["manual_review_codes"]

    invoice = ai_result_to_invoice(
        normalized,
        batch_id="batch_review_visibility",
        source_file="unreadable.pdf",
        vendor_key="ai_assisted",
        support_document_url="private://source/unreadable.pdf",
    )

    assert len(invoice["rows"]) == 1
    assert invoice["rows"][0]["Amount"] == 0
    assert invoice["rows"][0]["_meta"]["manual_review_reasons"]
