from webapp.backend.services.ai_invoice_processor import (
    _distribute_invoice_difference,
    _refresh_reconciliation_after_distribution,
)
from webapp.backend.services.accounting_readiness import evaluate_rows


def test_distribution_recomputes_arithmetic_without_resolving_other_blockers():
    normalized = {
        "total_amount": 67.82,
        "tax_amount": 12.99,
        "shipping_amount": 0,
        "fees_amount": 0,
        "validation_summary": {"total_reconciliation_passed": False, "reconciled_total": 54.83},
        "manual_review_codes": [
            "total_reconciliation_failed",
            "unexplained_invoice_difference",
            "property_mapping_required",
        ],
        "manual_review_reasons": [
            "Line items plus tax/shipping/fees total 54.83, but invoice total is 67.82.",
            (
                "The source line amounts plus every explicit tax, shipping, and fee component "
                "still differ from the invoice total."
            ),
            "Property could not be confirmed.",
        ],
    }
    items = [
        {"description": "Tangible item", "amount": 8.30, "quantity": 1},
        {"description": "Second tangible item", "amount": 46.53, "quantity": 1},
    ]

    adjusted = _distribute_invoice_difference(items, normalized)
    _refresh_reconciliation_after_distribution(normalized, adjusted)

    assert round(sum(item["amount"] for item in adjusted), 2) == 67.82
    assert normalized["validation_summary"]["total_reconciliation_passed"] is True
    assert normalized["validation_summary"]["reconciled_total"] == 67.82
    assert normalized["validation_summary"]["distributed_reconciliation_applied"] is True
    assert normalized["manual_review_codes"] == ["property_mapping_required"]
    assert normalized["manual_review_reasons"] == ["Property could not be confirmed."]


def test_current_snapshot_arithmetic_supersedes_stale_pre_distribution_flag():
    rows = []
    for index, amount in enumerate((30.00, 37.82), 1):
        rows.append({
            "Invoice Number": "INV-1", "Bill or Credit": "Bill",
            "Invoice Date": "2026-07-14", "Accounting Date": "2026-07-14",
            "Vendor": "Vendor", "Invoice Description": "Purchase",
            "Line Item Number": index, "Property Abbreviation": "TEST",
            "GL Account": "6669", "Line Item Description": f"Item {index}",
            "Amount": amount, "Expense Type": "General", "Is Replacement Reserve": False,
            "Due Date": "2026-08-13", "Document Url": "/private/doc",
            "_meta": {"invoice_group_id": "INV-1", "total_reconciliation_passed": False,
                      "ai_provenance": {"invoice_total": 67.82}},
        })
    readiness = evaluate_rows(rows)
    assert readiness.reconciliation_status == "passed"
    assert not any(issue.code == "total_mismatch" for issue in readiness.blockers)
