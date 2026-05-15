from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import ai_invoice_processor, canonical_rules


def _assert_equal(label: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    if not canonical_rules.CANONICAL_RULES_YAML.is_file():
        canonical_rules.import_canonical_rules_from_excel()

    payload = {
        "vendor_name": "Capital Waste Services",
        "invoice_number": "3150854",
        "invoice_date": "04/30/2026",
        "due_date": "05/30/2026",
        "bill_or_credit": "Bill",
        "account_number": "160243",
        "service_address": "River Canyon Apartments, 21726 River Canyon Rd, Chattanooga, TN 37405",
        "service_period_start": "05/01/2026",
        "service_period_end": "05/31/2026",
        "property_candidate": "River Canyon Apartments",
        "line_items": [
            {
                "description": "6 Yard Trash Service",
                "quantity": 1,
                "unit_price": 365.40,
                "amount": 365.40,
                "confidence": 0.95,
                "reason": "Visible service line.",
            },
            {
                "description": "Fuel Recovery Adjustment",
                "quantity": 1,
                "unit_price": 34.93,
                "amount": 34.93,
                "confidence": 0.95,
                "reason": "Visible fee line.",
            },
        ],
        "subtotal": 400.33,
        "tax_amount": 0,
        "total_amount": 400.33,
        "confidence": 0.95,
        "warnings": [],
    }
    normalized = ai_invoice_processor.validate_ai_extraction(payload)
    invoice = ai_invoice_processor.ai_result_to_invoice(
        normalized,
        batch_id="batch_20990101_000000_000",
        source_file="CapitalWasteChattanoogaHauling_invoice_3150854.pdf",
        vendor_key="unknown",
        support_document_url="https://dropbox.example/capital-waste-3150854.pdf",
        support_document_status="uploaded",
        support_document_dropbox_path="/Billing/QA/capital-waste-3150854.pdf",
    )
    rows = invoice.get("rows") or []
    _assert_equal("category", normalized.get("category"), "trash_collection_services")
    _assert_equal("vendor", normalized.get("vendor_name"), "Capital Waste Services")
    _assert_equal("invoice number", normalized.get("invoice_number"), "3150854")
    _assert_equal("invoice date", normalized.get("invoice_date"), "04/30/2026")
    _assert_equal("due date", normalized.get("due_date"), "05/30/2026")
    _assert_equal("property", normalized.get("property_abbreviation"), "RCC")
    _assert_equal("location", normalized.get("location"), "")
    _assert_equal("row count", len(rows), 2)
    _assert_equal("row 1 gl", rows[0]["GL Account"], "6940")
    _assert_equal("row 2 gl", rows[1]["GL Account"], "6940")
    _assert_equal("row 1 amount", rows[0]["Amount"], 365.40)
    _assert_equal("row 2 amount", rows[1]["Amount"], 34.93)
    _assert_equal("invoice description", rows[0]["Invoice Description"], "05/01/26-05/31/26 - River Canyon Apartments")
    _assert_equal(
        "line 1 description",
        rows[0]["Line Item Description"],
        "05/01/26-05/31/26 - River Canyon Apartments - 6 Yard Trash Service",
    )
    _assert_equal(
        "line 2 description",
        rows[1]["Line Item Description"],
        "05/01/26-05/31/26 - River Canyon Apartments - Fuel Recovery Adjustment",
    )
    if "payment" in " ".join(r["Line Item Description"].lower() for r in rows):
        raise AssertionError("Payment/remittance text leaked into payable rows.")
    print("Capital Waste canonical invoice smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
